import asyncio
import platform
import random
import re
import shutil
from decimal import Decimal
from pathlib import Path

from playwright.async_api import (
    async_playwright,
    Page,
    Playwright,
    BrowserContext,
)
from playwright_stealth import Stealth
from app.logging_config import get_logger
from app.schemas import Product
from app.services.captcha import RuCaptchaSolver, CaptchaSolverError
from app.settings import settings

logger = get_logger(__name__)

# Firefox user agents for different platforms
FIREFOX_USER_AGENTS = {
    "Darwin": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:134.0) Gecko/20100101 Firefox/134.0",
    "Linux": "Mozilla/5.0 (X11; Linux x86_64; rv:134.0) Gecko/20100101 Firefox/134.0",
    "Windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
}


class OzonBlockedError(Exception):
    """Raised when Ozon blocks access and refresh doesn't help."""
    pass


class OzonPageLoadError(Exception):
    """Raised when page fails to load (timeout, network error)."""
    pass


class OzonParser:
    def __init__(self, captcha_solver: RuCaptchaSolver | None = None) -> None:
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._captcha_solver = captcha_solver
        self._proxies: list[str] = self._parse_proxies()
        self._current_proxy_idx: int = 0
        self._restart_lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        """Get or create restart lock."""
        if self._restart_lock is None:
            self._restart_lock = asyncio.Lock()
        return self._restart_lock

    def _parse_proxies(self) -> list[str]:
        """Parse proxy list from settings."""
        if not settings.proxy_list:
            return []
        proxies = [p.strip() for p in settings.proxy_list.split(",") if p.strip()]
        logger.info(f"Loaded {len(proxies)} proxies")
        return proxies

    def _get_next_proxy(self) -> dict | None:
        """Get next proxy in rotation, or single proxy_server if set."""
        # Priority 1: Single proxy server (e.g., "http://127.0.0.1:8888")
        if settings.proxy_server:
            logger.info(f"Using proxy server: {settings.proxy_server}")
            return {"server": settings.proxy_server}

        # Priority 2: Proxy rotation list
        if not self._proxies:
            return None

        proxy_str = self._proxies[self._current_proxy_idx]
        self._current_proxy_idx = (self._current_proxy_idx + 1) % len(self._proxies)

        # Parse user:pass@host:port
        if "@" in proxy_str:
            auth, server = proxy_str.rsplit("@", 1)
            username, password = auth.split(":", 1)
            host, port = server.rsplit(":", 1)
            proxy = {
                "server": f"http://{host}:{port}",
                "username": username,
                "password": password,
            }
        else:
            proxy = {"server": f"http://{proxy_str}"}

        logger.info(f"Using proxy: {proxy['server']}")
        return proxy

    def _build_launch_options(self) -> dict:
        user_data_dir = Path("browser_data")
        user_data_dir.mkdir(exist_ok=True)

        is_firefox = settings.browser_type == "firefox"

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

        if is_firefox:
            # Firefox user agent matching current Firefox version
            options["user_agent"] = FIREFOX_USER_AGENTS.get(
                platform.system(), FIREFOX_USER_AGENTS["Linux"]
            )

            # Firefox-specific preferences for anti-detection
            options["firefox_user_prefs"] = {
                # Disable webdriver detection
                "dom.webdriver.enabled": False,
                "useAutomationExtension": False,
                # Privacy and fingerprint resistance (but not too aggressive)
                "privacy.resistFingerprinting": False,  # True breaks many sites
                "privacy.trackingprotection.enabled": False,
                # Disable telemetry
                "toolkit.telemetry.enabled": False,
                "datareporting.healthreport.uploadEnabled": False,
                # WebGL - keep enabled for fingerprint consistency
                "webgl.disabled": False,
                # Media settings (look like real browser)
                "media.peerconnection.enabled": True,
                "media.navigator.enabled": True,
                # Geolocation
                "geo.enabled": True,
                # Performance settings
                "network.http.pipelining": True,
                "network.http.proxy.pipelining": True,
                # Disable safe browsing (faster, less detectable)
                "browser.safebrowsing.enabled": False,
                "browser.safebrowsing.malware.enabled": False,
                # Canvas - allow for consistent fingerprint
                "canvas.poisondata": False,
                # Disable devtools detection
                "devtools.selfxss.count": 100,
            }
        else:
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
            # User-Agent должен соответствовать реальной версии Chromium
            # Playwright bundled Chromium ~133-134, но лучше использовать 131 (стабильный Chrome)
            # Важно: на Linux сервере лучше притворяться Windows - меньше подозрений
            options["user_agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )

        proxy = self._get_next_proxy()
        if proxy:
            options["proxy"] = proxy

        return options

    def _get_browser_type(self):
        """Get browser type based on settings."""
        if not self._playwright:
            raise RuntimeError("Playwright not initialized")
        browser_map = {
            "chromium": self._playwright.chromium,
            "firefox": self._playwright.firefox,
            "webkit": self._playwright.webkit,
        }
        return browser_map.get(settings.browser_type, self._playwright.chromium)

    async def __aenter__(self) -> "OzonParser":
        self._playwright = await async_playwright().start()
        self._context = await self._get_browser_type().launch_persistent_context(
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

            self._context = await self._get_browser_type().launch_persistent_context(
                **self._build_launch_options()
            )
            logger.info("Browser restarted with clean profile")

    async def _new_page(self, block_resources: bool = True) -> Page:
        if not self._context:
            raise RuntimeError("Context not initialized. Use async with.")
        page = await self._context.new_page()
        page.set_default_timeout(settings.browser_timeout)

        # Block heavy resources to speed up loading
        if block_resources:
            blocked_types = {"image", "stylesheet", "font", "media"}
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

        is_firefox = settings.browser_type == "firefox"

        if is_firefox:
            # Firefox-specific stealth script
            await page.add_init_script("""
                // Hide webdriver property
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined,
                    configurable: true
                });

                // Firefox doesn't have these Chrome-specific properties
                delete window.chrome;
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;

                // Consistent navigator properties for Firefox
                const platform = navigator.userAgent.includes('Mac') ? 'MacIntel' :
                                navigator.userAgent.includes('Windows') ? 'Win32' : 'Linux x86_64';
                Object.defineProperty(navigator, 'platform', { get: () => platform });

                // Firefox typical hardware values
                Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
                // Note: Firefox doesn't expose deviceMemory

                // Screen properties
                Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
                Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });

                // Firefox-specific: override Notification permission check
                const originalQuery = window.Notification && Notification.requestPermission;
                if (originalQuery) {
                    Notification.requestPermission = function() {
                        return Promise.resolve('default');
                    };
                }

                // Plugins array (Firefox shows empty by default in privacy mode)
                Object.defineProperty(navigator, 'plugins', {
                    get: () => {
                        const plugins = [];
                        plugins.length = 0;
                        return plugins;
                    }
                });

                // Firefox doesn't leak automation through permissions API
                if (navigator.permissions) {
                    const originalQuery = navigator.permissions.query;
                    navigator.permissions.query = (parameters) => {
                        if (parameters.name === 'notifications') {
                            return Promise.resolve({ state: 'prompt', onchange: null });
                        }
                        return originalQuery.call(navigator.permissions, parameters);
                    };
                }

                // Consistent WebGL for Firefox
                const getParameterProxyHandler = {
                    apply: function(target, thisArg, args) {
                        const param = args[0];
                        // UNMASKED_VENDOR_WEBGL
                        if (param === 37445) {
                            return 'Mozilla';
                        }
                        // UNMASKED_RENDERER_WEBGL - Firefox shows different format
                        if (param === 37446) {
                            return 'Mesa Intel(R) UHD Graphics 630 (CFL GT2)';
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

                // Override Date to use Moscow timezone consistently
                const originalDate = Date;
                const moscowOffset = -180; // UTC+3 in minutes (negative because getTimezoneOffset returns inverted)
                Date.prototype.getTimezoneOffset = function() { return moscowOffset; };
            """)
        else:
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

    @staticmethod
    def _parse_price(price_text: str | None) -> Decimal | None:
        if not price_text:
            return None
        digits = re.sub(r"[^\d]", "", price_text)
        return Decimal(digits) if digits else None

    @staticmethod
    def _parse_rating(rating_text: str | None) -> float | None:
        if not rating_text:
            return None
        match = re.search(r"(\d+[.,]?\d*)", rating_text)
        if match:
            return float(match.group(1).replace(",", "."))
        return None

    @staticmethod
    def _parse_reviews_count(reviews_text: str | None) -> int | None:
        if not reviews_text:
            return None
        digits = re.sub(r"[^\d]", "", reviews_text)
        return int(digits) if digits else None

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
        """Attempt to solve captcha automatically via RuCaptcha, fallback to manual."""
        logger.warning("Captcha/challenge detected!")

        # Try automatic solving if captcha solver is configured
        if self._captcha_solver:
            try:
                solved = await self._solve_captcha_auto(page)
                if solved:
                    return
            except CaptchaSolverError as e:
                logger.warning(f"Automatic captcha solving failed: {e}")

        # Fallback to manual solving
        logger.warning("Please solve captcha manually in the browser window...")
        logger.info("You have 60 seconds to solve the captcha")
        for _ in range(60):
            if not await self._is_captcha_page(page):
                logger.info("Captcha solved!")
                return
            await page.wait_for_timeout(1000)
        logger.warning("Captcha timeout - proceeding anyway...")

    async def _solve_captcha_auto(self, page: Page) -> bool:
        """Attempt to solve captcha automatically using RuCaptcha."""
        if not self._captcha_solver:
            return False

        page_url = page.url

        # Try Cloudflare Turnstile
        turnstile_element = await page.query_selector(
            "[data-sitekey].cf-turnstile, .cf-turnstile[data-sitekey], "
            "iframe[src*='challenges.cloudflare.com']"
        )
        if turnstile_element:
            site_key = await turnstile_element.get_attribute("data-sitekey")
            if not site_key:
                parent = await page.query_selector(".cf-turnstile[data-sitekey]")
                if parent:
                    site_key = await parent.get_attribute("data-sitekey")

            if site_key:
                logger.info(f"Found Cloudflare Turnstile with sitekey: {site_key[:20]}...")
                token = await self._captcha_solver.solve_turnstile(
                    site_key=site_key, page_url=page_url,
                )
                await page.evaluate("""
                    (token) => {
                        const input = document.querySelector('[name="cf-turnstile-response"]');
                        if (input) input.value = token;
                        const form = document.querySelector('form');
                        if (form) form.submit();
                    }
                """, token)
                await page.wait_for_timeout(3000)
                if not await self._is_captcha_page(page):
                    logger.info("Turnstile solved successfully!")
                    return True

        # Try reCAPTCHA
        recaptcha_element = await page.query_selector("[data-sitekey]")
        if recaptcha_element:
            site_key = await recaptcha_element.get_attribute("data-sitekey")
            if site_key:
                logger.info(f"Found reCAPTCHA v2 with sitekey: {site_key[:20]}...")
                size = await recaptcha_element.get_attribute("data-size")
                token = await self._captcha_solver.solve_recaptcha_v2(
                    site_key=site_key, page_url=page_url, invisible=size == "invisible",
                )
                await page.evaluate("""
                    (token) => {
                        const textarea = document.querySelector('#g-recaptcha-response');
                        if (textarea) { textarea.value = token; textarea.style.display = 'block'; }
                        const hidden = document.querySelector('[name="g-recaptcha-response"]');
                        if (hidden) hidden.value = token;
                    }
                """, token)
                submit = await page.query_selector(
                    "button[type='submit'], input[type='submit'], button:has-text('Подтвердить')"
                )
                if submit:
                    await submit.click()
                    await page.wait_for_timeout(3000)
                if not await self._is_captcha_page(page):
                    logger.info("reCAPTCHA solved successfully!")
                    return True

        # Try image captcha
        captcha_image = await page.query_selector(
            "img[src*='captcha'], img[alt*='captcha'], .captcha-image img"
        )
        if captcha_image:
            logger.info("Found image captcha")
            src = await captcha_image.get_attribute("src")
            if src and src.startswith("data:image"):
                image_base64 = src.split(",")[1]
            else:
                import base64
                image_bytes = await captcha_image.screenshot()
                image_base64 = base64.b64encode(image_bytes).decode()

            text = await self._captcha_solver.solve_image_captcha(image_base64)
            logger.info(f"Image captcha solved: {text}")
            captcha_input = await page.query_selector(
                "input[name*='captcha'], input[id*='captcha'], input[placeholder*='код'], input[type='text']"
            )
            if captcha_input:
                await captcha_input.fill(text)
                submit = await page.query_selector(
                    "button[type='submit'], input[type='submit'], button:has-text('Подтвердить')"
                )
                if submit:
                    await submit.click()
                    await page.wait_for_timeout(3000)
                if not await self._is_captcha_page(page):
                    logger.info("Image captcha solved successfully!")
                    return True

        logger.warning("Unknown captcha type, cannot solve automatically")
        return False

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
        Search for a product position in Ozon search results.
        Uses pagination (page=N) which works more reliably on servers.
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
        page_num = 1
        products_per_page = 36  # Ozon typically shows 36 per page
        max_pages = (max_position // products_per_page) + 2

        try:
            # Use pagination instead of infinite scroll (works better on servers)
            base_search_url = f"{settings.base_url}/search/?text={query}"
            search_url = base_search_url
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

            # Human-like behavior: small random delay + mouse movement after page load
            await page.wait_for_timeout(random.randint(300, 800))
            # Move mouse to simulate human presence (fast, no click)
            await page.mouse.move(
                random.randint(400, 800),
                random.randint(200, 400)
            )

            # Wait for products to load
            try:
                await page.wait_for_selector("a[href*='/product/']", timeout=15000)
                logger.debug("Products loaded on page")
            except Exception:
                logger.warning("Timeout waiting for products to appear")
                raise OzonPageLoadError(f"No products found for query: {query}")

            if await self._is_captcha_page(page):
                await self._wait_for_captcha(page)
                await page.wait_for_timeout(500)

            # Handle block page
            if await self._is_blocked_page(page):
                if not await self._handle_block_page(page):
                    raise OzonBlockedError("block_restart")

            # Collect products from first page
            new_products = await self._collect_products_from_page(page, seen_products)
            initial_count = len(new_products)
            logger.info(f"Page {page_num}: found {initial_count} products")

            for product_id in new_products:
                position += 1
                if product_id == target_article:
                    logger.info(f"Found article {target_article} at position {position}")
                    return position

            # Check if infinite scroll works (products > 30 usually means it works)
            # Try one scroll to see if more products load
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)
            scroll_products = await self._collect_products_from_page(page, seen_products)

            use_pagination = len(scroll_products) == 0  # No new products = pagination mode

            # Get actual URL after possible redirect (e.g., /search/ -> /category/)
            current_url = page.url.split('?')[0]  # Base URL without query params
            # Preserve query params but remove page if exists
            current_params = page.url.split('?')[1] if '?' in page.url else ''
            current_params = '&'.join(p for p in current_params.split('&') if not p.startswith('page='))

            if use_pagination:
                logger.info("Infinite scroll disabled, switching to pagination mode")

            # Add scroll products to position count
            for product_id in scroll_products:
                position += 1
                if product_id == target_article:
                    logger.info(f"Found article {target_article} at position {position}")
                    return position

            # Pagination loop
            empty_pages = 0
            max_empty_pages = 2

            while position < max_position and page_num < max_pages:
                if use_pagination:
                    # Navigate to next page using current URL (after redirect)
                    page_num += 1
                    if current_params:
                        pagination_url = f"{current_url}?{current_params}&page={page_num}"
                    else:
                        pagination_url = f"{current_url}?page={page_num}"
                    logger.debug(f"Opening page {page_num}: {pagination_url}")

                    try:
                        await page.goto(pagination_url, wait_until="domcontentloaded")
                    except Exception as e:
                        if "Timeout" in str(e):
                            logger.warning(f"Page {page_num} timeout, trying to continue...")
                        else:
                            raise

                    # Human-like delay
                    await page.wait_for_timeout(random.randint(300, 700))
                    await page.mouse.move(random.randint(400, 800), random.randint(200, 400))

                    # Check for captcha/block
                    is_captcha, is_blocked = await self._check_page_status(page)
                    if is_captcha:
                        await self._wait_for_captcha(page)
                    if is_blocked:
                        if not await self._handle_block_page(page):
                            logger.error("Blocked during pagination")
                            return -1

                    # Wait for products
                    try:
                        await page.wait_for_selector("a[href*='/product/']", timeout=10000)
                    except Exception:
                        logger.warning(f"No products on page {page_num}")
                        empty_pages += 1
                        if empty_pages >= max_empty_pages:
                            logger.info(f"Reached end of results at page {page_num}")
                            return None
                        continue

                    new_products = await self._collect_products_from_page(page, seen_products)
                else:
                    # Infinite scroll mode (for local/residential IPs)
                    await page.evaluate("""
                        () => {
                            const h = document.body.scrollHeight;
                            window.scrollTo(0, h);
                        }
                    """)
                    await page.wait_for_timeout(800)

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
                    page_num += 1  # Count scrolls as "pages" for logging

                if not new_products:
                    empty_pages += 1
                    if empty_pages >= max_empty_pages:
                        logger.info(f"No more products, total checked: {position}")
                        return None
                    continue

                empty_pages = 0
                logger.debug(f"Page {page_num}: +{len(new_products)} products (total: {position + len(new_products)})")

                for product_id in new_products:
                    position += 1
                    if product_id == target_article:
                        logger.info(f"FOUND! Article {target_article} at position {position}")
                        logger.info(f"Total pages: {page_num}, total products checked: {position}")
                        return position
                    if position >= max_position:
                        logger.info(f"Reached max position {max_position}, article not found")
                        return None

                if position % 300 == 0:
                    logger.info(f"Progress: checked {position} products (page {page_num})...")

            # If we exit the loop normally, article was not found
            logger.info(f"NOT FOUND: Article {target_article} not in top {position} positions")
            logger.info(f"Total pages: {page_num}")
            return None

        finally:
            if not page_provided:
                await page.close()

    async def parse_product(self, url: str) -> Product:
        logger.info(f"Parsing product: {url}")
        page = await self._new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)

            if await self._is_captcha_page(page):
                await self._wait_for_captcha(page)
                await page.wait_for_timeout(500)

            try:
                await page.wait_for_selector("h1", timeout=15000)
            except Exception:
                pass

            title_el = await page.query_selector("h1")
            title = await title_el.inner_text() if title_el else "Unknown"

            price: Decimal | None = None
            for selector in [
                "[data-widget='webPrice'] span",
                "[data-widget='webSale'] span",
                "span[class*='price']",
                "span[class*='Price']",
            ]:
                price_el = await page.query_selector(selector)
                if price_el:
                    price = self._parse_price(await price_el.inner_text())
                    if price:
                        break

            rating: float | None = None
            reviews_count: int | None = None
            for selector in [
                "[data-widget='webSingleProductScore']",
                "[data-widget='webReviewProductScore']",
                "div[class*='rating']",
            ]:
                rating_el = await page.query_selector(selector)
                if rating_el:
                    rating_text = await rating_el.inner_text()
                    rating = self._parse_rating(rating_text)
                    reviews_count = self._parse_reviews_count(rating_text)
                    if rating:
                        break

            seller: str | None = None
            for selector in [
                "[data-widget='webCurrentSeller'] a",
                "[data-widget='webSeller'] a",
                "a[href*='/seller/']",
            ]:
                seller_el = await page.query_selector(selector)
                if seller_el:
                    seller = (await seller_el.inner_text()).strip()
                    if seller:
                        break

            out_of_stock_el = await page.query_selector("[data-widget='webOutOfStock']")

            product = Product(
                url=page.url,
                title=title.strip(),
                price=price,
                original_price=None,
                rating=rating,
                reviews_count=reviews_count,
                seller=seller,
                in_stock=out_of_stock_el is None,
            )
            logger.info(f"Successfully parsed: {product.title[:50]}...")
            return product
        finally:
            await page.close()
