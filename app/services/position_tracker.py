from dataclasses import dataclass
from datetime import datetime

from app.logging_config import get_logger
from app.services.parser import OzonParser
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

    def get_or_create_column(self, worksheet, column_name: str) -> int:
        """Get column index by name or insert new column D (after Запрос). Returns 1-based index."""
        headers = worksheet.row_values(1)

        # If column already exists at position D, use it
        if len(headers) >= 4 and headers[3] == column_name:
            logger.info(f"Column '{column_name}' already exists at position D")
            return 4

        # Insert new column at position D (index 4) - right after "Запрос"
        # This shifts all existing columns to the right
        worksheet.insert_cols([[""]], col=4)
        worksheet.update_cell(1, 4, column_name)
        logger.info(f"Inserted new column '{column_name}' at position D")
        return 4

    async def run(self, max_position: int = 1000) -> None:
        """Run position tracking for all tasks and save results."""
        worksheet = self.sheets.get_worksheet(self.WORKSHEET_NAME)
        tasks = self.get_tasks_from_sheet()

        if not tasks:
            logger.warning("No tasks found")
            return

        # Get or create column for today's date
        today = datetime.now().strftime("%d.%m")
        col_idx = self.get_or_create_column(worksheet, today)

        logger.info(f"Starting position tracking for {len(tasks)} queries (max position: {max_position})")

        for i, task in enumerate(tasks, 1):
            logger.info(f"[{i}/{len(tasks)}] Article: {task.article}, Query: {task.query}")

            position = await self.parser.find_product_position(
                query=task.query,
                target_article=task.article,
                max_position=max_position,
            )

            # Write result to sheet: position number or "1000+" if not found
            result = str(position) if position else f"{max_position}+"
            worksheet.update_cell(task.row_index, col_idx, result)
            logger.info(f"Position: {result}")

        logger.info("Position tracking completed")
