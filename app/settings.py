from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    browser_headless: bool | str = "new"  # True, False, or "new" (new headless mode)
    browser_timeout: int = 30000
    base_url: str = "https://www.ozon.ru"
    # Scroll delay settings (milliseconds)
    scroll_networkidle_timeout: int = 5000  # Max wait for new content after scroll
    scroll_fallback_delay: int = 300  # Fallback delay if networkidle times out
    initial_load_networkidle_timeout: int = 3000  # Max wait for initial page load
    initial_load_fallback_delay: int = 500  # Fallback delay if initial networkidle times out

    # Logging level: DEBUG, INFO, WARNING, ERROR
    log_level: str = "INFO"

    google_credentials_path: str = "credentials.json"
    google_spreadsheet_id: str = ""

    # RuCaptcha settings
    ru_captcha_api_key: str = ""
    captcha_solve_timeout: int = 120  # Max seconds to wait for captcha solution

    # Telegram notifications
    bot_token: str = ""

    # Proxy list (comma-separated, format: user:pass@host:port)
    proxy_list: str = ""

    # Parallel tabs for parsing (be careful, high values may trigger ban)
    parallel_tabs: int = 1


settings = Settings()
