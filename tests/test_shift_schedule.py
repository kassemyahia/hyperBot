"""Offline regression tests for weekly shifts and durable reminders."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bot.services.shift_schedule import (
    ShiftStore, check_shift_reminder, day_index, format_reminder,
)

SIX = ["CoachOne", "CocoachTwo", "MemberThree", "MemberFour", "MemberFive", "MemberSix"]


class ShiftStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ShiftStore(Path(self.temp.name) / "hyperbot.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_day_indexes(self):
        self.assertEqual(day_index(date(2026, 11, 15)), 0)
        self.assertEqual(day_index(date(2026, 11, 19)), 4)
        self.assertIsNone(day_index(date(2026, 11, 20)))
        self.assertIsNone(day_index(date(2026, 11, 21)))

    def test_weekly_roster_and_durable_reopen(self):
        self.store.set_weekly(0, SIX)
        self.assertEqual(self.store.effective(date(2026, 11, 15)).names, tuple(SIX))
        reopened = ShiftStore(Path(self.temp.name) / "hyperbot.db")
        self.assertTrue(reopened.effective(date(2026, 11, 22)).complete)

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.store.set_weekly(5, SIX)
        with self.assertRaises(ValueError):
            self.store.set_weekly(0, SIX[:-1])
        with self.assertRaises(ValueError):
            self.store.set_weekly(0, SIX[:-1] + [SIX[0].upper()])

    def test_date_override_and_cancellation(self):
        d = date(2026, 11, 16)
        self.store.set_weekly(1, SIX)
        replaced = ["AnotherCoach", *SIX[1:]]
        self.store.set_override(d, replaced)
        self.assertEqual(self.store.effective(d).names, tuple(replaced))
        self.assertEqual(self.store.effective(date(2026, 11, 23)).names, tuple(SIX))
        self.store.cancel(d)
        self.assertFalse(self.store.effective(d).complete)
        self.store.uncancel(d)
        self.assertTrue(self.store.effective(d).complete)
        self.store.clear_override(d)
        self.assertEqual(self.store.effective(d).names, tuple(SIX))

    def test_reminder_ledger_claim_once(self):
        d = date(2026, 11, 15)
        now = datetime(2026, 11, 14, 18, tzinfo=ZoneInfo("Asia/Damascus"))
        self.assertTrue(self.store.claim(d, now))
        self.assertFalse(self.store.claim(d, now))
        self.store.finish(d, message_id=99)
        self.assertEqual(self.store.history()[0]["status"], "sent")
        self.assertFalse(self.store.claim(d, now))

    def test_escaped_mentions(self):
        d = date(2026, 11, 15)
        self.store.set_weekly(0, SIX)
        result = format_reminder(d, self.store.effective(d))
        self.assertIn("@CoachOne", result)
        self.assertEqual(result, (
            "🔔 تذكير بالمناوبة\n"
            "<b>الكوتش:</b> @CoachOne\n"
            "<b>مساعد الكوتش:</b> @CocoachTwo\n"
            "<b>أعضاء المناوبة:</b>\n"
            "@MemberThree\n@MemberFour\n@MemberFive\n@MemberSix\n"
            "نذكّركم المناوبة غداً.\n"
            "يرجى الالتزام بالموعد المحدد، وشكراً"
        ))
        self.assertEqual(result.count("@"), 6)


class FakeBot:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        if self.fail:
            raise TimeoutError("simulate uncertain Telegram response")
        return SimpleNamespace(message_id=999)


class ShiftSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ShiftStore(Path(self.temp.name) / "db.sqlite")
        self.store.set_weekly(0, SIX)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_saturday_evening_sends_sunday_once(self):
        bot = FakeBot()
        now = datetime(2026, 11, 14, 18, 0, tzinfo=ZoneInfo("Asia/Damascus"))
        context = SimpleNamespace(bot=bot)
        self.assertTrue(await check_shift_reminder(context, store=self.store,
                         group_id=-1001234, now=now))
        self.assertFalse(await check_shift_reminder(context, store=self.store,
                          group_id=-1001234, now=now))
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(bot.sent[0]["chat_id"], -1001234)

    async def test_no_off_schedule_send(self):
        bot = FakeBot()
        context = SimpleNamespace(bot=bot)
        for moment in [
            datetime(2026, 11, 14, 17, 59, tzinfo=ZoneInfo("Asia/Damascus")),
            datetime(2026, 11, 14, 18, 16, tzinfo=ZoneInfo("Asia/Damascus")),
            datetime(2026, 11, 19, 18, 0, tzinfo=ZoneInfo("Asia/Damascus")),
        ]:
            self.assertFalse(await check_shift_reminder(context, store=self.store,
                             group_id=-1001234, now=moment))
        self.assertEqual(bot.sent, [])

    async def test_uncertain_delivery_does_not_auto_retry(self):
        bot = FakeBot(fail=True)
        context = SimpleNamespace(bot=bot)
        now = datetime(2026, 11, 14, 18, 0, tzinfo=ZoneInfo("Asia/Damascus"))
        with self.assertRaises(TimeoutError):
            await check_shift_reminder(context, store=self.store, group_id=-1001234, now=now)
        self.assertFalse(await check_shift_reminder(context, store=self.store,
                         group_id=-1001234, now=now))
        self.assertEqual(self.store.history()[0]["status"], "uncertain")


    async def test_configured_topic_and_time(self):
        bot = FakeBot()
        context = SimpleNamespace(bot=bot)
        for hour, minute in [(17, 59), (18, 16), (21, 0)]:
            self.assertFalse(await check_shift_reminder(context, store=self.store,
                group_id=-1001234, topic_id=353, reminder_time="18:00",
                now=datetime(2026, 11, 14, hour, minute, tzinfo=ZoneInfo("Asia/Damascus"))))
        now = datetime(2026, 11, 14, 18, 0, tzinfo=ZoneInfo("Asia/Damascus"))
        self.assertTrue(await check_shift_reminder(context, store=self.store,
            group_id=-1001234, topic_id=353, reminder_time="18:00", now=now))
        self.assertEqual(bot.sent[0]["message_thread_id"], 353)
        self.assertFalse(await check_shift_reminder(context, store=self.store,
            group_id=-1001234, topic_id=353, reminder_time="18:00", now=now))

    async def test_custom_time_and_general_topic(self):
        bot = FakeBot()
        context = SimpleNamespace(bot=bot)
        self.assertTrue(await check_shift_reminder(context, store=self.store,
            group_id=-1001234, topic_id=1, reminder_time="19:50",
            now=datetime(2026, 11, 14, 20, 5, tzinfo=ZoneInfo("Asia/Damascus"))))
        self.assertNotIn("message_thread_id", bot.sent[0])
