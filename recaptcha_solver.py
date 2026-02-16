"""reCAPTCHA Solver - Unified Interface.

This module provides a unified interface for reCAPTCHA token generation.

Strategy:
- Default: Selenium (uses Chrome/Edge available on system)
- Fallback: Playwright only after 3 consecutive Selenium failures

Usage:
    from veononstop.services.recaptcha_solver import get_solver, get_recaptcha_token
    
    # Using pool (recommended)
    solver = get_solver()
    token = await solver.get_token(cookie)
    
    # Or convenience function
    token = await get_recaptcha_token(cookie)
"""

# Re-export from pool module
from .recaptcha_pool import (
    RecaptchaSolverPool,
    RecaptchaSolverPool as RecaptchaSolver,  # Alias for backward compatibility
    get_solver_pool as get_solver,
    get_recaptcha_token,
    ROTATION_CONFIG,
)

# Export error class from Selenium solver (primary)
from .recaptcha_solver_selenium import RecaptchaSolverError

# Export clear_all_profiles from Selenium solver
from .recaptcha_solver_selenium import clear_all_profiles as clear_selenium_profiles

# Try to export from Playwright too
try:
    from .recaptcha_solver_playwright import clear_all_profiles as clear_playwright_profiles
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    clear_playwright_profiles = lambda: 0


def clear_all_profiles() -> int:
    """Clear all reCAPTCHA profiles from both Selenium and Playwright."""
    count = clear_selenium_profiles()
    if PLAYWRIGHT_AVAILABLE:
        count += clear_playwright_profiles()
    return count


__all__ = [
    "RecaptchaSolver",
    "RecaptchaSolverPool",
    "RecaptchaSolverError",
    "get_solver",
    "get_recaptcha_token",
    "clear_all_profiles",
    "ROTATION_CONFIG",
]
