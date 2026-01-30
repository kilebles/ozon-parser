from pydantic_settings import BaseSettings, SettingsConfigDict


# City name -> (latitude, longitude, timezone)
CITY_COORDINATES: dict[str, tuple[float, float, str]] = {
    "москва": (55.7558, 37.6173, "Europe/Moscow"),
    "санкт-петербург": (59.9343, 30.3351, "Europe/Moscow"),
    "спб": (59.9343, 30.3351, "Europe/Moscow"),
    "новосибирск": (55.0084, 82.9357, "Asia/Novosibirsk"),
    "екатеринбург": (56.8389, 60.6057, "Asia/Yekaterinburg"),
    "казань": (55.7887, 49.1221, "Europe/Moscow"),
    "нижний новгород": (56.2965, 43.9361, "Europe/Moscow"),
    "челябинск": (55.1644, 61.4368, "Asia/Yekaterinburg"),
    "самара": (53.1959, 50.1002, "Europe/Samara"),
    "омск": (54.9885, 73.3242, "Asia/Omsk"),
    "ростов-на-дону": (47.2357, 39.7015, "Europe/Moscow"),
    "уфа": (54.7388, 55.9721, "Asia/Yekaterinburg"),
    "красноярск": (56.0153, 92.8932, "Asia/Krasnoyarsk"),
    "воронеж": (51.6720, 39.1843, "Europe/Moscow"),
    "пермь": (58.0105, 56.2502, "Asia/Yekaterinburg"),
    "волгоград": (48.7080, 44.5133, "Europe/Volgograd"),
    "краснодар": (45.0355, 38.9753, "Europe/Moscow"),
    "сочи": (43.5855, 39.7231, "Europe/Moscow"),
    "владивосток": (43.1155, 131.8855, "Asia/Vladivostok"),
    "хабаровск": (48.4827, 135.0838, "Asia/Vladivostok"),
    "иркутск": (52.2978, 104.2964, "Asia/Irkutsk"),
    "тюмень": (57.1522, 65.5272, "Asia/Yekaterinburg"),
    "тольятти": (53.5078, 49.4204, "Europe/Samara"),
    "барнаул": (53.3548, 83.7698, "Asia/Barnaul"),
    "ижевск": (56.8527, 53.2114, "Europe/Samara"),
    "ульяновск": (54.3142, 48.4031, "Europe/Samara"),
    "ярославль": (57.6261, 39.8845, "Europe/Moscow"),
    "томск": (56.4846, 84.9476, "Asia/Tomsk"),
    "калининград": (54.7104, 20.4522, "Europe/Kaliningrad"),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    browser_headless: bool = True
    browser_timeout: int = 30000
    base_url: str = "https://www.ozon.ru"
    browser_state_path: str = "browser_state.json"

    # Scroll delay settings (milliseconds)
    scroll_networkidle_timeout: int = 2000  # Max wait for network to settle after scroll
    scroll_fallback_delay: int = 300  # Fallback delay if networkidle times out
    initial_load_networkidle_timeout: int = 3000  # Max wait for initial page load
    initial_load_fallback_delay: int = 500  # Fallback delay if initial networkidle times out

    # City name for geolocation (default: Москва)
    geo_city: str = "Москва"

    # Logging level: DEBUG, INFO, WARNING, ERROR
    log_level: str = "INFO"

    google_credentials_path: str = "credentials.json"
    google_spreadsheet_id: str = ""

    # Proxy settings (format: http://user:pass@host:port or socks5://host:port)
    proxy_url: str = ""

    # RuCaptcha settings
    ru_captcha_api_key: str = ""
    captcha_solve_timeout: int = 120  # Max seconds to wait for captcha solution

    @property
    def geo_coordinates(self) -> tuple[float, float, str]:
        """Returns (latitude, longitude, timezone) for the configured city."""
        city_key = self.geo_city.lower().strip()
        if city_key in CITY_COORDINATES:
            return CITY_COORDINATES[city_key]
        raise ValueError(
            f"Unknown city: {self.geo_city}. "
            f"Available cities: {', '.join(sorted(CITY_COORDINATES.keys()))}"
        )

    @property
    def geo_latitude(self) -> float:
        return self.geo_coordinates[0]

    @property
    def geo_longitude(self) -> float:
        return self.geo_coordinates[1]

    @property
    def geo_timezone(self) -> str:
        return self.geo_coordinates[2]


settings = Settings()
