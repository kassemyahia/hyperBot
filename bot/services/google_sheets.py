"""Private, idempotent Google Sheets catalog access."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from bot.database.models import CatalogEntry
from bot.services.deep_links import validate_website_id

logger = logging.getLogger(__name__)

SHEET_NAMES = ("softwares", "courses")
REQUIRED_COLUMNS = ("File ID", "Telegram Link", "Telegram Status")
GOOGLE_API_RETRIES = 3


class SheetsError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CatalogIdTarget:
    row_number: int
    requested_file_id: str
    expected_title: str
    expected_source_url: str


def _column_name(index: int) -> str:
    value = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        value = chr(65 + remainder) + value
    return value


def _header_key(value: str) -> str:
    return " ".join(value.split()).casefold()


def _positions(headers: list[str]) -> dict[str, int]:
    """Map whitespace/case-normalized headings while keeping the original row."""
    return {_header_key(str(name)): index for index, name in enumerate(headers)}


def _column_index(headers: list[str], name: str) -> int | None:
    return _positions(headers).get(_header_key(name))


def _execute(request: Any) -> dict[str, Any]:
    """Use the Google client's bounded retry support for transient API failures."""
    return request.execute(num_retries=GOOGLE_API_RETRIES)


class GoogleSheetsCatalog:
    def __init__(
        self, spreadsheet_id: str, credentials_path: str | None = None,
        *, service: Any | None = None,
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.credentials_path = credentials_path
        self._service = service

    @property
    def service(self) -> Any:
        if self._service is None:
            try:
                from google.oauth2.service_account import Credentials
                from googleapiclient.discovery import build
            except ImportError as exc:
                raise SheetsError(
                    "Google dependencies are not installed; run pip install -r requirements.txt"
                ) from exc
            scopes = ["https://www.googleapis.com/auth/spreadsheets"]
            credentials = Credentials.from_service_account_file(
                self.credentials_path, scopes=scopes
            ) if self.credentials_path else None
            self._service = build("sheets", "v4", credentials=credentials,
                                  cache_discovery=False)
        return self._service

    def _values(self) -> Any:
        return self.service.spreadsheets().values()

    def _read(self, sheet_name: str) -> list[list[str]]:
        try:
            response = _execute(self._values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{sheet_name}'",
            ))
            return response.get("values", [])
        except Exception as exc:
            logger.warning("Google Sheets catalog read failed: %s", type(exc).__name__)
            raise SheetsError("Unable to read Google Sheets catalog") from exc

    def read_rows(self, sheet_name: str) -> list[list[str]]:
        """Read an arbitrary worksheet without modifying its columns or cells."""
        if not sheet_name or any(char in sheet_name for char in ("'", "!", "\\n")):
            raise ValueError("Invalid worksheet name")
        return self._read(sheet_name)

    def _ensure_columns(self, sheet_name: str, values: list[list[str]]) -> tuple[list[str], list[list[str]]]:
        headers = list(values[0]) if values else []
        positions = _positions(headers)
        missing = [
            column for column in REQUIRED_COLUMNS
            if _header_key(column) not in positions
        ]
        if missing:
            headers.extend(missing)
            try:
                _execute(self._values().update(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"'{sheet_name}'!1:1", valueInputOption="RAW",
                    body={"values": [headers]},
                ))
            except Exception as exc:
                raise SheetsError("Unable to add catalog columns") from exc
        return headers, values[1:] if values else []

    def load_entries(self) -> list[CatalogEntry]:
        result: list[CatalogEntry] = []
        for sheet_name in SHEET_NAMES:
            values = self._read(sheet_name)
            if not values:
                continue
            headers = values[0]
            positions = _positions(headers)
            for row_number, row in enumerate(values[1:], start=2):
                def cell(name: str) -> str | None:
                    index = positions.get(_header_key(name))
                    return row[index].strip() if index is not None and index < len(row) and row[index].strip() else None
                file_id, title = cell("File ID"), cell("File Name")
                if title:
                    result.append(CatalogEntry(
                        file_id=file_id, title=title, size=cell("Size"),
                        category=cell("Category"), source_type=cell("Source Type"),
                        sheet_name=sheet_name, row_number=row_number,
                        telegram_link=cell("Telegram Link"),
                        telegram_status=cell("Telegram Status"),
                    ))
        return result

    def initialize_missing_file_ids(self, *, approved: bool = False) -> int:
        """Explicitly initialize stable IDs; never called by routine synchronization."""
        if not approved:
            raise ValueError("File ID initialization requires explicit approval")
        changed = 0
        for sheet_name in SHEET_NAMES:
            values = self._read(sheet_name)
            headers, rows = self._ensure_columns(sheet_name, values)
            file_id_index = _column_index(headers, "File ID")
            name_index = _column_index(headers, "File Name")
            if file_id_index is None:
                raise SheetsError("Unable to locate the File ID column")
            prefix = "sw" if sheet_name == "softwares" else "course"
            updates: list[dict[str, Any]] = []
            for row_number, row in enumerate(rows, start=2):
                has_name = name_index is not None and name_index < len(row) and row[name_index].strip()
                has_id = file_id_index < len(row) and row[file_id_index].strip()
                if has_name and not has_id:
                    updates.append({"range": f"'{sheet_name}'!{_column_name(file_id_index + 1)}{row_number}",
                                    "values": [[f"{prefix}_{uuid.uuid4().hex}"]]})
            if updates:
                try:
                    _execute(self._values().batchUpdate(
                        spreadsheetId=self.spreadsheet_id,
                        body={"valueInputOption": "RAW", "data": updates},
                    ))
                except Exception as exc:
                    raise SheetsError("Unable to initialize stable File IDs") from exc
                changed += len(updates)
        return changed

    def ensure_entry_file_id(
        self, *, sheet_name: str, row_number: int, requested_file_id: str,
        expected_title: str, expected_source_url: str, approved: bool = False,
    ) -> CatalogEntry:
        """Initialize one verified catalog row without touching any other row ID."""
        return self.ensure_entries_file_ids(
            sheet_name=sheet_name,
            targets=(CatalogIdTarget(
                row_number=row_number, requested_file_id=requested_file_id,
                expected_title=expected_title,
                expected_source_url=expected_source_url,
            ),),
            approved=approved,
        )[0]

    def ensure_entries_file_ids(
        self, *, sheet_name: str, targets: tuple[CatalogIdTarget, ...],
        approved: bool = False,
    ) -> list[CatalogEntry]:
        """Initialize a verified set of rows in one bounded, idempotent batch."""
        if not approved:
            raise ValueError("Targeted File ID initialization requires explicit approval")
        if sheet_name not in SHEET_NAMES or not targets:
            raise ValueError("Invalid catalog sheet or empty target set")
        if len({target.row_number for target in targets}) != len(targets):
            raise ValueError("Target rows cannot be repeated")
        normalized_targets = tuple(CatalogIdTarget(
            row_number=target.row_number,
            requested_file_id=validate_website_id(target.requested_file_id.strip()),
            expected_title=target.expected_title,
            expected_source_url=target.expected_source_url,
        ) for target in targets)
        if any(target.row_number < 2 for target in normalized_targets):
            raise ValueError("Invalid catalog data row")

        values = self._read(sheet_name)
        if not values or any(
            target.row_number > len(values) for target in normalized_targets
        ):
            raise SheetsError("Target catalog row does not exist")
        original_headers = list(values[0])
        original_positions = _positions(original_headers)
        title_index = original_positions.get(_header_key("File Name"))
        source_index = original_positions.get(_header_key("Source Type"))
        for target in normalized_targets:
            target_row = values[target.row_number - 1]

            def original_cell(index: int | None) -> str | None:
                if index is None or index >= len(target_row):
                    return None
                value = target_row[index].strip()
                return value or None

            if (original_cell(title_index) != target.expected_title
                    or original_cell(source_index) != (target.expected_source_url or None)):
                raise SheetsError("Target catalog row identity changed; refusing update")

        headers, _ = self._ensure_columns(sheet_name, values)
        file_id_index = _column_index(headers, "File ID")
        if file_id_index is None:
            raise SheetsError("Unable to locate the File ID column")
        entries = self.load_entries()
        by_file_id: dict[str, list[CatalogEntry]] = {}
        for entry in entries:
            if entry.file_id:
                by_file_id.setdefault(entry.file_id, []).append(entry)
        reserved_ids = set(by_file_id)
        expected_ids: dict[int, str] = {}
        updates: list[dict[str, Any]] = []
        for target in normalized_targets:
            target_row = values[target.row_number - 1]
            existing_file_id = (
                target_row[file_id_index].strip()
                if file_id_index < len(target_row) else ""
            )
            if existing_file_id:
                matches = by_file_id.get(existing_file_id, [])
                if (len(matches) != 1 or matches[0].sheet_name != sheet_name
                        or matches[0].row_number != target.row_number):
                    raise SheetsError(
                        "Existing File ID must identify exactly one catalog row"
                    )
                expected_ids[target.row_number] = existing_file_id
                continue
            if target.requested_file_id in reserved_ids:
                raise SheetsError(
                    "Requested File ID already belongs to another catalog row"
                )
            reserved_ids.add(target.requested_file_id)
            expected_ids[target.row_number] = target.requested_file_id
            updates.append({
                "range": (
                    f"'{sheet_name}'!{_column_name(file_id_index + 1)}"
                    f"{target.row_number}"
                ),
                "values": [[target.requested_file_id]],
            })
        if updates:
            try:
                _execute(self._values().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"valueInputOption": "RAW", "data": updates},
                ))
            except Exception as exc:
                raise SheetsError("Unable to initialize target File IDs") from exc

        refreshed = {
            entry.row_number: entry for entry in self.load_entries()
            if entry.sheet_name == sheet_name
            and entry.row_number in expected_ids
        }
        if any(
            row_number not in refreshed
            or refreshed[row_number].file_id != expected_file_id
            for row_number, expected_file_id in expected_ids.items()
        ):
            raise SheetsError("Target File ID writes could not be verified")
        return [refreshed[target.row_number] for target in normalized_targets]


    def update_source_link(
        self, *, sheet_name: str, row_number: int, expected_title: str,
        expected_source_url: str | None, source_url: str,
    ) -> CatalogEntry:
        """Write only Source Type after validating the target row's identity."""
        if sheet_name not in SHEET_NAMES or row_number < 2:
            raise ValueError("Invalid target row")
        from bot.services.matching import parse_telegram_source_url
        if parse_telegram_source_url(source_url) is None:
            raise ValueError("Invalid Telegram source URL")
        entries = self.load_entries()
        targets = [entry for entry in entries
                   if entry.sheet_name == sheet_name and entry.row_number == row_number]
        if len(targets) != 1:
            raise SheetsError("Target catalog row missing")
        entry = targets[0]
        if entry.title != expected_title or entry.source_type != (expected_source_url or None):
            raise SheetsError("Catalog row identity changed")
        if any(other.source_type == source_url
               and (other.sheet_name, other.row_number) != (sheet_name, row_number)
               for other in entries):
            raise SheetsError("Another catalog row already uses the source URL")
        if entry.source_type == source_url:
            return entry
        values = self._read(sheet_name)
        headers = values[0]
        index = _column_index(headers, "Source Type")
        if index is None:
            raise SheetsError("Source Type column not found")
        try:
            _execute(self._values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{sheet_name}'!{_column_name(index + 1)}{row_number}",
                valueInputOption="RAW", body={"values": [[source_url]]},
            ))
        except Exception as exc:
            raise SheetsError("Unable to update Telegram source URL") from exc
        updated = [e for e in self.load_entries()
                   if e.sheet_name == sheet_name and e.row_number == row_number]
        if len(updated) != 1 or updated[0].source_type != source_url:
            raise SheetsError("Source-link update could not be verified")
        return updated[0]

    def append_catalog_entry(
        self, sheet_name: str, *, file_id: str, title: str, size: str,
        path: str, category: str, source_url: str, details: str,
    ) -> CatalogEntry:
        """Add a new approved catalog item without changing existing rows."""
        from bot.services.matching import parse_telegram_source_url
        file_id = validate_website_id(file_id)
        title = title.strip()
        if sheet_name not in SHEET_NAMES or not 1 <= len(title) <= 200:
            raise ValueError("Invalid destination or title")
        if parse_telegram_source_url(source_url) is None:
            raise ValueError("Invalid Telegram source URL")
        current = self.load_entries()
        existing_id = [e for e in current if e.file_id == file_id]
        if len(existing_id) == 1 and existing_id[0].source_type == source_url:
            return existing_id[0]
        if existing_id or any(e.source_type == source_url for e in current):
            raise SheetsError("Source URL or File ID is already used")
        data = self._read(sheet_name)
        headers, _ = self._ensure_columns(sheet_name, data)
        positions = _positions(headers)
        row = [""] * len(headers)
        fields = {
            "File Name": title, "Size": size[:80], "Path": path[:250],
            "Category": category[:100], "Source Type": source_url,
            "Details": details[:1200], "File ID": file_id,
            "Telegram Link": "", "Telegram Status": "pending_review",
        }
        for key, value in fields.items():
            index = positions.get(_header_key(key))
            if index is not None:
                row[index] = value
        try:
            _execute(self._values().append(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{sheet_name}'!A1",
                valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            ))
        except Exception as exc:
            raise SheetsError("Failed to create catalog entry; verify Sheet before retry") from exc
        matches = [e for e in self.load_entries() if e.file_id == file_id]
        if len(matches) != 1 or matches[0].source_type != source_url:
            raise SheetsError("New catalog row could not be verified")
        return matches[0]

    def append_catalog_only(
        self, sheet_name: str, *, file_id: str, title: str, size: str,
        path: str, category: str, source_url: str, details: str,
        pending: bool,
    ) -> CatalogEntry:
        """Append a catalog row without publishing a download or touching SQLite."""
        from bot.services.matching import parse_telegram_source_url

        file_id = validate_website_id(file_id)
        if sheet_name not in SHEET_NAMES or not 1 <= len(title.strip()) <= 200:
            raise ValueError("Invalid destination or title")
        fields = {"File Name": title.strip(), "Size": size, "Path": path,
                  "Category": category, "Source Type": source_url,
                  "Details": details}
        entries = self.load_entries()
        existing = [e for e in entries if e.file_id == file_id]
        data = self._read(sheet_name)
        headers = list(data[0]) if data else []
        positions = _positions(headers)
        if any(sum(_header_key(str(h)) == _header_key(k) for h in headers) != 1
               for k in (*fields, *[k for k in REQUIRED_COLUMNS if _header_key(k) in positions])):
            raise ValueError("Missing or duplicate catalog headers")
        if existing:
            if len(existing) != 1 or existing[0].sheet_name != sheet_name:
                raise ValueError("Request ID already used")
            row = data[existing[0].row_number - 1]
            if all((str(row[positions[_header_key(k)]]).strip()
                    if positions[_header_key(k)] < len(row) else "") == v
                   for k, v in fields.items()):
                return existing[0]
            raise ValueError("Request ID already used for different data")

        def identity(value):
            ref = parse_telegram_source_url(value)
            if ref is None:
                return None
            return ((ref.username or "").casefold(), ref.internal_chat_id, ref.message_id)

        source_identity = identity(source_url)
        if pending and any(identity(e.source_type) == source_identity for e in entries):
            raise ValueError("Telegram source already exists in the catalog")
        headers, _ = self._ensure_columns(sheet_name, data)
        positions = _positions(headers)
        row = [""] * len(headers)
        fields.update({"File ID": file_id, "Telegram Link": "",
                       "Telegram Status": "pending_review" if pending else ""})
        for k, v in fields.items():
            row[positions[_header_key(k)]] = v
        try:
            # Appending is not intrinsically idempotent. Never automatically retry
            # an ambiguous write; the stable request ID permits a verified retry.
            self._values().append(
                spreadsheetId=self.spreadsheet_id, range=f"'{sheet_name}'!A1",
                valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            ).execute(num_retries=0)
        except Exception as exc:
            raise SheetsError("Append not confirmed; refresh before retry") from exc
        matches = [e for e in self.load_entries() if e.file_id == file_id]
        if len(matches) != 1 or matches[0].sheet_name != sheet_name:
            raise SheetsError("New catalog row could not be verified")
        return matches[0]

    def update_registration(self, file_id: str, telegram_link: str | None,
                            status: str) -> CatalogEntry:
        return self.update_registrations({file_id: (telegram_link, status)})[0]

    def update_registrations(
        self, updates: dict[str, tuple[str | None, str]],
    ) -> list[CatalogEntry]:
        """Synchronize a validated set of stable IDs in one bounded batch."""
        if not updates:
            return []
        allowed_statuses = {"available", "unavailable", "pending_review"}
        if any(status not in allowed_statuses for _, status in updates.values()):
            raise ValueError("Invalid Telegram Status")
        entries = self.load_entries()
        by_file_id: dict[str, list[CatalogEntry]] = {}
        for entry in entries:
            if entry.file_id in updates:
                by_file_id.setdefault(entry.file_id, []).append(entry)
        if any(len(by_file_id.get(file_id, [])) != 1 for file_id in updates):
            raise SheetsError("Each File ID must identify exactly one catalog row")
        targets = [by_file_id[file_id][0] for file_id in updates]
        columns: dict[str, tuple[str, str]] = {}
        for sheet_name in {entry.sheet_name for entry in targets}:
            values = self._read(sheet_name)
            headers, _ = self._ensure_columns(sheet_name, values)
            link_index = _column_index(headers, "Telegram Link")
            status_index = _column_index(headers, "Telegram Status")
            if link_index is None or status_index is None:
                raise SheetsError("Unable to locate Telegram synchronization columns")
            columns[sheet_name] = (
                _column_name(link_index + 1),
                _column_name(status_index + 1),
            )
        data: list[dict[str, Any]] = []
        for file_id, (telegram_link, status) in updates.items():
            entry = by_file_id[file_id][0]
            link_col, status_col = columns[entry.sheet_name]
            data.extend((
                {"range": f"'{entry.sheet_name}'!{link_col}{entry.row_number}",
                 "values": [[telegram_link or ""]]},
                {"range": f"'{entry.sheet_name}'!{status_col}{entry.row_number}",
                 "values": [[status]]},
            ))
        try:
            _execute(self._values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "RAW", "data": data},
            ))
        except Exception as exc:
            logger.warning("Google Sheets registration update failed: %s", type(exc).__name__)
            raise SheetsError("Unable to synchronize Google Sheets") from exc
        refreshed_by_id = {
            entry.file_id: entry for entry in self.load_entries()
            if entry.file_id in updates
        }
        if any(
            file_id not in refreshed_by_id
            or refreshed_by_id[file_id].telegram_link != (link or None)
            or refreshed_by_id[file_id].telegram_status != status
            for file_id, (link, status) in updates.items()
        ):
            raise SheetsError("Google Sheets synchronization could not be verified")
        return [refreshed_by_id[file_id] for file_id in updates]
