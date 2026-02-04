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
        """Get next proxy in rotation."""
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

        args = [
            # Anti-detection
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-site-isolation-trials",
            # Performance & stability
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            # Hide automation
            "--disable-infobars",
            "--disable-background-networking",
            "--disable-breakpad",
            "--disable-component-update",
            "--no-first-run",
            "--no-default-browser-check",
            # Window size (not default 800x600)
            "--window-size=1920,1080",
            "--start-maximized",
        ]

        # Use new headless mode if enabled (less detectable)
        if settings.browser_headless and settings.browser_headless_new:
            args.append("--headless=new")

        options = dict(
            user_data_dir=str(user_data_dir),
            headless=settings.browser_headless and not settings.browser_headless_new,
            args=args,
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
                if platform.system() == "Linux"
                else "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
            color_scheme="light",
            timezone_id="Europe/Moscow",
            geolocation={"latitude": 55.7558, "longitude": 37.6173},  # Moscow
            permissions=["geolocation"],
        )

        proxy = self._get_next_proxy()
        if proxy:
            options["proxy"] = proxy

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

    async def _new_page(self) -> Page:
        if not self._context:
            raise RuntimeError("Context not initialized. Use async with.")
        page = await self._context.new_page()
        page.set_default_timeout(settings.browser_timeout)

        # Apply stealth mode (hides webdriver, fixes fingerprints, etc.)
        stealth = Stealth(
            navigator_languages_override=("ru-RU", "ru"),
            navigator_platform_override="Linux x86_64" if platform.system() == "Linux" else "MacIntel",
        )
        await stealth.apply_stealth_async(page)

        return page

    async def _warmup(self, page: Page) -> None:
        """Visit homepage before searching to look like a real user."""
        logger.info("Warming up: visiting ozon.ru homepage")
        try:
            await page.goto(settings.base_url, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            await page.wait_for_timeout(2000)
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
        """Collect new product IDs from current page state."""
        links = await page.query_selector_all("a[href*='/product/']")
        new_products: list[str] = []
        for link in links:
            href = await link.get_attribute("href")
            product_id = self._extract_product_id(href)
            if product_id and product_id not in seen_products:
                seen_products.add(product_id)
                new_products.append(product_id)
        return new_products

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

            # Wait for products to load
            try:
                await page.wait_for_selector("a[href*='/product/']", timeout=15000)
                logger.debug("Products loaded on page")
            except Exception:
                logger.warning("Timeout waiting for products to appear")
                raise OzonPageLoadError(f"No products found for query: {query}")

            # Wait for network to settle
            try:
                await page.wait_for_load_state("networkidle", timeout=settings.initial_load_networkidle_timeout)
            except Exception:
                await page.wait_for_timeout(settings.initial_load_fallback_delay)

            if await self._is_captcha_page(page):
                await self._wait_for_captcha(page)
                await page.wait_for_timeout(2000)

            # Handle block page
            if await self._is_blocked_page(page):
                if not await self._handle_block_page(page):
                    raise OzonBlockedError("block_restart")

            # Collect initial products
            new_products = await self._collect_products_from_page(page, seen_products)
            logger.info(f"Initial load: found {len(new_products)} products")

            for product_id in new_products:
                position += 1
                if product_id == target_article:
                    logger.info(f"Found article {target_article} at position {position}")
                    return position

            # Scroll and load more products
            no_new_products_count = 0
            max_no_new_products = 8  # Increased for slow proxies

            while position < max_position:
                scroll_count += 1

                prev_product_count = len(seen_products)
                prev_height = await page.evaluate("document.body.scrollHeight")

                await page.mouse.wheel(0, random.randint(2000, 5000))
                await page.wait_for_timeout(300)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

                # Dynamic wait: wait for new products to appear (not just page height)
                for wait_attempt in range(10):  # Up to 10 seconds
                    await page.wait_for_timeout(1000)

                    # Check for captcha/block during wait
                    if await self._is_captcha_page(page):
                        await self._wait_for_captcha(page)
                        await page.wait_for_timeout(2000)

                    if await self._is_blocked_page(page):
                        if not await self._handle_block_page(page):
                            logger.error("Failed to bypass block page during scroll")
                            return -1

                    # Check if new products appeared
                    current_products = await self._collect_products_from_page(page, set(seen_products))
                    if current_products:
                        logger.debug(f"New products loaded after {wait_attempt + 1}s wait")
                        break

                    # Check if page height changed (might still be loading)
                    current_height = await page.evaluate("document.body.scrollHeight")
                    if current_height == prev_height and wait_attempt >= 2:
                        # Page stopped growing, probably at the end
                        break

                new_products = await self._collect_products_from_page(page, seen_products)
                current_height = await page.evaluate("document.body.scrollHeight")

                if not new_products:
                    if current_height == prev_height:
                        # Страница не выросла и новых товаров нет — достигли конца
                        logger.info(f"Reached end of page, total checked: {position}")
                        break
                    # Страница выросла (подгрузился футер/баннер), но товаров нет
                    no_new_products_count += 1
                    if no_new_products_count >= max_no_new_products:
                        logger.info(f"No more products loading, total checked: {position}")
                        break
                    continue

                no_new_products_count = 0
                logger.debug(f"Scroll #{scroll_count}: +{len(new_products)} products")

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
                    logger.info(f"Progress: checked {position} products (scroll #{scroll_count})...")

            if position < max_position:
                logger.warning(
                    f"INCOMPLETE: Only checked {position}/{max_position} positions "
                    f"(page ended prematurely)"
                )
                return -1

            logger.info(f"NOT FOUND: Article {target_article} not in top {position} positions")
            logger.info(f"Total scrolls: {scroll_count}")
            return None

        finally:
            if not page_provided:
                await page.close()

    async def parse_product(self, url: str) -> Product:
        logger.info(f"Parsing product: {url}")
        page = await self._new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            if await self._is_captcha_page(page):
                await self._wait_for_captcha(page)
                await page.wait_for_timeout(2000)

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
