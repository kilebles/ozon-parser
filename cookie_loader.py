"""Cookie Loader - loads cookies from JSON files.

Loads cookies exported from Cookie-Editor browser extension
and converts them to HTTP Cookie header format.
"""

import json
from pathlib import Path
from typing import List, Optional


class CookieLoader:
    """Loads cookies from JSON files (Cookie-Editor export format)."""

    def __init__(self, base_dir: Optional[Path] = None):
        """Initialize cookie service.

        Args:
            base_dir: Base directory for cookie files. Defaults to script directory.
        """
        self._base_dir = base_dir or Path(__file__).parent.parent

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def load_cookies_from_json(self, file_path: Path) -> str:
        """Load cookies from JSON file (Cookie-Editor export format) and convert to string.

        Args:
            file_path: Path to the JSON file with cookies.

        Returns:
            Cookie string in "name=value; name=value" format.
        """
        if not file_path.exists():
            return ""

        with open(file_path, 'r', encoding='utf-8') as f:
            cookies_list = json.load(f)

        cookie_parts = []
        for cookie in cookies_list:
            name = cookie.get("name", "")
            value = cookie.get("value", "")
            if name and value:
                cookie_parts.append(f"{name}={value}")

        return "; ".join(cookie_parts)

    def load_all_cookies(
        self,
        google_file: str = "google_cookies.json",
        labs_file: str = "labs_cookies.json"
    ) -> str:
        """Load cookies from google_cookies.json and labs_cookies.json.

        Args:
            google_file: Filename for Google cookies.
            labs_file: Filename for Labs cookies.

        Returns:
            Combined cookie string.
        """
        google_cookies = self.load_cookies_from_json(self._base_dir / google_file)
        labs_cookies = self.load_cookies_from_json(self._base_dir / labs_file)

        all_cookies = []
        if google_cookies:
            all_cookies.append(google_cookies)
        if labs_cookies:
            all_cookies.append(labs_cookies)

        return "; ".join(all_cookies)

    def get_cookie_names(self, file_path: Path) -> List[str]:
        """Get list of cookie names from a JSON file.

        Args:
            file_path: Path to the JSON file.

        Returns:
            List of cookie names.
        """
        if not file_path.exists():
            return []

        with open(file_path, 'r', encoding='utf-8') as f:
            cookies_list = json.load(f)

        return [c.get("name", "") for c in cookies_list if c.get("name")]


# Singleton instance
_cookie_loader: Optional[CookieLoader] = None


def get_cookie_loader(base_dir: Optional[Path] = None) -> CookieLoader:
    """Get the cookie loader singleton.

    Args:
        base_dir: Base directory for cookie files.

    Returns:
        CookieLoader instance.
    """
    global _cookie_loader
    if _cookie_loader is None:
        _cookie_loader = CookieLoader(base_dir)
    return _cookie_loader


def load_all_cookies(base_dir: Optional[Path] = None) -> str:
    """Convenience function to load all cookies.

    Args:
        base_dir: Base directory for cookie files.

    Returns:
        Combined cookie string.
    """
    loader = get_cookie_loader(base_dir)
    return loader.load_all_cookies()
