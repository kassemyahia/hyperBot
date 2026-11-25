"""Read dated shift assignments from the dedicated, read-only Google Sheet.

The source is authoritative when SHIFT_SHEET_ID is configured. The separate
SQLite shift_reminders table remains the durable once-per-date delivery ledger.
"""
from __future__ import annotations

import re
from datetime import date

from bot.services.google_sheets import GoogleSheetsCatalog, SheetsError
from bot.services.shift_schedule import DAY_NAMES_AR, Shift, day_index

DATE_IN_CELL = re.compile(r"\(\s*(\d{1,2})\s*/\s*(\d{1,2})(?:\s*/\s*(\d{4}))?\s*\)")


class SheetShiftSchedule:
    def __init__(
        self,
        spreadsheet_id: str,
        credentials_path: str | None,
        *,
        sheet_name: str = "Hyper Shifts",
        starting_year: int,
        catalog: GoogleSheetsCatalog | None = None,
    ) -> None:
        if not 2000 <= starting_year <= 2100:
            raise ValueError("SHIFT_SHEET_YEAR must be a four-digit calendar year")
        self.sheet_name = sheet_name
        self.starting_year = starting_year
        self.catalog = catalog or GoogleSheetsCatalog(spreadsheet_id, credentials_path)

    def load(self) -> dict[date, Shift]:
        """Fetch one snapshot; never silently fall back to an outdated local roster."""
        rows = self.catalog.read_rows(self.sheet_name)
        if len(rows) < 5:
            raise SheetsError("Shift sheet has no expected header row")
        header = rows[4]
        expected = {1: "اليوم", 2: "coach", 3: "co-coach"}
        for column, label in expected.items():
            value = str(header[column] or "").strip().casefold() if len(header) > column else ""
            if value != label.casefold():
                raise SheetsError("Unexpected shift sheet header; refusing to send")

        schedule: dict[date, Shift] = {}
        year = self.starting_year
        last_month: int | None = None
        for row_number, row in enumerate(rows[5:], start=6):
            raw_day = str(row[1] or "").strip() if len(row) > 1 else ""
            found = DATE_IN_CELL.search(raw_day)
            if not found:
                continue  # week headings and blank separators
            day, month = int(found.group(1)), int(found.group(2))
            if found.group(3):
                item_year = int(found.group(3))
            else:
                if last_month == 12 and month == 1:
                    year += 1
                item_year = year
            try:
                when = date(item_year, month, day)
            except ValueError as exc:
                raise SheetsError(f"Invalid date in shift row {row_number}") from exc
            last_month = month
            if when in schedule:
                raise SheetsError(f"Duplicate shift date in row {row_number}")
            index = day_index(when)
            day_label = raw_day.splitlines()[0].strip().replace("أ", "ا").replace("إ", "ا")
            if (index is None or
                DAY_NAMES_AR[index].replace("أ", "ا").replace("إ", "ا") != day_label):
                raise SheetsError(f"Weekday/date mismatch in shift row {row_number}")

            names = tuple(
                str(row[column] or "").strip() if len(row) > column else ""
                for column in range(2, 8)
            )
            complete = all(names) and len({name.casefold() for name in names}) == 6
            schedule[when] = Shift(
                date=when.isoformat(), names=names, cancelled=False,
                complete=bool(complete),
            )
        if not schedule:
            raise SheetsError("No dated shifts found in the configured sheet")
        return schedule

    def effective(self, when: date) -> Shift:
        return self.load().get(when, Shift(when.isoformat(), (), False, False))
