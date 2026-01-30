import asyncio
import fcntl
import sys
from datetime import datetime
from pathlib import Path

from app.logging_config import setup_logging, get_logger
from app.services import GoogleSheetsService, OzonBlockedError
from app.services.captcha import RuCaptchaSolver, CaptchaSolverError
from app.services.parser import OzonParser
from app.services.position_tracker import PositionTracker
from app.settings import settings

logger = get_logger(__name__)

LOCK_FILE = Path("/tmp/ozon-parser.lock")


def create_captcha_solver() -> RuCaptchaSolver | None:
    """Create captcha solver if API key is configured."""
    if not settings.ru_captcha_api_key:
        logger.info("RuCaptcha API key not configured, captcha solving disabled")
        return None

    try:
        solver = RuCaptchaSolver()
        logger.info("RuCaptcha solver initialized")
        return solver
    except CaptchaSolverError as e:
        logger.warning(f"Failed to initialize captcha solver: {e}")
        return None


async def main() -> None:
    setup_logging()

    logger.info("Starting Ozon Position Tracker...")

    sheets = GoogleSheetsService()
    sheets.connect()

    captcha_solver = create_captcha_solver()

    try:
        async with OzonParser(captcha_solver=captcha_solver) as parser:
            tracker = PositionTracker(sheets, parser)
            await tracker.run(max_position=1000)

        # After 23:00 - consolidate hourly data into daily average
        current_hour = datetime.now().hour
        if current_hour == 23:
            logger.info("End of day - consolidating hourly results into daily average...")
            worksheet = sheets.get_worksheet(tracker.WORKSHEET_NAME)
            tracker.consolidate_daily_results(worksheet)

        logger.info("Done!")
    except OzonBlockedError as e:
        logger.error(f"Парсер остановлен: {e}")


if __name__ == "__main__":
    lock_fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another instance is already running, exiting.")
        sys.exit(0)

    try:
        asyncio.run(main())
    finally:
        fcntl.flock(lock_fp, fcntl.LOCK_UN)
        lock_fp.close()
