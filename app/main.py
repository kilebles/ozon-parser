import asyncio
import random
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app.logging_config import setup_logging, get_logger
from app.services import GoogleSheetsService, OzonBlockedError
from app.services.parser import OzonParser
from app.services.position_tracker import PositionTracker
from app.services.telegram import get_telegram_notifier
from app.settings import settings

logger = get_logger(__name__)
telegram = get_telegram_notifier()


async def process_spreadsheet(
    spreadsheet_id: str,
    parser: OzonParser,
    max_position: int = 1000,
) -> None:
    """Process a single spreadsheet in its own browser tab."""
    sheets = GoogleSheetsService(spreadsheet_id)
    sheets.connect()

    short_id = spreadsheet_id[:8]
    try:
        tracker = PositionTracker(sheets, parser)
        await tracker.run(max_position=max_position)
    except OzonBlockedError as e:
        logger.error(f"[{short_id}] Blocked: {e}")
        await telegram.send_message(f"<b>[{short_id}] Блокировка</b>\n{e}")
    except Exception as e:
        logger.exception(f"[{short_id}] Error: {e}")
        await telegram.send_message(f"<b>[{short_id}] Ошибка</b>\n{e}")


async def run_tracker() -> None:
    spreadsheet_ids = settings.spreadsheet_ids_list

    if not spreadsheet_ids:
        logger.error("No spreadsheet IDs configured. Set GOOGLE_SPREADSHEET_IDS in .env")
        return

    logger.info(f"Starting tracker for {len(spreadsheet_ids)} spreadsheets")
    await telegram.send_message(f"Запуск трекинга для {len(spreadsheet_ids)} таблиц")

    try:
        async with OzonParser() as parser:
            # Process all spreadsheets in parallel (each in its own tab)
            tasks = []
            for i, spreadsheet_id in enumerate(spreadsheet_ids):
                # Stagger start times slightly
                if i > 0:
                    await asyncio.sleep(random.uniform(1, 2))
                task = asyncio.create_task(process_spreadsheet(spreadsheet_id, parser))
                tasks.append(task)

            await asyncio.gather(*tasks, return_exceptions=True)

        logger.info("Done")
        await telegram.send_message("Трекинг завершён")
    except OzonBlockedError as e:
        logger.error(f"Парсер остановлен: {e}")
        await telegram.send_message(f"<b>Парсер остановлен</b>\n{e}")
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        await telegram.send_message(f"<b>Критическая ошибка</b>\n{e}")


def job() -> None:
    """Wrapper to run async tracker from sync scheduler."""
    asyncio.run(run_tracker())


def daily_summary_job() -> None:
    """
    Create daily summary column with averages for all spreadsheets.
    Runs at 11:00 daily. Creates a new summary column at D with blue background.
    """
    logger.info("Starting daily summary job...")

    spreadsheet_ids = settings.spreadsheet_ids_list
    if not spreadsheet_ids:
        logger.error("No spreadsheet IDs configured")
        return

    for spreadsheet_id in spreadsheet_ids:
        short_id = spreadsheet_id[:8]
        try:
            sheets = GoogleSheetsService(spreadsheet_id)
            sheets.connect()

            tracker = PositionTracker(sheets, parser=None)  # type: ignore
            result = tracker.create_daily_summary()

            if result:
                logger.info(f"[{short_id}] Daily summary created for {sheets.spreadsheet.title}")
            else:
                logger.info(f"[{short_id}] No summary needed for {sheets.spreadsheet.title}")
        except Exception as e:
            logger.error(f"[{short_id}] Failed to create summary: {e}")




if __name__ == "__main__":
    setup_logging()

    # Run once immediately on start
    if "--once" in sys.argv:
        job()
    elif "--summary" in sys.argv:
        # Create daily summary manually
        daily_summary_job()
    else:
        logger.info("Starting scheduler (tracking: every 2 hours, daily summary: 11:00)")
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

        # Daily summary job - at 11:00 (creates summary column before new day's tracking)
        scheduler.add_job(
            daily_summary_job,
            CronTrigger(hour=11, minute=0),
            max_instances=1,
            id="daily_summary",
        )

        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Scheduler stopped")
