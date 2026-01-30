import asyncio
import random
import re
from dataclasses import dataclass
from datetime import datetime

from app.logging_config import get_logger
from app.services.parser import OzonParser, OzonBlockedError
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

    def _get_today_hourly_columns(self, headers: list[str]) -> list[int]:
        """
        Find all hourly columns for today.
        Returns list of 1-based column indices.
        """
        today = datetime.now().strftime("%d.%m")
        # Pattern: "28.01 14:00" (date with hour)
        pattern = re.compile(rf"^{re.escape(today)} \d{{2}}:00$")

        columns = []
        for i, header in enumerate(headers):
            if pattern.match(header):
                columns.append(i + 1)  # 1-based index

        return columns

    def consolidate_daily_results(self, worksheet) -> None:
        """
        Consolidate hourly columns into daily average.
        Called at end of day (after 23:00 run).

        1. Find all hourly columns for today (e.g., "28.01 14:00", "28.01 15:00")
        2. Calculate average position for each row
        3. Replace all hourly columns with single daily column ("28.01")
        """
        headers = worksheet.row_values(1)
        today = datetime.now().strftime("%d.%m")

        hourly_columns = self._get_today_hourly_columns(headers)

        if len(hourly_columns) < 2:
            logger.info("Less than 2 hourly columns found, skipping consolidation")
            return

        logger.info(f"Consolidating {len(hourly_columns)} hourly columns into daily average")

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

        # Delete hourly columns (from right to left to preserve indices)
        hourly_columns_sorted = sorted(hourly_columns, reverse=True)
        for col_idx in hourly_columns_sorted:
            worksheet.delete_columns(col_idx)
            logger.debug(f"Deleted column {col_idx}")

        # Insert new daily column at position D with header and all averages
        # Prepare column data: header + averages
        column_data = [[today]] + [[avg] for avg in averages]
        worksheet.insert_cols(column_data, col=4)

        logger.info(f"Consolidated into daily column '{today}' with averages")

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

                # Reuse the same page for all requests
                try:
                    position = await self.parser.find_product_position(
                        query=task.query,
                        target_article=task.article,
                        max_position=max_position,
                        page=page,
                    )
                except OzonBlockedError:
                    logger.warning("Block detected - restarting browser and retrying query...")
                    await page.close()
                    await self.parser.restart_browser()
                    page = await self.parser._new_page()
                    await self.parser._warmup(page)
                    await asyncio.sleep(random.uniform(5, 10))
                    # Retry the same query after restart
                    try:
                        position = await self.parser.find_product_position(
                            query=task.query,
                            target_article=task.article,
                            max_position=max_position,
                            page=page,
                        )
                    except Exception as e:
                        logger.error(f"Still failing after restart: {e}")
                        position = None
                except Exception as e:
                    logger.error(f"Error processing query '{task.query}': {e}")
                    position = None

                # Prepare result: position number or "1000+" if not found
                result = str(position) if position else f"{max_position}+"
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
