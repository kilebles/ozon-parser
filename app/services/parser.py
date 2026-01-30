import re
from decimal import Decimal
from pathlib import Path

from playwright.async_api import (
    async_playwright,
    Page,
    Browser,
    Playwright,
    BrowserContext,
)
from app.logging_config import get_logger
from app.schemas import Product
from app.services.captcha import RuCaptchaSolver, CaptchaSolverError
from app.settings import settings

logger = get_logger(__name__)


class OzonBlockedError(Exception):
    """Raised when Ozon blocks access and refresh doesn't help."""
    pass


class OzonParser:
    def __init__(self, captcha_solver: RuCaptchaSolver | None = None) -> None:
        self._browser: Browser | None = None
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._captcha_solver = captcha_solver

    async def __aenter__(self) -> "OzonParser":
        self._playwright = await async_playwright().start()

        user_data_dir = Path("browser_data")
        user_data_dir.mkdir(exist_ok=True)

        launch_options = dict(
            user_data_dir=str(user_data_dir),
            headless=settings.browser_headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
            timezone_id=settings.geo_timezone,
            geolocation={
                "latitude": settings.geo_latitude,
                "longitude": settings.geo_longitude,
            },
            permissions=["geolocation"],
        )

        if settings.proxy_url:
            from urllib.parse import urlparse
            parsed = urlparse(settings.proxy_url)
            proxy = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
            if parsed.username:
                proxy["username"] = parsed.username
            if parsed.password:
                proxy["password"] = parsed.password
            launch_options["proxy"] = proxy
            logger.info(f"Using proxy: {parsed.hostname}:{parsed.port}")

        self._context = await self._playwright.chromium.launch_persistent_context(
            **launch_options
        )

        # Log current region
        self._log_current_region()

        return self

    def _log_current_region(self) -> None:
        """Log the configured region for parsing."""
        logger.info(
            f"Parsing region: {settings.geo_city} "
            f"(lat={settings.geo_latitude}, lon={settings.geo_longitude}, tz={settings.geo_timezone})"
        )

    async def __aexit__(self, *_) -> None:
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()

    async def _new_page(self) -> Page:
        if not self._context:
            raise RuntimeError("Context not initialized. Use async with.")
        page = await self._context.new_page()
        page.set_default_timeout(settings.browser_timeout)
        return page

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

    async def _is_antibot_challenge(self, page: Page) -> bool:
        """Check if page is Ozon's antibot challenge (auto-solving JS check)."""
        try:
            title = await page.title()
            return "antibot challenge" in title.lower()
        except Exception:
            return False

    async def _is_blocked_page(self, page: Page) -> bool:
        """Check if we hit the 'Доступ ограничен' block page."""
        try:
            # Check for block page heading
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

        # Check if it's Ozon's antibot challenge (auto-solving)
        if await self._is_antibot_challenge(page):
            logger.info("Ozon Antibot Challenge detected - waiting for automatic resolution...")
            for i in range(30):  # Wait up to 30 seconds
                await page.wait_for_timeout(1000)
                if not await self._is_captcha_page(page):
                    logger.info(f"Antibot challenge passed automatically in {i + 1}s")
                    return
            logger.warning("Antibot challenge timeout - may need manual intervention")
            # Continue to manual fallback

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
        """
        Attempt to solve captcha automatically using RuCaptcha.

        Returns True if captcha was solved, False if captcha type not supported.
        """
        if not self._captcha_solver:
            return False

        page_url = page.url

        # Try to find Cloudflare Turnstile
        turnstile_element = await page.query_selector(
            "[data-sitekey].cf-turnstile, .cf-turnstile[data-sitekey], "
            "iframe[src*='challenges.cloudflare.com']"
        )
        if turnstile_element:
            # Get sitekey - either from element or from iframe parent
            site_key = await turnstile_element.get_attribute("data-sitekey")
            if not site_key:
                # Try to find it in parent elements
                parent = await page.query_selector(".cf-turnstile[data-sitekey]")
                if parent:
                    site_key = await parent.get_attribute("data-sitekey")

            if site_key:
                logger.info(f"Found Cloudflare Turnstile with sitekey: {site_key[:20]}...")

                token = await self._captcha_solver.solve_turnstile(
                    site_key=site_key,
                    page_url=page_url,
                )

                # Inject Turnstile token
                await page.evaluate("""
                    (token) => {
                        // Find turnstile response input
                        const input = document.querySelector('[name="cf-turnstile-response"]');
                        if (input) {
                            input.value = token;
                        }
                        // Try callback
                        if (typeof turnstile !== 'undefined' && turnstile.getResponse) {
                            // Trigger form submit or callback
                            const form = document.querySelector('form');
                            if (form) form.submit();
                        }
                    }
                """, token)

                logger.info("Turnstile token injected")
                await page.wait_for_timeout(3000)

                if not await self._is_captcha_page(page):
                    logger.info("Turnstile solved successfully!")
                    return True

                logger.warning("Turnstile token injection didn't work")

        # Try to find reCAPTCHA
        recaptcha_element = await page.query_selector("[data-sitekey]")
        if recaptcha_element:
            site_key = await recaptcha_element.get_attribute("data-sitekey")
            if site_key:
                logger.info(f"Found reCAPTCHA v2 with sitekey: {site_key[:20]}...")

                # Check if invisible
                size = await recaptcha_element.get_attribute("data-size")
                invisible = size == "invisible"

                # Solve reCAPTCHA
                token = await self._captcha_solver.solve_recaptcha_v2(
                    site_key=site_key,
                    page_url=page_url,
                    invisible=invisible,
                )

                # Inject the token
                await page.evaluate("""
                    (token) => {
                        const textarea = document.querySelector('#g-recaptcha-response');
                        if (textarea) {
                            textarea.value = token;
                            textarea.style.display = 'block';
                        }
                        // Also try to find hidden textarea in iframe
                        const hiddenTextarea = document.querySelector('[name="g-recaptcha-response"]');
                        if (hiddenTextarea) {
                            hiddenTextarea.value = token;
                        }
                        // Trigger callback if exists
                        if (typeof ___grecaptcha_cfg !== 'undefined') {
                            const clients = ___grecaptcha_cfg.clients;
                            if (clients) {
                                for (const key in clients) {
                                    const client = clients[key];
                                    if (client && client.callback) {
                                        client.callback(token);
                                    }
                                }
                            }
                        }
                    }
                """, token)

                logger.info("reCAPTCHA token injected, submitting...")

                # Try to click submit button
                submit_button = await page.query_selector(
                    "button[type='submit'], input[type='submit'], .captcha-submit, button:has-text('Подтвердить')"
                )
                if submit_button:
                    await submit_button.click()
                    await page.wait_for_timeout(3000)

                # Check if captcha is gone
                if not await self._is_captcha_page(page):
                    logger.info("reCAPTCHA solved successfully!")
                    return True

                logger.warning("reCAPTCHA token injection didn't work")
                return False

        # Try to find image captcha
        captcha_image = await page.query_selector(
            "img[src*='captcha'], img[alt*='captcha'], .captcha-image img"
        )
        if captcha_image:
            logger.info("Found image captcha")

            # Get image as base64
            src = await captcha_image.get_attribute("src")
            if src and src.startswith("data:image"):
                # Already base64
                image_base64 = src.split(",")[1]
            else:
                # Screenshot the element
                image_bytes = await captcha_image.screenshot()
                import base64
                image_base64 = base64.b64encode(image_bytes).decode()

            # Solve image captcha
            text = await self._captcha_solver.solve_image_captcha(image_base64)
            logger.info(f"Image captcha solved: {text}")

            # Find input field and enter text
            captcha_input = await page.query_selector(
                "input[name*='captcha'], input[id*='captcha'], input[placeholder*='код'], input[type='text']"
            )
            if captcha_input:
                await captcha_input.fill(text)

                # Submit
                submit_button = await page.query_selector(
                    "button[type='submit'], input[type='submit'], button:has-text('Подтвердить')"
                )
                if submit_button:
                    await submit_button.click()
                    await page.wait_for_timeout(3000)

                if not await self._is_captcha_page(page):
                    logger.info("Image captcha solved successfully!")
                    return True

            logger.warning("Image captcha solving didn't work")
            return False

        logger.warning("Unknown captcha type, cannot solve automatically")
        return False

    async def _handle_block_page(self, page: Page, max_retries: int = 3) -> bool:
        """
        Handle 'Доступ ограничен' block page by clicking 'Обновить'.
        Returns True if successfully bypassed, False otherwise.
        """
        for attempt in range(max_retries):
            if not await self._is_blocked_page(page):
                return True

            logger.warning(f"Block page detected, attempting refresh (attempt {attempt + 1}/{max_retries})")

            # Try clicking the "Обновить" button
            refresh_button = await page.query_selector("button:has-text('Обновить')")
            if refresh_button:
                await refresh_button.click()
                await page.wait_for_timeout(5000)
            else:
                # Fallback to page reload
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
        if not product_id.isdigit():
            return None

        return product_id

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

        Args:
            query: Search query
            target_article: Product article/ID to find
            max_position: Maximum position to search
            page: Optional existing page to reuse (for performance)
        """
        logger.info(f"{'='*60}")
        logger.info(f"Searching position for article {target_article}")
        logger.info(f"Query: {query}")
        logger.info(f"Region: {settings.geo_city}")
        logger.info(f"{'='*60}")

        # Reuse page if provided, otherwise create new one
        page_provided = page is not None
        if not page_provided:
            page = await self._new_page()

        seen_products: set[str] = set()
        position = 0
        scroll_count = 0

        try:
            search_url = f"{settings.base_url}/search/?text={query}&from_global=true"
            logger.debug(f"Opening URL: {search_url}")
            await page.goto(search_url, wait_until="domcontentloaded")

            # Wait for products to load (not just DOM ready)
            try:
                await page.wait_for_selector("a[href*='/product/']", timeout=15000)
                logger.debug("Products loaded on page")
            except Exception:
                logger.warning("Timeout waiting for products to appear")

            # Wait for network to settle instead of fixed timeout
            try:
                await page.wait_for_load_state("networkidle", timeout=settings.initial_load_networkidle_timeout)
            except Exception:
                # If networkidle times out, wait a bit anyway
                await page.wait_for_timeout(settings.initial_load_fallback_delay)

            if await self._is_captcha_page(page):
                await self._wait_for_captcha(page)
                await page.wait_for_timeout(2000)

            # Handle block page
            if await self._is_blocked_page(page):
                if not await self._handle_block_page(page):
                    logger.error("Failed to bypass block page - stopping parser")
                    raise OzonBlockedError(
                        "Ozon заблокировал доступ. Попробуйте: "
                        "1) Подождать 15-30 минут, "
                        "2) Сменить IP (VPN/прокси), "
                        "3) Удалить browser_data/"
                    )

            # Collect initial products
            new_products = await self._collect_products_from_page(page, seen_products)
            logger.info(f"Initial load: found {len(new_products)} products")

            # Diagnostic: if no products found initially, log page state
            if not new_products:
                logger.warning(f"No products found on initial load. URL: {page.url}")
                # Save screenshot for debugging
                screenshot_path = f"debug_screenshot_{target_article}.png"
                await page.screenshot(path=screenshot_path)
                logger.warning(f"Screenshot saved to {screenshot_path}")
                # Check if we got redirected or blocked
                all_links = await page.query_selector_all("a")
                logger.warning(f"Total links on page: {len(all_links)}")
                # Sample some hrefs for debugging
                sample_hrefs = []
                for link in all_links[:10]:
                    href = await link.get_attribute("href")
                    if href:
                        sample_hrefs.append(href)
                if sample_hrefs:
                    logger.warning(f"Sample hrefs: {sample_hrefs}")

            for product_id in new_products:
                position += 1
                if product_id == target_article:
                    logger.info(f"Found article {target_article} at position {position}")
                    return position

            # Scroll and load more products
            no_new_products_count = 0
            max_no_new_products = 10  # Stop after 10 scrolls with no new products

            while position < max_position:
                # Scroll down aggressively
                scroll_count += 1
                await page.evaluate("if(document.body) window.scrollTo(0, document.body.scrollHeight)")

                # Wait for network to settle after scroll instead of fixed timeout
                try:
                    await page.wait_for_load_state("networkidle", timeout=settings.scroll_networkidle_timeout)
                except Exception:
                    # If networkidle times out, wait minimally
                    await page.wait_for_timeout(settings.scroll_fallback_delay)

                # Check for captcha or block after scroll
                if await self._is_captcha_page(page):
                    await self._wait_for_captcha(page)
                    await page.wait_for_timeout(2000)

                if await self._is_blocked_page(page):
                    if not await self._handle_block_page(page):
                        logger.error("Failed to bypass block page during scroll")
                        break

                # Collect new products
                new_products = await self._collect_products_from_page(page, seen_products)

                if not new_products:
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

                if position % 50 == 0:
                    logger.info(f"Progress: checked {position} products (scroll #{scroll_count})...")

            logger.info(f"NOT FOUND: Article {target_article} not in top {position} positions")
            logger.info(f"Total scrolls: {scroll_count}")
            return None

        finally:
            # Only close page if we created it ourselves
            if not page_provided:
                await page.close()

    async def parse_product(self, url: str) -> Product:
        logger.info(f"Parsing product: {url}")
        page = await self._new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")

            # Wait for initial load
            await page.wait_for_timeout(3000)

            # Check for captcha and wait if needed
            if await self._is_captcha_page(page):
                await self._wait_for_captcha(page)
                await page.wait_for_timeout(2000)

            # After captcha, we might be on a different page due to redirect
            # Try to find product elements
            try:
                await page.wait_for_selector("h1", timeout=15000)
            except Exception:
                pass

            title_el = await page.query_selector("h1")
            title = await title_el.inner_text() if title_el else "Unknown"

            price: Decimal | None = None
            original_price: Decimal | None = None

            price_selectors = [
                "[data-widget='webPrice'] span",
                "[data-widget='webSale'] span",
                "span[class*='price']",
                "span[class*='Price']",
            ]
            for selector in price_selectors:
                price_el = await page.query_selector(selector)
                if price_el:
                    price_text = await price_el.inner_text()
                    price = self._parse_price(price_text)
                    if price:
                        break

            rating: float | None = None
            reviews_count: int | None = None
            rating_selectors = [
                "[data-widget='webSingleProductScore']",
                "[data-widget='webReviewProductScore']",
                "div[class*='rating']",
            ]
            for selector in rating_selectors:
                rating_el = await page.query_selector(selector)
                if rating_el:
                    rating_text = await rating_el.inner_text()
                    rating = self._parse_rating(rating_text)
                    reviews_count = self._parse_reviews_count(rating_text)
                    if rating:
                        break

            seller: str | None = None
            seller_selectors = [
                "[data-widget='webCurrentSeller'] a",
                "[data-widget='webSeller'] a",
                "a[href*='/seller/']",
            ]
            for selector in seller_selectors:
                seller_el = await page.query_selector(selector)
                if seller_el:
                    seller = await seller_el.inner_text()
                    if seller:
                        seller = seller.strip()
                        break

            out_of_stock_el = await page.query_selector("[data-widget='webOutOfStock']")
            in_stock = out_of_stock_el is None

            product = Product(
                url=page.url,
                title=title.strip(),
                price=price,
                original_price=original_price,
                rating=rating,
                reviews_count=reviews_count,
                seller=seller,
                in_stock=in_stock,
            )
            logger.info(f"Successfully parsed: {product.title[:50]}...")
            return product
        finally:
            await page.close()
