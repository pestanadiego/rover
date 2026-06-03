import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from google.auth.transport.requests import Request as AuthRequest
from google.oauth2 import service_account

from rover.common import sheet_id_and_gid, ssl_context


GOOGLE_SHEET_URL_ENV_VAR = "GOOGLE_SHEET_URL"
GOOGLE_SERVICE_ACCOUNT_FILE_ENV_VAR = "GOOGLE_SERVICE_ACCOUNT_FILE"
SCRAPE_KEYWORD_HEADER = "Scrape Keyword"
ASIN_HEADER = "ASIN"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


class ScrapeKeywordSheetUpdater:
    def __init__(
        self,
        sheet_url: str,
        service_account_file: Path,
        keyword_header: str = SCRAPE_KEYWORD_HEADER,
    ):
        sheet_id, gid = sheet_id_and_gid(sheet_url)

        if not sheet_id:
            raise RuntimeError("GOOGLE_SHEET_URL must be a Google Sheets URL.")

        self.sheet_id = sheet_id
        self.gid = gid or "0"
        self.service_account_file = service_account_file
        self.keyword_header = keyword_header
        self.credentials = service_account.Credentials.from_service_account_file(
            service_account_file,
            scopes=[SHEETS_SCOPE],
        )
        self.sheet_title = self.find_sheet_title()

    @classmethod
    def from_env(cls, project_root: Path) -> "ScrapeKeywordSheetUpdater":
        sheet_url = os.getenv(GOOGLE_SHEET_URL_ENV_VAR)
        service_account_file = os.getenv(GOOGLE_SERVICE_ACCOUNT_FILE_ENV_VAR)

        if not sheet_url:
            raise RuntimeError(f"Missing {GOOGLE_SHEET_URL_ENV_VAR} in .env.")

        if not service_account_file:
            raise RuntimeError(f"Missing {GOOGLE_SERVICE_ACCOUNT_FILE_ENV_VAR} in .env.")

        credentials_path = Path(service_account_file)
        if not credentials_path.is_absolute():
            credentials_path = project_root / credentials_path

        if not credentials_path.exists():
            raise RuntimeError(f"Service account file not found: {credentials_path}")

        return cls(sheet_url, credentials_path)

    def row_count(self) -> int:
        values = self.read_sheet_values()
        return len(values)

    def fill_scrape_keyword(
        self,
        keyword: str,
        asin: str | None,
        min_row_number: int,
        timeout_seconds: int = 30,
    ) -> int | None:
        deadline = time.time() + timeout_seconds

        while time.time() < deadline:
            row_number = self.find_keyword_row(keyword, asin, min_row_number)

            if row_number:
                self.update_keyword_cell(row_number, keyword)
                return row_number

            time.sleep(2)

        return None

    def find_keyword_row(
        self,
        keyword: str,
        asin: str | None,
        min_row_number: int,
    ) -> int | None:
        values = self.read_sheet_values()
        headers = self.headers_from_values(values)
        keyword_index = column_index(headers, self.keyword_header)

        if keyword_index is None:
            raise RuntimeError(f"Missing {self.keyword_header!r} column in Google Sheet.")

        asin_index = column_index(headers, ASIN_HEADER)
        first_data_row = max(min_row_number, 2)

        if asin and asin_index is not None:
            row_number = self.find_row_by_asin(values, asin, asin_index, keyword_index, first_data_row)
            if row_number:
                return row_number

        return self.find_latest_blank_keyword_row(values, keyword_index, first_data_row)

    def find_row_by_asin(
        self,
        values: list[list[str]],
        asin: str,
        asin_index: int,
        keyword_index: int,
        first_data_row: int,
    ) -> int | None:
        for row_number in range(len(values), first_data_row - 1, -1):
            row = values[row_number - 1]
            row_asin = cell_value(row, asin_index)
            row_keyword = cell_value(row, keyword_index)

            if row_asin == asin and not row_keyword:
                return row_number

        return None

    def find_latest_blank_keyword_row(
        self,
        values: list[list[str]],
        keyword_index: int,
        first_data_row: int,
    ) -> int | None:
        for row_number in range(len(values), first_data_row - 1, -1):
            row = values[row_number - 1]

            if row and not cell_value(row, keyword_index):
                return row_number

        return None

    def update_keyword_cell(self, row_number: int, keyword: str) -> None:
        headers = self.headers_from_values(self.read_sheet_values())
        keyword_index = column_index(headers, self.keyword_header)

        if keyword_index is None:
            raise RuntimeError(f"Missing {self.keyword_header!r} column in Google Sheet.")

        column_letter = column_number_to_letter(keyword_index + 1)
        cell_range = f"{self.sheet_title}!{column_letter}{row_number}"
        path = f"/v4/spreadsheets/{self.sheet_id}/values/{quote(cell_range, safe='')}?valueInputOption=RAW"
        body = {"values": [[keyword]]}

        self.request_json("PUT", path, body)

    def read_sheet_values(self) -> list[list[str]]:
        sheet_range = f"{self.sheet_title}!A:ZZ"
        path = f"/v4/spreadsheets/{self.sheet_id}/values/{quote(sheet_range, safe='')}"
        response = self.request_json("GET", path)
        return response.get("values", [])

    def headers_from_values(self, values: list[list[str]]) -> list[str]:
        if not values:
            return []
        return [str(value).strip() for value in values[0]]

    def find_sheet_title(self) -> str:
        response = self.request_json(
            "GET",
            f"/v4/spreadsheets/{self.sheet_id}?fields=sheets.properties",
        )

        for sheet in response.get("sheets", []):
            properties = sheet.get("properties", {})
            if str(properties.get("sheetId")) == str(self.gid):
                return properties["title"]

        raise RuntimeError(f"Could not find Google Sheet tab with gid {self.gid}.")

    def request_json(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        token = self.access_token()
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = UrlRequest(
            f"https://sheets.googleapis.com{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

        try:
            with urlopen(request, timeout=30, context=ssl_context()) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            message = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Google Sheets API HTTP {error.code}: {message}") from error
        except URLError as error:
            raise RuntimeError(f"Google Sheets API request failed: {error.reason}") from error

    def access_token(self) -> str:
        if not self.credentials.valid:
            self.credentials.refresh(AuthRequest())

        return self.credentials.token


def column_index(headers: list[str], header_name: str) -> int | None:
    normalized_header_name = normalize_header(header_name)

    for index, header in enumerate(headers):
        if normalize_header(header) == normalized_header_name:
            return index

    return None


def normalize_header(header: str) -> str:
    return "".join(character.lower() for character in header if character.isalnum())


def cell_value(row: list[str], index: int) -> str:
    if index >= len(row):
        return ""

    return str(row[index]).strip()


def column_number_to_letter(column_number: int) -> str:
    letters = ""

    while column_number:
        column_number, remainder = divmod(column_number - 1, 26)
        letters = chr(65 + remainder) + letters

    return letters
