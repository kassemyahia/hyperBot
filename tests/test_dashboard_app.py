"""Browser-level security and form integration, with temporary SQLite only."""
from __future__ import annotations

import importlib
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from bot.services.shift_schedule import ShiftStore


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "DASHBOARD_PASSWORD": "local-long-test-password-only",
            "DASHBOARD_SESSION_SECRET": "some-session-secret-for-unit-tests-only-12345",
            "DASHBOARD_SECURE_COOKIES": "false",
        })
        self.env.start()
        module = importlib.import_module("dashboard.app")
        self.module = module
        self.store = ShiftStore(Path(self.temp.name) / "test.db")
        self.repo = SimpleNamespace(list_candidates=lambda **kw: [], list=lambda: [])
        self.settings = SimpleNamespace(
            source_chat_username="Hyper_Vault",
            shift_timezone="Asia/Damascus", shift_sheet_id=None,
            shift_group_chat_id=None,
        )
        self.context = patch.object(
            module, "service_context",
            lambda: (self.settings, self.repo, None, self.store),
        )
        self.context.start()
        self.client = TestClient(module.app, follow_redirects=False)

    def tearDown(self):
        self.client.close()
        self.context.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_login_requires_password(self):
        initial = self.client.get("/")
        self.assertEqual(initial.status_code, 303)
        self.assertEqual(initial.headers["location"], "/login")
        # The login form is public; no catalog details are exposed pre-auth.
        self.assertEqual(self.client.get("/login").status_code, 200)
        self.assertEqual(self.client.post("/login", data={
            "passphrase": "wrongpassword"
        }).status_code, 401)
        self.assertEqual(self.client.post("/login", data={
            "passphrase": "local-long-test-password-only"
        }).status_code, 303)
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("لوحة إدارة", page.text)

    def test_branded_templates_and_local_assets(self):
        login = self.client.get("/login")
        self.assertIn("data-theme-toggle", login.text)
        self.assertIn("/static/dashboard/hyper-logo.png", login.text)
        css = self.client.get("/static/dashboard/theme.css")
        self.assertEqual(css.status_code, 200)
        self.assertIn("#e2a425", css.text)
        self.assertIn("#2b3439", css.text)
        self.assertEqual(self.client.get("/static/dashboard/theme.js").status_code, 200)
        self.assertEqual(self.client.get("/static/dashboard/hyper-logo.png").status_code, 200)
        self.assertEqual(self.client.get("/static/dashboard/cairo.ttf").status_code, 200)
        self.client.post("/login", data={"passphrase": "local-long-test-password-only"})
        dashboard = self.client.get("/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("data-theme-toggle", dashboard.text)
        self.assertIn('id="shift-calendar"', dashboard.text)
        self.assertIn('action="/files/reconcile"', dashboard.text)

    def test_csrf_rejects_unauthorized_mutation(self):
        self.client.post("/login", data={"passphrase": "local-long-test-password-only"})
        response = self.client.post("/shifts/weekly", data={
            "day": "0", "coach": "CoachOne", "co_coach": "CoCoachTwo",
            "member1": "MemberOne", "member2": "MemberTwo",
            "member3": "MemberThree", "member4": "MemberFour",
            "csrf": "wrong",
        })
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.store.effective(__import__("datetime").date(2026, 11, 15)).complete)

    def test_local_shift_write_blocked_in_sheet_mode(self):
        self.client.post("/login", data={"passphrase": "local-long-test-password-only"})
        page = self.client.get("/")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
        self.assertIsNotNone(csrf)
        with patch.dict(os.environ, {"SHIFT_SHEET_ID": "real-sheet-id"}):
            response = self.client.post("/shifts/weekly", data={
                "day": "0", "coach": "CoachOne", "co_coach": "CoCoachTwo",
                "member1": "MemberOne", "member2": "MemberTwo",
                "member3": "MemberThree", "member4": "MemberFour",
                "csrf": csrf.group(1),
            })
        self.assertEqual(response.status_code, 409)
        self.assertFalse(
            self.store.effective(__import__("datetime").date(2026, 11, 15)).complete
        )

    def test_shift_save_after_valid_csrf(self):
        self.client.post("/login", data={"passphrase": "local-long-test-password-only"})
        page = self.client.get("/")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
        self.assertIsNotNone(csrf)
        response = self.client.post("/shifts/weekly", data={
            "day": "0", "coach": "CoachOne", "co_coach": "CoCoachTwo",
            "member1": "MemberOne", "member2": "MemberTwo",
            "member3": "MemberThree", "member4": "MemberFour",
            "csrf": csrf.group(1),
        })
        self.assertEqual(response.status_code, 303)
        self.assertTrue(self.store.effective(__import__("datetime").date(2026, 11, 15)).complete)
