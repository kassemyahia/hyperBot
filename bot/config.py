"""Environment-backed application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(RuntimeError):
    """Raised when required configuration is invalid or missing."""


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Load a simple .env file without overriding real environment variables."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, value)


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"Missing required environment variable: {name}")
    return value


def _integer(name: str) -> int:
    value = _required(name)
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def _integer_set(name: str, *, required: bool = True) -> frozenset[int]:
    raw = _required(name) if required else os.getenv(name, "")
    try:
        values = frozenset(int(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as exc:
        raise ConfigurationError(f"{name} must contain comma-separated integers") from exc
    if required and not values:
        raise ConfigurationError(f"{name} must contain at least one user ID")
    return values


def _boolean(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, str(default)).strip().casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _database_url() -> str:
    value = os.getenv("DATABASE_URL", "sqlite:///data/hyperbot.db").strip()
    if not value.startswith("sqlite:///"):
        raise ConfigurationError(
            "Only sqlite:/// DATABASE_URL values are currently supported"
        )
    return value


@dataclass(frozen=True, slots=True)
class CatalogSettings:
    """Minimal, secret-free settings needed to export website links."""

    bot_username: str
    database_url: str
    google_sheet_id: str | None = None
    google_application_credentials: str | None = None

    @classmethod
    def from_env(
        cls, *, dotenv_path: Path = Path(".env")
    ) -> "CatalogSettings":
        _load_dotenv(dotenv_path)
        return cls(
            bot_username=_required("BOT_USERNAME").lstrip("@"),
            database_url=_database_url(),
            google_sheet_id=os.getenv("GOOGLE_SHEET_ID", "").strip() or None,
            google_application_credentials=os.getenv(
                "GOOGLE_APPLICATION_CREDENTIALS", ""
            ).strip() or None,
        )

    @property
    def database_path(self) -> Path:
        return Path(self.database_url.removeprefix("sqlite:///"))


@dataclass(frozen=True, slots=True)
class ImportSettings:
    source_chat_id: int
    telegram_api_id: int
    telegram_api_hash: str
    telethon_session_path: Path
    database_url: str
    google_sheet_id: str | None
    google_application_credentials: str | None
    bot_username: str
    auto_approve_exact_matches: bool
    auto_sync_google_sheets: bool

    @classmethod
    def from_env(cls, *, dotenv_path: Path = Path(".env")) -> "ImportSettings":
        _load_dotenv(dotenv_path)
        source = os.getenv("SOURCE_CHAT_ID", os.getenv("SOURCE_CHANNEL_ID", "")).strip()
        if not source:
            raise ConfigurationError("SOURCE_CHAT_ID is required")
        session = Path(os.getenv("TELETHON_SESSION_PATH", "data/telethon/hyper-vault"))
        return cls(
            source_chat_id=int(source), telegram_api_id=_integer("TELEGRAM_API_ID"),
            telegram_api_hash=_required("TELEGRAM_API_HASH"),
            telethon_session_path=session, database_url=_database_url(),
            google_sheet_id=os.getenv("GOOGLE_SHEET_ID", "").strip() or None,
            google_application_credentials=os.getenv(
                "GOOGLE_APPLICATION_CREDENTIALS", ""
            ).strip() or None,
            bot_username=os.getenv("BOT_USERNAME", "HyperTeamITE_Bot").lstrip("@"),
            auto_approve_exact_matches=_boolean("AUTO_APPROVE_EXACT_MATCHES"),
            auto_sync_google_sheets=_boolean("AUTO_SYNC_GOOGLE_SHEETS", True),
        )

    @property
    def database_path(self) -> Path:
        return Path(self.database_url.removeprefix("sqlite:///"))


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    bot_username: str
    source_channel_id: int
    download_request_chat_id: int
    complaint_recipient_chat_id: int
    admin_user_ids: frozenset[int]
    database_url: str
    log_level: str
    rate_limit_requests: int
    rate_limit_window_seconds: int
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    telethon_session_path: str = "data/telethon/hyper-vault"
    google_sheet_id: str | None = None
    google_application_credentials: str | None = None
    included_topic_ids: frozenset[int] = frozenset()
    excluded_topic_ids: frozenset[int] = frozenset()
    auto_approve_exact_matches: bool = False
    auto_sync_google_sheets: bool = True
    min_file_size_bytes: int = 1
    source_chat_username: str | None = None
    shift_group_chat_id: int | None = None
    shift_timezone: str = "Asia/Damascus"
    shift_sheet_id: str | None = None
    shift_sheet_tab: str = "Hyper Shifts"
    shift_sheet_year: int | None = None
    catalog_reconcile_enabled: bool = False
    catalog_auto_register: bool = False

    @classmethod
    def from_env(cls, *, dotenv_path: Path = Path(".env")) -> "Settings":
        _load_dotenv(dotenv_path)
        username = _required("BOT_USERNAME").lstrip("@")
        database_url = _database_url()
        try:
            requests = int(os.getenv("RATE_LIMIT_REQUESTS", "5"))
            window = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
        except ValueError as exc:
            raise ConfigurationError("Rate-limit settings must be integers") from exc
        if requests < 1 or window < 1:
            raise ConfigurationError("Rate-limit settings must be positive")
        source_raw = os.getenv("SOURCE_CHAT_ID", "").strip()
        if not source_raw:
            source_raw = os.getenv("SOURCE_CHANNEL_ID", "").strip()
        if not source_raw:
            raise ConfigurationError(
                "Missing required environment variable: SOURCE_CHAT_ID "
                "(SOURCE_CHANNEL_ID is accepted for compatibility)"
            )
        shift_sheet_id = os.getenv("SHIFT_SHEET_ID", "").strip() or None
        sheet_year_raw = os.getenv("SHIFT_SHEET_YEAR", "").strip()
        try:
            shift_sheet_year = int(sheet_year_raw) if sheet_year_raw else None
        except ValueError as exc:
            raise ConfigurationError("SHIFT_SHEET_YEAR must be a four-digit year") from exc
        if shift_sheet_id and not (shift_sheet_year and 2000 <= shift_sheet_year <= 2100):
            raise ConfigurationError("SHIFT_SHEET_YEAR is required with SHIFT_SHEET_ID")
        shift_sheet_tab = os.getenv("SHIFT_SHEET_TAB", "Hyper Shifts").strip()
        if shift_sheet_id and not shift_sheet_tab:
            raise ConfigurationError("SHIFT_SHEET_TAB cannot be blank")
        try:
            source_chat_id = int(source_raw)
            api_id = int(os.getenv("TELEGRAM_API_ID", "0")) or None
            min_size = int(os.getenv("MIN_FILE_SIZE_BYTES", "1"))
            shift_group_raw = os.getenv("SHIFT_GROUP_CHAT_ID", "").strip()
            shift_group_chat_id = int(shift_group_raw) if shift_group_raw else None
            if shift_group_chat_id == 0:
                shift_group_chat_id = None
        except ValueError as exc:
            raise ConfigurationError(
                "SOURCE_CHAT_ID, TELEGRAM_API_ID, MIN_FILE_SIZE_BYTES, and SHIFT_GROUP_CHAT_ID must be integers"
            ) from exc
        if source_chat_id >= 0:
            raise ConfigurationError("SOURCE_CHAT_ID must be a negative group/chat ID")
        if min_size < 0:
            raise ConfigurationError("MIN_FILE_SIZE_BYTES cannot be negative")
        if shift_group_chat_id is not None and (
            shift_group_chat_id >= 0 or shift_group_chat_id == source_chat_id
        ):
            raise ConfigurationError("SHIFT_GROUP_CHAT_ID must be a separate, negative group ID")
        return cls(
            bot_token=_required("BOT_TOKEN"),
            bot_username=username,
            source_channel_id=source_chat_id,
            download_request_chat_id=_integer("DOWNLOAD_REQUEST_CHAT_ID"),
            complaint_recipient_chat_id=_integer("COMPLAINT_RECIPIENT_CHAT_ID"),
            admin_user_ids=_integer_set("ADMIN_USER_IDS"),
            database_url=database_url,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            rate_limit_requests=requests,
            rate_limit_window_seconds=window,
            telegram_api_id=api_id,
            telegram_api_hash=os.getenv("TELEGRAM_API_HASH", "").strip() or None,
            telethon_session_path=os.getenv(
                "TELETHON_SESSION_PATH", "data/telethon/hyper-vault"
            ).strip(),
            google_sheet_id=os.getenv("GOOGLE_SHEET_ID", "").strip() or None,
            google_application_credentials=os.getenv(
                "GOOGLE_APPLICATION_CREDENTIALS", ""
            ).strip() or None,
            included_topic_ids=_integer_set("INCLUDED_TOPIC_IDS", required=False),
            excluded_topic_ids=_integer_set("EXCLUDED_TOPIC_IDS", required=False),
            auto_approve_exact_matches=_boolean("AUTO_APPROVE_EXACT_MATCHES"),
            auto_sync_google_sheets=_boolean("AUTO_SYNC_GOOGLE_SHEETS", True),
            min_file_size_bytes=min_size,
            source_chat_username=os.getenv(
                "SOURCE_CHAT_USERNAME", ""
            ).strip().lstrip("@") or None,
            shift_group_chat_id=shift_group_chat_id,
            shift_timezone=os.getenv("SHIFT_TIMEZONE", "Asia/Damascus").strip(),
            shift_sheet_id=shift_sheet_id,
            shift_sheet_tab=shift_sheet_tab,
            shift_sheet_year=shift_sheet_year,
            catalog_reconcile_enabled=_boolean("CATALOG_RECONCILE_ENABLED"),
            catalog_auto_register=_boolean("CATALOG_AUTO_REGISTER"),

        )

    @property
    def database_path(self) -> Path:
        return Path(self.database_url.removeprefix("sqlite:///"))

    @property
    def source_chat_id(self) -> int:
        """Forum supergroup ID; old code may still use source_channel_id."""
        return self.source_channel_id
