import asyncio
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app.logging_config import setup_logging, get_logger
from app.services import GoogleSheetsService, OzonBlockedError
from app.services.captcha import RuCaptchaSolver, CaptchaSolverError
from app.services.parser import OzonParser
from app.services.position_tracker import PositionTracker
from app.services.telegram import get_telegram_notifier
from app.settings import settings

logger = get_logger(__name__)
telegram = get_telegram_notifier()


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


async def run_tracker() -> None:
    logger.info("Starting Ozon Position Tracker...")
    await telegram.send_message("Запуск трекинга позиций...")

    sheets = GoogleSheetsService()
    sheets.connect()

    captcha_solver = create_captcha_solver()

    try:
        async with OzonParser(captcha_solver=captcha_solver) as parser:
            tracker = PositionTracker(sheets, parser)
            await tracker.run(max_position=1000)

        logger.info("Done!")
        await telegram.send_message("Трекинг позиций завершён")
    except OzonBlockedError as e:
        logger.error(f"Парсер остановлен: {e}")
        await telegram.send_message(f"<b>Парсер остановлен</b>\n{e}")
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        await telegram.send_message(f"<b>Критическая ошибка</b>\n{e}")


def job() -> None:
    """Wrapper to run async tracker from sync scheduler."""
    asyncio.run(run_tracker())


def consolidation_job() -> None:
    """
    Consolidate yesterday's hourly columns into a single daily column.
    Runs at 12:00 daily.
    """
    logger.info("Starting daily consolidation job...")

    sheets = GoogleSheetsService()
    sheets.connect()

    # Create a minimal tracker just for consolidation (no parser needed)
    tracker = PositionTracker(sheets, parser=None)  # type: ignore
    result = tracker.consolidate_yesterday()

    if result:
        logger.info("Daily consolidation completed successfully")
    else:
        logger.info("No consolidation needed")


def consolidate_all_job() -> None:
    """
    Consolidate all dates with >= 3 hourly columns into daily averages.
    Run manually with --consolidate-all flag.
    """
    logger.info("Starting consolidation of all old hourly columns...")

    sheets = GoogleSheetsService()
    sheets.connect()

    tracker = PositionTracker(sheets, parser=None)  # type: ignore
    count = tracker.consolidate_old_hourly_columns(min_columns=3)

    logger.info(f"Consolidation complete: {count} dates processed")


if __name__ == "__main__":
    setup_logging()

    # Run once immediately on start
    if "--once" in sys.argv:
        job()
    elif "--consolidate" in sys.argv:
        # Run yesterday's consolidation manually
        consolidation_job()
    elif "--consolidate-all" in sys.argv:
        # Consolidate all old hourly columns
        consolidate_all_job()
    else:
        logger.info("Starting scheduler (tracking: every 2 hours, consolidation: 12:00)")
        # Run immediately, then at fixed hours
        job()

        scheduler = BlockingScheduler()

        # Position tracking job - every 2 hours (odd hours)
        scheduler.add_job(
            job,
            CronTrigger(hour="1,3,5,7,9,11,13,15,17,19,21,23", minute=0),
            max_instances=1,
            id="position_tracking",
        )

        # Daily consolidation job - at 12:00
        scheduler.add_job(
            consolidation_job,
            CronTrigger(hour=12, minute=0),
            max_instances=1,
            id="daily_consolidation",
        )

        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Scheduler stopped")
