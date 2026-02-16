"""reCAPTCHA Solver Pool - Selenium Only with Playwright Fallback.

This module provides a unified interface for reCAPTCHA token generation.

Strategy:
- macOS: Playwright primary (Selenium has issues on macOS)
- Windows/Linux: Selenium primary (uses Chrome/Edge available on system)
- Fallback: Switch to other solver after 3 consecutive failures

Features:
- Per-account tracking
- Automatic fallback after failures
- Unified API
- Proxy support with automatic rotation
- Cloudflare WARP integration for IP rotation
"""

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, Optional, Any, TYPE_CHECKING

# Import Selenium solver
from .recaptcha_solver_selenium import RecaptchaSolver as SeleniumSolver
from .recaptcha_solver_selenium import RecaptchaSolverError

# Import proxy manager
from .proxy_manager import ProxyManager, Proxy, get_proxy_manager

# Import WARP manager
from .warp_manager import WarpManager, get_warp_manager, WarpStatus

# Try to import Playwright solver
try:
    from .recaptcha_solver_playwright import RecaptchaSolver as PlaywrightSolver
    from .recaptcha_solver_playwright import check_browser_installed, ensure_browser_installed
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    PlaywrightSolver = None
    check_browser_installed = None
    ensure_browser_installed = None


class SolverType(Enum):
    """Solver type enum."""
    SELENIUM = "selenium"
    PLAYWRIGHT = "playwright"


# Determine default solver based on platform
# macOS/Linux: Playwright (auto-downloads browsers, no system Chrome needed)
# Windows: Selenium (Chrome/Edge usually available)
if sys.platform in ("darwin", "linux") and PLAYWRIGHT_AVAILABLE:
    DEFAULT_SOLVER = SolverType.PLAYWRIGHT
else:
    DEFAULT_SOLVER = SolverType.SELENIUM


# Config
ROTATION_CONFIG = {
    "MAX_REQUESTS_BEFORE_ROTATE": 50,  # Rotate profile after 50 requests
    "MAX_FAILURES_BEFORE_FALLBACK": 3,  # Fallback after 3 consecutive failures
}


@dataclass
class AccountSolverState:
    """Track solver state for each account."""
    cookie_hash: str
    current_solver: SolverType = field(default_factory=lambda: DEFAULT_SOLVER)
    consecutive_failures: int = 0
    total_requests: int = 0
    total_failures: int = 0
    last_success: Optional[datetime] = None
    fallback_activated: bool = False
    
    def record_success(self):
        """Record a successful request."""
        self.consecutive_failures = 0
        self.total_requests += 1
        self.last_success = datetime.now()
    
    def record_failure(self) -> bool:
        """Record a failed request.
        
        Returns:
            True if should fallback to other solver
        """
        self.consecutive_failures += 1
        self.total_failures += 1
        self.total_requests += 1
        
        # Check if should fallback
        if self.consecutive_failures >= ROTATION_CONFIG["MAX_FAILURES_BEFORE_FALLBACK"] and not self.fallback_activated:
            return True
        
        return False
    
    def activate_fallback(self):
        """Activate fallback solver."""
        # Switch to the other solver
        if self.current_solver == SolverType.SELENIUM:
            self.current_solver = SolverType.PLAYWRIGHT
        else:
            self.current_solver = SolverType.SELENIUM
        self.fallback_activated = True
        self.consecutive_failures = 0


class RecaptchaSolverPool:
    """reCAPTCHA Solver Pool - Platform-aware solver selection.
    
    Strategy:
    - macOS: Playwright primary (Selenium has timeout issues)
    - Windows/Linux: Selenium primary (better compatibility)
    - Fallback to other solver after 3 consecutive failures
    
    Usage:
        pool = RecaptchaSolverPool()
        token = await pool.get_token(cookie)
    """
    
    def __init__(self, proxy_manager: Optional[ProxyManager] = None):
        """Initialize the solver pool.
        
        Args:
            proxy_manager: Optional ProxyManager for proxy support.
                          If None, uses global proxy manager.
        """
        self._selenium_solver: Optional[SeleniumSolver] = None
        self._playwright_solver: Optional[PlaywrightSolver] = None
        self._account_states: Dict[str, AccountSolverState] = {}
        self._lock = asyncio.Lock()
        self._proxy_manager = proxy_manager
        self._warp_manager = get_warp_manager()
        self._warp_failure_count = 0
        self._warp_request_count = 0  # Track total WARP requests for rotation
        self._max_warp_failures = 3
        
        solver_name = "Playwright" if DEFAULT_SOLVER == SolverType.PLAYWRIGHT else "Selenium"
        print(f"🔐 [RecaptchaPool] Initialized ({solver_name} primary on {sys.platform})")
    
    @property
    def proxy_manager(self) -> ProxyManager:
        """Get proxy manager (global if not set)."""
        if self._proxy_manager is None:
            self._proxy_manager = get_proxy_manager()
        return self._proxy_manager
    
    def set_proxy_manager(self, proxy_manager: ProxyManager) -> None:
        """Set the proxy manager.
        
        Args:
            proxy_manager: ProxyManager instance to use.
        """
        self._proxy_manager = proxy_manager
    
    def _get_selenium_solver(self) -> SeleniumSolver:
        """Get or create Selenium solver."""
        if self._selenium_solver is None:
            self._selenium_solver = SeleniumSolver(clear_profiles_on_init=False)
        return self._selenium_solver
    
    def _get_playwright_solver(self) -> Optional[PlaywrightSolver]:
        """Get or create Playwright solver (if available)."""
        if not PLAYWRIGHT_AVAILABLE:
            return None
        
        if self._playwright_solver is None:
            try:
                self._playwright_solver = PlaywrightSolver(
                    browser_engine="webkit",
                    clear_profiles_on_init=False
                )
            except Exception as e:
                print(f"⚠️ [RecaptchaPool] Playwright not available: {e}")
                return None
        
        return self._playwright_solver
    
    def _get_cookie_hash(self, cookie: str) -> str:
        """Generate cookie hash for tracking."""
        import hashlib
        import json
        
        if isinstance(cookie, list):
            cookie_str = json.dumps(cookie, sort_keys=True)
        else:
            cookie_str = str(cookie)
        
        return hashlib.sha256(cookie_str.encode()).hexdigest()[:16]
    
    def _get_account_state(self, cookie: str) -> AccountSolverState:
        """Get or create account state."""
        cookie_hash = self._get_cookie_hash(cookie)
        
        if cookie_hash not in self._account_states:
            self._account_states[cookie_hash] = AccountSolverState(cookie_hash=cookie_hash)
        
        return self._account_states[cookie_hash]
    
    async def get_token(self, cookie: str, action: str = "VIDEO_GENERATION") -> Optional[str]:
        """Get reCAPTCHA token.
        
        Strategy:
        1. If WARP is enabled, use WARP proxy
        2. Otherwise, use proxy from ProxyManager
        3. Try Selenium first (default)
        4. If Selenium fails 3 times consecutively, fallback to Playwright
        5. Once Playwright is activated, use it for this account
        
        Args:
            cookie: Cookie string for the account
            action: reCAPTCHA action (VIDEO_GENERATION or IMAGE_GENERATION)
            
        Returns:
            reCAPTCHA token string, or None if failed
        """
        state = self._get_account_state(cookie)
        
        # Get proxy - prioritize WARP if enabled
        proxy = await self._get_proxy()
        
        # Determine which solver to use based on state
        if state.current_solver == SolverType.PLAYWRIGHT and PLAYWRIGHT_AVAILABLE:
            return await self._get_token_playwright(cookie, state, proxy, action)
        else:
            return await self._get_token_selenium(cookie, state, proxy, action)
    
    async def initialize_browser(self, cookie: str) -> bool:
        """Initialize browser for an account without getting token.
        
        This is useful for pre-warming browsers before batch processing.
        The browser will be ready to get tokens quickly.
        
        Args:
            cookie: Cookie string for the account
            
        Returns:
            True if browser initialized successfully
        """
        state = self._get_account_state(cookie)
        proxy = await self._get_proxy()
        
        # Use solver based on platform default
        if state.current_solver == SolverType.PLAYWRIGHT and PLAYWRIGHT_AVAILABLE:
            solver = self._get_playwright_solver()
            solver_name = "Playwright"
            try:
                # Playwright initialize doesn't take proxy
                success = await solver.initialize(cookie)
                if success:
                    # Log disabled - contains cookie hash
                    # print(f"✅ [RecaptchaPool] Browser pre-warmed ({solver_name}) for {state.cookie_hash[:8]}...")
                    pass
                return success
            except Exception as e:
                print(f"❌ [RecaptchaPool] {solver_name} failed: {str(e)[:50]}")
                return False
        else:
            solver = self._get_selenium_solver()
            solver_name = "Selenium"
            try:
                success = await solver.initialize(cookie, proxy)
                if success:
                    # Log disabled - contains cookie hash
                    # print(f"✅ [RecaptchaPool] Browser pre-warmed ({solver_name}) for {state.cookie_hash[:8]}...")
                    pass
                return success
            except Exception as e:
                print(f"❌ [RecaptchaPool] {solver_name} failed: {str(e)[:50]}")
                return False
    
    async def _get_proxy(self) -> Optional[Proxy]:
        """Get proxy to use for reCAPTCHA solving.
        
        Priority:
        1. WARP proxy if enabled and ready
        2. Proxy from ProxyManager
        3. None (direct connection)
        
        Returns:
            Proxy object or None
        """
        # Check WARP first
        if self._warp_manager.is_enabled:
            # Ensure WARP is ready
            if await self._warp_manager.ensure_ready_async():
                warp_proxy = await self._warp_manager.get_proxy_async()
                if warp_proxy:
                    return warp_proxy
            else:
                print(f"⚠️ [RecaptchaPool] WARP not ready, falling back to proxy manager")
        
        # Fallback to proxy manager
        return self.proxy_manager.get_next_proxy()
    
    async def _get_token_selenium(self, cookie: str, state: AccountSolverState, proxy: Optional[Proxy] = None, action: str = "VIDEO_GENERATION") -> Optional[str]:
        """Get token using Selenium.
        
        Args:
            cookie: Cookie string for the account.
            state: AccountSolverState for this cookie.
            proxy: Optional proxy to use.
            action: reCAPTCHA action (VIDEO_GENERATION or IMAGE_GENERATION).
        """
        solver = self._get_selenium_solver()
        
        # Check if using WARP proxy
        is_warp_proxy = (
            proxy is not None and 
            proxy.host == self._warp_manager.PROXY_HOST and 
            proxy.port == self._warp_manager.PROXY_PORT
        )
        
        proxy_info = ""
        if proxy:
            if is_warp_proxy:
                proxy_info = " via WARP"
            else:
                proxy_info = f" via {proxy.host}:{proxy.port}"
        
        # Log disabled - contains cookie hash
        # print(f"🔐 [RecaptchaPool] Using Selenium for {state.cookie_hash[:8]}...{proxy_info} (failures: {state.consecutive_failures}/{ROTATION_CONFIG['MAX_FAILURES_BEFORE_FALLBACK']})")
        
        try:
            token = await solver.get_token(cookie, proxy, action)
            
            if token and len(token) > 100:
                async with self._lock:
                    state.record_success()
                    # Reset WARP failure count on success
                    if is_warp_proxy:
                        self._warp_failure_count = 0
                        self._warp_request_count += 1
                        
                        # Rotate WARP IP after MAX_REQUESTS_BEFORE_ROTATE requests
                        if self._warp_request_count >= ROTATION_CONFIG["MAX_REQUESTS_BEFORE_ROTATE"]:
                            print(f"🔄 [RecaptchaPool] WARP reached {self._warp_request_count} requests, rotating IP...")
                            await self._warp_manager.rotate_ip_async()
                            self._warp_request_count = 0
                
                # Record success to proxy manager (only for non-WARP proxies)
                if proxy and not is_warp_proxy:
                    self.proxy_manager.record_success(proxy)
                
                print(f"✅ [RecaptchaPool] Selenium success")
                return token
            else:
                raise RecaptchaSolverError("Invalid token from Selenium")
                
        except Exception as e:
            error_msg = str(e)[:100]
            print(f"❌ [RecaptchaPool] Selenium failed: {error_msg}")
            
            # Handle WARP failure - rotate IP if too many failures
            if is_warp_proxy:
                async with self._lock:
                    self._warp_failure_count += 1
                    if self._warp_failure_count >= self._max_warp_failures:
                        print(f"🔄 [RecaptchaPool] WARP failures reached {self._warp_failure_count}, rotating IP...")
                        await self._warp_manager.rotate_ip_async()
                        self._warp_failure_count = 0
            elif proxy:
                # Record failure to proxy manager (triggers rotation)
                self.proxy_manager.record_failure(proxy)
            
            async with self._lock:
                should_fallback = state.record_failure()
            
            if should_fallback and PLAYWRIGHT_AVAILABLE:
                # Log disabled - contains cookie hash
                # print(f"🔄 [RecaptchaPool] Activating Playwright fallback for {state.cookie_hash[:8]}...")
                async with self._lock:
                    state.activate_fallback()
                
                # Try Playwright immediately with new proxy
                new_proxy = await self._get_proxy()
                return await self._get_token_playwright(cookie, state, new_proxy, action)
            
            return None
    
    async def _get_token_playwright(self, cookie: str, state: AccountSolverState, proxy: Optional[Proxy] = None, action: str = "VIDEO_GENERATION") -> Optional[str]:
        """Get token using Playwright (fallback).
        
        Args:
            cookie: Cookie string for the account.
            state: AccountSolverState for this cookie.
            proxy: Optional proxy to use.
            action: reCAPTCHA action (VIDEO_GENERATION or IMAGE_GENERATION).
        """
        solver = self._get_playwright_solver()
        
        if solver is None:
            print(f"❌ [RecaptchaPool] Playwright not available, cannot fallback")
            return None
        
        proxy_info = f" via {proxy.host}:{proxy.port}" if proxy else ""
        # Log disabled - contains cookie hash
        # print(f"🔐 [RecaptchaPool] Using Playwright (fallback) for {state.cookie_hash[:8]}...{proxy_info}")
        
        try:
            # Note: Playwright solver may need to be updated to support proxy
            token = await solver.get_token(cookie, action)
            
            if token and len(token) > 100:
                async with self._lock:
                    state.record_success()
                    # Track WARP requests for rotation (even via Playwright)
                    if self._warp_manager.is_enabled:
                        self._warp_request_count += 1
                        if self._warp_request_count >= ROTATION_CONFIG["MAX_REQUESTS_BEFORE_ROTATE"]:
                            print(f"🔄 [RecaptchaPool] WARP reached {self._warp_request_count} requests, rotating IP...")
                            await self._warp_manager.rotate_ip_async()
                            self._warp_request_count = 0
                
                # Record success to proxy manager
                if proxy:
                    self.proxy_manager.record_success(proxy)
                
                print(f"✅ [RecaptchaPool] Playwright success")
                return token
            else:
                raise RecaptchaSolverError("Invalid token from Playwright")
                
        except Exception as e:
            error_msg = str(e)[:100]
            print(f"❌ [RecaptchaPool] Playwright failed: {error_msg}")
            
            # Handle WARP failure - rotate IP if too many failures (same as Selenium)
            if self._warp_manager.is_enabled:
                async with self._lock:
                    self._warp_failure_count += 1
                    if self._warp_failure_count >= self._max_warp_failures:
                        print(f"🔄 [RecaptchaPool] WARP failures reached {self._warp_failure_count}, rotating IP...")
                        await self._warp_manager.rotate_ip_async()
                        self._warp_failure_count = 0
            
            # Record failure to proxy manager
            if proxy:
                self.proxy_manager.record_failure(proxy)
            
            async with self._lock:
                state.record_failure()
            
            return None
    
    async def record_403_error(self, cookie: str) -> bool:
        """Record a 403 error for the account.
        
        Args:
            cookie: Cookie string for the account
            
        Returns:
            True if fallback was activated
        """
        state = self._get_account_state(cookie)
        
        async with self._lock:
            should_fallback = state.record_failure()
        
        # Also notify the current solver
        if state.current_solver == SolverType.SELENIUM:
            solver = self._get_selenium_solver()
            await solver.record_403_error(cookie)
        elif state.fallback_activated:
            solver = self._get_playwright_solver()
            if solver:
                await solver.record_403_error(cookie)
        
        if should_fallback and PLAYWRIGHT_AVAILABLE:
            async with self._lock:
                state.activate_fallback()
            # Log disabled - contains cookie hash
            # print(f"🔄 [RecaptchaPool] 403 triggered Playwright fallback for {state.cookie_hash[:8]}...")
            return True
        
        return False
    
    async def cleanup(self, cookie: str) -> None:
        """Cleanup resources for a specific cookie."""
        cookie_hash = self._get_cookie_hash(cookie)
        
        # Cleanup from both solvers
        if self._selenium_solver:
            await self._selenium_solver.cleanup(cookie)
        if self._playwright_solver:
            await self._playwright_solver.cleanup(cookie)
        
        # Remove state
        async with self._lock:
            if cookie_hash in self._account_states:
                del self._account_states[cookie_hash]
    
    async def cleanup_all(self) -> None:
        """Cleanup all resources."""
        if self._selenium_solver:
            await self._selenium_solver.cleanup_all()
            self._selenium_solver = None
        
        if self._playwright_solver:
            await self._playwright_solver.cleanup_all()
            self._playwright_solver = None
        
        async with self._lock:
            self._account_states.clear()
        
        print("🧹 [RecaptchaPool] All resources cleaned up")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get pool statistics."""
        # Check browser installation status
        browser_status = self.check_browser_ready()
        
        stats = {
            "selenium_active": self._selenium_solver is not None,
            "playwright_active": self._playwright_solver is not None,
            "playwright_available": PLAYWRIGHT_AVAILABLE,
            "browser_ready": browser_status[0],
            "browser_status": browser_status[1],
            "accounts": {},
            "proxy_stats": self.proxy_manager.get_stats() if self._proxy_manager else None,
            "warp": {
                "installed": self._warp_manager.is_installed,
                "enabled": self._warp_manager.is_enabled,
                "failure_count": self._warp_failure_count,
                "request_count": self._warp_request_count,
                "max_requests_before_rotate": ROTATION_CONFIG["MAX_REQUESTS_BEFORE_ROTATE"],
            },
        }
        
        # Get WARP status if installed
        if self._warp_manager.is_installed:
            warp_info = self._warp_manager.get_status()
            stats["warp"]["status"] = warp_info.status.value
            stats["warp"]["mode"] = warp_info.mode.value if warp_info.mode else None
            stats["warp"]["is_ready"] = warp_info.is_ready
        
        for cookie_hash, state in self._account_states.items():
            stats["accounts"][cookie_hash[:8]] = {
                "current_solver": state.current_solver.value,
                "consecutive_failures": state.consecutive_failures,
                "total_requests": state.total_requests,
                "total_failures": state.total_failures,
                "fallback_activated": state.fallback_activated,
                "last_success": state.last_success.isoformat() if state.last_success else None,
            }
        
        if self._selenium_solver:
            stats["selenium_instances"] = self._selenium_solver.get_instance_count()
        if self._playwright_solver:
            stats["playwright_instances"] = self._playwright_solver.get_instance_count()
        
        return stats
    
    def check_browser_ready(self) -> tuple:
        """Check if browser solver is ready.
        
        Returns:
            tuple: (is_ready: bool, status_message: str)
        """
        # Check based on default solver
        if DEFAULT_SOLVER == SolverType.PLAYWRIGHT:
            if not PLAYWRIGHT_AVAILABLE:
                return (False, "Playwright not installed")
            if check_browser_installed is None:
                return (False, "Browser check function not available")
            
            is_installed, version, error = check_browser_installed("webkit")
            if is_installed:
                return (True, f"WebKit ready ({version})")
            else:
                return (False, error or "WebKit not installed")
        else:
            # Selenium - check Chrome driver
            return (True, "Selenium ready")
    
    # WARP management methods
    
    @property
    def warp_manager(self) -> WarpManager:
        """Get WARP manager instance."""
        return self._warp_manager
    
    def set_warp_enabled(self, enabled: bool) -> None:
        """Enable or disable WARP usage.
        
        Args:
            enabled: True to enable WARP, False to disable.
        """
        self._warp_manager.is_enabled = enabled
    
    def is_warp_enabled(self) -> bool:
        """Check if WARP is enabled."""
        return self._warp_manager.is_enabled
    
    def is_warp_installed(self) -> bool:
        """Check if WARP is installed."""
        return self._warp_manager.is_installed
    
    async def ensure_warp_ready(self) -> bool:
        """Ensure WARP is ready to use.
        
        Returns:
            True if WARP is ready.
        """
        if not self._warp_manager.is_enabled:
            return False
        return await self._warp_manager.ensure_ready_async()
    
    async def rotate_warp_ip(self) -> bool:
        """Manually rotate WARP IP.
        
        Returns:
            True if rotation successful.
        """
        if not self._warp_manager.is_installed:
            return False
        
        result = await self._warp_manager.rotate_ip_async()
        if result:
            self._warp_failure_count = 0
        return result
    
    def get_warp_status(self) -> Dict[str, Any]:
        """Get WARP status information.
        
        Returns:
            Dict with WARP status.
        """
        if not self._warp_manager.is_installed:
            return {
                "installed": False,
                "enabled": False,
                "status": "not_installed",
            }
        
        info = self._warp_manager.get_status()
        return {
            "installed": True,
            "enabled": self._warp_manager.is_enabled,
            "status": info.status.value,
            "mode": info.mode.value if info.mode else None,
            "is_ready": info.is_ready,
            "proxy_url": self._warp_manager.get_proxy_url() if info.is_ready else None,
            "failure_count": self._warp_failure_count,
        }
    
    def get_instance_count(self) -> int:
        """Get total number of active browser instances."""
        count = 0
        if self._selenium_solver:
            count += self._selenium_solver.get_instance_count()
        if self._playwright_solver:
            count += self._playwright_solver.get_instance_count()
        return count


# Global pool instance
_pool: Optional[RecaptchaSolverPool] = None


def get_solver_pool() -> RecaptchaSolverPool:
    """Get or create the global solver pool instance."""
    global _pool
    if _pool is None:
        _pool = RecaptchaSolverPool()
    return _pool


async def get_recaptcha_token(cookie: str, action: str = "VIDEO_GENERATION") -> Optional[str]:
    """Convenience function to get reCAPTCHA token using the pool.
    
    Args:
        cookie: Cookie string for the account.
        action: reCAPTCHA action (VIDEO_GENERATION or IMAGE_GENERATION).
    """
    pool = get_solver_pool()
    return await pool.get_token(cookie, action)


# Aliases for backward compatibility
RecaptchaSolver = RecaptchaSolverPool
get_solver = get_solver_pool
