"""
Main entry point using Playwright with enhanced anti-bot features.

Uses playwright-stealth and comprehensive browser fingerprint spoofing
for better anti-detection on servers.
"""
import asyncio
import random
import sys
from pathlib import Path

# Add project root to path for direct execution
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.logging_config import setup_logging, get_logger
from app.services import GoogleSheetsService, OzonBlockedError
from app.services.parser_playwright import OzonParserPlaywright
from app.services.position_tracker import PositionTracker
from app.settings import settings

logger = get_logger(__name__)


async def process_spreadsheet(
    spreadsheet_id: str,
    parser: OzonParserPlaywright,
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
    except Exception as e:
        logger.exception(f"[{short_id}] Error: {e}")


async def run_tracker() -> None:
    spreadsheet_ids = settings.spreadsheet_ids_list

    if not spreadsheet_ids:
        logger.error("No spreadsheet IDs configured. Set GOOGLE_SPREADSHEET_IDS in .env")
        return

    logger.info(f"Starting Playwright tracker for {len(spreadsheet_ids)} spreadsheets")

    try:
        async with OzonParserPlaywright() as parser:
            # Process spreadsheets sequentially to avoid browser conflicts
            for spreadsheet_id in spreadsheet_ids:
                await process_spreadsheet(spreadsheet_id, parser)

        logger.info("Done")
    except OzonBlockedError as e:
        logger.error(f"Парсер остановлен: {e}")
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")


if __name__ == "__main__":
    setup_logging()
    asyncio.run(run_tracker())
