"""
Ozon Parser using Selenium with enhanced anti-bot features.

Based on recaptcha_solver_selenium.py anti-detection techniques:
- Chrome/Edge auto-detection with driver auto-download
- Comprehensive stealth settings
- Profile management
- Headless mode optimized for server
- CPU/memory optimizations
"""
import asyncio
import os
import random
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.logging_config import get_logger
from app.settings import settings

logger = get_logger(__name__)

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.edge.options import Options as EdgeOptions
    from selenium.webdriver.edge.service import Service as EdgeService
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.action_chains import ActionChains
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False
    webdriver = None
    ChromeOptions = None
    EdgeOptions = None

try:
    from webdriver_manager.chrome import ChromeDriverManager
    from webdriver_manager.microsoft import EdgeChromiumDriverManager
    WEBDRIVER_MANAGER_AVAILABLE = True
except ImportError:
    WEBDRIVER_MANAGER_AVAILABLE = False
    ChromeDriverManager = None
    EdgeChromiumDriverManager = None


class OzonBlockedError(Exception):
    """Raised when Ozon blocks access and refresh doesn't help."""
    pass


class OzonPageLoadError(Exception):
    """Raised when page fails to load (timeout, network error)."""
    pass


# Screen resolutions for fingerprint randomization
SCREEN_RESOLUTIONS = [
    (1920, 1080), (2560, 1440), (1366, 768),
    (1536, 864), (1440, 900), (1280, 720)
]


def _get_profiles_dir() -> Path:
    """Get the directory for browser profiles."""
    if os.name == 'nt':  # Windows
        base_dir = Path(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')))
    else:  # Linux/Mac
        base_dir = Path(os.path.expanduser('~/.local/share'))

    profiles_dir = base_dir / 'ozon-parser' / 'selenium-profiles'
    profiles_dir.mkdir(parents=True, exist_ok=True)
    return profiles_dir


def _cleanup_profile_locks(profile_path: Path) -> None:
    """Clean up Chrome profile lock files."""
    lock_files = ["SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile"]

    for lock_file in lock_files:
        lock_path = profile_path / lock_file
        if lock_path.exists():
            try:
                lock_path.unlink()
                logger.debug(f"Removed lock: {lock_file}")
            except Exception:
                pass

    default_dir = profile_path / "Default"
    if default_dir.exists():
        for lock_file in lock_files:
            lock_path = default_dir / lock_file
            if lock_path.exists():
                try:
                    lock_path.unlink()
                except Exception:
                    pass


def _kill_zombie_chrome_processes() -> int:
    """Kill zombie Chrome processes that may be holding locks."""
    killed = 0

    if os.name == 'nt':  # Windows
        try:
            result = subprocess.run(
                ['taskkill', '/F', '/IM', 'chromedriver.exe'],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                killed += 1
                logger.info("Killed chromedriver.exe")
        except Exception:
            pass

        try:
            import psutil
            our_markers = ["ozon-parser", "selenium-profiles"]

            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    proc_name = (proc.info.get('name') or '').lower()
                    if 'chrome' in proc_name:
                        cmdline = proc.info.get('cmdline') or []
                        cmdline_str = ' '.join(cmdline) if cmdline else ''

                        for marker in our_markers:
                            if marker in cmdline_str:
                                proc.kill()
                                killed += 1
                                logger.info(f"Killed Chrome (PID: {proc.info['pid']})")
                                break
                except Exception:
                    pass
        except ImportError:
            pass
        except Exception:
            pass

    else:  # Linux/Mac
        try:
            subprocess.run(['pkill', '-f', 'chromedriver'], capture_output=True, timeout=10)
            killed += 1
        except Exception:
            pass

    return killed


def _get_random_fingerprint() -> Dict[str, Any]:
    """Generate random browser fingerprint."""
    res = random.choice(SCREEN_RESOLUTIONS)
    chrome_version = random.randint(120, 131)
    return {
        "width": res[0],
        "height": res[1],
        "user_agent": (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_version}.0.0.0 Safari/537.36"
        )
    }


def _detect_available_browser() -> Tuple[str, Optional[str]]:
    """Detect which browser is available on the system."""
    if sys.platform == "darwin":  # macOS
        chrome_paths = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]

        for path in chrome_paths:
            if os.path.exists(path):
                logger.info(f"Found Chrome at: {path}")
                return ("chrome", path)

        logger.info("No browser found at common paths, trying default Chrome")
        return ("chrome", None)

    elif sys.platform == "win32":  # Windows
        chrome_paths = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]

        edge_paths = [
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        ]

        for path in chrome_paths:
            if os.path.exists(path):
                logger.info(f"Found Chrome at: {path}")
                return ("chrome", path)

        for path in edge_paths:
            if os.path.exists(path):
                logger.info(f"Found Edge at: {path}")
                return ("edge", path)

        logger.info("No browser found at common paths, trying default Chrome")
        return ("chrome", None)

    else:  # Linux
        chrome_paths = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]

        for path in chrome_paths:
            if os.path.exists(path):
                logger.info(f"Found Chrome at: {path}")
                return ("chrome", path)

        logger.info("No browser found at common paths, trying default Chrome")
        return ("chrome", None)


def _get_driver_service(browser_type: str) -> Any:
    """Get the appropriate driver service with auto-download."""
    logger.debug(f"Getting driver for: {browser_type}")

    if WEBDRIVER_MANAGER_AVAILABLE:
        try:
            if browser_type == "edge":
                logger.debug("Downloading Edge driver...")
                driver_path = EdgeChromiumDriverManager().install()
                return EdgeService(driver_path)
            else:
                logger.debug("Downloading Chrome driver...")
                driver_path = ChromeDriverManager().install()
                return ChromeService(driver_path)
        except Exception as e:
            logger.warning(f"Driver download failed: {e}")

    # Fallback to system driver
    logger.debug("Using system driver (fallback)")
    if browser_type == "edge":
        return EdgeService()
    return ChromeService()


class SeleniumPage:
    """
    Wrapper to make Selenium driver compatible with Playwright Page interface.
    Used by PositionTracker which expects a Page-like object.
    """

    def __init__(self, driver: Any):
        self.driver = driver

    async def close(self) -> None:
        """No-op for Selenium - we reuse the same driver."""
        pass

    async def wait_for_timeout(self, ms: int) -> None:
        """Wait for specified milliseconds."""
        await asyncio.sleep(ms / 1000)


class OzonParserSelenium:
    """
    Ozon parser using Selenium with enhanced anti-detection.

    Features:
    - Chrome/Edge support with auto-detection
    - Comprehensive stealth scripts
    - Profile management
    - Headless mode optimized for server
    - CPU/memory optimizations
    """

    def __init__(self) -> None:
        """Initialize parser."""
        if not SELENIUM_AVAILABLE:
            raise RuntimeError(
                "Selenium is not installed. "
                "Install it with: uv add selenium webdriver-manager"
            )

        self._driver: Any = None
        self._restart_lock: asyncio.Lock | None = None
        self._profiles_dir = _get_profiles_dir()
        self._profile_path: Path | None = None

    def _get_lock(self) -> asyncio.Lock:
        """Get or create restart lock."""
        if self._restart_lock is None:
            self._restart_lock = asyncio.Lock()
        return self._restart_lock

    def _create_driver(self, fingerprint: Dict[str, Any], profile_path: Optional[Path] = None) -> Any:
        """Create Chrome/Edge WebDriver with stealth settings."""
        browser_type, browser_path = _detect_available_browser()

        if browser_type == "edge":
            return self._create_edge_driver(fingerprint, profile_path, browser_path)
        else:
            return self._create_chrome_driver(fingerprint, profile_path, browser_path)

    def _create_chrome_driver(
        self,
        fingerprint: Dict[str, Any],
        profile_path: Optional[Path] = None,
        browser_path: Optional[str] = None
    ) -> Any:
        """Create Chrome WebDriver with stealth settings."""
        options = ChromeOptions()

        if browser_path and os.path.exists(browser_path):
            options.binary_location = browser_path

        if profile_path:
            profile_path.mkdir(parents=True, exist_ok=True)
            _cleanup_profile_locks(profile_path)
            options.add_argument(f"--user-data-dir={profile_path}")

        # Headless mode
        if settings.browser_headless:
            if settings.browser_headless_new:
                options.add_argument("--headless=new")
            else:
                options.add_argument("--headless")

        # Stealth settings
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-infobars")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-popup-blocking")
        options.add_argument(f"--user-agent={fingerprint['user_agent']}")
        options.add_argument(f"--window-size={fingerprint['width']},{fingerprint['height']}")

        # Performance settings
        prefs = {
            "profile.managed_default_content_settings.images": 2,  # Block images
            "profile.default_content_setting_values.notifications": 2,
        }
        options.add_experimental_option("prefs", prefs)

        # CPU/memory optimization
        options.add_argument("--renderer-process-limit=1")
        options.add_argument("--disable-features=IsolateOrigins,site-per-process")
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_argument("--disable-renderer-backgrounding")
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--disable-background-networking")
        options.add_argument("--enable-low-end-device-mode")
        options.add_argument("--mute-audio")
        options.add_argument("--log-level=3")

        # Crash prevention
        options.add_argument("--disable-crash-reporter")
        options.add_argument("--disable-breakpad")

        # Exclude automation flags
        options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        options.add_experimental_option("useAutomationExtension", False)

        # Get driver service
        service = _get_driver_service("chrome")

        try:
            driver = webdriver.Chrome(service=service, options=options)
            logger.info(f"Chrome launched (headless={settings.browser_headless})")
        except Exception as e:
            error_msg = str(e)
            logger.warning(f"Chrome launch failed: {error_msg[:100]}")

            if "crashed" in error_msg.lower() or "session not created" in error_msg.lower():
                logger.info("Killing zombie Chrome processes...")
                _kill_zombie_chrome_processes()

                if profile_path:
                    _cleanup_profile_locks(profile_path)

                time.sleep(2)

                # Retry without profile
                options = ChromeOptions()
                if browser_path and os.path.exists(browser_path):
                    options.binary_location = browser_path

                if settings.browser_headless:
                    options.add_argument("--headless=new")
                options.add_argument("--disable-blink-features=AutomationControlled")
                options.add_argument("--no-sandbox")
                options.add_argument("--disable-gpu")
                options.add_argument("--mute-audio")
                options.add_argument(f"--user-agent={fingerprint['user_agent']}")
                options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
                options.add_experimental_option("useAutomationExtension", False)

                driver = webdriver.Chrome(service=service, options=options)
                logger.info("Chrome launched (headless, no profile)")
            else:
                raise e

        # Execute stealth script
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru', 'en-US', 'en']});
                window.chrome = {runtime: {}};
                Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
                Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
                Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});

                // WebGL spoofing
                const getParameterProxyHandler = {
                    apply: function(target, thisArg, args) {
                        const param = args[0];
                        if (param === 37445) return 'Google Inc. (NVIDIA)';
                        if (param === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1080 Direct3D11 vs_5_0 ps_5_0, D3D11)';
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

                // Timezone
                Date.prototype.getTimezoneOffset = function() { return -180; };

                // Hide automation
                delete window.__selenium_unwrapped;
                delete window.__driver_evaluate;
                delete document.$cdc_asdjflasutopfhvcZLmcfl_;
            """
        })

        return driver

    def _create_edge_driver(
        self,
        fingerprint: Dict[str, Any],
        profile_path: Optional[Path] = None,
        browser_path: Optional[str] = None
    ) -> Any:
        """Create Edge WebDriver with stealth settings."""
        options = EdgeOptions()

        if browser_path and os.path.exists(browser_path):
            options.binary_location = browser_path

        if profile_path:
            profile_path.mkdir(parents=True, exist_ok=True)
            _cleanup_profile_locks(profile_path)
            options.add_argument(f"--user-data-dir={profile_path}")

        if settings.browser_headless:
            options.add_argument("--headless=new")

        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument(f"--user-agent={fingerprint['user_agent']}")
        options.add_argument("--mute-audio")
        options.add_argument("--log-level=3")

        options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        options.add_experimental_option("useAutomationExtension", False)

        service = _get_driver_service("edge")

        try:
            driver = webdriver.Edge(service=service, options=options)
            logger.info("Edge launched (headless)")
        except Exception as e:
            error_msg = str(e)
            logger.warning(f"Edge launch failed: {error_msg[:100]}")

            if "crashed" in error_msg.lower() or "session not created" in error_msg.lower():
                _kill_zombie_chrome_processes()
                if profile_path:
                    _cleanup_profile_locks(profile_path)
                time.sleep(2)

                options = EdgeOptions()
                if browser_path and os.path.exists(browser_path):
                    options.binary_location = browser_path
                options.add_argument("--headless=new")
                options.add_argument("--disable-blink-features=AutomationControlled")
                options.add_argument("--no-sandbox")
                options.add_argument("--mute-audio")
                options.add_argument(f"--user-agent={fingerprint['user_agent']}")
                options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])

                driver = webdriver.Edge(service=service, options=options)
                logger.info("Edge launched (headless, no profile)")
            else:
                raise e

        # Stealth script
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                window.chrome = {runtime: {}};
                Date.prototype.getTimezoneOffset = function() { return -180; };
            """
        })

        return driver

    async def __aenter__(self) -> "OzonParserSelenium":
        fingerprint = _get_random_fingerprint()
        self._profile_path = self._profiles_dir / f"profile_{random.randint(1000, 9999)}"

        loop = asyncio.get_event_loop()
        self._driver = await loop.run_in_executor(
            None, self._create_driver, fingerprint, self._profile_path
        )

        self._driver.set_page_load_timeout(settings.browser_timeout // 1000)
        self._driver.set_script_timeout(60)

        return self

    async def _new_page(self) -> SeleniumPage:
        """
        Return a Page-like wrapper for compatibility with PositionTracker.
        Selenium reuses the same driver, so this just wraps it.
        """
        return SeleniumPage(self._driver)

    async def _warmup(self, page: SeleniumPage) -> None:
        """Visit homepage before searching to look like a real user."""
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: self._driver.get(settings.base_url))
            await asyncio.sleep(random.uniform(0.5, 1.2))

            # Random mouse movement simulation via JS
            await loop.run_in_executor(None, lambda: self._driver.execute_script("""
                var event = new MouseEvent('mousemove', {
                    clientX: Math.random() * 800 + 100,
                    clientY: Math.random() * 400 + 100
                });
                document.dispatchEvent(event);
            """))
            await asyncio.sleep(random.uniform(0.2, 0.5))
        except Exception:
            await asyncio.sleep(0.5)

    async def __aexit__(self, *_) -> None:
        if self._driver:
            try:
                self._driver.quit()
            except Exception as e:
                logger.debug(f"Driver quit error: {e}")

        if self._profile_path and self._profile_path.exists():
            try:
                shutil.rmtree(self._profile_path)
            except Exception as e:
                logger.debug(f"Profile cleanup error: {e}")

    async def restart_browser(self) -> None:
        """Close browser, wipe profiles, and relaunch."""
        lock = self._get_lock()

        async with lock:
            logger.info("Restarting browser with clean profile...")

            if self._driver:
                try:
                    self._driver.quit()
                except Exception:
                    pass
                self._driver = None

            # Kill zombie processes
            _kill_zombie_chrome_processes()

            # Clean profile
            if self._profiles_dir.exists():
                try:
                    shutil.rmtree(self._profiles_dir)
                    logger.info("Deleted browser profiles")
                except Exception as e:
                    logger.warning(f"Failed to delete profiles: {e}")

            self._profiles_dir.mkdir(parents=True, exist_ok=True)

            # Relaunch
            fingerprint = _get_random_fingerprint()
            self._profile_path = self._profiles_dir / f"profile_{random.randint(1000, 9999)}"

            loop = asyncio.get_event_loop()
            self._driver = await loop.run_in_executor(
                None, self._create_driver, fingerprint, self._profile_path
            )

            self._driver.set_page_load_timeout(settings.browser_timeout // 1000)
            self._driver.set_script_timeout(60)

            logger.info("Browser restarted with clean profile")

    async def _is_captcha_page(self) -> bool:
        """Check if current page is a captcha challenge."""
        try:
            loop = asyncio.get_event_loop()
            title = await loop.run_in_executor(None, lambda: self._driver.title)
            captcha_keywords = ["бот", "robot", "bot", "captcha", "подтверд", "confirm", "antibot", "challenge"]
            return any(kw in title.lower() for kw in captcha_keywords)
        except Exception:
            return False

    async def _is_blocked_page(self) -> bool:
        """Check if we hit the 'Доступ ограничен' block page."""
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, lambda: self._driver.execute_script("""
                const h1 = document.querySelector('h1');
                return h1 && h1.innerText.toLowerCase().includes('доступ ограничен');
            """))
            return bool(result)
        except Exception:
            return False

    async def _check_page_status(self) -> tuple[bool, bool]:
        """Check for captcha and block in a single JS call."""
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, lambda: self._driver.execute_script("""
                const title = document.title.toLowerCase();
                const captchaKeywords = ['бот', 'robot', 'bot', 'captcha', 'подтверд', 'confirm', 'antibot', 'challenge'];
                const isCaptcha = captchaKeywords.some(kw => title.includes(kw));
                const h1 = document.querySelector('h1');
                const isBlocked = h1 && h1.innerText.toLowerCase().includes('доступ ограничен');
                return { isCaptcha: isCaptcha, isBlocked: isBlocked };
            """))
            return result.get("isCaptcha", False), result.get("isBlocked", False)
        except Exception:
            return False, False

    async def _wait_for_captcha(self) -> None:
        """Wait for manual captcha solving."""
        logger.warning("Captcha/challenge detected!")
        logger.warning("Please solve captcha manually in the browser window...")
        logger.info("You have 60 seconds to solve the captcha")
        for _ in range(60):
            if not await self._is_captcha_page():
                logger.info("Captcha solved!")
                return
            await asyncio.sleep(1)
        logger.warning("Captcha timeout - proceeding anyway...")

    async def _handle_block_page(self, max_retries: int = 3) -> bool:
        """Handle 'Доступ ограничен' block page."""
        logger.info("Block page detected, waiting for JS challenge to resolve...")

        for i in range(5):
            await asyncio.sleep(2)
            if not await self._is_blocked_page():
                logger.info(f"JS challenge resolved after {(i + 1) * 2}s")
                return True

        for attempt in range(max_retries):
            if not await self._is_blocked_page():
                return True
            logger.warning(f"Still blocked, refreshing (attempt {attempt + 1}/{max_retries})")

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._driver.refresh)
            await asyncio.sleep(5)

        return not await self._is_blocked_page()

    async def _collect_products_from_page(self, seen_products: set[str]) -> list[str]:
        """Collect new product IDs from current page state."""
        seen_list = list(seen_products) if len(seen_products) < 2000 else []

        loop = asyncio.get_event_loop()
        all_ids = await loop.run_in_executor(None, lambda: self._driver.execute_script("""
            const seen = arguments[0];
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
        """, seen_list))

        for product_id in all_ids:
            seen_products.add(product_id)
        return all_ids

    async def _scroll_page(self) -> None:
        """Perform human-like scroll."""
        loop = asyncio.get_event_loop()

        # Random mouse movement
        await loop.run_in_executor(None, lambda: self._driver.execute_script("""
            window.scrollBy({
                top: arguments[0],
                behavior: 'smooth'
            });
        """, random.randint(600, 1000)))

        await asyncio.sleep(random.uniform(0.4, 0.8))

        # Sometimes extra scroll
        if random.random() < 0.3:
            await loop.run_in_executor(None, lambda: self._driver.execute_script("""
                window.scrollBy({
                    top: arguments[0],
                    behavior: 'smooth'
                });
            """, random.randint(100, 300)))
            await asyncio.sleep(random.uniform(0.2, 0.4))

    async def find_product_position(
        self, query: str, target_article: str, max_position: int = 1000, page: Any = None
    ) -> int | None:
        """
        Search for a product position in Ozon search results using infinite scroll.
        Returns the position (1-based) or None if not found within max_position.

        Args:
            query: Search query
            target_article: Product ID to find
            max_position: Maximum position to search
            page: Ignored for Selenium (kept for API compatibility with Playwright)
        """
        # Note: page parameter is ignored - Selenium uses self._driver
        logger.info(f"Search: {query} -> {target_article}")

        seen_products: set[str] = set()
        position = 0
        scroll_count = 0

        try:
            search_url = f"{settings.base_url}/search/?text={query}"
            loop = asyncio.get_event_loop()

            try:
                await loop.run_in_executor(None, lambda: self._driver.get(search_url))
            except Exception as e:
                if "Timeout" in str(e):
                    logger.warning("Page load timeout, waiting for products...")
                    try:
                        await loop.run_in_executor(None, lambda: WebDriverWait(self._driver, 30).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/product/']"))
                        ))
                        logger.info("Products loaded after extended wait")
                    except Exception:
                        raise OzonPageLoadError(f"Page load timeout for query: {query}")
                else:
                    raise OzonPageLoadError(f"Page load error: {e}")

            # Wait for JS challenge
            body_length = await loop.run_in_executor(None, lambda: self._driver.execute_script(
                "return document.body.innerHTML.length"
            ))
            if body_length < 5000:
                logger.info(f"Small page ({body_length} chars), waiting for JS...")
                for i in range(30):
                    await asyncio.sleep(0.5)
                    body_length = await loop.run_in_executor(None, lambda: self._driver.execute_script(
                        "return document.body.innerHTML.length"
                    ))
                    if body_length > 10000:
                        logger.info(f"JS challenge resolved after {(i+1)*0.5}s")
                        break

            # Human-like delay
            await asyncio.sleep(random.uniform(0.3, 0.8))

            # Check for block/captcha
            if await self._is_blocked_page():
                logger.warning("Block page detected after load")
                if not await self._handle_block_page():
                    raise OzonBlockedError("block_restart")

            if await self._is_captcha_page():
                await self._wait_for_captcha()

            # Wait for products
            try:
                await loop.run_in_executor(None, lambda: WebDriverWait(self._driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/product/']"))
                ))
            except Exception:
                raise OzonPageLoadError(f"No products found for query: {query}")

            # Collect initial products
            new_products = await self._collect_products_from_page(seen_products)

            for product_id in new_products:
                position += 1
                if product_id == target_article:
                    logger.info(f"Found {target_article} at position {position}")
                    return position

            # Infinite scroll loop
            empty_scrolls = 0
            max_empty_scrolls = 5

            while position < max_position:
                scroll_count += 1

                await self._scroll_page()

                # Random pause
                if random.random() < 0.1:
                    await asyncio.sleep(random.uniform(1.0, 2.0))

                # Check for captcha/block
                is_captcha, is_blocked = await self._check_page_status()
                if is_captcha:
                    await self._wait_for_captcha()
                    continue
                if is_blocked:
                    if not await self._handle_block_page():
                        return -1
                    continue

                new_products = await self._collect_products_from_page(seen_products)

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

        except OzonBlockedError:
            raise
        except OzonPageLoadError:
            raise
        except Exception as e:
            logger.exception(f"Search error: {e}")
            raise OzonPageLoadError(f"Search error: {e}")
