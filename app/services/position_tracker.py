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
from app.settings import settings

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

    async def _take_screenshot(self, page: Page) -> bytes | None:
        """Take screenshot with fallback methods."""
        # Method 1: standard screenshot with disabled animations
        try:
            return await page.screenshot(
                timeout=3000,
                animations="disabled",
            )
        except Exception as e:
            logger.debug(f"Standard screenshot failed: {e}")

        # Method 2: CDP screenshot (bypasses some issues)
        try:
            cdp = await page.context.new_cdp_session(page)
            result = await cdp.send("Page.captureScreenshot", {"format": "png"})
            await cdp.detach()
            import base64
            return base64.b64decode(result["data"])
        except Exception as e:
            logger.debug(f"CDP screenshot failed: {e}")

        return None

    async def _notify_error(
        self, message: str, page: Page | None = None
    ) -> None:
        """Send error notification to Telegram with optional screenshot."""
        if not self.telegram.enabled:
            return

        screenshot: bytes | None = None
        if page:
            logger.debug("Taking screenshot for error notification...")
            screenshot = await self._take_screenshot(page)
            if screenshot:
                logger.debug(f"Screenshot taken, size: {len(screenshot)} bytes")
            else:
                logger.warning("All screenshot methods failed")

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
        # Pattern: "28.01 14:00" or "28.01 6:00" (date with hour, 1 or 2 digits)
        pattern = re.compile(rf"^{re.escape(date_str)} \d{{1,2}}:\d{{2}}$")

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

        # Find leftmost hourly column position
        insert_position = min(hourly_columns)

        # First, update the leftmost column with averages (reuse it as daily column)
        # Update header
        worksheet.update_cell(1, insert_position, date_str)

        # Update values in batch
        if averages:
            cells_to_update = []
            for row_idx, avg in enumerate(averages, start=2):  # Start from row 2
                cells_to_update.append({
                    'range': f'{self._col_letter(insert_position)}{row_idx}',
                    'values': [[avg]]
                })

            # Batch update in chunks to avoid API limits
            for i in range(0, len(cells_to_update), 100):
                chunk = cells_to_update[i:i+100]
                worksheet.batch_update(chunk)

        # Delete other hourly columns (from right to left, skip the one we kept)
        hourly_columns_sorted = sorted(hourly_columns, reverse=True)
        for col_idx in hourly_columns_sorted:
            if col_idx != insert_position:
                worksheet.delete_columns(col_idx)
                logger.debug(f"Deleted column {col_idx}")

        logger.info(f"Consolidated into daily column '{date_str}' with averages")
        return True

    @staticmethod
    def _col_letter(col_num: int) -> str:
        """Convert column number (1-based) to letter (A, B, ..., Z, AA, AB, ...)."""
        result = ""
        while col_num > 0:
            col_num, remainder = divmod(col_num - 1, 26)
            result = chr(65 + remainder) + result
        return result

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

    def consolidate_old_hourly_columns(self, min_columns: int = 3) -> int:
        """
        Find and consolidate all dates that have more than min_columns hourly columns.
        Called at startup to clean up old data.

        Args:
            min_columns: Minimum hourly columns to trigger consolidation (default 3)

        Returns:
            Number of dates consolidated.
        """
        worksheet = self.sheets.get_worksheet(self.WORKSHEET_NAME)
        headers = worksheet.row_values(1)

        # Find all unique dates with hourly columns
        # Pattern: "DD.MM HH:MM" (1 or 2 digit hour)
        hourly_pattern = re.compile(r"^(\d{2}\.\d{2}) \d{1,2}:\d{2}$")
        dates_with_hours: dict[str, int] = {}

        for header in headers:
            match = hourly_pattern.match(header)
            if match:
                date_str = match.group(1)
                dates_with_hours[date_str] = dates_with_hours.get(date_str, 0) + 1

        # Filter dates with enough hourly columns
        dates_to_consolidate = [
            date_str for date_str, count in dates_with_hours.items()
            if count >= min_columns
        ]

        if not dates_to_consolidate:
            logger.info("No dates with enough hourly columns to consolidate")
            return 0

        logger.info(f"Found {len(dates_to_consolidate)} dates to consolidate: {dates_to_consolidate}")

        consolidated = 0
        for date_str in dates_to_consolidate:
            # Re-fetch worksheet to get updated headers after each consolidation
            worksheet = self.sheets.get_worksheet(self.WORKSHEET_NAME)
            if self.consolidate_daily_results(worksheet, date_str):
                consolidated += 1

        logger.info(f"Consolidated {consolidated} dates at startup")
        return consolidated

    async def _write_cell_async(
        self, worksheet, row: int, col: int, value: str, is_found: bool = False
    ) -> None:
        """Write to cell asynchronously without blocking. Green background if found."""
        try:
            await asyncio.to_thread(worksheet.update_cell, row, col, value)

            # Set green background if position found (< 1000)
            if is_found:
                cell_label = f"{self._col_letter(col)}{row}"
                await asyncio.to_thread(
                    worksheet.format,
                    cell_label,
                    {"backgroundColor": {"red": 0.7, "green": 1.0, "blue": 0.7}}
                )
        except Exception as e:
            logger.error(f"Failed to write to cell ({row}, {col}): {e}")

    async def _safe_close_page(self, page: Page) -> None:
        """Safely close page, ignoring errors if already closed."""
        try:
            await page.close()
        except Exception:
            pass

    async def _get_fresh_page(self, worker_id: int) -> Page:
        """Get a fresh page, handling browser restarts if needed."""
        try:
            page = await self.parser._new_page()
            await self.parser._warmup(page)
            return page
        except Exception as e:
            logger.warning(f"[Worker {worker_id}] Failed to create page, restarting browser: {e}")
            await self.parser.restart_browser()
            page = await self.parser._new_page()
            await self.parser._warmup(page)
            return page

    async def _process_single_task(
        self,
        task: SearchTask,
        task_num: int,
        total_tasks: int,
        max_position: int,
        page: Page,
        worker_id: int,
    ) -> tuple[SearchTask, str, Page]:
        """Process a single search task. Returns (task, result, page)."""
        logger.info(f"[Worker {worker_id}] [{task_num}/{total_tasks}] Article: {task.article}, Query: {task.query}")

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
                logger.warning(f"[Worker {worker_id}] Block detected")
                await self._notify_error(
                    f"[W{worker_id}] Блокировка Ozon при поиске '{task.query}'", page
                )
                await self._safe_close_page(page)
                page = await self._get_fresh_page(worker_id)
                await asyncio.sleep(random.uniform(5, 10))
                continue
            except OzonPageLoadError as e:
                logger.warning(f"[Worker {worker_id}] Page load error (attempt {attempt + 1}/3): {e}")
                await self._notify_error(
                    f"[W{worker_id}] Ошибка загрузки '{task.query}' (попытка {attempt + 1}/3)", page
                )
                await self._safe_close_page(page)
                page = await self._get_fresh_page(worker_id)
                await asyncio.sleep(random.uniform(3, 6))
                continue
            except Exception as e:
                error_str = str(e)
                # Browser/context closed - get fresh page
                if "closed" in error_str.lower() or "target" in error_str.lower():
                    logger.warning(f"[Worker {worker_id}] Browser closed, getting fresh page")
                    page = await self._get_fresh_page(worker_id)
                    await asyncio.sleep(random.uniform(2, 4))
                    continue
                if "ERR_TIMED_OUT" in error_str or "Timeout" in error_str:
                    logger.warning(f"[Worker {worker_id}] Timeout error (attempt {attempt + 1}/3): {e}")
                    await self._notify_error(
                        f"[W{worker_id}] Таймаут '{task.query}' (попытка {attempt + 1}/3)", page
                    )
                    await self._safe_close_page(page)
                    page = await self._get_fresh_page(worker_id)
                    await asyncio.sleep(random.uniform(3, 6))
                    continue
                logger.error(f"[Worker {worker_id}] Error processing query '{task.query}': {e}")
                await self._notify_error(f"[W{worker_id}] Ошибка '{task.query}': {e}", page)
                position = -1
                break

            if position == -1:
                if attempt < 2:
                    logger.warning(f"[Worker {worker_id}] Incomplete results, retrying...")
                    await self._notify_error(
                        f"[W{worker_id}] Неполные результаты '{task.query}' (попытка {attempt + 2}/3)", page
                    )
                    await self._safe_close_page(page)
                    page = await self._get_fresh_page(worker_id)
                    await asyncio.sleep(random.uniform(3, 6))
                    continue
                else:
                    await self._notify_error(
                        f"[W{worker_id}] Не удалось получить результаты '{task.query}'", page
                    )
            break

        is_found = position is not None and position > 0
        if is_found:
            result = str(position)
        elif position is None:
            result = f"{max_position}+"
        else:
            result = "—"

        logger.info(f"[Worker {worker_id}] Position for {task.article}: {result}")
        return (task, result, page, is_found)

    async def _worker(
        self,
        worker_id: int,
        task_queue: asyncio.Queue,
        total_tasks: int,
        max_position: int,
        worksheet,
        col_idx: int,
        results: list,
    ) -> None:
        """Worker that processes tasks from queue."""
        page = await self._get_fresh_page(worker_id)

        try:
            while True:
                try:
                    task_num, task = task_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                # Random delay between tasks to look more human
                if task_num > 1:
                    await asyncio.sleep(random.uniform(1, 3))

                is_found = False
                try:
                    task, result, page, is_found = await self._process_single_task(
                        task, task_num, total_tasks, max_position, page, worker_id
                    )
                except Exception as e:
                    logger.error(f"[Worker {worker_id}] Fatal error: {e}")
                    result = "—"
                    # Try to get fresh page for next task
                    try:
                        page = await self._get_fresh_page(worker_id)
                    except Exception:
                        logger.error(f"[Worker {worker_id}] Cannot recover, stopping")
                        task_queue.task_done()
                        break

                results.append((task, result))

                # Write result immediately (green if found)
                await self._write_cell_async(worksheet, task.row_index, col_idx, result, is_found)

                task_queue.task_done()
        finally:
            await self._safe_close_page(page)

    async def run(self, max_position: int = 1000) -> None:
        """Run position tracking for all tasks with parallel workers."""
        worksheet = self.sheets.get_worksheet(self.WORKSHEET_NAME)
        tasks = self.get_tasks_from_sheet()

        if not tasks:
            logger.warning("No tasks found")
            return

        col_idx = self.get_or_create_hourly_column(worksheet)
        num_workers = min(settings.parallel_tabs, len(tasks))

        logger.info(
            f"Starting position tracking: {len(tasks)} queries, "
            f"{num_workers} parallel tabs, max position {max_position}"
        )

        # Create task queue
        task_queue: asyncio.Queue = asyncio.Queue()
        for i, task in enumerate(tasks, 1):
            await task_queue.put((i, task))

        results: list = []

        # Start workers with staggered delay
        workers = []
        for worker_id in range(num_workers):
            if worker_id > 0:
                await asyncio.sleep(random.uniform(2, 4))  # Stagger worker starts
            worker = asyncio.create_task(
                self._worker(worker_id, task_queue, len(tasks), max_position, worksheet, col_idx, results)
            )
            workers.append(worker)

        # Wait for all workers to complete
        await asyncio.gather(*workers)

        logger.info(f"Position tracking completed: {len(results)}/{len(tasks)} tasks processed")
