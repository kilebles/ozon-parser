"""
Ozon Parser using Playwright with Chromium persistent context.

Uses the same approach as original parser.py:
- Chromium with launch_persistent_context (saves full browser profile)
- user_data_dir for session persistence
- Anti-detection args
- Cookie loading from cookies.json as fallback
"""
import asyncio
import json
import random
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import (
    async_playwright,
    Page,
    Playwright,
    BrowserContext,
    Browser,
)

from app.logging_config import get_logger
from app.settings import settings

logger = get_logger(__name__)


def _get_user_data_dir() -> Path:
    """Get the directory for browser profile (separate from original parser)."""
    import os
    if os.name == 'nt':  # Windows
        base_dir = Path(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')))
    else:  # Linux/Mac
        base_dir = Path(os.path.expanduser('~/.local/share'))

    user_data_dir = base_dir / 'ozon-parser' / 'chromium-profile'
    user_data_dir.mkdir(parents=True, exist_ok=True)
    return user_data_dir


def _get_cookies_file_path() -> Path:
    """Get path to cookies.json in project root."""
    return Path(__file__).parent.parent.parent / "cookies.json"


def _load_cookies_from_json() -> List[Dict[str, Any]]:
    """
    Load cookies from cookies.json (Cookie-Editor export format).
    Converts to Playwright cookie format.
    """
    cookies_path = _get_cookies_file_path()
    if not cookies_path.exists():
        logger.debug("cookies.json not found")
        return []

    try:
        with open(cookies_path, 'r', encoding='utf-8') as f:
            raw_cookies = json.load(f)

        # Convert Cookie-Editor format to Playwright format
        playwright_cookies = []
        for cookie in raw_cookies:
            pw_cookie = {
                "name": cookie.get("name", ""),
                "value": cookie.get("value", ""),
                "domain": cookie.get("domain", ".ozon.ru"),
                "path": cookie.get("path", "/"),
            }

            # Handle expiration
            if "expirationDate" in cookie and cookie["expirationDate"]:
                pw_cookie["expires"] = cookie["expirationDate"]

            # Handle other flags
            if cookie.get("secure"):
                pw_cookie["secure"] = True
            if cookie.get("httpOnly"):
                pw_cookie["httpOnly"] = True

            # Handle sameSite
            same_site = cookie.get("sameSite", "").lower()
            if same_site in ("strict", "lax", "none"):
                pw_cookie["sameSite"] = same_site.capitalize()
                if same_site == "none":
                    pw_cookie["sameSite"] = "None"

            if pw_cookie["name"] and pw_cookie["value"]:
                playwright_cookies.append(pw_cookie)

        logger.info(f"Loaded {len(playwright_cookies)} cookies from cookies.json")
        return playwright_cookies
    except Exception as e:
        logger.error(f"Failed to load cookies: {e}")
        return []


class OzonBlockedError(Exception):
    """Raised when Ozon blocks access and refresh doesn't help."""
    pass


class OzonPageLoadError(Exception):
    """Raised when page fails to load (timeout, network error)."""
    pass


class OzonParserPlaywright:
    """
    Ozon parser using Playwright with Chromium persistent context.

    Same approach as original parser.py:
    - launch_persistent_context with user_data_dir
    - Full browser profile persistence (cookies, localStorage, etc.)
    - Anti-detection chromium args
    """

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._restart_lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._restart_lock is None:
            self._restart_lock = asyncio.Lock()
        return self._restart_lock

    def _build_launch_options(self) -> dict:
        """Build launch options similar to original parser.py."""
        user_data_dir = _get_user_data_dir()

        # Slightly randomize viewport
        viewport_width = random.choice([1920, 1903, 1912, 1920])
        viewport_height = random.choice([1080, 969, 1040, 1080])

        options = dict(
            user_data_dir=str(user_data_dir),
            headless=settings.browser_headless,
            viewport={"width": viewport_width, "height": viewport_height},
            locale="ru-RU",
            color_scheme="light",
            timezone_id="Europe/Moscow",
            geolocation={"latitude": 55.7558, "longitude": 37.6173},
            permissions=["geolocation"],
        )

        # Anti-detection args (from original parser.py)
        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-site-isolation-trials",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-setuid-sandbox",
            "--disable-infobars",
            "--disable-background-networking",
            "--disable-breakpad",
            "--disable-component-update",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-service-autorun",
            "--password-store=basic",
            "--use-mock-keychain",
            "--use-gl=swiftshader",
            "--enable-webgl",
            "--enable-webgl2",
            "--ignore-gpu-blocklist",
            "--enable-gpu-rasterization",
            "--window-size=1920,1080",
            "--start-maximized",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-hang-monitor",
            "--disable-ipc-flooding-protection",
            "--disable-popup-blocking",
            "--disable-prompt-on-repost",
            "--font-render-hinting=none",
            "--disable-font-subpixel-positioning",
        ]
        options["args"] = args

        return options

    async def _create_context(self) -> BrowserContext:
        """Create browser context using launch_persistent_context."""
        options = self._build_launch_options()
        logger.debug(f"Creating chromium persistent context...")

        context = await self._playwright.chromium.launch_persistent_context(**options)
        logger.debug("Chromium persistent context created")

        # Load cookies from cookies.json if profile is fresh
        cookies = _load_cookies_from_json()
        if cookies:
            try:
                await context.add_cookies(cookies)
                logger.debug(f"Added {len(cookies)} cookies to context")
            except Exception as e:
                logger.warning(f"Failed to add some cookies: {e}")

        return context

    async def __aenter__(self) -> "OzonParserPlaywright":
        self._playwright = await async_playwright().start()
        self._context = await self._create_context()
        logger.info(f"Playwright chromium started (headless={settings.browser_headless})")
        return self

    async def __aexit__(self, *_) -> None:
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass

    async def restart_browser(self) -> None:
        """Close and relaunch browser."""
        lock = self._get_lock()

        async with lock:
            logger.info("Restarting browser...")

            if self._context:
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None

            if not self._playwright:
                self._playwright = await async_playwright().start()

            self._context = await self._create_context()
            logger.info("Browser restarted")

    async def _new_page(self) -> Page:
        """Create a new page with resource blocking."""
        if not self._context:
            raise RuntimeError("Context not initialized. Use async with.")

        page = await self._context.new_page()
        page.set_default_timeout(settings.browser_timeout)

        # Block heavy resources (from original parser.py)
        blocked_types = {"image", "font", "media"}
        blocked_urls = ["mc.yandex", "google-analytics", "facebook", "vk.com/rtrg", "top-fwz", "criteo"]

        async def handle_route(route):
            request = route.request
            url = request.url
            if request.resource_type in blocked_types:
                await route.abort()
                return
            if any(blocked in url for blocked in blocked_urls):
                await route.abort()
                return
            await route.continue_()

        await page.route("**/*", handle_route)

        return page

    async def _warmup(self, page: Page) -> None:
        """Visit homepage before searching (from original parser.py)."""
        try:
            await page.goto(settings.base_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(random.randint(500, 1200))
            await page.mouse.move(
                random.randint(300, 900),
                random.randint(200, 500)
            )
            await page.wait_for_timeout(random.randint(200, 500))
            # Save cookies after warmup
            await self._save_storage_state()
        except Exception:
            await page.wait_for_timeout(500)

    # ============ Detection methods from original parser.py ============

    async def _is_captcha_page(self, page: Page) -> bool:
        try:
            title = await page.title()
            captcha_keywords = ["бот", "robot", "bot", "captcha", "подтверд", "confirm", "antibot", "challenge"]
            return any(kw in title.lower() for kw in captcha_keywords)
        except Exception:
            return False

    async def _is_blocked_page(self, page: Page) -> bool:
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
        logger.warning("Captcha detected! Waiting 60s for manual solve...")
        for _ in range(60):
            if not await self._is_captcha_page(page):
                logger.info("Captcha solved!")
                return
            await page.wait_for_timeout(1000)
        logger.warning("Captcha timeout")

    async def _handle_block_page(self, page: Page, max_retries: int = 3) -> bool:
        logger.info("Block page detected, waiting for JS challenge...")

        for i in range(5):
            await page.wait_for_timeout(2000)
            if not await self._is_blocked_page(page):
                logger.info(f"JS challenge resolved after {(i + 1) * 2}s")
                return True

        for attempt in range(max_retries):
            if not await self._is_blocked_page(page):
                return True
            logger.warning(f"Still blocked, refreshing ({attempt + 1}/{max_retries})")
            refresh_button = await page.query_selector("button:has-text('Обновить')")
            if refresh_button:
                await refresh_button.click()
            else:
                await page.reload()
            await page.wait_for_timeout(5000)

        return not await self._is_blocked_page(page)

    # ============ Product collection from original parser.py ============

    async def _collect_products_from_page(self, page: Page, seen_products: set[str]) -> list[str]:
        seen_list = list(seen_products) if len(seen_products) < 2000 else []

        all_ids = await page.evaluate("""
            (seen) => {
                const seenSet = new Set(seen);
                const ids = [];
                const links = document.getElementsByTagName('a');
                for (let i = 0; i < links.length; i++) {
                    const href = links[i].href;
                    if (!href || !href.includes('/product/')) continue;
                    if (href.includes('/reviews') || href.includes('/questions')) continue;
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

        for product_id in all_ids:
            seen_products.add(product_id)
        return all_ids

    async def find_product_position(
        self, query: str, target_article: str, max_position: int = 1000, page: Page | None = None
    ) -> int | None:
        """Search for a product position using infinite scroll."""
        logger.info(f"Search: {query} -> {target_article}")

        page_provided = page is not None
        if not page_provided:
            page = await self._new_page()

        seen_products: set[str] = set()
        position = 0
        scroll_count = 0

        try:
            search_url = f"{settings.base_url}/search/?text={query}"
            try:
                await page.goto(search_url, wait_until="domcontentloaded")
            except Exception as e:
                if "Timeout" in str(e) or "ERR_TIMED_OUT" in str(e):
                    logger.warning("Page load timeout, waiting for products...")
                    try:
                        await page.wait_for_selector("a[href*='/product/']", timeout=30000)
                    except Exception:
                        raise OzonPageLoadError(f"Page load timeout: {query}")
                else:
                    raise OzonPageLoadError(f"Page load error: {e}")

            # Wait for JS challenge
            body_length = await page.evaluate("document.body.innerHTML.length")
            if body_length < 5000:
                logger.info(f"Small page ({body_length} chars), waiting for JS...")
                for i in range(30):
                    await page.wait_for_timeout(500)
                    body_length = await page.evaluate("document.body.innerHTML.length")
                    if body_length > 10000:
                        logger.info(f"JS resolved after {(i+1)*0.5}s")
                        break

            # Human-like delay
            await page.wait_for_timeout(random.randint(300, 800))
            await page.mouse.move(random.randint(400, 800), random.randint(200, 400))

            # Check block/captcha
            if await self._is_blocked_page(page):
                logger.warning("Block page detected")
                if not await self._handle_block_page(page):
                    raise OzonBlockedError("block_restart")

            if await self._is_captcha_page(page):
                await self._wait_for_captcha(page)

            # Wait for products
            try:
                await page.wait_for_selector("a[href*='/product/']", timeout=15000)
            except Exception:
                raise OzonPageLoadError(f"No products found: {query}")

            # Collect initial products
            new_products = await self._collect_products_from_page(page, seen_products)

            for product_id in new_products:
                position += 1
                if product_id == target_article:
                    logger.info(f"Found {target_article} at position {position}")
                    return position

            # Infinite scroll
            empty_scrolls = 0
            max_empty_scrolls = 5

            while position < max_position:
                scroll_count += 1

                # Human-like scroll
                await page.mouse.move(random.randint(300, 900), random.randint(200, 500))
                await page.wait_for_timeout(random.randint(100, 300))

                scroll_amount = random.randint(600, 1000)
                await page.mouse.wheel(0, scroll_amount)
                await page.wait_for_timeout(random.randint(400, 800))

                if random.random() < 0.3:
                    await page.mouse.wheel(0, random.randint(100, 300))
                    await page.wait_for_timeout(random.randint(200, 400))

                if random.random() < 0.1:
                    await page.wait_for_timeout(random.randint(1000, 2000))

                # Check captcha/block
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
                    if empty_scrolls >= max_empty_scrolls:
                        return None
                    continue

                empty_scrolls = 0
                logger.debug(f"Scroll {scroll_count}: +{len(new_products)} (total: {position + len(new_products)})")

                for product_id in new_products:
                    position += 1
                    if product_id == target_article:
                        logger.info(f"Found {target_article} at position {position}")
                        return position
                    if position >= max_position:
                        return None

            return None

        finally:
            if not page_provided:
                await page.close()
