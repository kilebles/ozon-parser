import logging
import sys
from datetime import datetime
from pathlib import Path

from app.settings import settings


def setup_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Create logs directory
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    # Log file with timestamp
    log_filename = logs_dir / f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"

    # Format for both console and file
    log_format = "%(asctime)s | %(levelname)-8s | %(message)s"
    date_format = "%H:%M:%S"

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(logging.Formatter(log_format, date_format))

    # File handler - always DEBUG level to capture everything
    file_handler = logging.FileHandler(log_filename, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format, date_format))

    logging.basicConfig(
        level=logging.DEBUG,  # Root level DEBUG, handlers filter
        handlers=[console_handler, file_handler],
        force=True,
    )

    # Suppress noisy loggers
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("gspread").setLevel(logging.WARNING)

    logging.info(f"Logging to {log_filename}")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
