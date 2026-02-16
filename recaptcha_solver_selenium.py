"""reCAPTCHA Enterprise Solver using Selenium.

This module provides reCAPTCHA token generation for API calls
using browser automation with Selenium (Chrome/Edge).

Features:
- Per-account browser isolation (like Puppeteer)
- Profile rotation after max uses or 403 errors
- Clear profiles on startup
- Automatic browser rotation after max uses
- Resource cleanup for idle browsers
- Hidden browser window (headless mode)
- Auto-detect Chrome/Edge and download matching driver
- Proxy support (HTTP, HTTPS, SOCKS5) with auth via Chrome extension
"""

import asyncio
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .proxy_manager import Proxy

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.edge.options import Options as EdgeOptions
    from selenium.webdriver.edge.service import Service as EdgeService
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False
    webdriver = None
    ChromeOptions = None
    EdgeOptions = None

# Try to import webdriver_manager for auto driver download
try:
    from webdriver_manager.chrome import ChromeDriverManager
    from webdriver_manager.microsoft import EdgeChromiumDriverManager
    WEBDRIVER_MANAGER_AVAILABLE = True
except ImportError:
    WEBDRIVER_MANAGER_AVAILABLE = False
    ChromeDriverManager = None
    EdgeChromiumDriverManager = None


# === GLOBAL LOG CALLBACK FOR UI ===
# This allows UI to receive debug logs from Selenium
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
BROWSER_MAX_USES = 50

# Profile rotation config (from TypeScript)
PROFILE_ROTATION_CONFIG = {
    "MAX_REQUESTS_PER_PROFILE": 50,  # Rotate profile after 50 requests
    "MAX_403_BEFORE_ROTATE": 3,      # Rotate profile after 3 consecutive 403 errors
}

# Screen resolutions for fingerprint
SCREEN_RESOLUTIONS = [
    (1920, 1080), (2560, 1440), (1366, 768),
    (1536, 864), (1440, 900), (1280, 720)
]


def _get_profiles_dir() -> Path:
    """Get the directory for reCAPTCHA browser profiles."""
    if os.name == 'nt':  # Windows
        base_dir = Path(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')))
    else:  # Linux/Mac
        base_dir = Path(os.path.expanduser('~/.local/share'))
    
    profiles_dir = base_dir / 'veononstop' / 'recaptcha-profiles'
    profiles_dir.mkdir(parents=True, exist_ok=True)
    return profiles_dir


def clear_all_profiles() -> int:
    """Clear all reCAPTCHA profiles on startup.
    
    This helps prevent Chrome crash due to corrupted profiles.
    """
    profiles_dir = _get_profiles_dir()
    count = 0
    
    # Use ASCII-safe logging to avoid encoding issues on Windows
    print(f"[Profiles] Clearing profiles in: {profiles_dir}")
    
    try:
        if profiles_dir.exists():
            for item in profiles_dir.iterdir():
                if item.is_dir():
                    # Try multiple times with delay for locked files
                    for attempt in range(3):
                        try:
                            shutil.rmtree(item, ignore_errors=True)
                            if not item.exists():
                                count += 1
                                print(f"   [OK] Deleted: {item.name}")
                                break
                        except Exception as e:
                            if attempt == 2:
                                print(f"   [WARN] Could not delete {item.name}: {e}")
                        time.sleep(0.5)
    except Exception as e:
        print(f"   [ERROR] Error clearing profiles: {e}")
    
    if count > 0:
        print(f"[Profiles] Cleared {count} Selenium profile(s)")
    
    return count


def _cleanup_profile_locks(profile_path: Path) -> None:
    """Clean up Chrome profile lock files that may cause 'session not created' error.
    
    Chrome creates lock files (SingletonLock, SingletonSocket, SingletonCookie)
    that can prevent new sessions if Chrome crashed without cleanup.
    """
    lock_files = ["SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile"]
    
    for lock_file in lock_files:
        lock_path = profile_path / lock_file
        if lock_path.exists():
            try:
                lock_path.unlink()
                print(f"   🔓 Removed lock: {lock_file}")
            except Exception:
                pass
    
    # Also clean up Default/SingletonLock if exists
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
    """Kill zombie Chrome processes that may be holding locks.
    
    This is needed when Chrome crashes without proper cleanup,
    leaving processes that hold profile locks.
    
    Returns:
        Number of processes killed.
    """
    killed = 0
    
    if os.name == 'nt':  # Windows
        try:
            # Kill chromedriver processes
            result = subprocess.run(
                ['taskkill', '/F', '/IM', 'chromedriver.exe'],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                killed += 1
                print("   🔪 Killed chromedriver.exe")
        except Exception:
            pass
        
        # Kill Chrome processes started by our app (with our profile marker)
        try:
            import psutil
            our_markers = ["veononstop", "recaptcha-profiles"]
            
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
                                print(f"   🔪 Killed Chrome (PID: {proc.info['pid']})")
                                break
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
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
    return {
        "width": res[0],
        "height": res[1],
        "user_agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(120, 131)}.0.0.0 Safari/537.36"
    }


def _parse_cookies(cookie_input) -> List[Dict[str, str]]:
    """Parse cookies from various formats."""
    if isinstance(cookie_input, list):
        return cookie_input
    
    if isinstance(cookie_input, str):
        # Try JSON
        if cookie_input.startswith('['):
            try:
                return json.loads(cookie_input)
            except:
                pass
        
        # Parse string format "name=value; name2=value2"
        cookies = []
        for part in cookie_input.split(';'):
            part = part.strip()
            if '=' in part:
                name, value = part.split('=', 1)
                cookies.append({
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": "labs.google",
                    "path": "/"
                })
        return cookies
    
    return []


def _detect_available_browser() -> Tuple[str, Optional[str]]:
    """Detect which browser is available on the system.
    
    Returns:
        Tuple of (browser_type, browser_path) where browser_type is 'chrome' or 'edge'
    """
    import sys
    
    if sys.platform == "darwin":  # macOS
        # Common Chrome paths on macOS
        chrome_paths = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
        
        # Check Chrome
        for path in chrome_paths:
            if os.path.exists(path):
                print(f"🔍 [Browser] Found Chrome at: {path}")
                return ("chrome", path)
        
        # Default to Chrome (let Selenium find it)
        print("🔍 [Browser] No browser found at common paths, trying default Chrome")
        return ("chrome", None)
    
    elif sys.platform == "win32":  # Windows
        # Common Chrome paths on Windows
        chrome_paths = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]
        
        # Common Edge paths on Windows
        edge_paths = [
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        ]
        
        # Check Chrome first
        for path in chrome_paths:
            if os.path.exists(path):
                print(f"🔍 [Browser] Found Chrome at: {path}")
                return ("chrome", path)
        
        # Check Edge as fallback
        for path in edge_paths:
            if os.path.exists(path):
                print(f"🔍 [Browser] Found Edge at: {path}")
                return ("edge", path)
        
        # Default to Chrome (let Selenium find it)
        print("🔍 [Browser] No browser found at common paths, trying default Chrome")
        return ("chrome", None)
    
    else:  # Linux
        # Common Chrome paths on Linux
        chrome_paths = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]
        
        for path in chrome_paths:
            if os.path.exists(path):
                print(f"🔍 [Browser] Found Chrome at: {path}")
                return ("chrome", path)
        
        # Default to Chrome (let Selenium find it)
        print("🔍 [Browser] No browser found at common paths, trying default Chrome")
        return ("chrome", None)


def _get_driver_service(browser_type: str) -> Any:
    """Get the appropriate driver service with auto-download.
    
    Args:
        browser_type: 'chrome' or 'edge'
        
    Returns:
        Service object for the browser
    """
    _log_debug(f"[Selenium] Getting driver for: {browser_type}")
    _log_debug(f"[Selenium] webdriver_manager available: {WEBDRIVER_MANAGER_AVAILABLE}")
    
    if WEBDRIVER_MANAGER_AVAILABLE:
        try:
            if browser_type == "edge":
                _log_debug("[Selenium] Downloading Edge driver...")
                driver_path = EdgeChromiumDriverManager().install()
                _log_debug(f"[Selenium] Edge driver: {driver_path}")
                return EdgeService(driver_path)
            else:
                _log_debug("[Selenium] Downloading Chrome driver...")
                driver_path = ChromeDriverManager().install()
                _log_debug(f"[Selenium] Chrome driver: {driver_path}")
                return ChromeService(driver_path)
        except Exception as e:
            _log_debug(f"[Selenium] Driver download failed: {e}")
            import traceback
            traceback.print_exc()
    
    # Fallback to system driver
    _log_debug("[Selenium] Using system driver (fallback)")
    if browser_type == "edge":
        return EdgeService()
    return ChromeService()


def _get_proxy_extensions_dir() -> Path:
    """Get the directory for temporary proxy auth extensions."""
    if os.name == 'nt':  # Windows
        base_dir = Path(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')))
    else:  # Linux/Mac
        base_dir = Path(os.path.expanduser('~/.local/share'))
    
    ext_dir = base_dir / 'veononstop' / 'proxy-extensions'
    ext_dir.mkdir(parents=True, exist_ok=True)
    return ext_dir


def _create_proxy_auth_extension(proxy: "Proxy") -> Optional[str]:
    """Create a Chrome extension for proxy authentication.
    
    Chrome doesn't support proxy auth via command line, so we create
    a temporary extension that handles the auth.
    
    Args:
        proxy: Proxy object with authentication credentials.
        
    Returns:
        Path to the extension zip file, or None if no auth needed.
    """
    if not proxy.has_auth:
        return None
    
    # Create extension files
    manifest = {
        "version": "1.0.0",
        "manifest_version": 2,
        "name": "Proxy Auth Extension",
        "permissions": [
            "proxy",
            "tabs",
            "unlimitedStorage",
            "storage",
            "<all_urls>",
            "webRequest",
            "webRequestBlocking"
        ],
        "background": {
            "scripts": ["background.js"]
        },
        "minimum_chrome_version": "22.0.0"
    }
    
    # Determine proxy scheme
    scheme = proxy.proxy_type.value
    if scheme == "socks5":
        scheme = "socks5"
    else:
        scheme = "http"  # Chrome uses 'http' for both http and https proxies
    
    background_js = f'''
var config = {{
    mode: "fixed_servers",
    rules: {{
        singleProxy: {{
            scheme: "{scheme}",
            host: "{proxy.host}",
            port: parseInt({proxy.port})
        }},
        bypassList: ["localhost"]
    }}
}};

chrome.proxy.settings.set({{value: config, scope: "regular"}}, function() {{}});

function callbackFn(details) {{
    return {{
        authCredentials: {{
            username: "{proxy.username}",
            password: "{proxy.password}"
        }}
    }};
}}

chrome.webRequest.onAuthRequired.addListener(
    callbackFn,
    {{urls: ["<all_urls>"]}},
    ['blocking']
);
'''
    
    # Create extension directory
    ext_dir = _get_proxy_extensions_dir()
    ext_hash = hashlib.md5(f"{proxy.host}:{proxy.port}:{proxy.username}".encode()).hexdigest()[:8]
    ext_path = ext_dir / f"proxy_auth_{ext_hash}.zip"
    
    # Create zip file
    try:
        with zipfile.ZipFile(ext_path, 'w') as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            zf.writestr("background.js", background_js)
        
        return str(ext_path)
    except Exception as e:
        print(f"⚠️ [Proxy] Failed to create auth extension: {e}")
        return None


def _cleanup_proxy_extension(extension_path: str) -> None:
    """Cleanup temporary proxy auth extension.
    
    Args:
        extension_path: Path to the extension zip file.
    """
    try:
        if extension_path and os.path.exists(extension_path):
            os.remove(extension_path)
    except Exception:
        pass


def clear_proxy_extensions() -> int:
    """Clear all proxy auth extensions."""
    ext_dir = _get_proxy_extensions_dir()
    count = 0
    
    try:
        if ext_dir.exists():
            for item in ext_dir.iterdir():
                if item.is_file() and item.name.startswith('proxy_auth_'):
                    try:
                        item.unlink()
                        count += 1
                    except Exception:
                        pass
    except Exception:
        pass
    
    if count > 0:
        print(f"[Profiles] Cleared {count} proxy extension(s)")
    
    return count


@dataclass
class BrowserInstance:
    """Represents a browser instance for a specific account."""
    cookie_hash: str
    driver: Any = None
    profile_path: Optional[Path] = None
    proxy_extension_path: Optional[str] = None  # Path to proxy auth extension
    created_at: datetime = field(default_factory=datetime.now)
    last_used: datetime = field(default_factory=datetime.now)
    use_count: int = 0
    error_403_count: int = 0
    is_ready: bool = False
    is_frozen: bool = False  # Track if page is frozen to reduce CPU
    
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
    
    def cleanup_extension(self):
        """Cleanup proxy auth extension if exists."""
        if self.proxy_extension_path:
            _cleanup_proxy_extension(self.proxy_extension_path)
            self.proxy_extension_path = None
    
    def freeze_page(self):
        """Freeze page to reduce CPU usage when idle.
        
        Uses Chrome DevTools Protocol to set page lifecycle state to frozen.
        This significantly reduces CPU usage while keeping the browser ready.
        """
        if self.driver and not self.is_frozen:
            try:
                self.driver.execute_cdp_cmd("Page.setWebLifecycleState", {"state": "frozen"})
                self.is_frozen = True
            except Exception:
                pass  # Silently fail if CDP not supported
    
    def unfreeze_page(self):
        """Unfreeze page before using it.
        
        Restores page to active state so JavaScript can execute.
        """
        if self.driver and self.is_frozen:
            try:
                self.driver.execute_cdp_cmd("Page.setWebLifecycleState", {"state": "active"})
                self.is_frozen = False
            except Exception:
                pass  # Silently fail if CDP not supported
    
    def navigate_to_blank(self):
        """Navigate to blank page to reduce CPU/memory when idle.
        
        This stops all JavaScript execution and releases page resources.
        """
        if self.driver:
            try:
                self.driver.get("about:blank")
                self.is_frozen = False  # Reset frozen state
            except Exception:
                pass


class RecaptchaSolverError(Exception):
    """Custom exception for reCAPTCHA solver errors."""
    pass


class RecaptchaSolver:
    """reCAPTCHA Enterprise solver using Selenium.
    
    This class manages browser instances for obtaining reCAPTCHA tokens.
    Each account (identified by cookie hash) gets its own browser instance
    to maintain session isolation.
    """
    
    # Token validity (~20 seconds)
    TOKEN_VALIDITY_MS = 20000
    
    def __init__(self, clear_profiles_on_init: bool = True):
        """Initialize the reCAPTCHA solver."""
        if not SELENIUM_AVAILABLE:
            raise RecaptchaSolverError(
                "Selenium is not installed. "
                "Install it with: pip install selenium"
            )
        
        self._browsers: Dict[str, BrowserInstance] = {}
        self._lock = asyncio.Lock()
        self._profiles_dir = _get_profiles_dir()
        
        # Clear profiles on startup
        if clear_profiles_on_init:
            clear_all_profiles()
        
        print("🔐 [reCAPTCHA] Initialized Selenium solver")

    def _get_cookie_hash(self, cookie: str) -> str:
        """Generate a hash from cookie for browser identification."""
        if isinstance(cookie, list):
            cookie_str = json.dumps(cookie, sort_keys=True)
        else:
            cookie_str = str(cookie)
        
        hash1 = hashlib.sha256(cookie_str.encode()).hexdigest()[:16]
        suffix = cookie_str[-20:].replace('=', '').replace(';', '')[:10]
        return f"sel_{hash1}_{suffix}"
    
    def _get_profile_path(self, cookie_hash: str) -> Path:
        """Get profile directory path for a cookie hash."""
        return self._profiles_dir / f"profile_{cookie_hash[:16]}"
    
    def _create_driver(self, fingerprint: Dict[str, Any], profile_path: Optional[Path] = None, proxy: Optional["Proxy"] = None) -> Tuple[Any, Optional[str]]:
        """Create Chrome/Edge WebDriver with stealth settings - HEADLESS MODE.
        
        Auto-detects available browser and downloads matching driver.
        
        Args:
            fingerprint: Browser fingerprint settings.
            profile_path: Optional profile directory path.
            proxy: Optional proxy to use.
            
        Returns:
            Tuple of (driver, extension_path) where extension_path is the proxy auth extension if created.
        """
        _log_debug(f"[Selenium] Creating driver...")
        _log_debug(f"[Selenium] profile_path: {profile_path}")
        
        browser_type, browser_path = _detect_available_browser()
        _log_debug(f"[Selenium] browser: {browser_type}, path: {browser_path}")
        
        if browser_type == "edge":
            return self._create_edge_driver(fingerprint, profile_path, browser_path, proxy)
        else:
            return self._create_chrome_driver(fingerprint, profile_path, browser_path, proxy)
    
    def _create_chrome_driver(self, fingerprint: Dict[str, Any], profile_path: Optional[Path] = None, browser_path: Optional[str] = None, proxy: Optional["Proxy"] = None) -> Tuple[Any, Optional[str]]:
        """Create Chrome WebDriver with stealth settings.
        
        Args:
            fingerprint: Browser fingerprint settings.
            profile_path: Optional profile directory path.
            browser_path: Optional path to Chrome binary.
            proxy: Optional proxy to use.
            
        Returns:
            Tuple of (driver, extension_path) where extension_path is the proxy auth extension if created.
        """
        options = ChromeOptions()
        extension_path = None
        
        # Use specific browser binary if provided
        if browser_path and os.path.exists(browser_path):
            options.binary_location = browser_path
        
        # Use profile directory if provided
        if profile_path:
            profile_path.mkdir(parents=True, exist_ok=True)
            # Clean up lock files that may cause "session not created" error
            _cleanup_profile_locks(profile_path)
            options.add_argument(f"--user-data-dir={profile_path}")
        
        # Headless mode - use old headless for macOS compatibility
        options.add_argument("--headless")
        
        # Configure proxy
        if proxy:
            if proxy.has_auth:
                # Note: Proxy auth extension may not work well with headless mode
                print(f"🔐 [Proxy] Using auth proxy: {proxy.host}:{proxy.port}")
                extension_path = _create_proxy_auth_extension(proxy)
                if extension_path:
                    options.add_extension(extension_path)
                    print(f"🔐 [Proxy] Using auth proxy: {proxy.host}:{proxy.port}")
            else:
                proxy_arg = proxy.to_chrome_arg()
                options.add_argument(f"--proxy-server={proxy_arg}")
                print(f"🔐 [Proxy] Using proxy: {proxy_arg}")
        
        # Stealth settings (safe for reCAPTCHA)
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-gpu")
        
        # Don't disable extensions if we're using proxy auth extension
        if not (proxy and proxy.has_auth):
            options.add_argument("--disable-extensions")
        
        options.add_argument("--disable-infobars")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-popup-blocking")
        options.add_argument(f"--user-agent={fingerprint['user_agent']}")
        options.add_argument(f"--window-size={fingerprint['width']},{fingerprint['height']}")
        
        # === FASTER PAGE LOAD ===
        # Disable images to speed up loading (reCAPTCHA doesn't need images)
        prefs = {
            "profile.managed_default_content_settings.images": 2,  # Block images
            "profile.default_content_setting_values.notifications": 2,
            "profile.managed_default_content_settings.stylesheets": 1,  # Allow CSS (needed)
            "profile.managed_default_content_settings.javascript": 1,  # Allow JS (needed)
        }
        options.add_experimental_option("prefs", prefs)
        
        # === CPU/MEMORY OPTIMIZATION (safe for reCAPTCHA - JS still works) ===
        # Limit renderer processes - KEY for reducing CPU with many browsers
        options.add_argument("--renderer-process-limit=1")
        options.add_argument("--disable-features=IsolateOrigins,site-per-process")
        
        # Reduce background CPU usage
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_argument("--disable-renderer-backgrounding")
        options.add_argument("--disable-background-timer-throttling")
        
        # Disable unnecessary network/telemetry
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-domain-reliability")
        options.add_argument("--disable-component-extensions-with-background-pages")
        options.add_argument("--enable-features=NetworkService,NetworkServiceInProcess")
        options.add_argument("--metrics-recording-only")
        
        # Reduce media/graphics processing
        options.add_argument("--disable-features=AudioServiceOutOfProcess")
        options.add_argument("--force-color-profile=srgb")
        options.add_argument("--disable-remote-fonts")
        
        # Memory optimization
        options.add_argument("--disable-ipc-flooding-protection")
        options.add_argument("--enable-low-end-device-mode")
        options.add_argument("--memory-pressure-off")
        
        # Audio/logging
        options.add_argument("--mute-audio")
        options.add_argument("--log-level=3")
        
        # Disable unnecessary features
        options.add_argument("--disable-session-crashed-bubble")
        options.add_argument("--disable-features=TranslateUI")
        options.add_argument("--disable-hang-monitor")
        options.add_argument("--disable-prompt-on-repost")
        options.add_argument("--disable-client-side-phishing-detection")
        options.add_argument("--disable-default-apps")
        options.add_argument("--disable-component-update")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-translate")
        options.add_argument("--disable-sync")
        
        # Crash prevention
        options.add_argument("--disable-crash-reporter")
        options.add_argument("--disable-breakpad")
        
        # Exclude automation flags
        options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        options.add_experimental_option("useAutomationExtension", False)
        
        # Get driver service with auto-download
        _log_debug("[Selenium] Getting driver service...")
        service = _get_driver_service("chrome")
        _log_debug(f"[Selenium] Driver service ready")
        
        try:
            _log_debug("[Selenium] Creating Chrome webdriver...")
            driver = webdriver.Chrome(service=service, options=options)
            _log_debug("[Selenium] Chrome launched (headless)")
        except Exception as e:
            error_msg = str(e)
            _log_debug(f"[Selenium] Chrome failed: {error_msg[:100]}")
            
            # If Chrome crashes, try killing zombie processes and retry
            if "crashed" in error_msg.lower() or "session not created" in error_msg.lower() or "DevToolsActive" in error_msg:
                print(f"⚠️ [Browser] Killing zombie Chrome processes...")
                _kill_zombie_chrome_processes()
                
                # Clean profile if exists
                if profile_path:
                    print(f"⚠️ [Browser] Cleaning profile: {profile_path}")
                    _cleanup_profile_locks(profile_path)
                
                # Wait a bit for processes to fully terminate
                time.sleep(2)
                
                # Retry without profile
                print(f"⚠️ [Browser] Retrying without user profile...")
                options = ChromeOptions()
                if browser_path and os.path.exists(browser_path):
                    options.binary_location = browser_path
                
                # Headless mode
                options.add_argument("--headless=new")
                options.add_argument("--disable-blink-features=AutomationControlled")
                options.add_argument("--disable-dev-shm-usage")
                options.add_argument("--no-sandbox")
                options.add_argument("--disable-gpu")
                options.add_argument("--disable-extensions")
                options.add_argument("--mute-audio")
                options.add_argument("--log-level=3")
                options.add_argument("--no-first-run")
                options.add_argument("--disable-crash-reporter")
                options.add_argument("--disable-breakpad")
                options.add_argument(f"--user-agent={fingerprint['user_agent']}")
                options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
                options.add_experimental_option("useAutomationExtension", False)
                
                driver = webdriver.Chrome(service=service, options=options)
                print(f"🔐 [Browser] Chrome launched (headless mode, no profile)")
            else:
                raise e
        
        # Execute stealth script to hide automation detection
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                window.chrome = {runtime: {}};
                
                // Hide automation detection
                Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
                Object.defineProperty(navigator, 'productSub', {get: () => '20030107'});
                Object.defineProperty(navigator, 'vendor', {get: () => 'Google Inc.'});
                
                // Override permissions
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({ state: Notification.permission }) :
                        originalQuery(parameters)
                );
            """
        })
        
        return driver, extension_path
    
    def _create_edge_driver(self, fingerprint: Dict[str, Any], profile_path: Optional[Path] = None, browser_path: Optional[str] = None, proxy: Optional["Proxy"] = None) -> Tuple[Any, Optional[str]]:
        """Create Edge WebDriver with stealth settings.
        
        Args:
            fingerprint: Browser fingerprint settings.
            profile_path: Optional profile directory path.
            browser_path: Optional path to Edge binary.
            proxy: Optional proxy to use.
            
        Returns:
            Tuple of (driver, extension_path) where extension_path is the proxy auth extension if created.
        """
        options = EdgeOptions()
        extension_path = None
        
        # Use specific browser binary if provided
        if browser_path and os.path.exists(browser_path):
            options.binary_location = browser_path
        
        # Use profile directory if provided
        if profile_path:
            profile_path.mkdir(parents=True, exist_ok=True)
            _cleanup_profile_locks(profile_path)
            options.add_argument(f"--user-data-dir={profile_path}")
        
        # Headless mode
        options.add_argument("--headless=new")
        
        # Configure proxy
        if proxy:
            if proxy.has_auth:
                extension_path = _create_proxy_auth_extension(proxy)
                if extension_path:
                    options.add_extension(extension_path)
                    print(f"🔐 [Proxy] Using auth proxy: {proxy.host}:{proxy.port}")
            else:
                proxy_arg = proxy.to_chrome_arg()
                options.add_argument(f"--proxy-server={proxy_arg}")
                print(f"🔐 [Proxy] Using proxy: {proxy_arg}")
        
        # Stealth settings
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-gpu")
        
        if not (proxy and proxy.has_auth):
            options.add_argument("--disable-extensions")
        
        options.add_argument("--disable-infobars")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-popup-blocking")
        options.add_argument(f"--user-agent={fingerprint['user_agent']}")
        
        # Performance settings
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_argument("--disable-renderer-backgrounding")
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--mute-audio")
        options.add_argument("--log-level=3")
        
        # Disable unnecessary features
        options.add_argument("--disable-session-crashed-bubble")
        options.add_argument("--disable-features=TranslateUI")
        options.add_argument("--disable-hang-monitor")
        options.add_argument("--disable-prompt-on-repost")
        options.add_argument("--disable-client-side-phishing-detection")
        options.add_argument("--disable-default-apps")
        options.add_argument("--disable-component-update")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-translate")
        options.add_argument("--disable-sync")
        
        # Crash prevention
        options.add_argument("--disable-crash-reporter")
        options.add_argument("--disable-breakpad")
        
        # Exclude automation flags
        options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        options.add_experimental_option("useAutomationExtension", False)
        
        # Get driver service with auto-download
        service = _get_driver_service("edge")
        
        try:
            driver = webdriver.Edge(service=service, options=options)
            print(f"🔐 [Browser] Edge launched (headless mode)")
        except Exception as e:
            error_msg = str(e)
            print(f"❌ [Browser] Edge launch failed: {error_msg[:100]}")
            
            if "crashed" in error_msg.lower() or "session not created" in error_msg.lower() or "DevToolsActive" in error_msg:
                print(f"⚠️ [Browser] Killing zombie processes and retrying...")
                _kill_zombie_chrome_processes()
                
                if profile_path:
                    _cleanup_profile_locks(profile_path)
                
                time.sleep(2)
                
                options = EdgeOptions()
                if browser_path and os.path.exists(browser_path):
                    options.binary_location = browser_path
                
                # Headless mode
                options.add_argument("--headless=new")
                options.add_argument("--disable-blink-features=AutomationControlled")
                options.add_argument("--disable-dev-shm-usage")
                options.add_argument("--no-sandbox")
                options.add_argument("--disable-gpu")
                options.add_argument("--disable-extensions")
                options.add_argument("--mute-audio")
                options.add_argument("--log-level=3")
                options.add_argument("--no-first-run")
                options.add_argument("--disable-crash-reporter")
                options.add_argument("--disable-breakpad")
                options.add_argument(f"--user-agent={fingerprint['user_agent']}")
                options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
                options.add_experimental_option("useAutomationExtension", False)
                
                driver = webdriver.Edge(service=service, options=options)
                print(f"🔐 [Browser] Edge launched (headless mode, no profile)")
            else:
                raise e
        
        # Execute stealth script to hide headless detection
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                window.chrome = {runtime: {}};
                
                // Hide headless detection
                Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
                Object.defineProperty(navigator, 'productSub', {get: () => '20030107'});
                Object.defineProperty(navigator, 'vendor', {get: () => 'Google Inc.'});
                
                // Override permissions
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({ state: Notification.permission }) :
                        originalQuery(parameters)
                );
            """
        })
        
        return driver, extension_path
    
    def _initialize_browser_sync(self, driver: Any, cookies: List[Dict[str, str]]) -> bool:
        """Initialize browser with cookies and navigate to site (sync)."""
        try:
            # Set longer timeouts for slow network
            driver.set_page_load_timeout(120)  # 2 minutes for page load
            driver.set_script_timeout(60)  # 1 minute for scripts
            
            _log_debug("[Selenium] Navigating to labs.google...")
            # Navigate to domain first
            driver.get("https://labs.google")
            _log_debug("[Selenium] Adding cookies...")
            time.sleep(0.5)
            
            # Add cookies
            for cookie in cookies:
                try:
                    cookie_dict = {
                        "name": cookie.get("name", ""),
                        "value": cookie.get("value", ""),
                        "domain": cookie.get("domain", "labs.google"),
                        "path": cookie.get("path", "/"),
                    }
                    driver.add_cookie(cookie_dict)
                except Exception:
                    pass
            
            _log_debug(f"[Selenium] Navigating to {RECAPTCHA_URL}...")
            # Navigate to site
            driver.get(RECAPTCHA_URL)
            _log_debug("[Selenium] Waiting for grecaptcha...")
            time.sleep(1.5)
            
            # Wait for grecaptcha to load
            for i in range(15):  # 15 attempts x 2s = 30s max
                try:
                    has_grecaptcha = driver.execute_script(
                        "return typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined';"
                    )
                    if has_grecaptcha:
                        _log_debug("[Selenium] grecaptcha loaded!")
                        return True
                except Exception:
                    pass
                _log_debug(f"[Selenium] Waiting... ({i+1}/15)")
                time.sleep(2.0)
            
            _log_debug("[Selenium] grecaptcha NOT loaded after timeout")
            return False
            
        except Exception as e:
            _log_debug(f"[Selenium] Init error: {e}")
            return False
    
    async def initialize(self, cookie: str, proxy: Optional["Proxy"] = None) -> bool:
        """Initialize a browser instance for the given cookie.
        
        Args:
            cookie: Cookie string for authentication.
            proxy: Optional proxy to use for this browser.
            
        Returns:
            True if initialization successful.
        """
        async with self._lock:
            cookie_hash = self._get_cookie_hash(cookie)
            cookies = _parse_cookies(cookie)
            
            if not cookies:
                raise RecaptchaSolverError("No valid cookies found")
            
            # Check if already initialized
            if cookie_hash in self._browsers:
                instance = self._browsers[cookie_hash]
                
                if instance.needs_rotation():
                    _log_debug(f"[Selenium] Rotating profile (uses: {instance.use_count})")
                    await self._rotate_profile(cookie_hash)
                elif not instance.is_expired() and instance.is_ready:
                    _log_debug(f"[Selenium] Browser already ready, reusing")
                    instance.last_used = datetime.now()
                    return True
                else:
                    _log_debug(f"[Selenium] Browser expired, closing")
                    await self._close_browser(cookie_hash)
            
            # Check pool limit
            if len(self._browsers) >= MAX_BROWSERS:
                oldest = min(self._browsers.items(), key=lambda x: x[1].last_used)
                _log_debug(f"[Selenium] Pool full, closing oldest browser")
                await self._close_browser(oldest[0])
            
            try:
                fingerprint = _get_random_fingerprint()
                profile_path = self._get_profile_path(cookie_hash)
                
                _log_debug(f"[Selenium] Creating new browser...")
                # Run browser creation in thread pool
                loop = asyncio.get_event_loop()
                driver, extension_path = await loop.run_in_executor(
                    None, self._create_driver, fingerprint, profile_path, proxy
                )
                
                instance = BrowserInstance(
                    cookie_hash=cookie_hash, 
                    driver=driver,
                    profile_path=profile_path,
                    proxy_extension_path=extension_path
                )
                
                _log_debug(f"[Selenium] Initializing browser with cookies...")
                # Initialize with cookies
                success = await loop.run_in_executor(
                    None, self._initialize_browser_sync, driver, cookies
                )
                
                if success:
                    instance.is_ready = True
                    self._browsers[cookie_hash] = instance
                    _log_debug(f"[Selenium] Browser ready!")
                    return True
                else:
                    _log_debug(f"[Selenium] Init failed, closing browser")
                    driver.quit()
                    if extension_path:
                        _cleanup_proxy_extension(extension_path)
                    raise RecaptchaSolverError("Failed to initialize browser - grecaptcha not loaded")
                    
            except Exception as e:
                _log_debug(f"[Selenium] Error: {e}")
                raise RecaptchaSolverError(f"Failed to initialize browser: {e}")
    
    async def _close_browser(self, cookie_hash: str, delete_profile: bool = False) -> None:
        """Close a specific browser."""
        if cookie_hash in self._browsers:
            instance = self._browsers[cookie_hash]
            try:
                if instance.driver:
                    instance.driver.quit()
            except:
                pass
            
            # Cleanup proxy auth extension
            instance.cleanup_extension()
            
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

    def _get_token_sync(self, driver: Any, action: str = RECAPTCHA_ACTION) -> Optional[str]:
        """Get reCAPTCHA token synchronously.
        
        Args:
            driver: Selenium WebDriver instance.
            action: reCAPTCHA action (VIDEO_GENERATION or IMAGE_GENERATION).
        """
        try:
            token = driver.execute_async_script("""
                var callback = arguments[arguments.length - 1];
                var siteKey = arguments[0];
                var action = arguments[1];
                
                grecaptcha.enterprise.ready(function() {
                    grecaptcha.enterprise.execute(siteKey, {action: action})
                        .then(function(token) { callback(token); })
                        .catch(function(err) { callback(null); });
                });
            """, RECAPTCHA_SITE_KEY, action)
            
            return token
        except Exception:
            return None
    
    async def get_token(self, cookie: str, proxy: Optional["Proxy"] = None, action: str = RECAPTCHA_ACTION) -> Optional[str]:
        """Get a reCAPTCHA Enterprise token.
        
        Args:
            cookie: Cookie string for authentication.
            proxy: Optional proxy to use.
            action: reCAPTCHA action (VIDEO_GENERATION or IMAGE_GENERATION).
            
        Returns:
            reCAPTCHA token string, or None if failed.
        """
        cookie_hash = self._get_cookie_hash(cookie)
        
        # Ensure browser is initialized
        if cookie_hash not in self._browsers:
            await self.initialize(cookie, proxy)
        
        instance = self._browsers.get(cookie_hash)
        if not instance or not instance.driver:
            raise RecaptchaSolverError("Browser not initialized")
        
        # Check if needs rotation
        if instance.needs_rotation():
            await self._rotate_profile(cookie_hash)
            await self.initialize(cookie, proxy)
            instance = self._browsers.get(cookie_hash)
            if not instance or not instance.driver:
                raise RecaptchaSolverError("Browser not initialized after rotation")
        
        async with self._lock:
            try:
                instance.mark_used()
                
                # Unfreeze page before using (if it was frozen)
                instance.unfreeze_page()
                
                print(f"🔐 [reCAPTCHA] Request {instance.use_count}/{PROFILE_ROTATION_CONFIG['MAX_REQUESTS_PER_PROFILE']} (action: {action})")
                
                # Execute reCAPTCHA in thread pool
                loop = asyncio.get_event_loop()
                token = await loop.run_in_executor(
                    None, lambda: self._get_token_sync(instance.driver, action)
                )
                
                if token and len(token) > 100:
                    instance.reset_403_count()
                    print(f"🔐 [reCAPTCHA] ✅ Token obtained")
                    
                    # Freeze page after getting token to reduce CPU
                    instance.freeze_page()
                    
                    return token
                else:
                    # Token failed, refresh page
                    await loop.run_in_executor(None, instance.driver.refresh)
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
        
        clear_all_profiles()
        clear_proxy_extensions()
    
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
                "is_frozen": instance.is_frozen,
                "needs_rotation": instance.needs_rotation(),
            }
        return stats


# Global solver instance
_solver: Optional[RecaptchaSolver] = None


def get_solver() -> RecaptchaSolver:
    """Get or create the global reCAPTCHA solver instance."""
    global _solver
    if _solver is None:
        _solver = RecaptchaSolver()
    return _solver


async def get_recaptcha_token(cookie: str) -> Optional[str]:
    """Convenience function to get reCAPTCHA token."""
    solver = get_solver()
    return await solver.get_token(cookie)
