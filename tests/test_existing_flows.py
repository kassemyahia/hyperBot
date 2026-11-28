import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from telegram.error import BadRequest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.config import ConfigurationError, Settings
from bot.handlers.flows import (
    ARCHIVE_TEXT, TEAM_TEXT, create_complaint_handler, create_request_handler,
    create_menu_handler,
)
from bot.keyboards.keyboards import CHECK_AVAILABILITY, TEAM_INFO
from telegram.ext import ConversationHandler


def settings():
    return Settings(
        bot_token="test", bot_username="test_bot", source_channel_id=-1001,
        download_request_chat_id=-1002, complaint_recipient_chat_id=300,
        admin_user_ids=frozenset({1}),
        database_url="sqlite:///:memory:", log_level="INFO",
        rate_limit_requests=5, rate_limit_window_seconds=60,
    )


def update_with(text):
    message = SimpleNamespace(text=text, chat_id=555, reply_text=AsyncMock())
    return SimpleNamespace(effective_message=message), message


class ExistingFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_request_reaches_configured_chat(self):
        update, message = update_with("كورس بايثون")
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        await create_request_handler(settings())(update, context)
        context.bot.send_message.assert_awaited_once()
        self.assertEqual(-1002, context.bot.send_message.await_args.args[0])
        self.assertIn("كورس بايثون", context.bot.send_message.await_args.args[1])
        message.reply_text.assert_awaited_once()
        self.assertNotIn('message_thread_id', context.bot.send_message.await_args.kwargs)

    async def test_complaint_does_not_reveal_sender_identity(self):
        update, _ = update_with("اقتراح مجهول")
        update.effective_user = SimpleNamespace(id=999, username="secret")
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        await create_complaint_handler(settings())(update, context)
        outgoing = context.bot.send_message.await_args.args[1]
        self.assertIn("اقتراح مجهول", outgoing)
        self.assertNotIn("999", outgoing)
        self.assertNotIn("secret", outgoing)


    async def test_download_request_uses_explicit_forum_topic(self):
        update, message = update_with("طلب جديد")
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        await create_request_handler(replace(settings(), download_request_topic_id=42))(update, context)
        self.assertEqual({"message_thread_id": 42}, context.bot.send_message.await_args.kwargs)
        self.assertIn("تم استلام طلبك", message.reply_text.await_args.args[0])

    async def test_general_topic_omits_thread_parameter(self):
        update, _ = update_with("طلب جديد")
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        await create_request_handler(replace(settings(), download_request_topic_id=1))(update, context)
        self.assertEqual({}, context.bot.send_message.await_args.kwargs)

    async def test_failed_topic_does_not_fall_back_to_general(self):
        update, message = update_with("طلب جديد")
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(
            side_effect=BadRequest("Message thread not found"))))
        await create_request_handler(replace(settings(), download_request_topic_id=42))(update, context)
        context.bot.send_message.assert_awaited_once()
        self.assertIn("تعذر استلام الطلب", message.reply_text.await_args.args[0])


class DownloadTopicConfigTests(unittest.TestCase):
    def load(self, topic=None, chat="-1002"):
        env = {"BOT_TOKEN": "test", "BOT_USERNAME": "test_bot",
               "SOURCE_CHAT_ID": "-1001", "DOWNLOAD_REQUEST_CHAT_ID": chat,
               "COMPLAINT_RECIPIENT_CHAT_ID": "300", "ADMIN_USER_IDS": "1"}
        if topic is not None:
            env["DOWNLOAD_REQUEST_TOPIC_ID"] = topic
        with patch.dict("os.environ", env, clear=True):
            return Settings.from_env(dotenv_path=Path("/nonexistent-download-topic-test.env"))

    def test_optional_topic(self):
        for value in (None, "", "  "):
            with self.subTest(value=value):
                self.assertIsNone(self.load(value).download_request_topic_id)

    def test_positive_topic(self):
        self.assertEqual(1, self.load("1").download_request_topic_id)
        self.assertEqual(42, self.load("42").download_request_topic_id)

    def test_invalid_topic(self):
        for value in ("0", "-1", "invalid", "-1002_1"):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                self.load(value)

    def test_topic_requires_group_chat(self):
        with self.assertRaises(ConfigurationError):
            self.load("42", "1234")


class MenuInformationTests(unittest.IsolatedAsyncioTestCase):
    async def test_team_info_uses_official_links_page(self):
        update, message = update_with(TEAM_INFO)
        message.chat = SimpleNamespace(type="private")
        self.assertEqual(ConversationHandler.END,
                         await create_menu_handler(settings())(update, SimpleNamespace()))
        message.reply_text.assert_awaited_once_with(TEAM_TEXT)
        self.assertIn("https://hyper-team-ops.github.io/HyperTeam-Links/", TEAM_TEXT)
        self.assertIn("خدماتنا الأساسية:", TEAM_TEXT)

    async def test_archive_uses_new_crawler_url_and_download_instructions(self):
        update, message = update_with(CHECK_AVAILABILITY)
        message.chat = SimpleNamespace(type="private")
        self.assertEqual(ConversationHandler.END,
                         await create_menu_handler(settings())(update, SimpleNamespace()))
        message.reply_text.assert_awaited_once_with(ARCHIVE_TEXT)
        self.assertIn("https://hyper-team-ops.github.io/HyperTeam-Crawler/", ARCHIVE_TEXT)
        self.assertIn("حمّل الملف عبر البوت", ARCHIVE_TEXT)
        self.assertNotIn("akaalikh.github.io", ARCHIVE_TEXT)
