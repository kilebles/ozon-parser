"""reCAPTCHA Enterprise Solver using Playwright.

This module provides reCAPTCHA token generation for API calls
using browser automation with Playwright (WebKit/Chromium/Firefox).

Features:
- Per-account browser isolation
- Profile rotation after max uses or 403 errors
- Headless mode (completely hidden)
- Multiple browser engine support (WebKit, Chromium, Firefox)
- Async-native implementation
- Proxy support
"""

import asyncio
import hashlib
import json
import os
import random
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .proxy_manager import Proxy

try:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    async_playwright = None
    Browser = None
    BrowserContext = None
    Page = None


# === GLOBAL LOG CALLBACK FOR UI ===
_ui_log_callback: Optional[Callable[[str], None]] = None

def set_ui_log_callback(callback: Optional[Callable[[str], None]]):
    """Set callback function to send logs to UI."""
    global _ui_log_callback
    _ui_log_callback = callback

def _log_debug(msg: str):
    """Log debug message to both console and UI (if callback set)."""
    print(msg, flush=True)
    if _ui_log_callback:
        try:
            _ui_log_callback(msg)
        except Exception:
            pass


# reCAPTCHA Config for Google Labs
RECAPTCHA_SITE_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
RECAPTCHA_URL = "https://labs.google/fx/tools/flow"
RECAPTCHA_ACTION = "VIDEO_GENERATION"

# Browser pool config
MAX_BROWSERS = 10
BROWSER_IDLE_TIMEOUT = 300  # 5 minutes
BROWSER_MAX_AGE = 1800  # 30 minutes
BROWSER_MAX_USES = 25  # Reduced - Google starts rejecting tokens after ~30-40 requests

# Profile rotation config
PROFILE_ROTATION_CONFIG = {
    "MAX_REQUESTS_PER_PROFILE": 25,  # Reduced from 50 - safer margin
    "MAX_403_BEFORE_ROTATE": 1,  # Rotate immediately on first reCAPTCHA rejection
}

# Browser engine options: "webkit", "chromium", "firefox"
DEFAULT_BROWSER_ENGINE = "webkit"

# Screen resolutions for fingerprint
SCREEN_RESOLUTIONS = [
    (1920, 1080), (2560, 1440), (1366, 768),
    (1536, 864), (1440, 900), (1280, 720)
]


# === BROWSER INSTALLATION CHECK ===

def _get_playwright_browsers_path() -> Path:
    """Get the path where Playwright stores browsers."""
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', os.path.expanduser('~'))) / '.cache' / 'ms-playwright'
    else:
        return Path.home() / '.cache' / 'ms-playwright'


def check_browser_installed(browser_type: str = "webkit") -> tuple:
    """Check if specific Playwright browser is installed.
    
    Args:
        browser_type: webkit, chromium, or firefox
        
    Returns:
        tuple: (is_installed: bool, version: str | None, error: str | None)
    """
    import sys
    browsers_path = _get_playwright_browsers_path()
    
    if not browsers_path.exists():
        return (False, None, "Playwright browsers directory not found")
    
    # Look for browser folder
    browser_patterns = {
        "webkit": "webkit-*",
        "chromium": "chromium-*", 
        "firefox": "firefox-*"
    }
    
    pattern = browser_patterns.get(browser_type, f"{browser_type}-*")
    matches = list(browsers_path.glob(pattern))
    
    if not matches:
        return (False, None, f"{browser_type} browser not installed")
    
    # Check if browser executable exists
    browser_dir = matches[0]
    executable = None
    
    if browser_type == "webkit":
        if sys.platform == "darwin":
            executable = browser_dir / "pw_run.sh"
            if not executable.exists():
                executable = browser_dir / "Playwright.app"
        elif sys.platform == "win32":
            executable = browser_dir / "Playwright.exe"
        else:
            executable = browser_dir / "pw_run.sh"
    elif browser_type == "chromium":
        if sys.platform == "darwin":
            executable = browser_dir / "chrome-mac" / "Chromium.app"
        elif sys.platform == "win32":
            executable = browser_dir / "chrome-win" / "chrome.exe"
        else:
            executable = browser_dir / "chrome-linux" / "chrome"
    else:  # firefox
        if sys.platform == "darwin":
            executable = browser_dir / "firefox" / "Nightly.app"
        elif sys.platform == "win32":
            executable = browser_dir / "firefox" / "firefox.exe"
        else:
            executable = browser_dir / "firefox" / "firefox"
    
    if executable and not executable.exists():
        return (False, None, f"{browser_type} browser files corrupted or incomplete")
    
    return (True, browser_dir.name, None)


async def ensure_browser_installed(browser_type: str = "webkit", 
                                   progress_callback: Optional[Callable[[str], None]] = None) -> bool:
    """Ensure browser is installed, auto-install if needed.
    
    Args:
        browser_type: webkit, chromium, or firefox
        progress_callback: Optional callback for progress updates
        
    Returns:
        True if browser is ready
    """
    import sys
    
    is_installed, version, error = check_browser_installed(browser_type)
    
    if is_installed:
        if progress_callback:
            progress_callback(f"✅ {browser_type} browser ready ({version})")
        return True
    
    # Need to install
    _log_debug(f"[Playwright] {browser_type} browser not found, installing...")
    if progress_callback:
        progress_callback(f"📦 Installing {browser_type} browser... (may take 1-2 minutes)")
    
    try:
        # Run playwright install
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "playwright", "install", browser_type,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        if process.returncode == 0:
            _log_debug(f"[Playwright] ✅ {browser_type} browser installed successfully")
            if progress_callback:
                progress_callback(f"✅ {browser_type} browser installed successfully")
            return True
        else:
            error_msg = stderr.decode()[:200] if stderr else "Unknown error"
            _log_debug(f"[Playwright] ❌ Failed to install {browser_type}: {error_msg}")
            if progress_callback:
                progress_callback(f"❌ Failed to install {browser_type}: {error_msg}")
            return False
            
    except Exception as e:
        _log_debug(f"[Playwright] ❌ Error installing {browser_type}: {str(e)[:100]}")
        if progress_callback:
            progress_callback(f"❌ Error installing {browser_type}: {str(e)[:100]}")
        return False


def _get_profiles_dir() -> Path:
    """Get the directory for reCAPTCHA browser profiles."""
    if os.name == 'nt':
        base_dir = Path(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')))
    else:
        base_dir = Path(os.path.expanduser('~/.local/share'))
    
    profiles_dir = base_dir / 'veononstop' / 'recaptcha-profiles-playwright'
    profiles_dir.mkdir(parents=True, exist_ok=True)
    return profiles_dir


def clear_all_profiles() -> int:
    """Clear all reCAPTCHA profiles on startup."""
    profiles_dir = _get_profiles_dir()
    count = 0
    
    try:
        if profiles_dir.exists():
            for item in profiles_dir.iterdir():
                if item.is_dir() and item.name.startswith('profile_'):
                    # Try multiple times with delay for locked files
                    for attempt in range(3):
                        try:
                            shutil.rmtree(item, ignore_errors=True)
                            if not item.exists():
                                count += 1
                                break
                        except Exception:
                            pass
                        import time
                        time.sleep(0.5)
    except Exception:
        pass
    
    if count > 0:
        print(f"🧹 Cleared {count} Playwright profile(s)")
    
    return count


def _get_random_fingerprint() -> Dict[str, Any]:
    """Generate random browser fingerprint."""
    res = random.choice(SCREEN_RESOLUTIONS)
    chrome_version = random.randint(120, 131)
    return {
        "width": res[0],
        "height": res[1],
        "user_agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_version}.0.0.0 Safari/537.36"
    }


def _parse_cookies(cookie_input) -> List[Dict[str, str]]:
    """Parse cookies from various formats."""
    if isinstance(cookie_input, list):
        return cookie_input
    
    if isinstance(cookie_input, str):
        if cookie_input.startswith('['):
            try:
                return json.loads(cookie_input)
            except:
                pass
        
        cookies = []
        for part in cookie_input.split(';'):
            part = part.strip()
            if '=' in part:
                name, value = part.split('=', 1)
                cookies.append({
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".labs.google",
                    "path": "/"
                })
        return cookies
    
    return []


@dataclass
class BrowserInstance:
    """Represents a browser instance for a specific account."""
    cookie_hash: str
    browser: Any = None
    context: Any = None
    page: Any = None
    profile_path: Optional[Path] = None
    created_at: datetime = field(default_factory=datetime.now)
    last_used: datetime = field(default_factory=datetime.now)
    use_count: int = 0
    error_403_count: int = 0
    is_ready: bool = False
    
    def is_expired(self) -> bool:
        """Check if browser should be recycled."""
        age = (datetime.now() - self.created_at).total_seconds()
        idle = (datetime.now() - self.last_used).total_seconds()
        return age > BROWSER_MAX_AGE or idle > BROWSER_IDLE_TIMEOUT or self.use_count >= BROWSER_MAX_USES
    
    def needs_rotation(self) -> bool:
        """Check if profile needs rotation."""
        return (
            self.use_count >= PROFILE_ROTATION_CONFIG["MAX_REQUESTS_PER_PROFILE"] or
            self.error_403_count >= PROFILE_ROTATION_CONFIG["MAX_403_BEFORE_ROTATE"]
        )
    
    def mark_used(self):
        """Mark browser as recently used."""
        self.last_used = datetime.now()
        self.use_count += 1
    
    def record_403(self):
        """Record a 403 error."""
        self.error_403_count += 1
    
    def reset_403_count(self):
        """Reset 403 error count on success."""
        self.error_403_count = 0


class RecaptchaSolverError(Exception):
    """Custom exception for reCAPTCHA solver errors."""
    pass


class RecaptchaSolver:
    """reCAPTCHA Enterprise solver using Playwright.
    
    This class manages browser instances for obtaining reCAPTCHA tokens.
    Each account (identified by cookie hash) gets its own browser instance
    to maintain session isolation.
    
    Features:
    - Per-account browser isolation
    - Profile rotation after max uses or 403 errors
    - Headless mode (completely hidden)
    - Multiple browser engine support
    """
    
    TOKEN_VALIDITY_MS = 20000
    
    def __init__(self, browser_engine: str = DEFAULT_BROWSER_ENGINE, 
                 clear_profiles_on_init: bool = True,
                 auto_install: bool = True):
        """Initialize the reCAPTCHA solver.
        
        Args:
            browser_engine: Browser engine to use ("webkit", "chromium", "firefox")
            clear_profiles_on_init: If True, clear all profiles on initialization
            auto_install: If True, auto-install browser if not found
        """
        if not PLAYWRIGHT_AVAILABLE:
            raise RecaptchaSolverError(
                "Playwright is not installed. "
                "Install it with: pip install playwright && playwright install"
            )
        
        self._browsers: Dict[str, BrowserInstance] = {}
        self._lock = asyncio.Lock()
        self._profiles_dir = _get_profiles_dir()
        self._browser_engine = browser_engine
        self._playwright = None
        self._auto_install = auto_install
        self._needs_browser_install = False
        
        # Check if browser is installed
        is_installed, version, error = check_browser_installed(browser_engine)
        if not is_installed:
            if auto_install:
                _log_debug(f"[Playwright] {browser_engine} not found: {error}. Will auto-install on first use.")
                self._needs_browser_install = True
            else:
                raise RecaptchaSolverError(
                    f"Playwright {browser_engine} browser not installed. "
                    f"Install it with: playwright install {browser_engine}"
                )
        else:
            _log_debug(f"[Playwright] {browser_engine} browser found: {version}")
        
        if clear_profiles_on_init:
            clear_all_profiles()
        
        print(f"🔐 [reCAPTCHA] Initialized Playwright solver ({browser_engine})")

    def _get_cookie_hash(self, cookie: str) -> str:
        """Generate a hash from cookie for browser identification."""
        if isinstance(cookie, list):
            cookie_str = json.dumps(cookie, sort_keys=True)
        else:
            cookie_str = str(cookie)
        
        hash1 = hashlib.sha256(cookie_str.encode()).hexdigest()[:16]
        suffix = cookie_str[-20:].replace('=', '').replace(';', '')[:10]
        return f"pw_{hash1}_{suffix}"
    
    def _get_profile_path(self, cookie_hash: str) -> Path:
        """Get profile directory path for a cookie hash."""
        return self._profiles_dir / f"profile_{cookie_hash[:16]}"
    
    async def _ensure_playwright(self):
        """Ensure Playwright is started and browser is installed."""
        # Auto-install browser if needed
        if self._needs_browser_install:
            _log_debug(f"[Playwright] Auto-installing {self._browser_engine} browser...")
            success = await ensure_browser_installed(
                self._browser_engine,
                progress_callback=_log_debug
            )
            if not success:
                raise RecaptchaSolverError(
                    f"Failed to install {self._browser_engine} browser. "
                    "Please check internet connection and try again, or run manually: "
                    f"playwright install {self._browser_engine}"
                )
            self._needs_browser_install = False
        
        if self._playwright is None:
            self._playwright = await async_playwright().start()
    
    async def _create_browser(self, fingerprint: Dict[str, Any], profile_path: Optional[Path] = None) -> tuple:
        """Create browser, context, and page."""
        await self._ensure_playwright()
        
        _log_debug(f"[Playwright] Creating {self._browser_engine} browser...")
        
        # Launch browser based on engine
        launch_args = {
            "headless": True,
        }
        
        if self._browser_engine == "chromium":
            launch_args["args"] = [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ]
            browser = await self._playwright.chromium.launch(**launch_args)
        elif self._browser_engine == "firefox":
            browser = await self._playwright.firefox.launch(**launch_args)
        else:  # webkit (default)
            browser = await self._playwright.webkit.launch(**launch_args)
        
        _log_debug(f"[Playwright] Browser launched")
        
        # Create context with fingerprint
        context = await browser.new_context(
            user_agent=fingerprint["user_agent"],
            viewport={"width": fingerprint["width"], "height": fingerprint["height"]},
        )
        
        # Add stealth script for Chromium
        if self._browser_engine == "chromium":
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                window.chrome = {runtime: {}};
            """)
        
        page = await context.new_page()

        # Set up Bearer token interception
        async def intercept_token(route, request):
            global _captured_bearer_token, _token_capture_time

            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer ") and "aisandbox-pa.googleapis.com" in request.url:
                token = auth_header[7:]  # Remove "Bearer " prefix
                if token != _captured_bearer_token:
                    _captured_bearer_token = token
                    _token_capture_time = time.time()
                    _log_debug(f"[Playwright] Bearer token captured: {token[:40]}...")

            await route.continue_()

        await page.route("**/*", intercept_token)

        return browser, context, page
    
    async def _initialize_browser(self, context: Any, page: Any, cookies: List[Dict[str, str]]) -> bool:
        """Initialize browser with cookies and navigate to site."""
        try:
            _log_debug("[Playwright] Navigating to labs.google...")
            # Navigate to domain first
            await page.goto("https://labs.google", timeout=120000)
            _log_debug("[Playwright] Adding cookies...")
            await asyncio.sleep(1)
            
            # Add cookies
            for c in cookies:
                try:
                    await context.add_cookies([{
                        "name": c.get("name", ""),
                        "value": c.get("value", ""),
                        "domain": ".labs.google",
                        "path": "/",
                        "url": "https://labs.google",
                    }])
                except Exception:
                    pass
            
            _log_debug(f"[Playwright] Navigating to {RECAPTCHA_URL}...")
            # Navigate to reCAPTCHA site
            await page.goto(RECAPTCHA_URL, wait_until="networkidle", timeout=120000)
            _log_debug("[Playwright] Waiting for grecaptcha...")
            await asyncio.sleep(2)
            
            # Wait for grecaptcha to load
            for i in range(15):
                has_grecaptcha = await page.evaluate("""
                    () => typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined'
                """)
                if has_grecaptcha:
                    _log_debug("[Playwright] grecaptcha loaded!")
                    return True
                _log_debug(f"[Playwright] Waiting... ({i+1}/15)")
                await asyncio.sleep(1)
            
            _log_debug("[Playwright] grecaptcha NOT loaded after timeout")
            return False
            
        except Exception as e:
            _log_debug(f"[Playwright] Init error: {e}")
            return False
    
    async def initialize(self, cookie: str) -> bool:
        """Initialize a browser instance for the given cookie."""
        async with self._lock:
            cookie_hash = self._get_cookie_hash(cookie)
            cookies = _parse_cookies(cookie)
            
            if not cookies:
                raise RecaptchaSolverError("No valid cookies found")
            
            # Check if already initialized
            if cookie_hash in self._browsers:
                instance = self._browsers[cookie_hash]
                
                if instance.needs_rotation():
                    _log_debug(f"[Playwright] Rotating profile (uses: {instance.use_count})")
                    await self._rotate_profile(cookie_hash)
                elif not instance.is_expired() and instance.is_ready:
                    _log_debug(f"[Playwright] Browser already ready, reusing")
                    instance.last_used = datetime.now()
                    return True
                else:
                    _log_debug(f"[Playwright] Browser expired, closing")
                    await self._close_browser(cookie_hash)
            
            # Check pool limit
            if len(self._browsers) >= MAX_BROWSERS:
                oldest = min(self._browsers.items(), key=lambda x: x[1].last_used)
                _log_debug(f"[Playwright] Pool full, closing oldest browser")
                await self._close_browser(oldest[0])
            
            try:
                fingerprint = _get_random_fingerprint()
                profile_path = self._get_profile_path(cookie_hash)
                
                _log_debug(f"[Playwright] Creating new browser...")
                browser, context, page = await self._create_browser(fingerprint, profile_path)
                
                instance = BrowserInstance(
                    cookie_hash=cookie_hash,
                    browser=browser,
                    context=context,
                    page=page,
                    profile_path=profile_path
                )
                
                _log_debug(f"[Playwright] Initializing browser with cookies...")
                # Initialize with cookies
                success = await self._initialize_browser(context, page, cookies)
                
                if success:
                    instance.is_ready = True
                    self._browsers[cookie_hash] = instance
                    _log_debug(f"[Playwright] Browser ready!")
                    return True
                else:
                    _log_debug(f"[Playwright] Init failed, closing browser")
                    await browser.close()
                    raise RecaptchaSolverError("Failed to initialize browser - grecaptcha not loaded")
                    
            except Exception as e:
                _log_debug(f"[Playwright] Error: {e}")
                raise RecaptchaSolverError(f"Failed to initialize browser: {e}")
    
    async def _close_browser(self, cookie_hash: str, delete_profile: bool = False) -> None:
        """Close a specific browser."""
        if cookie_hash in self._browsers:
            instance = self._browsers[cookie_hash]
            try:
                if instance.browser:
                    await instance.browser.close()
            except:
                pass
            
            if delete_profile and instance.profile_path and instance.profile_path.exists():
                try:
                    shutil.rmtree(instance.profile_path)
                except Exception:
                    pass
            
            del self._browsers[cookie_hash]
    
    async def _rotate_profile(self, cookie_hash: str) -> None:
        """Rotate profile for a cookie."""
        # Log disabled - contains cookie hash
        # print(f"🔐 [reCAPTCHA] Rotating profile for {cookie_hash[:20]}...")
        await self._close_browser(cookie_hash, delete_profile=True)
    
    async def record_403_error(self, cookie: str) -> bool:
        """Record a 403 error and rotate profile if needed."""
        cookie_hash = self._get_cookie_hash(cookie)
        
        if cookie_hash in self._browsers:
            instance = self._browsers[cookie_hash]
            instance.record_403()
            
            if instance.error_403_count >= PROFILE_ROTATION_CONFIG["MAX_403_BEFORE_ROTATE"]:
                await self._rotate_profile(cookie_hash)
                return True
        
        return False
    
    async def get_token(self, cookie: str, action: str = RECAPTCHA_ACTION) -> Optional[str]:
        """Get a reCAPTCHA Enterprise token.
        
        Args:
            cookie: Cookie string for authentication.
            action: reCAPTCHA action (VIDEO_GENERATION or IMAGE_GENERATION).
        """
        cookie_hash = self._get_cookie_hash(cookie)
        
        # Ensure browser is initialized
        if cookie_hash not in self._browsers:
            await self.initialize(cookie)
        
        instance = self._browsers.get(cookie_hash)
        if not instance or not instance.page:
            raise RecaptchaSolverError("Browser not initialized")
        
        # Check if needs rotation
        if instance.needs_rotation():
            await self._rotate_profile(cookie_hash)
            await self.initialize(cookie)
            instance = self._browsers.get(cookie_hash)
            if not instance or not instance.page:
                raise RecaptchaSolverError("Browser not initialized after rotation")
        
        async with self._lock:
            try:
                instance.mark_used()
                
                print(f"🔐 [reCAPTCHA] Request {instance.use_count}/{PROFILE_ROTATION_CONFIG['MAX_REQUESTS_PER_PROFILE']} (action: {action})")
                
                # Execute reCAPTCHA
                token = await instance.page.evaluate(f"""
                    () => new Promise((resolve, reject) => {{
                        grecaptcha.enterprise.ready(() => {{
                            grecaptcha.enterprise.execute('{RECAPTCHA_SITE_KEY}', {{action: '{action}'}})
                                .then(token => resolve(token))
                                .catch(err => reject(err.message));
                        }});
                    }})
                """)
                
                if token and len(token) > 100:
                    instance.reset_403_count()
                    print(f"🔐 [reCAPTCHA] ✅ Token obtained")
                    return token
                else:
                    # Token failed, refresh page
                    await instance.page.reload()
                    await asyncio.sleep(2)
                    raise RecaptchaSolverError("Failed to obtain valid token")
                    
            except Exception as e:
                await self._close_browser(cookie_hash, delete_profile=True)
                raise RecaptchaSolverError(f"Failed to get reCAPTCHA token: {e}")
    
    async def cleanup(self, cookie: str) -> None:
        """Cleanup browser instance for a specific cookie."""
        cookie_hash = self._get_cookie_hash(cookie)
        async with self._lock:
            await self._close_browser(cookie_hash, delete_profile=True)
    
    async def cleanup_idle(self) -> int:
        """Cleanup idle browser instances."""
        async with self._lock:
            to_remove = [
                cookie_hash for cookie_hash, instance in self._browsers.items()
                if instance.is_expired() or instance.needs_rotation()
            ]
            
            for cookie_hash in to_remove:
                await self._close_browser(cookie_hash, delete_profile=True)
            
            return len(to_remove)
    
    async def cleanup_all(self) -> None:
        """Cleanup all browser instances and profiles."""
        async with self._lock:
            for cookie_hash in list(self._browsers.keys()):
                await self._close_browser(cookie_hash, delete_profile=True)
        
        # Close playwright
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        
        clear_all_profiles()
    
    def get_instance_count(self) -> int:
        """Get number of active browser instances."""
        return len(self._browsers)
    
    def get_instance_stats(self) -> Dict[str, dict]:
        """Get statistics for all browser instances."""
        stats = {}
        for cookie_hash, instance in self._browsers.items():
            stats[cookie_hash[:8]] = {
                "created_at": instance.created_at.isoformat(),
                "last_used": instance.last_used.isoformat(),
                "use_count": instance.use_count,
                "error_403_count": instance.error_403_count,
                "is_ready": instance.is_ready,
                "needs_rotation": instance.needs_rotation(),
            }
        return stats


# Global solver instance
_solver: Optional[RecaptchaSolver] = None

# Global Bearer token storage
_captured_bearer_token: Optional[str] = None
_token_capture_time: float = 0


def get_captured_token() -> Optional[str]:
    """Get the last captured Bearer token."""
    return _captured_bearer_token


def get_token_age_seconds() -> float:
    """Get age of captured token in seconds."""
    if _token_capture_time == 0:
        return float('inf')
    return time.time() - _token_capture_time


def update_env_token(token: str) -> bool:
    """Update VEO3_TOKEN in .env file.

    Args:
        token: New token value.

    Returns:
        True if successful.
    """
    env_path = Path(__file__).parent.parent / ".env"

    if not env_path.exists():
        _log_debug("[TokenRefresh] .env file not found")
        return False

    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        updated = False
        for i, line in enumerate(lines):
            if line.startswith("VEO3_TOKEN="):
                lines[i] = f"VEO3_TOKEN={token}\n"
                updated = True
                break

        if not updated:
            lines.append(f"VEO3_TOKEN={token}\n")

        with open(env_path, 'w', encoding='utf-8') as f:
            f.writelines(lines)

        _log_debug("[TokenRefresh] VEO3_TOKEN updated in .env")
        return True

    except Exception as e:
        _log_debug(f"[TokenRefresh] Error updating .env: {e}")
        return False


def check_and_update_token() -> Optional[str]:
    """Check if new token was captured and update .env if so.

    Returns:
        New token if updated, None otherwise.
    """
    token = get_captured_token()
    if token:
        # Get current token from env
        import os
        current = os.getenv("VEO3_TOKEN", "").strip()

        if token != current:
            _log_debug(f"[TokenRefresh] New token detected, updating...")
            if update_env_token(token):
                # Also update environment variable for current session
                os.environ["VEO3_TOKEN"] = token
                return token

    return None


def get_solver(browser_engine: str = DEFAULT_BROWSER_ENGINE) -> RecaptchaSolver:
    """Get or create the global reCAPTCHA solver instance."""
    global _solver
    if _solver is None:
        _solver = RecaptchaSolver(browser_engine=browser_engine)
    return _solver


async def get_recaptcha_token(cookie: str) -> Optional[str]:
    """Convenience function to get reCAPTCHA token."""
    solver = get_solver()
    return await solver.get_token(cookie)
