import asyncio

from app.logging_config import setup_logging, get_logger
from app.services import GoogleSheetsService, OzonBlockedError
from app.services.parser import OzonParser
from app.services.position_tracker import PositionTracker

logger = get_logger(__name__)


async def main() -> None:
    setup_logging()

    logger.info("Starting Ozon Position Tracker...")

    sheets = GoogleSheetsService()
    sheets.connect()

    try:
        async with OzonParser() as parser:
            tracker = PositionTracker(sheets, parser)
            await tracker.run(max_position=1000)
        logger.info("Done!")
    except OzonBlockedError as e:
        logger.error(f"Парсер остановлен: {e}")


if __name__ == "__main__":
    asyncio.run(main())
