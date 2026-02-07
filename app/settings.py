from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    browser_headless: bool = True
    browser_headless_new: bool = True  # Use new headless mode (less detectable)
    browser_timeout: int = 30000
    base_url: str = "https://www.ozon.ru"

    # Logging level: DEBUG, INFO, WARNING, ERROR
    log_level: str = "INFO"

    google_credentials_path: str = "credentials.json"
    google_spreadsheet_id: str = ""

    # Telegram notifications
    bot_token: str = ""

    # Parallel tabs for parsing (be careful, high values may trigger ban)
    parallel_tabs: int = 1


settings = Settings()
