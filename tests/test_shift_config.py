"""Optional shift group env setting must never prevent dashboard login."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from bot.config import ConfigurationError, Settings


class ShiftGroupConfigTests(unittest.TestCase):
    def settings_with(self, value):
        env = {
            "BOT_TOKEN": "test-only",
            "BOT_USERNAME": "HyperTeamITE_Bot",
            "SOURCE_CHAT_ID": "-1001234567890",
            "DOWNLOAD_REQUEST_CHAT_ID": "-1001234567891",
            "COMPLAINT_RECIPIENT_CHAT_ID": "1234",
            "ADMIN_USER_IDS": "1234",
            "AUTO_APPROVE_EXACT_MATCHES": "false",
            "AUTO_SYNC_GOOGLE_SHEETS": "false",
        }
        if value is not None:
            env["SHIFT_GROUP_CHAT_ID"] = value
        with patch.dict("os.environ", env, clear=True):
            return Settings.from_env(dotenv_path=Path("/nonexistent-shift-test.env"))

    def test_empty_value_is_disabled(self):
        self.assertIsNone(self.settings_with("").shift_group_chat_id)

    def test_whitespace_is_disabled(self):
        self.assertIsNone(self.settings_with("  ").shift_group_chat_id)

    def test_missing_value_is_disabled(self):
        self.assertIsNone(self.settings_with(None).shift_group_chat_id)

    def test_zero_is_disabled(self):
        self.assertIsNone(self.settings_with("0").shift_group_chat_id)

    def test_valid_group_id_is_accepted(self):
        self.assertEqual(self.settings_with("-1009876543210").shift_group_chat_id,
                         -1009876543210)

    def test_placeholder_is_rejected_with_clear_error(self):
        with self.assertRaises(ConfigurationError):
            self.settings_with("-100...")

    def test_source_group_cannot_be_shift_destination(self):
        with self.assertRaises(ConfigurationError):
            self.settings_with("-1001234567890")
