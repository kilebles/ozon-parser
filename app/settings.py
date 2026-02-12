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
    google_spreadsheet_ids: str = ""  # Comma-separated list of spreadsheet IDs

    # Telegram notifications
    bot_token: str = ""

    @property
    def spreadsheet_ids_list(self) -> list[str]:
        """Parse comma-separated spreadsheet IDs into a list."""
        if not self.google_spreadsheet_ids:
            return []
        return [sid.strip() for sid in self.google_spreadsheet_ids.split(",") if sid.strip()]


settings = Settings()
