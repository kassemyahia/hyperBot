"""Existing user request, complaint, and information workflows."""

from __future__ import annotations

import logging

from telegram import ReplyKeyboardRemove, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes, ConversationHandler

from bot.config import Settings
from bot.keyboards.keyboards import (
    CHECK_AVAILABILITY, COMPLAINT, DOWNLOAD_REQUEST, TEAM_INFO, main_menu,
)

logger = logging.getLogger(__name__)

SEND_REQUEST = 1
COMPLAINT_STATE = 2
SEND_FINISH = 3
SEND_MESSAGE = 4
ADMIN_BUTTONS = 5

ARCHIVE_TEXT = """📚 HyperTeam Crawler | أرشيف هايبر تيم
منصتك لاستكشاف البرامج والكورسات والموارد التعليمية المتوفرة في أرشيف الفريق، والاطلاع على أحجامها وتفاصيلها بسهولة.
🔗 تصفّح الأرشيف:
https://hyper-team-ops.github.io/HyperTeam-Crawler/
كيف تحصل على المحتوى؟
1️⃣ اختر التصنيف المناسب لك.
2️⃣ ابحث عن البرنامج أو الكورس الذي تحتاجه.
3️⃣ اطّلع على تفاصيل المحتوى.
4️⃣ حمّل الملف عبر البوت إذا كان متاحاً، أو قدّم طلباً للحصول عليه.
يمكنك أيضاً مشاهدة الكورسات عبر يوتيوب عند توفّر رابط المشاهدة.
📌 في حال كان المحتوى متوفراً في المكتب فقط، يرجى إحضار اسمه كما يظهر في الأرشيف عند زيارتنا.
⚡ مع هايبر تيم، المعرفة أقرب إليك!"""

TEAM_TEXT = """نحن فريق تقني مكرّس لتقديم أفضل دعم تقني وعلمي لزملائنا في جامعة حلب.
خدماتنا الأساسية:
• الدعم التقني اليومي: حل المشاكل التقنية وتثبيت البرامج والأنظمة.
• توفير الموارد: جمع وتنظيم البرامج والكورسات والأبحاث التي يحتاجها الطالب.
• التطوير المستمر: تنظيم ورشات عمل فصلية وورشات عمل متخصصة لرفع المهارات التقنية.
🎯 هدفنا: توفير بيئة تعليمية وتقنية داعمة تسهّل عليكم رحلتكم الدراسية.
🔗 جميع روابط الفريق الرسمية في مكان واحد:
https://hyper-team-ops.github.io/HyperTeam-Links/"""


def create_menu_handler(settings: Settings):
    async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        message = update.effective_message
        if message is None or not message.text:
            return ConversationHandler.END
        text = message.text.strip()
        if message.chat.type in {"group", "supergroup"}:
            mention = f"@{settings.bot_username}"
            if mention.casefold() not in text.casefold():
                return ConversationHandler.END
            text = text.replace(mention, "").strip()

        if text == CHECK_AVAILABILITY:
            await message.reply_text(ARCHIVE_TEXT)
        elif text == DOWNLOAD_REQUEST:
            await message.reply_text(
                "فضلاً، أرسل اسم الكورس أو البرنامج وحدد نوعه، وأرفق الرابط "
                "إن توفر. استخدم /cancel للإلغاء.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return SEND_REQUEST
        elif text == TEAM_INFO:
            await message.reply_text(TEAM_TEXT)
        elif text == COMPLAINT:
            await message.reply_text(
                "أرسل الشكوى أو الاقتراح من فضلك. ستصل الرسالة دون اسمك أو "
                "معرّفك. استخدم /cancel للإلغاء.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return COMPLAINT_STATE
        else:
            await message.reply_text("أمر غير صالح. استخدم /start لإظهار القائمة.")
        return ConversationHandler.END

    return menu_handler


def create_request_handler(settings: Settings):
    async def receive_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        message = update.effective_message
        if message is None or not message.text:
            return ConversationHandler.END
        # Telegram's General topic (ID 1) is addressed by omitting the
        # thread parameter. Other forum topics require their numeric ID.
        topic = settings.download_request_topic_id
        thread = {"message_thread_id": topic} if topic is not None and topic != 1 else {}
        try:
            await context.bot.send_message(
                settings.download_request_chat_id,
                f"طلب تحميل جديد\nرقم المحادثة: {message.chat_id}"
                f"\nالمحتوى:\n{message.text}",
                **thread,
            )
        except TelegramError as exc:
            logger.warning("Unable to send download request: %s", type(exc).__name__)
            await message.reply_text(
                "⚠️ تعذر استلام الطلب حالياً. يرجى المحاولة لاحقاً.",
                reply_markup=main_menu(),
            )
            return ConversationHandler.END
        await message.reply_text(
            "تم استلام طلبك، وسيتم إعلامك عند انتهاء تحميله.", reply_markup=main_menu()
        )
        return ConversationHandler.END

    return receive_request


def create_complaint_handler(settings: Settings):
    async def receive_complaint(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        message = update.effective_message
        if message is None or not message.text:
            return ConversationHandler.END
        try:
            # No user, username, chat, or message identifiers are included.
            await context.bot.send_message(
                settings.complaint_recipient_chat_id,
                f"شكوى أو اقتراح مجهول:\n\n{message.text}",
            )
        except TelegramError as exc:
            logger.warning("Unable to send anonymous complaint: %s", type(exc).__name__)
            await message.reply_text(
                "⚠️ تعذر استلام الرسالة حالياً. يرجى المحاولة لاحقاً.",
                reply_markup=main_menu(),
            )
            return ConversationHandler.END
        await message.reply_text(
            "تم استلام الرسالة بشكل مجهول، شكراً لاقتراحك.",
            reply_markup=main_menu(),
        )
        return ConversationHandler.END

    return receive_complaint
