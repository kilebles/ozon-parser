import asyncio
import random
import re
from dataclasses import dataclass
from datetime import datetime

from playwright.async_api import Page

from app.logging_config import get_logger
from app.services.parser import OzonParser, OzonBlockedError, OzonPageLoadError
from app.services.sheets import GoogleSheetsService

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
        self._short_id: str = ""  # Set in run()

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

        logger.debug(f"Found {len(tasks)} search tasks")
        return tasks

    def get_incomplete_tasks(
        self, tasks: list[SearchTask], worksheet, col_idx: int
    ) -> list[SearchTask]:
        """
        Check column and return only tasks that have empty cells.
        If all cells are filled, returns empty list.
        """
        if not tasks:
            return []

        # Get all values from column
        col_values = worksheet.col_values(col_idx)

        # Find tasks with empty cells
        incomplete_tasks = []
        for task in tasks:
            row_idx = task.row_index
            # col_values is 0-indexed, row_index is 1-indexed
            if row_idx > len(col_values) or not col_values[row_idx - 1].strip():
                incomplete_tasks.append(task)

        return incomplete_tasks

    def get_column_for_tracking(
        self, tasks: list[SearchTask], worksheet
    ) -> tuple[int, list[SearchTask]]:
        """
        Determine which column to use and which tasks to process.

        Logic:
        1. Check if column D has incomplete tasks -> resume them
        2. If D is complete -> create new column at D, process all tasks

        Returns: (column_index, tasks_to_process)
        """
        headers = worksheet.row_values(1)

        # Check if column D exists and has a date header (not A/B/C fixed columns)
        if len(headers) >= 4:
            header_d = headers[3]
            # Check if it looks like a date column (DD.MM format)
            if re.match(r"^\d{2}\.\d{2}", header_d):
                # Check for incomplete tasks in column D
                incomplete = self.get_incomplete_tasks(tasks, worksheet, col_idx=4)
                if incomplete:
                    return 4, incomplete

        # Column D is complete or doesn't exist - create new column
        now = datetime.now()
        column_name = now.strftime("%d.%m %H:00")

        # Check if current hour column already exists at D
        if len(headers) >= 4 and headers[3] == column_name:
            return 4, tasks

        # Insert new column at position D
        worksheet.insert_cols([[""]], col=4)
        worksheet.update_cell(1, 4, column_name)
        return 4, tasks

    @staticmethod
    def _col_letter(col_num: int) -> str:
        """Convert column number (1-based) to letter (A, B, ..., Z, AA, AB, ...)."""
        result = ""
        while col_num > 0:
            col_num, remainder = divmod(col_num - 1, 26)
            result = chr(65 + remainder) + result
        return result

    async def _write_cell_async(
        self, worksheet, row: int, col: int, value: str, is_found: bool = False
    ) -> None:
        """Write to cell asynchronously without blocking. Green background if found."""
        try:
            await asyncio.to_thread(worksheet.update_cell, row, col, value)

            # Set background color: green if found, white otherwise
            cell_label = f"{self._col_letter(col)}{row}"
            if is_found:
                bg_color = {"red": 0.7, "green": 1.0, "blue": 0.7}  # Light green
            else:
                bg_color = {"red": 1.0, "green": 1.0, "blue": 1.0}  # White
            await asyncio.to_thread(
                worksheet.format,
                cell_label,
                {"backgroundColor": bg_color}
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
        logger.debug(f"[W{worker_id}] [{task_num}/{total_tasks}] {task.article}: {task.query}")

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
                await self._safe_close_page(page)
                page = await self._get_fresh_page(worker_id)
                await asyncio.sleep(random.uniform(5, 10))
                continue
            except OzonPageLoadError as e:
                logger.warning(f"[Worker {worker_id}] Page load error (attempt {attempt + 1}/3): {e}")
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
                    await self._safe_close_page(page)
                    page = await self._get_fresh_page(worker_id)
                    await asyncio.sleep(random.uniform(3, 6))
                    continue
                logger.error(f"[Worker {worker_id}] Error processing query '{task.query}': {e}")
                position = -1
                break

            if position == -1:
                # -1 means block page detected, retry with fresh browser
                if attempt < 2:
                    logger.warning(f"[Worker {worker_id}] Blocked during search, retrying...")
                    await self._safe_close_page(page)
                    page = await self._get_fresh_page(worker_id)
                    await asyncio.sleep(random.uniform(3, 6))
                    continue
            break

        is_found = position is not None and position > 0
        if is_found:
            result = str(position)
            logger.info(f"[{self._short_id}] {task.article}: {result}")
        elif position is None:
            result = f"{max_position}+"
            logger.info(f"[{self._short_id}] {task.article}: {result} (не найден)")
        else:
            result = "—"
            logger.info(f"[{self._short_id}] {task.article}: {result} (ошибка)")

        return (task, result, page, is_found)

    async def run(self, max_position: int = 1000) -> None:
        """Run position tracking for all tasks in single spreadsheet (single tab)."""
        spreadsheet_name = self.sheets.spreadsheet.title
        self._short_id = self.sheets.spreadsheet_id[:8]

        worksheet = self.sheets.get_worksheet(self.WORKSHEET_NAME)
        all_tasks = self.get_tasks_from_sheet()

        if not all_tasks:
            logger.warning(f"[{self._short_id}] No tasks in '{spreadsheet_name}'")
            return

        # Get column and tasks to process (may resume incomplete)
        col_idx, tasks = self.get_column_for_tracking(all_tasks, worksheet)

        if len(tasks) < len(all_tasks):
            logger.info(
                f"[{self._short_id}] {spreadsheet_name}: resuming {len(tasks)}/{len(all_tasks)} incomplete"
            )
        else:
            logger.info(f"[{self._short_id}] {spreadsheet_name}: {len(tasks)} queries")

        page = await self._get_fresh_page(0)
        results: list = []

        try:
            for task_num, task in enumerate(tasks, 1):
                # Short random delay between tasks
                if task_num > 1:
                    await asyncio.sleep(random.uniform(0.5, 1.5))

                is_found = False
                try:
                    task, result, page, is_found = await self._process_single_task(
                        task, task_num, len(tasks), max_position, page, worker_id=0
                    )
                except Exception as e:
                    logger.error(f"[{self._short_id}] Fatal error: {e}")
                    result = "—"
                    try:
                        page = await self._get_fresh_page(0)
                    except Exception:
                        logger.error(f"[{self._short_id}] Cannot recover, stopping")
                        break

                results.append((task, result))
                await self._write_cell_async(worksheet, task.row_index, col_idx, result, is_found)
        finally:
            await self._safe_close_page(page)

        logger.info(f"[{self._short_id}] Done: {len(results)}/{len(tasks)}")
