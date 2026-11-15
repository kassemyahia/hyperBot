"""Catalog reconciliation tests: exact source links only, stable tokens, no fuzzy publishing."""
from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from dataclasses import replace

from bot.database.models import CatalogEntry
from bot.database.repository import SQLiteFileRepository
from bot.services.catalog_reconciliation import reconcile_catalog


class FakeSheets:
    def __init__(self, entries):
        self.entries = entries
        self.writes = []
        self.ids_created = 0

    def load_entries(self):
        return self.entries

    def ensure_entry_file_id(self, *, sheet_name, row_number, requested_file_id,
                             expected_title, expected_source_url, approved):
        assert approved
        assert expected_title == self.entries[0].title
        assert expected_source_url == self.entries[0].source_type
        self.ids_created += 1
        entry = replace(self.entries[0], file_id=requested_file_id)
        self.entries[0] = entry
        return entry

    def update_registration(self, file_id, link, status):
        self.writes.append((file_id, link, status))
        self.entries = [
            replace(entry, telegram_link=link, telegram_status=status)
            if entry.file_id == file_id else entry for entry in self.entries
        ]


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteFileRepository(Path(self.tmp.name) / "test.db")
        self.source = -1001234567890

    def tearDown(self):
        self.tmp.cleanup()

    def discovery(self, message_id):
        return self.repo.add_discovered_message(
            source_chat_id=self.source, message_id=message_id,
            topic_id=84, topic_title="Browsers", filename="Firefox.zip",
            caption=None, file_size=1024, mime_type="application/zip",
            media_group_id=None, message_date=datetime.now(UTC),
        )

    def entry(self, file_id=None, url="https://t.me/Hyper_Vault/84/999"):
        return CatalogEntry(
            file_id=file_id, title="Firefox", size="1KB", category="Browsers",
            source_type=url, sheet_name="softwares", row_number=200,
        )

    def run_reconcile(self, sheets, auto=True):
        return reconcile_catalog(
            self.repo, sheets, source_chat_id=self.source,
            source_username="Hyper_Vault",
            bot_username="HyperTeamITE_Bot", auto_register=auto,
        )

    def test_verified_source_link_generates_id_and_link_once(self):
        self.discovery(999)
        sheets = FakeSheets([self.entry()])
        result = self.run_reconcile(sheets)
        self.assertEqual(result["linked"], 1)
        self.assertEqual(sheets.ids_created, 1)
        record = self.repo.get_by_source_message(self.source, 999)
        self.assertIsNotNone(record)
        self.assertTrue(sheets.writes[0][1].startswith(
            "https://t.me/HyperTeamITE_Bot?start=f_"))
        token = record.public_token
        again = self.run_reconcile(sheets)
        self.assertEqual(again["linked"], 0)
        self.assertEqual(self.repo.get_by_source_message(self.source, 999).public_token, token)

    def test_reject_unrelated_group_link(self):
        self.discovery(999)
        sheets = FakeSheets([self.entry("sw_firefox", "https://t.me/OtherGroup/84/999")])
        result = self.run_reconcile(sheets)
        self.assertEqual(result["linked"], 0)

    def test_duplicate_source_urls_are_quarantined(self):
        self.discovery(999)
        rows = [self.entry("sw_firefox"), replace(self.entry("sw_other"), row_number=201)]
        sheets = FakeSheets(rows)
        result = self.run_reconcile(sheets)
        self.assertEqual(result["linked"], 0)

    def test_only_source_link_not_filename_can_auto_publish(self):
        self.discovery(999)
        sheets = FakeSheets([self.entry("sw_firefox", "")])
        result = self.run_reconcile(sheets)
        self.assertEqual(result["linked"], 0)

    def test_auto_register_off(self):
        self.discovery(999)
        sheets = FakeSheets([self.entry("sw_firefox")])
        result = self.run_reconcile(sheets, auto=False)
        self.assertEqual(result["linked"], 0)
