import asyncio
import random
import shutil
from pathlib import Path

from playwright.async_api import (
    async_playwright,
    Page,
    Playwright,
    BrowserContext,
)
from playwright_stealth import Stealth
from app.logging_config import get_logger
from app.settings import settings

logger = get_logger(__name__)


class OzonBlockedError(Exception):
    """Raised when Ozon blocks access and refresh doesn't help."""
    pass


class OzonPageLoadError(Exception):
    """Raised when page fails to load (timeout, network error)."""
    pass


class OzonParser:
    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._restart_lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        """Get or create restart lock."""
        if self._restart_lock is None:
            self._restart_lock = asyncio.Lock()
        return self._restart_lock

    def _build_launch_options(self) -> dict:
        user_data_dir = Path("browser_data")
        user_data_dir.mkdir(exist_ok=True)

        # Slightly randomize viewport to look more human (within common resolutions)
        viewport_width = random.choice([1920, 1903, 1912, 1920])
        viewport_height = random.choice([1080, 969, 1040, 1080])

        options = dict(
            user_data_dir=str(user_data_dir),
            headless=settings.browser_headless,
            viewport={"width": viewport_width, "height": viewport_height},
            locale="ru-RU",
            color_scheme="light",
            timezone_id="Europe/Moscow",
            geolocation={"latitude": 55.7558, "longitude": 37.6173},  # Moscow
            permissions=["geolocation"],
        )

        # Chromium args - comprehensive anti-detection for server environment
        args = [
            # Core anti-detection
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-site-isolation-trials",
            # Sandbox and stability
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-setuid-sandbox",
            # Hide automation indicators
            "--disable-infobars",
            "--disable-background-networking",
            "--disable-breakpad",
            "--disable-component-update",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-service-autorun",
            "--password-store=basic",
            "--use-mock-keychain",
            # WebGL - use SwiftShader but hide it
            "--use-gl=swiftshader",
            "--enable-webgl",
            "--enable-webgl2",
            # GPU flags to avoid detection
            "--ignore-gpu-blocklist",
            "--enable-gpu-rasterization",
            # Window
            "--window-size=1920,1080",
            "--start-maximized",
            # Disable features that expose headless
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-hang-monitor",
            "--disable-ipc-flooding-protection",
            "--disable-popup-blocking",
            "--disable-prompt-on-repost",
            # Font rendering (important for fingerprint)
            "--font-render-hinting=none",
            "--disable-font-subpixel-positioning",
            # Disable notifications
            "--disable-notifications",
            "--disable-desktop-notifications",
            # Misc
            "--metrics-recording-only",
            "--mute-audio",
            "--no-pings",
            "--disable-sync",
        ]
        if settings.browser_headless and settings.browser_headless_new:
            args.append("--headless=new")
            options["headless"] = False  # Use flag instead

        options["args"] = args
        options["user_agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )

        return options

    async def __aenter__(self) -> "OzonParser":
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            **self._build_launch_options()
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()

    async def restart_browser(self) -> None:
        """Close browser, wipe browser_data, and relaunch with clean profile.

        Thread-safe: uses lock to prevent concurrent restarts.
        """
        lock = self._get_lock()

        async with lock:
            logger.info("Restarting browser with clean profile...")
            if self._context:
                try:
                    await self._context.close()
                except Exception as e:
                    logger.debug(f"Context close error (may be already closed): {e}")
                self._context = None

            browser_data = Path("browser_data")
            if browser_data.exists():
                try:
                    shutil.rmtree(browser_data)
                    logger.info("Deleted browser_data/")
                except Exception as e:
                    logger.warning(f"Failed to delete browser_data: {e}")

            if not self._playwright:
                self._playwright = await async_playwright().start()

            self._context = await self._playwright.chromium.launch_persistent_context(
                **self._build_launch_options()
            )
            logger.info("Browser restarted with clean profile")

    async def _new_page(self, block_resources: bool = True) -> Page:
        if not self._context:
            raise RuntimeError("Context not initialized. Use async with.")
        page = await self._context.new_page()
        page.set_default_timeout(settings.browser_timeout)

        # Block heavy resources to speed up loading
        # NOTE: Do NOT block stylesheet - Ozon needs CSS for proper rendering
        if block_resources:
            blocked_types = {"image", "font", "media"}
            # Also block tracking/analytics URLs
            blocked_urls = ["mc.yandex", "google-analytics", "facebook", "vk.com/rtrg", "top-fwz", "criteo"]

            async def handle_route(route):
                request = route.request
                url = request.url
                # Block by resource type
                if request.resource_type in blocked_types:
                    await route.abort()
                    return
                # Block tracking/analytics
                if any(blocked in url for blocked in blocked_urls):
                    await route.abort()
                    return
                await route.continue_()

            await page.route("**/*", handle_route)

        # Chromium stealth - always pretend to be Windows for consistency
        stealth = Stealth(
            navigator_languages_override=("ru-RU", "ru"),
            navigator_platform_override="Win32",  # Windows - less suspicious than Linux
        )
        await stealth.apply_stealth_async(page)

        # Comprehensive stealth for server environment
        await page.add_init_script("""
                // ============ WebGL Spoofing ============
                const getParameterProxyHandler = {
                    apply: function(target, thisArg, args) {
                        const param = args[0];
                        // UNMASKED_VENDOR_WEBGL
                        if (param === 37445) {
                            return 'Google Inc. (NVIDIA)';
                        }
                        // UNMASKED_RENDERER_WEBGL
                        if (param === 37446) {
                            return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1080 Direct3D11 vs_5_0 ps_5_0, D3D11)';
                        }
                        // MAX_TEXTURE_SIZE - desktop GPU value
                        if (param === 3379) {
                            return 16384;
                        }
                        // MAX_VERTEX_ATTRIBS
                        if (param === 34921) {
                            return 16;
                        }
                        // MAX_VIEWPORT_DIMS
                        if (param === 3386) {
                            return new Int32Array([32767, 32767]);
                        }
                        return Reflect.apply(target, thisArg, args);
                    }
                };

                ['WebGLRenderingContext', 'WebGL2RenderingContext'].forEach(ctx => {
                    if (window[ctx]) {
                        const proto = window[ctx].prototype;
                        const originalGetParameter = proto.getParameter;
                        proto.getParameter = new Proxy(originalGetParameter, getParameterProxyHandler);
                    }
                });

                // ============ Canvas Fingerprint Protection ============
                const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
                HTMLCanvasElement.prototype.toDataURL = function(type) {
                    if (this.width === 0 || this.height === 0) {
                        return originalToDataURL.apply(this, arguments);
                    }
                    // Add subtle noise to canvas to randomize fingerprint
                    const ctx = this.getContext('2d');
                    if (ctx) {
                        const imageData = ctx.getImageData(0, 0, this.width, this.height);
                        const data = imageData.data;
                        // Subtle noise - change just a few pixels slightly
                        for (let i = 0; i < data.length; i += 4 * 100) {
                            data[i] = data[i] ^ 1;  // XOR with 1 - minimal change
                        }
                        ctx.putImageData(imageData, 0, 0);
                    }
                    return originalToDataURL.apply(this, arguments);
                };

                const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;
                CanvasRenderingContext2D.prototype.getImageData = function() {
                    const imageData = originalGetImageData.apply(this, arguments);
                    // Same subtle noise
                    for (let i = 0; i < imageData.data.length; i += 4 * 100) {
                        imageData.data[i] = imageData.data[i] ^ 1;
                    }
                    return imageData;
                };

                // ============ AudioContext Spoofing ============
                // Servers often don't have audio hardware - spoof it
                if (window.AudioContext || window.webkitAudioContext) {
                    const OriginalAudioContext = window.AudioContext || window.webkitAudioContext;

                    const spoofedAudioContext = function() {
                        const ctx = new OriginalAudioContext();

                        // Override createAnalyser to return consistent values
                        const originalCreateAnalyser = ctx.createAnalyser.bind(ctx);
                        ctx.createAnalyser = function() {
                            const analyser = originalCreateAnalyser();
                            const originalGetFloatFrequencyData = analyser.getFloatFrequencyData.bind(analyser);
                            analyser.getFloatFrequencyData = function(array) {
                                originalGetFloatFrequencyData(array);
                                // Add tiny noise
                                for (let i = 0; i < array.length; i += 10) {
                                    array[i] = array[i] + (Math.random() * 0.0001);
                                }
                            };
                            return analyser;
                        };

                        return ctx;
                    };

                    if (window.AudioContext) {
                        window.AudioContext = spoofedAudioContext;
                    }
                    if (window.webkitAudioContext) {
                        window.webkitAudioContext = spoofedAudioContext;
                    }
                }

                // ============ Hardware/Navigator Spoofing ============
                Object.defineProperty(navigator, 'hardwareConcurrency', {
                    get: () => 8
                });

                Object.defineProperty(navigator, 'deviceMemory', {
                    get: () => 8
                });

                // Connection API - look like broadband
                if (navigator.connection) {
                    Object.defineProperty(navigator.connection, 'effectiveType', {
                        get: () => '4g'
                    });
                    Object.defineProperty(navigator.connection, 'downlink', {
                        get: () => 10
                    });
                    Object.defineProperty(navigator.connection, 'rtt', {
                        get: () => 50
                    });
                }

                // ============ Screen Properties ============
                Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
                Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });
                Object.defineProperty(screen, 'availWidth', { get: () => screen.width });
                Object.defineProperty(screen, 'availHeight', { get: () => screen.height - 40 }); // Taskbar

                // ============ Plugins (Chrome shows some by default) ============
                Object.defineProperty(navigator, 'plugins', {
                    get: () => {
                        const plugins = [
                            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                            { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' }
                        ];
                        plugins.item = (i) => plugins[i];
                        plugins.namedItem = (name) => plugins.find(p => p.name === name);
                        plugins.refresh = () => {};
                        return plugins;
                    }
                });

                // ============ Permissions API ============
                if (navigator.permissions) {
                    const originalQuery = navigator.permissions.query;
                    navigator.permissions.query = (parameters) => {
                        if (parameters.name === 'notifications') {
                            return Promise.resolve({ state: 'prompt', onchange: null });
                        }
                        if (parameters.name === 'push') {
                            return Promise.resolve({ state: 'prompt', onchange: null });
                        }
                        if (parameters.name === 'midi') {
                            return Promise.resolve({ state: 'prompt', onchange: null });
                        }
                        return originalQuery.call(navigator.permissions, parameters);
                    };
                }

                // ============ Battery API (if available) ============
                if (navigator.getBattery) {
                    navigator.getBattery = () => Promise.resolve({
                        charging: true,
                        chargingTime: 0,
                        dischargingTime: Infinity,
                        level: 1.0,
                        addEventListener: () => {},
                        removeEventListener: () => {}
                    });
                }

                // ============ Timezone consistency ============
                const moscowOffset = -180;
                Date.prototype.getTimezoneOffset = function() { return moscowOffset; };

                // ============ Hide automation indicators ============
                // Remove Playwright/Puppeteer traces
                delete window.__playwright;
                delete window.__puppeteer_evaluation_script__;
                delete window.__selenium_unwrapped;
                delete window.__driver_evaluate;
                delete window.__webdriver_evaluate;
                delete window.__fxdriver_evaluate;
                delete window.__driver_unwrapped;
                delete window.__webdriver_unwrapped;
                delete window.__fxdriver_unwrapped;
                delete window._Selenium_IDE_Recorder;
                delete window._selenium;
                delete window.calledSelenium;
                delete document.__webdriver_script_fn;
                delete document.$cdc_asdjflasutopfhvcZLmcfl_;
                delete document.$chrome_asyncScriptInfo;

                // Hide headless indicators in error stack traces
                const originalError = Error;
                Error = function(...args) {
                    const error = new originalError(...args);
                    const originalStack = error.stack;
                    if (originalStack) {
                        error.stack = originalStack.replace(/headless/gi, 'chrome');
                    }
                    return error;
                };
                Error.prototype = originalError.prototype;
            """)

        return page

    async def _warmup(self, page: Page) -> None:
        """Visit homepage before searching to look like a real user."""
        logger.info("Warming up: visiting ozon.ru homepage")
        try:
            await page.goto(settings.base_url, wait_until="domcontentloaded")
            # Human-like: wait a bit and move mouse
            await page.wait_for_timeout(random.randint(500, 1200))
            await page.mouse.move(
                random.randint(300, 900),
                random.randint(200, 500)
            )
            await page.wait_for_timeout(random.randint(200, 500))
        except Exception:
            await page.wait_for_timeout(500)
        logger.info("Warmup complete")

    async def _is_captcha_page(self, page: Page) -> bool:
        try:
            title = await page.title()
            captcha_keywords = ["бот", "robot", "bot", "captcha", "подтверд", "confirm", "antibot", "challenge"]
            return any(kw in title.lower() for kw in captcha_keywords)
        except Exception:
            return False

    async def _is_blocked_page(self, page: Page) -> bool:
        """Check if we hit the 'Доступ ограничен' block page."""
        try:
            heading = await page.query_selector("h1")
            if heading:
                text = await heading.inner_text()
                if "доступ ограничен" in text.lower():
                    return True
            return False
        except Exception:
            return False

    async def _check_page_status(self, page: Page) -> tuple[bool, bool]:
        """Check for captcha and block in a single JS call. Returns (is_captcha, is_blocked)."""
        try:
            result = await page.evaluate("""
                () => {
                    const title = document.title.toLowerCase();
                    const captchaKeywords = ['бот', 'robot', 'bot', 'captcha', 'подтверд', 'confirm', 'antibot', 'challenge'];
                    const isCaptcha = captchaKeywords.some(kw => title.includes(kw));

                    const h1 = document.querySelector('h1');
                    const isBlocked = h1 && h1.innerText.toLowerCase().includes('доступ ограничен');

                    return { isCaptcha, isBlocked };
                }
            """)
            return result.get("isCaptcha", False), result.get("isBlocked", False)
        except Exception:
            return False, False

    async def _wait_for_captcha(self, page: Page) -> None:
        """Wait for manual captcha/challenge solving."""
        logger.warning("Captcha/challenge detected!")
        logger.warning("Please solve captcha manually in the browser window...")
        logger.info("You have 60 seconds to solve the captcha")
        for _ in range(60):
            if not await self._is_captcha_page(page):
                logger.info("Captcha solved!")
                return
            await page.wait_for_timeout(1000)
        logger.warning("Captcha timeout - proceeding anyway...")

    async def _handle_block_page(self, page: Page, max_retries: int = 3) -> bool:
        """
        Handle 'Доступ ограничен' block page.
        Waits for JS challenge to resolve, then retries with refresh.
        """
        logger.info("Block page detected, waiting for JS challenge to resolve...")

        # Wait for JS challenge (up to 10s)
        for i in range(5):
            await page.wait_for_timeout(2000)
            if not await self._is_blocked_page(page):
                logger.info(f"JS challenge resolved after {(i + 1) * 2}s")
                return True

        # Try refresh
        for attempt in range(max_retries):
            if not await self._is_blocked_page(page):
                return True
            logger.warning(f"Still blocked, refreshing (attempt {attempt + 1}/{max_retries})")
            refresh_button = await page.query_selector("button:has-text('Обновить')")
            if refresh_button:
                await refresh_button.click()
            else:
                await page.reload()
            await page.wait_for_timeout(5000)

        return not await self._is_blocked_page(page)

    def _extract_product_id(self, href: str) -> str | None:
        """Extract product ID from URL."""
        if not href or "/product/" not in href:
            return None
        if "/reviews" in href or "/questions" in href:
            return None
        product_path = href.split("/product/")[-1].split("?")[0].rstrip("/")
        parts = product_path.split("-")
        if not parts:
            return None
        product_id = parts[-1]
        return product_id if product_id.isdigit() else None

    async def _collect_products_from_page(
        self, page: Page, seen_products: set[str]
    ) -> list[str]:
        """Collect new product IDs from current page state using optimized JS extraction."""
        # Pass seen products to JS to filter there (reduces data transfer)
        seen_list = list(seen_products) if len(seen_products) < 2000 else []

        all_ids = await page.evaluate("""
            (seen) => {
                const seenSet = new Set(seen);
                const ids = [];
                // Use faster iteration with early termination hints
                const links = document.getElementsByTagName('a');
                for (let i = 0; i < links.length; i++) {
                    const href = links[i].href;
                    if (!href || !href.includes('/product/')) continue;
                    if (href.includes('/reviews') || href.includes('/questions')) continue;
                    // Extract ID directly without full regex
                    const productIdx = href.indexOf('/product/');
                    if (productIdx === -1) continue;
                    const afterProduct = href.substring(productIdx + 9);
                    const queryIdx = afterProduct.indexOf('?');
                    const path = queryIdx > -1 ? afterProduct.substring(0, queryIdx) : afterProduct;
                    const lastDash = path.lastIndexOf('-');
                    if (lastDash === -1) continue;
                    const id = path.substring(lastDash + 1).replace(/\\/$/, '');
                    if (!/^\\d+$/.test(id)) continue;
                    if (!seenSet.has(id) && !ids.includes(id)) {
                        ids.push(id);
                    }
                }
                return ids;
            }
        """, seen_list)

        # Update seen set and return new products
        for product_id in all_ids:
            seen_products.add(product_id)
        return all_ids

    async def find_product_position(
        self, query: str, target_article: str, max_position: int = 1000, page: Page | None = None
    ) -> int | None:
        """
        Search for a product position in Ozon search results using infinite scroll.
        Returns the position (1-based) or None if not found within max_position.
        """
        logger.info(f"{'='*60}")
        logger.info(f"Searching position for article {target_article}")
        logger.info(f"Query: {query}")
        logger.info(f"{'='*60}")

        page_provided = page is not None
        if not page_provided:
            page = await self._new_page()

        seen_products: set[str] = set()
        position = 0
        scroll_count = 0

        try:
            search_url = f"{settings.base_url}/search/?text={query}"
            logger.debug(f"Opening URL: {search_url}")
            try:
                await page.goto(search_url, wait_until="domcontentloaded")
            except Exception as e:
                if "Timeout" in str(e) or "ERR_TIMED_OUT" in str(e):
                    logger.warning("Page load timeout, waiting for products...")
                    try:
                        await page.wait_for_selector("a[href*='/product/']", timeout=30000)
                        logger.info("Products loaded after extended wait")
                    except Exception:
                        logger.error("Products did not load - page timeout")
                        raise OzonPageLoadError(f"Page load timeout for query: {query}")
                else:
                    raise OzonPageLoadError(f"Page load error: {e}")

            # Check for JS challenge (antibot) - wait for it to resolve
            body_length = await page.evaluate("document.body.innerHTML.length")
            if body_length < 5000:
                # Dump what we got for debugging
                page_info = await page.evaluate("""
                    () => ({
                        url: location.href,
                        title: document.title,
                        bodyText: document.body.innerText.substring(0, 500),
                        scripts: document.querySelectorAll('script').length
                    })
                """)
                logger.info(f"Small page ({body_length} chars), waiting for JS... Title: {page_info.get('title')}")
                logger.debug(f"Page text: {page_info.get('bodyText', '')[:200]}")

                # Wait up to 15 seconds for JS challenge to resolve
                for i in range(30):
                    await page.wait_for_timeout(500)
                    body_length = await page.evaluate("document.body.innerHTML.length")
                    if body_length > 10000:
                        logger.info(f"JS challenge resolved after {(i+1)*0.5}s, body: {body_length} chars")
                        break
                else:
                    # Still small - dump current state
                    final_info = await page.evaluate("""
                        () => ({
                            url: location.href,
                            title: document.title,
                            bodyLength: document.body.innerHTML.length,
                            bodyText: document.body.innerText.substring(0, 300)
                        })
                    """)
                    logger.error(f"JS challenge FAILED after 15s. State: {final_info}")

            # Human-like behavior: small random delay + mouse movement after page load
            await page.wait_for_timeout(random.randint(300, 800))
            await page.mouse.move(
                random.randint(400, 800),
                random.randint(200, 400)
            )

            # Check for block/captcha before waiting for products
            if await self._is_blocked_page(page):
                logger.warning("Block page detected after load")
                if not await self._handle_block_page(page):
                    raise OzonBlockedError("block_restart")

            if await self._is_captcha_page(page):
                await self._wait_for_captcha(page)

            # Wait for products to load
            try:
                await page.wait_for_selector("a[href*='/product/']", timeout=15000)
                logger.debug("Products loaded on page")
            except Exception:
                # Debug: dump page info
                debug_info = await page.evaluate("""
                    () => ({
                        url: location.href,
                        title: document.title,
                        bodyLength: document.body.innerHTML.length,
                        hasProducts: document.querySelectorAll('a[href*="/product/"]').length,
                        h1: document.querySelector('h1')?.innerText || 'no h1'
                    })
                """)
                logger.warning(f"No products found. Debug: {debug_info}")
                raise OzonPageLoadError(f"No products found for query: {query}")

            # Collect products from first page
            new_products = await self._collect_products_from_page(page, seen_products)
            logger.info(f"Initial load: found {len(new_products)} products")

            for product_id in new_products:
                position += 1
                if product_id == target_article:
                    logger.info(f"Found article {target_article} at position {position}")
                    return position

            # Infinite scroll loop
            empty_scrolls = 0
            max_empty_scrolls = 3

            while position < max_position:
                # Scroll using multiple methods for better compatibility
                # Method 1: Smooth scroll with wheel event (triggers Intersection Observer)
                await page.evaluate("""
                    () => {
                        // Dispatch wheel event to trigger lazy loading
                        const event = new WheelEvent('wheel', {
                            deltaY: 1000,
                            bubbles: true
                        });
                        document.dispatchEvent(event);

                        // Scroll to bottom
                        window.scrollTo({
                            top: document.body.scrollHeight,
                            behavior: 'smooth'
                        });
                    }
                """)
                await page.wait_for_timeout(random.randint(800, 1200))

                # Method 2: Additional scroll event dispatch
                await page.evaluate("""
                    () => {
                        window.dispatchEvent(new Event('scroll'));
                        document.dispatchEvent(new Event('scroll'));
                    }
                """)
                await page.wait_for_timeout(random.randint(300, 500))
                scroll_count += 1

                # Check for captcha/block
                is_captcha, is_blocked = await self._check_page_status(page)
                if is_captcha:
                    await self._wait_for_captcha(page)
                    continue
                if is_blocked:
                    if not await self._handle_block_page(page):
                        return -1
                    continue

                new_products = await self._collect_products_from_page(page, seen_products)

                if not new_products:
                    empty_scrolls += 1
                    # Log current page height for debugging
                    page_height = await page.evaluate("document.body.scrollHeight")
                    scroll_pos = await page.evaluate("window.scrollY")
                    logger.debug(f"Empty scroll {empty_scrolls}/{max_empty_scrolls}, height={page_height}, pos={scroll_pos}")

                    # Try alternative scroll method on empty scrolls
                    if empty_scrolls == 1:
                        # Try keyboard scroll (End key)
                        await page.keyboard.press("End")
                        await page.wait_for_timeout(500)
                    elif empty_scrolls == 2:
                        # Try mouse wheel scroll
                        await page.mouse.wheel(0, 3000)
                        await page.wait_for_timeout(500)

                    if empty_scrolls >= max_empty_scrolls:
                        logger.info(f"No more products after {scroll_count} scrolls, total checked: {position}")
                        return None
                    continue

                empty_scrolls = 0
                logger.debug(f"Scroll {scroll_count}: +{len(new_products)} products (total: {position + len(new_products)})")

                for product_id in new_products:
                    position += 1
                    if product_id == target_article:
                        logger.info(f"FOUND! Article {target_article} at position {position}")
                        logger.info(f"Total scrolls: {scroll_count}, total products checked: {position}")
                        return position
                    if position >= max_position:
                        logger.info(f"Reached max position {max_position}, article not found")
                        return None

                if position % 300 == 0:
                    logger.info(f"Progress: checked {position} products (scroll {scroll_count})...")

            # If we exit the loop normally, article was not found
            logger.info(f"NOT FOUND: Article {target_article} not in top {position} positions")
            logger.info(f"Total scrolls: {scroll_count}")
            return None

        finally:
            if not page_provided:
                await page.close()
