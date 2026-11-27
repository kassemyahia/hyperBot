"""Durable weekly shift roster and one-shot reminder ledger.

Day indexes are Sunday=0 through Thursday=4. Reminder state is stored in
the existing SQLite database, not in APScheduler's in-memory job store.
"""
from __future__ import annotations

import asyncio
import html
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
ROLES = ("coach", "co_coach", "member", "member", "member", "member")
DAY_NAMES_AR = ("الأحد", "الاثنين", "الثلاثاء", "الأربعاء", "الخميس")


def normalized_username(raw: str) -> str:
    name = raw.strip().lstrip("@")
    if not USERNAME.fullmatch(name):
        raise ValueError("Telegram username must contain 5-32 letters, digits or underscores")
    return name


def day_index(value: date) -> int | None:
    # Python Monday=0; the team's schedule starts on Sunday.
    day = (value.weekday() + 1) % 7
    return day if day < 5 else None


@dataclass(frozen=True)
class Shift:
    date: str
    names: tuple[str, ...]
    cancelled: bool
    complete: bool


class ShiftStore:
    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS shift_weekly (
                    day INTEGER NOT NULL CHECK(day BETWEEN 0 AND 4),
                    slot INTEGER NOT NULL CHECK(slot BETWEEN 0 AND 5),
                    username TEXT NOT NULL,
                    PRIMARY KEY(day, slot)
                );
                CREATE TABLE IF NOT EXISTS shift_overrides (
                    date TEXT NOT NULL,
                    slot INTEGER NOT NULL CHECK(slot BETWEEN 0 AND 5),
                    username TEXT NOT NULL,
                    PRIMARY KEY(date, slot)
                );
                CREATE TABLE IF NOT EXISTS shift_cancellations (
                    date TEXT PRIMARY KEY,
                    reason TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS shift_reminders (
                    date TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    attempted_at TEXT NOT NULL,
                    sent_at TEXT,
                    message_id INTEGER,
                    error TEXT
                );
            """)

    @staticmethod
    def _validate_six(usernames: list[str]) -> list[str]:
        if len(usernames) != 6:
            raise ValueError("A shift must contain exactly six usernames")
        cleaned = [normalized_username(name) for name in usernames]
        if len({name.casefold() for name in cleaned}) != 6:
            raise ValueError("A person cannot occupy two roles in the same shift")
        return cleaned

    def set_weekly(self, day: int, usernames: list[str]) -> None:
        if day not in range(5):
            raise ValueError("Shifts operate Sunday through Thursday only")
        cleaned = self._validate_six(usernames)
        with self._connect() as conn:
            conn.execute("DELETE FROM shift_weekly WHERE day=?", (day,))
            conn.executemany(
                "INSERT INTO shift_weekly(day,slot,username) VALUES(?,?,?)",
                [(day, slot, username) for slot, username in enumerate(cleaned)],
            )

    def set_override(self, when: date, usernames: list[str]) -> None:
        if day_index(when) is None:
            raise ValueError("No shift on Friday or Saturday")
        cleaned = self._validate_six(usernames)
        with self._connect() as conn:
            conn.execute("DELETE FROM shift_overrides WHERE date=?", (when.isoformat(),))
            conn.executemany(
                "INSERT INTO shift_overrides(date,slot,username) VALUES(?,?,?)",
                [(when.isoformat(), slot, username) for slot, username in enumerate(cleaned)],
            )
            conn.execute("DELETE FROM shift_cancellations WHERE date=?", (when.isoformat(),))

    def clear_override(self, when: date) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM shift_overrides WHERE date=?", (when.isoformat(),))

    def cancel(self, when: date, reason: str = "") -> None:
        if day_index(when) is None:
            raise ValueError("No scheduled shift on this date")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO shift_cancellations(date,reason) VALUES(?,?) "
                "ON CONFLICT(date) DO UPDATE SET reason=excluded.reason",
                (when.isoformat(), reason[:250]),
            )

    def uncancel(self, when: date) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM shift_cancellations WHERE date=?", (when.isoformat(),))

    def get_weekly(self) -> dict[int, list[str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT day,slot,username FROM shift_weekly ORDER BY day,slot"
            ).fetchall()
        result = {day: [""] * 6 for day in range(5)}
        for row in rows:
            result[row["day"]][row["slot"]] = row["username"]
        return result

    def effective(self, when: date) -> Shift:
        day = day_index(when)
        if day is None:
            return Shift(when.isoformat(), (), False, False)
        with self._connect() as conn:
            cancelled = conn.execute(
                "SELECT 1 FROM shift_cancellations WHERE date=?", (when.isoformat(),)
            ).fetchone() is not None
            rows = conn.execute(
                "SELECT slot,username FROM shift_overrides WHERE date=? ORDER BY slot",
                (when.isoformat(),),
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    "SELECT slot,username FROM shift_weekly WHERE day=? ORDER BY slot",
                    (day,),
                ).fetchall()
        values = [""] * 6
        for row in rows:
            values[row["slot"]] = row["username"]
        return Shift(when.isoformat(), tuple(values), cancelled,
                     not cancelled and all(values) and len({v.casefold() for v in values}) == 6)

    def upcoming(self, start: date, days: int = 14) -> list[Shift]:
        return [
            self.effective(start + timedelta(days=i))
            for i in range(days) if day_index(start + timedelta(days=i)) is not None
        ]

    def claim(self, when: date, now: datetime) -> bool:
        """An attempted send is claimed once, before the network call.

        If the process dies during a call, status remains 'sending'; it must be
        reconciled manually, avoiding duplicate Telegram messages.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO shift_reminders(date,status,attempted_at) "
                "VALUES(?, 'sending', ?)",
                (when.isoformat(), now.isoformat()),
            )
        return cursor.rowcount == 1

    def finish(self, when: date, *, message_id: int | None = None,
               error: str | None = None) -> None:
        status = "sent" if message_id is not None else "uncertain"
        with self._connect() as conn:
            conn.execute(
                "UPDATE shift_reminders SET status=?, sent_at=?, message_id=?, error=? "
                "WHERE date=? AND status='sending'",
                (status, datetime.now(ZoneInfo("UTC")).isoformat() if message_id else None,
                 message_id, error[:120] if error else None, when.isoformat()),
            )

    def history(self, limit: int = 30) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM shift_reminders ORDER BY date DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]


def format_reminder(when: date, shift: Shift) -> str:
    if not shift.complete or day_index(when) is None:
        raise ValueError("Cannot notify an incomplete or cancelled shift")
    day = DAY_NAMES_AR[day_index(when)]
    def mention(name: str) -> str:
        """Tag valid usernames; preserve ordinary names without fake mentions."""
        value = name.strip()
        candidate = value.lstrip("@")
        if USERNAME.fullmatch(candidate):
            return "@" + html.escape(candidate, quote=False)
        if value.startswith("@"):
            # Avoid accidentally tagging a partial username such as @user.name.
            return "<code>" + html.escape(value, quote=False) + "</code>"
        return html.escape(value, quote=False)
    return (
        "📢 <b>تذكير بمناوبة يوم " + day + "</b>\n\n"
        "👨‍🏫 <b>الكوتش:</b> " + mention(shift.names[0]) + "\n"
        "🤝 <b>مساعد الكوتش:</b> " + mention(shift.names[1]) + "\n"
        "👥 <b>أعضاء المناوبة:</b>\n" +
        "\n".join(mention(name) for name in shift.names[2:]) +
        "\n\n⏰ المناوبة غداً — يرجى تأكيد الجاهزية."
    )


async def check_shift_reminder(context, *, store: ShiftStore, group_id: int,
                               timezone: str = "Asia/Damascus",
                               now: datetime | None = None,
                               sheet_schedule: object | None = None,
                               topic_id: int | None = None,
                               reminder_time: str = "18:00") -> bool:
    """Tick every minute, within 15 minutes of the configured local time."""
    local_now = (now or datetime.now(ZoneInfo(timezone))).astimezone(ZoneInfo(timezone))
    hour, minute = map(int, reminder_time.split(":"))
    start = hour * 60 + minute
    current = local_now.hour * 60 + local_now.minute
    # Do not cross midnight: the target is always tomorrow's shift.
    if not start <= current <= min(start + 15, 1439):
        return False
    when = local_now.date() + timedelta(days=1)
    # Read the live roster before claiming a send. A Sheets outage cannot
    # consume a reminder or trigger a fallback to stale local assignments.
    shift = (await asyncio.to_thread(sheet_schedule.effective, when)
             if sheet_schedule is not None else store.effective(when))
    if not shift.complete or not store.claim(when, local_now):
        return False
    try:
        result = await context.bot.send_message(
            chat_id=group_id, text=format_reminder(when, shift),
            parse_mode="HTML", disable_web_page_preview=True,
            **({"message_thread_id": topic_id} if topic_id not in (None, 1) else {}),
        )
    except Exception as exc:
        # Network ambiguity: Telegram might have delivered it despite the error.
        # Never auto-retry without confirming delivery in the destination chat.
        store.finish(when, error=type(exc).__name__)
        raise
    store.finish(when, message_id=result.message_id)
    return True
