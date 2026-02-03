import asyncio
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from playwright.async_api import Page

from app.logging_config import get_logger
from app.services.parser import OzonParser, OzonBlockedError, OzonPageLoadError
from app.services.sheets import GoogleSheetsService
from app.services.telegram import get_telegram_notifier

logger = get_logger(__name__)


@dataclass
class SearchTask:
    row_index: int  # 1-based row number in sheet
    article: str
    query: str


class PositionTracker:
    WORKSHEET_NAME = "Позиции"

    def __init__(
        self, sheets_service: GoogleSheetsService, parser: OzonParser
    ) -> None:
        self.sheets = sheets_service
        self.parser = parser
        self.telegram = get_telegram_notifier()

    async def _notify_error(
        self, message: str, page: Page | None = None
    ) -> None:
        """Send error notification to Telegram with optional screenshot."""
        if not self.telegram.enabled:
            return

        screenshot: bytes | None = None
        if page:
            try:
                logger.debug("Taking screenshot for error notification...")
                screenshot = await asyncio.wait_for(
                    page.screenshot(full_page=False, timeout=10000),
                    timeout=15
                )
                logger.debug(f"Screenshot taken, size: {len(screenshot)} bytes")
            except asyncio.TimeoutError:
                logger.warning("Screenshot timed out")
            except Exception as e:
                logger.warning(f"Failed to take screenshot: {e}")

        if screenshot:
            logger.debug("Sending screenshot to Telegram...")
            sent = await self.telegram.send_photo(screenshot, f"<b>Ошибка</b>\n{message}")
            if sent:
                logger.debug("Screenshot sent successfully")
                return
            logger.warning("Failed to send screenshot to Telegram, sending text only")

        await self.telegram.send_message(f"<b>Ошибка</b>\n{message}")

    def get_tasks_from_sheet(self) -> list[SearchTask]:
        """Parse the sheet and return list of search tasks."""
        worksheet = self.sheets.get_worksheet(self.WORKSHEET_NAME)
        data = worksheet.get_all_values()

        tasks: list[SearchTask] = []
        current_article: str | None = None

        for row_idx, row in enumerate(data[1:], start=2):  # Skip header, 1-based index
            if len(row) < 3:
                continue

            article_cell = row[0].strip()
            query_cell = row[2].strip()

            # If article cell has value, this is a product header row
            if article_cell:
                current_article = article_cell
                # Skip this row - it contains product name, not a search query
                continue

            # If no article but we have a current_article and a query
            if current_article and query_cell:
                tasks.append(
                    SearchTask(
                        row_index=row_idx,
                        article=current_article,
                        query=query_cell,
                    )
                )

        logger.info(f"Found {len(tasks)} search tasks")
        return tasks

    def get_or_create_hourly_column(self, worksheet) -> int:
        """
        Get or create column for current hour.
        Format: "28.01 14:00"
        Returns 1-based column index.
        """
        now = datetime.now()
        column_name = now.strftime("%d.%m %H:00")
        headers = worksheet.row_values(1)

        # Check if column already exists at position D
        if len(headers) >= 4 and headers[3] == column_name:
            logger.info(f"Column '{column_name}' already exists at position D")
            return 4

        # Insert new column at position D (index 4)
        worksheet.insert_cols([[""]], col=4)
        worksheet.update_cell(1, 4, column_name)
        logger.info(f"Inserted new column '{column_name}' at position D")
        return 4

    def _get_hourly_columns_for_date(self, headers: list[str], date_str: str) -> list[int]:
        """
        Find all hourly columns for a specific date.
        Returns list of 1-based column indices.

        Args:
            headers: List of column headers
            date_str: Date in format "DD.MM" (e.g., "03.02")
        """
        # Pattern: "28.01 14:00" (date with hour)
        pattern = re.compile(rf"^{re.escape(date_str)} \d{{2}}:00$")

        columns = []
        for i, header in enumerate(headers):
            if pattern.match(header):
                columns.append(i + 1)  # 1-based index

        return columns

    def consolidate_daily_results(self, worksheet, date_str: str | None = None) -> bool:
        """
        Consolidate hourly columns into daily average.

        1. Find all hourly columns for the date (e.g., "28.01 14:00", "28.01 15:00")
        2. Calculate average position for each row
        3. Replace all hourly columns with single daily column ("28.01")

        Args:
            worksheet: Google Sheets worksheet
            date_str: Date to consolidate in "DD.MM" format. If None, uses today.

        Returns:
            True if consolidation was performed, False otherwise.
        """
        headers = worksheet.row_values(1)

        if date_str is None:
            date_str = datetime.now().strftime("%d.%m")

        hourly_columns = self._get_hourly_columns_for_date(headers, date_str)

        if len(hourly_columns) < 1:
            logger.info(f"No hourly columns found for {date_str}, skipping consolidation")
            return False

        if len(hourly_columns) == 1:
            # Only one hourly column - just rename it to date without time
            col_idx = hourly_columns[0]
            worksheet.update_cell(1, col_idx, date_str)
            logger.info(f"Renamed single hourly column to '{date_str}'")
            return True

        logger.info(f"Consolidating {len(hourly_columns)} hourly columns for {date_str} into daily average")

        # Get all data
        all_data = worksheet.get_all_values()
        num_rows = len(all_data)

        # Calculate averages for each row
        averages = []
        for row_idx in range(1, num_rows):  # Skip header
            values = []
            for col_idx in hourly_columns:
                if col_idx <= len(all_data[row_idx]):
                    cell_value = all_data[row_idx][col_idx - 1]
                    # Parse value: number or "1000+"
                    if cell_value:
                        if cell_value.endswith("+"):
                            values.append(1000)  # Treat "1000+" as 1000 for average
                        else:
                            try:
                                values.append(int(cell_value))
                            except ValueError:
                                pass

            if values:
                avg = sum(values) / len(values)
                # Format: round to integer, or "1000+" if all were 1000+
                if avg >= 1000:
                    averages.append("1000+")
                else:
                    averages.append(str(round(avg)))
            else:
                averages.append("")

        # Find leftmost hourly column position for inserting the daily column
        insert_position = min(hourly_columns)

        # Delete hourly columns (from right to left to preserve indices)
        hourly_columns_sorted = sorted(hourly_columns, reverse=True)
        for col_idx in hourly_columns_sorted:
            worksheet.delete_columns(col_idx)
            logger.debug(f"Deleted column {col_idx}")

        # Insert new daily column at the position where first hourly column was
        # Prepare column data: header + averages
        column_data = [[date_str]] + [[avg] for avg in averages]
        worksheet.insert_cols(column_data, col=insert_position)

        logger.info(f"Consolidated into daily column '{date_str}' with averages")
        return True

    def consolidate_yesterday(self) -> bool:
        """
        Consolidate yesterday's hourly columns into a single daily column.
        Called by scheduler at 12:00.

        Returns:
            True if consolidation was performed, False otherwise.
        """
        worksheet = self.sheets.get_worksheet(self.WORKSHEET_NAME)
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%d.%m")
        logger.info(f"Running daily consolidation for yesterday ({yesterday})")
        return self.consolidate_daily_results(worksheet, yesterday)

    async def _write_cell_async(self, worksheet, row: int, col: int, value: str) -> None:
        """Write to cell asynchronously without blocking."""
        try:
            # Run blocking gspread call in thread pool to not block event loop
            await asyncio.to_thread(worksheet.update_cell, row, col, value)
        except Exception as e:
            logger.error(f"Failed to write to cell ({row}, {col}): {e}")

    async def run(self, max_position: int = 1000) -> None:
        """Run position tracking for all tasks and save results."""
        worksheet = self.sheets.get_worksheet(self.WORKSHEET_NAME)
        tasks = self.get_tasks_from_sheet()

        if not tasks:
            logger.warning("No tasks found")
            return

        # Get or create column for current hour (e.g., "28.01 14:00")
        col_idx = self.get_or_create_hourly_column(worksheet)

        logger.info(f"Starting position tracking for {len(tasks)} queries (max position: {max_position})")

        # Create a single page for all requests (reuse for performance)
        page = await self.parser._new_page()
        await self.parser._warmup(page)

        # Track pending write tasks
        write_tasks: list[asyncio.Task] = []

        try:
            for i, task in enumerate(tasks, 1):
                # Delay between requests to avoid antibot detection
                if i > 1:
                    await asyncio.sleep(random.uniform(2, 5))

                logger.info(f"[{i}/{len(tasks)}] Article: {task.article}, Query: {task.query}")

                # Reuse the same page for all requests, retry up to 2 times
                position = None
                for attempt in range(3):
                    try:
                        position = await self.parser.find_product_position(
                            query=task.query,
                            target_article=task.article,
                            max_position=max_position,
                            page=page,
                        )
                    except OzonBlockedError:
                        logger.warning("Block detected - restarting browser...")
                        await self._notify_error(
                            f"Блокировка Ozon при поиске '{task.query}'", page
                        )
                        await page.close()
                        await self.parser.restart_browser()
                        page = await self.parser._new_page()
                        await self.parser._warmup(page)
                        await asyncio.sleep(random.uniform(5, 10))
                        continue
                    except OzonPageLoadError as e:
                        logger.warning(f"Page load error (attempt {attempt + 1}/3): {e}")
                        await self._notify_error(
                            f"Ошибка загрузки страницы '{task.query}' (попытка {attempt + 1}/3)", page
                        )
                        await page.close()
                        await self.parser.restart_browser()
                        page = await self.parser._new_page()
                        await self.parser._warmup(page)
                        await asyncio.sleep(random.uniform(5, 10))
                        continue
                    except Exception as e:
                        error_str = str(e)
                        # Handle timeout errors by restarting browser
                        if "ERR_TIMED_OUT" in error_str or "Timeout" in error_str:
                            logger.warning(f"Timeout error, restarting browser (attempt {attempt + 1}/3): {e}")
                            await self._notify_error(
                                f"Таймаут при поиске '{task.query}' (попытка {attempt + 1}/3)", page
                            )
                            await page.close()
                            await self.parser.restart_browser()
                            page = await self.parser._new_page()
                            await self.parser._warmup(page)
                            await asyncio.sleep(random.uniform(5, 10))
                            continue
                        logger.error(f"Error processing query '{task.query}': {e}")
                        await self._notify_error(f"Ошибка при поиске '{task.query}': {e}", page)
                        position = -1
                        break

                    # -1 means page ended prematurely — retry
                    if position == -1 and attempt < 2:
                        logger.warning(f"Incomplete results, retrying (attempt {attempt + 2}/3)...")
                        await asyncio.sleep(random.uniform(3, 6))
                        continue

                    break

                # Prepare result
                if position is not None and position > 0:
                    result = str(position)
                elif position is None:
                    result = f"{max_position}+"  # checked all 1000, not found
                else:
                    result = "—"  # incomplete or error
                logger.info(f"Position: {result}")

                # Write to sheet asynchronously (don't wait for completion)
                write_task = asyncio.create_task(
                    self._write_cell_async(worksheet, task.row_index, col_idx, result)
                )
                write_tasks.append(write_task)
        finally:
            # Close the reused page
            await page.close()

        # Wait for all pending writes to complete
        if write_tasks:
            logger.info(f"Waiting for {len(write_tasks)} sheet writes to complete...")
            await asyncio.gather(*write_tasks, return_exceptions=True)
            logger.info("All writes completed")

        logger.info("Position tracking completed")
