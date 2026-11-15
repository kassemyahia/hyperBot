"""Catalog reconciliation: trusted spreadsheet source links -> bot registrations.

Only exact, unique source URLs with an already discovered media message can be
auto-registered. Filename similarity never authorizes unattended publication.
"""
from __future__ import annotations

import logging
import secrets

from bot.database.repository import SQLiteFileRepository
from bot.services.deep_links import build_deep_link
from bot.services.file_registry import FileRegistry
from bot.services.google_sheets import GoogleSheetsCatalog
from bot.services.matching import FileMatcher, parse_telegram_source_url

logger = logging.getLogger(__name__)


def source_url(source_username: str, topic_id: int | None, message_id: int) -> str:
    if not source_username or not source_username.replace("_", "").isalnum():
        raise ValueError("SOURCE_CHAT_USERNAME must be configured")
    if topic_id:
        return f"https://t.me/{source_username}/{topic_id}/{message_id}"
    return f"https://t.me/{source_username}/{message_id}"


def reconcile_catalog(
    repository: SQLiteFileRepository,
    sheets: GoogleSheetsCatalog,
    *, source_chat_id: int,
    source_username: str,
    bot_username: str,
    auto_register: bool = False,
    limit: int = 25,
) -> dict[str, int]:
    """Refresh live catalog and re-sync already registered files.

    If auto_register is enabled, only *trusted*, unique source links matching
    recorded Telegram discoveries are published. If the file is new to Telegram
    and the bot was offline, the historical importer must discover it first.
    """
    counts = {"scanned": 0, "linked": 0, "repaired": 0, "skipped": 0, "errors": 0}
    entries = sheets.load_entries()
    matcher = FileMatcher(source_chat_id=source_chat_id,
                          source_username=source_username)
    refs: dict[int, list] = {}
    for entry in entries:
        ref = parse_telegram_source_url(entry.source_type)
        if ref is None or not matcher._is_configured_source(ref):
            continue
        refs.setdefault(ref.message_id, []).append(entry)
    for entry in entries:
        if counts["linked"] + counts["repaired"] >= limit:
            break
        record = repository.get_by_website_id(entry.file_id) if entry.file_id else None
        if record is not None:
            expected_link = build_deep_link(bot_username, record.public_token)
            expected_status = "available" if record.active else "unavailable"
            if entry.telegram_link != expected_link or entry.telegram_status != expected_status:
                try:
                    sheets.update_registration(entry.file_id, expected_link, expected_status)
                    repository.set_sync_state(entry.file_id, entry.sheet_name, expected_status)
                    counts["repaired"] += 1
                except Exception:
                    logger.exception("Catalog link repair failed for File ID")
                    counts["errors"] += 1
            continue
        ref = parse_telegram_source_url(entry.source_type)
        if ref is None or not matcher._is_configured_source(ref):
            continue
        counts["scanned"] += 1
        if not auto_register:
            counts["skipped"] += 1
            continue
        if len(refs.get(ref.message_id, [])) != 1:
            counts["skipped"] += 1
            continue
        discovery = repository.get_discovered_message(source_chat_id, ref.message_id)
        if discovery is None or discovery.state in {"ignored", "registered"}:
            counts["skipped"] += 1
            continue
        # The stored source was received from the configured Telegram group.
        if ref.topic_id is not None and discovery.topic_id != ref.topic_id:
            counts["skipped"] += 1
            continue
        if repository.get_by_source_message(source_chat_id, ref.message_id):
            counts["skipped"] += 1
            continue
        try:
            if not entry.file_id:
                entry = sheets.ensure_entry_file_id(
                    sheet_name=entry.sheet_name, row_number=entry.row_number,
                    requested_file_id=(
                        "sw_" if entry.sheet_name == "softwares" else "course_"
                    ) + secrets.token_hex(12),
                    expected_title=entry.title,
                    expected_source_url=entry.source_type,
                    approved=True,
                )
            registry = FileRegistry(repository, source_chat_id)
            registry.register(entry.file_id, entry.title, (ref.message_id,))
            record = repository.get_by_website_id(entry.file_id)
            assert record is not None
            repository.mark_related_registered((discovery.id,))
            # If synchronization fails, the next tick repairs the same record
            # without changing its random public token.
            sheets.update_registration(
                entry.file_id, build_deep_link(bot_username, record.public_token),
                "available",
            )
            repository.set_sync_state(entry.file_id, entry.sheet_name, "available")
            counts["linked"] += 1
        except Exception:
            counts["errors"] += 1
            logger.exception("Catalog source reconciliation failed")
    return counts
