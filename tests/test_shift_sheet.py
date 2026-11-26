"""Read-only dated shifts and reminder delivery regression tests."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bot.services.google_sheets import SheetsError
from bot.services.shift_schedule import ShiftStore, check_shift_reminder, format_reminder
from bot.services.shift_sheet import SheetShiftSchedule

HEADER = ["الأسبوع", "اليوم", "Coach", "Co-Coach", "المناوبين"]
NAMES = ["@CoachOne", "@CoCoachTwo", "@MemberOne", "@MemberTwo",
         "محمد عراطة", "@nour.subahi"]


def workbook(*body):
    return [["Hyper Shifts"], [], ["جدول مناوبات"], [], HEADER, *body]


class FakeCatalog:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def read_rows(self, name):
        assert name == "Hyper Shifts"
        self.calls += 1
        if isinstance(self.rows, Exception):
            raise self.rows
        return self.rows


class SheetParsingTests(unittest.TestCase):
    def source(self, rows):
        return SheetShiftSchedule(
            "test-sheet", None, starting_year=2026,
            catalog=FakeCatalog(rows),
        )

    def test_dated_rows_not_repeated_weekly(self):
        source = self.source(workbook(
            ["first week", "الأحد\n(15/11)", *NAMES],
            [None, "الإثنين\n(16/11)", "@CoachThree", *NAMES[1:]],
            [],
            ["second week", "الأحد\n(22/11)", *NAMES],
        ))
        result = source.load()
        self.assertEqual(len(result), 3)
        self.assertEqual(result[date(2026, 11, 15)].names, tuple(NAMES))
        self.assertTrue(result[date(2026, 11, 22)].complete)
        self.assertFalse(source.effective(date(2026, 11, 29)).complete)

    def test_december_to_january_rollover(self):
        source = self.source(workbook(
            [None, "الخميس\n(31/12)", *NAMES],
            [None, "الأحد\n(3/1)", *NAMES],
        ))
        rows = source.load()
        self.assertIn(date(2026, 12, 31), rows)
        self.assertIn(date(2027, 1, 3), rows)

    def test_duplicate_date_refused(self):
        source = self.source(workbook(
            [None, "الأحد\n(15/11)", *NAMES],
            [None, "الأحد\n(15/11)", *NAMES],
        ))
        with self.assertRaises(SheetsError):
            source.load()

    def test_mismatched_date_or_header_refused(self):
        for rows in (
            workbook([None, "الإثنين\n(15/11)", *NAMES]),
            [["Hyper Shifts"], [], [], [], ["bad"], [None, "الأحد\n(15/11)", *NAMES]],
        ):
            with self.subTest(rows=rows):
                with self.assertRaises(SheetsError):
                    self.source(rows).load()

    def test_incomplete_shift_not_publishable(self):
        names = NAMES[:-1] + [""]
        source = self.source(workbook([None, "الأحد\n(15/11)", *names]))
        self.assertFalse(source.effective(date(2026, 11, 15)).complete)

    def test_mention_only_valid_usernames(self):
        source = self.source(workbook([None, "الأحد\n(15/11)", *NAMES]))
        reminder = format_reminder(date(2026, 11, 15),
                                   source.effective(date(2026, 11, 15)))
        self.assertIn("@CoachOne", reminder)
        self.assertIn("محمد عراطة", reminder)
        self.assertIn("<code>@nour.subahi</code>", reminder)


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return SimpleNamespace(message_id=42)


class SheetReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ShiftStore(Path(self.temp.name) / "shift.db")

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_temporary_sheet_failure_does_not_claim_or_send(self):
        catalog = FakeCatalog(SheetsError("offline"))
        source = SheetShiftSchedule("id", None, starting_year=2026, catalog=catalog)
        bot = FakeBot()
        context = SimpleNamespace(bot=bot)
        now = datetime(2026, 11, 14, 21, 0, tzinfo=ZoneInfo("Asia/Damascus"))
        with self.assertRaises(SheetsError):
            await check_shift_reminder(
                context, store=self.store, group_id=-1001234,
                now=now, sheet_schedule=source,
            )
        self.assertEqual(bot.messages, [])
        self.assertEqual(self.store.history(), [])

        catalog.rows = workbook([None, "الأحد\n(15/11)", *NAMES])
        self.assertTrue(await check_shift_reminder(
            context, store=self.store, group_id=-1001234,
            now=now, sheet_schedule=source,
        ))
        self.assertFalse(await check_shift_reminder(
            context, store=self.store, group_id=-1001234,
            now=now, sheet_schedule=source,
        ))
        self.assertEqual(len(bot.messages), 1)
        self.assertEqual(self.store.history()[0]["status"], "sent")
