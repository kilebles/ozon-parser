from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

from app.logging_config import get_logger
from app.settings import settings

logger = get_logger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


class GoogleSheetsService:
    def __init__(self, spreadsheet_id: str | None = None) -> None:
        self._client: gspread.Client | None = None
        self._spreadsheet: gspread.Spreadsheet | None = None
        self._spreadsheet_id = spreadsheet_id

    def connect(self) -> None:
        credentials_path = Path(settings.google_credentials_path)
        if not credentials_path.exists():
            raise FileNotFoundError(
                f"Google credentials file not found: {credentials_path}"
            )

        credentials = Credentials.from_service_account_file(
            str(credentials_path),
            scopes=SCOPES,
        )
        self._client = gspread.authorize(credentials)

        if self._spreadsheet_id:
            self._spreadsheet = self._client.open_by_key(self._spreadsheet_id)

    @property
    def client(self) -> gspread.Client:
        if not self._client:
            raise RuntimeError("Not connected. Call connect() first.")
        return self._client

    @property
    def spreadsheet(self) -> gspread.Spreadsheet:
        if not self._spreadsheet:
            raise RuntimeError("No spreadsheet configured.")
        return self._spreadsheet

    @property
    def spreadsheet_id(self) -> str:
        if not self._spreadsheet_id:
            raise RuntimeError("No spreadsheet ID configured.")
        return self._spreadsheet_id

    def get_worksheet(self, name: str) -> gspread.Worksheet:
        return self.spreadsheet.worksheet(name)

    def list_spreadsheets(self) -> list[str]:
        spreadsheets = self.client.openall()
        return [s.title for s in spreadsheets]
