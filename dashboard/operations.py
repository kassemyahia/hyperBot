"""Shared server-side operations for local dashboard and standalone Hyperdash."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.config import Settings
from bot.database.repository import SQLiteFileRepository
from bot.services.catalog_reconciliation import reconcile_catalog, source_url
from bot.services.file_registry import FileRegistry
from bot.services.google_sheets import GoogleSheetsCatalog
from bot.services.review import ReviewService
from bot.services.shift_schedule import ShiftStore
from bot.services.shift_sheet import SheetShiftSchedule


def service_context():
    settings = Settings.from_env()
    repo = SQLiteFileRepository(settings.database_path)
    sheets = (GoogleSheetsCatalog(settings.google_sheet_id, settings.google_application_credentials)
              if settings.google_sheet_id else None)
    store = ShiftStore(settings.database_path)
    return settings, repo, sheets, store


def snapshot(settings, repo, sheets, store):
    entries = []
    sheet_error = None
    if sheets:
        try:
            entries = sheets.load_entries()
        except Exception as exc:
            sheet_error = type(exc).__name__
    candidates = []
    for c in repo.list_candidates(limit=100):
        m = repo.get_discovered(c.discovered_message_id)
        if not m:
            continue
        candidates.append({
            "candidate": c, "message": m,
            "source_url": source_url(settings.source_chat_username, m.topic_id, m.message_id)
            if settings.source_chat_username else ""
        })
    today = datetime.now(ZoneInfo(settings.shift_timezone)).date()
    shift_sheet_enabled = bool(settings.shift_sheet_id)
    shift_sheet_error = None
    if shift_sheet_enabled:
        try:
            assert settings.shift_sheet_year is not None
            shift_source = SheetShiftSchedule(
                settings.shift_sheet_id, settings.google_application_credentials,
                sheet_name=settings.shift_sheet_tab,
                starting_year=settings.shift_sheet_year,
            )
            shifts = [
                shift for shift_date, shift in sorted(shift_source.load().items())
                if shift_date >= today
            ][:30]
        except Exception as exc:
            shifts = []
            shift_sheet_error = type(exc).__name__
    else:
        shifts = store.upcoming(today, 14)

    return {
        "candidates": candidates, "entries": entries, "records": repo.list()[:100],
        "shifts": shifts, "weekly": store.get_weekly(), "history": store.history(),
        "today": today, "sheet_error": sheet_error, "shift_sheet_enabled": shift_sheet_enabled,
        "shift_sheet_error": shift_sheet_error,
        "shift_sheet_url": ("https://docs.google.com/spreadsheets/d/" + settings.shift_sheet_id + "/edit")
                           if settings.shift_sheet_id else "",
        "group_configured": bool(settings.shift_group_chat_id),
    }


def verified_candidate(repo, settings, candidate_id: int):
    candidate = repo.get_candidate(candidate_id)
    if candidate is None or candidate.status not in ("exact", "ambiguous", "unmatched"):
        raise ValueError("Pending candidate not found")
    message = repo.get_discovered(candidate.discovered_message_id)
    if message is None or message.source_chat_id != settings.source_chat_id:
        raise ValueError("Not a discovery from the configured Hyper Vault")
    if not message.filename and not message.mime_type:
        raise ValueError("Not a file")
    if repo.get_by_source_message(message.source_chat_id, message.message_id):
        raise ValueError("Source message already registered")
    if not settings.source_chat_username:
        raise ValueError("Set SOURCE_CHAT_USERNAME to generate source URLs")
    return candidate, message


def row_version(entry):
    # Detect stale selections without exposing the original vault URL.
    fields = [entry.sheet_name, entry.row_number, entry.file_id, entry.title,
              entry.source_type, entry.telegram_link, entry.telegram_status]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()


def link_candidate(settings, repo, sheets, candidate_id, sheet, row_number, expected_version=None):
    if not sheets:
        raise ValueError("Google Sheets is not configured")
    candidate, message = verified_candidate(repo, settings, candidate_id)
    entries = sheets.load_entries()
    targets = [e for e in entries if e.sheet_name == sheet and e.row_number == row_number]
    if len(targets) != 1:
        raise ValueError("Catalog row missing or changed")
    target = targets[0]
    if expected_version is not None and row_version(target) != expected_version:
        raise ValueError("Catalog row changed; refresh before linking")
    link = source_url(settings.source_chat_username, message.topic_id, message.message_id)
    # Do not steal another registered resource or silently overwrite a link.
    if target.source_type and target.source_type.startswith("https://t.me/") and target.source_type != link:
        raise ValueError("Catalog row has a different Telegram source. Resolve it first.")
    if not target.file_id:
        target = sheets.ensure_entry_file_id(
            sheet_name=sheet, row_number=row_number,
            requested_file_id=f"sw_{secrets.token_hex(12)}" if sheet == "softwares"
                              else f"course_{secrets.token_hex(12)}",
            expected_title=target.title,
            expected_source_url=target.source_type or "", approved=True,
        )
    sheets.update_source_link(
        sheet_name=sheet, row_number=row_number,
        expected_title=target.title, expected_source_url=target.source_type or "",
        source_url=link,
    )
    fresh = sheets.load_entries()
    review = ReviewService(
        repo, FileRegistry(repo, settings.source_chat_id),
        fresh, settings.bot_username, sheets,
    )
    outcome = review.approve(candidate_id, -1, file_id=target.file_id,
                             discovered_ids=(message.id,))
    return {"synchronized": outcome.synchronized}


def create_candidate(settings, repo, sheets, candidate_id, sheet, title, size="", path="", category="", details=""):
    if not sheets:
        raise ValueError("Google Sheets is not configured")
    if not title.strip():
        raise ValueError("File title is required")
    _, message = verified_candidate(repo, settings, candidate_id)
    source = source_url(settings.source_chat_username, message.topic_id, message.message_id)
    file_id = ("sw_" if sheet == "softwares" else "course_") + secrets.token_hex(12)
    sheets.append_catalog_entry(
        sheet, file_id=file_id, title=title.strip(), size=size.strip(),
        path=path.strip(), category=category.strip(), source_url=source,
        details=details.strip(),
    )
    fresh = sheets.load_entries()
    review = ReviewService(
        repo, FileRegistry(repo, settings.source_chat_id),
        fresh, settings.bot_username, sheets,
    )
    outcome = review.approve(candidate_id, -1, file_id=file_id,
                             discovered_ids=(message.id,))
    return {"synchronized": outcome.synchronized}


def reconcile_files(settings, repo, sheets):
    if not sheets or not settings.source_chat_username:
        raise ValueError("Configure Google Sheets and SOURCE_CHAT_USERNAME")
    return reconcile_catalog(repo, sheets, source_chat_id=settings.source_chat_id,
                             source_username=settings.source_chat_username,
                             bot_username=settings.bot_username, auto_register=True)


def add_catalog_entry(settings, repo, sheets, sheet, request_id, title, size="", path="",
                      category="", source="", details=""):
    from urllib.parse import urlsplit
    from bot.services.matching import FileMatcher, parse_telegram_source_url

    if not sheets:
        raise ValueError("Google Sheets is not configured")
    title, size, path, category, source, details = (
        value.strip() for value in (title, size, path, category, source, details)
    )
    if not title:
        raise ValueError("File title is required")
    pending = False
    if source:
        url = urlsplit(source)
        if url.scheme != "https" or not url.hostname or url.username or url.password or any(c.isspace() for c in source):
            raise ValueError("Use an HTTPS source URL")
        ref = parse_telegram_source_url(source)
        if ref is not None:
            matcher = FileMatcher(source_chat_id=settings.source_chat_id,
                                  source_username=settings.source_chat_username)
            if not matcher._is_configured_source(ref):
                raise ValueError("Telegram source must belong to the configured vault")
            # Normalize public and private vault links to one public reference.
            source = source_url(settings.source_chat_username, ref.topic_id, ref.message_id)
            pending = True
        elif url.hostname.casefold() in ("t.me", "www.t.me", "telegram.me", "www.telegram.me"):
            raise ValueError("Use a Telegram file message link, not a bot or invite link")
    file_id = ("sw_" if sheet == "softwares" else "course_") + request_id
    entry = sheets.append_catalog_only(
        sheet, file_id=file_id, title=title, size=size, path=path,
        category=category, source_url=source, details=details, pending=pending,
    )
    return {"created": True, "file_id": entry.file_id, "status": entry.telegram_status}
