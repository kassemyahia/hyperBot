"""Application composition and polling entry point."""

from __future__ import annotations

import asyncio
import logging
import re
import warnings

from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.warnings import PTBUserWarning
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.config import ConfigurationError, Settings
from bot.database.repository import SQLiteFileRepository
from bot.handlers.admin import create_admin_handlers
from bot.handlers.flows import (
    ADMIN_BUTTONS,
    COMPLAINT_STATE,
    SEND_FINISH,
    SEND_MESSAGE,
    SEND_REQUEST,
    create_complaint_handler,
    create_menu_handler,
    create_request_handler,
)
from bot.handlers.start import cancel_command, create_start_handler, help_command
from bot.handlers.review import create_review_handlers
from bot.handlers.storage import create_storage_handler
from bot.keyboards.keyboards import (
    CHECK_AVAILABILITY,
    COMPLAINT,
    DOWNLOAD_REQUEST,
    TEAM_INFO,
)
from bot.services.file_delivery import FileDeliveryService
from bot.services.file_registry import FileRegistry
from bot.services.discovery import MessageFilter
from bot.services.google_sheets import GoogleSheetsCatalog, SheetsError
from bot.services.matching import FileMatcher
from bot.services.pipeline import DiscoveryPipeline
from bot.services.review import ReviewService
from bot.services.rate_limit import RateLimiter
from bot.services.registration_drafts import RegistrationDraftStore
from bot.services.shift_schedule import ShiftStore, check_shift_reminder
from bot.services.shift_sheet import SheetShiftSchedule
from bot.services.catalog_reconciliation import reconcile_catalog
from bot.utils.logging import configure_logging

logger = logging.getLogger(__name__)


def build_application(settings: Settings) -> Application:
    repository = SQLiteFileRepository(settings.database_path)
    registry = FileRegistry(repository, settings.source_chat_id)
    delivery = FileDeliveryService()
    limiter = RateLimiter(settings.rate_limit_requests, settings.rate_limit_window_seconds)
    drafts = RegistrationDraftStore()
    start = create_start_handler(settings, repository, delivery, limiter)
    menu = create_menu_handler(settings)
    request = create_request_handler(settings)
    complaint = create_complaint_handler(settings)
    sheets = None
    catalog_entries = []
    if settings.google_sheet_id:
        sheets = GoogleSheetsCatalog(
            settings.google_sheet_id, settings.google_application_credentials
        )
        try:
            catalog_entries = sheets.load_entries()
        except SheetsError:
            logger.warning("Starting without Google catalog; synchronization can be retried")
            sheets = None
    pipeline = DiscoveryPipeline(
        repository,
        MessageFilter(
            included_topic_ids=settings.included_topic_ids,
            excluded_topic_ids=settings.excluded_topic_ids,
            min_file_size_bytes=settings.min_file_size_bytes,
        ),
        FileMatcher(
            source_chat_id=settings.source_chat_id,
            source_username=settings.source_chat_username,
        ), catalog_entries,
    )
    review_service = ReviewService(
        repository, registry, catalog_entries, settings.bot_username,
        sheets if settings.auto_sync_google_sheets else None,
    )
    async def sync_status(record, status: str) -> bool:
        return await asyncio.to_thread(review_service.sync_status, record, status)

    admin = create_admin_handlers(
        settings, repository, registry, delivery, drafts, sync_status
    )
    reviews = create_review_handlers(settings, repository, review_service)

    application = Application.builder().token(settings.bot_token).build()

    # Management commands remain available even while an unrelated conversation is open.
    application.add_handler(CommandHandler("file_register", admin["register"]), group=0)
    application.add_handler(CommandHandler("file_link", admin["link"]), group=0)
    application.add_handler(CommandHandler("file_update", admin["update"]), group=0)
    application.add_handler(CommandHandler("file_disable", admin["disable"]), group=0)
    application.add_handler(CommandHandler("file_enable", admin["enable"]), group=0)
    application.add_handler(CommandHandler("file_delete", admin["delete"]), group=0)
    application.add_handler(CommandHandler("file_test", admin["test"]), group=0)
    application.add_handler(CommandHandler("files", admin["list"]), group=0)
    application.add_handler(CommandHandler("file_begin", admin["begin"]), group=0)
    application.add_handler(CommandHandler("file_commit", admin["commit"]), group=0)
    application.add_handler(CommandHandler("file_abort", admin["abort"]), group=0)
    application.add_handler(CommandHandler("reviews", reviews["list"]), group=0)
    application.add_handler(CommandHandler("review_next", reviews["next"]), group=0)
    application.add_handler(CommandHandler("review_search", reviews["search"]), group=0)
    application.add_handler(CommandHandler("review_decided", reviews["decided"]), group=0)
    application.add_handler(CommandHandler("review_approve", reviews["approve"]), group=0)
    application.add_handler(CommandHandler("review_batch", reviews["batch"]), group=0)
    application.add_handler(CommandHandler("review_reconsider", reviews["reconsider"]), group=0)
    application.add_handler(
        CallbackQueryHandler(reviews["callback"], pattern=r"^review:[arisd]:\d+$"),
        group=0,
    )
    application.add_handler(
        MessageHandler(
            filters.Chat(chat_id=settings.source_chat_id),
            create_storage_handler(settings, pipeline, review_service),
        ),
        group=0,
    )

    menu_pattern = "^(?:" + "|".join(
        re.escape(choice)
        for choice in (CHECK_AVAILABILITY, DOWNLOAD_REQUEST, TEAM_INFO, COMPLAINT)
    ) + ")$"
    private_menu_choice = filters.ChatType.PRIVATE & filters.Regex(menu_pattern)

    # Captures only authorized administrators' forwarded storage messages and prevents
    # them from falling through into an unrelated active user conversation.
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & filters.User(user_id=settings.admin_user_ids)
            & filters.FORWARDED,
            admin["capture"],
        ),
        group=1,
    )

    # The admin callback and its following text must share a per-user/per-chat key, not
    # a per-message key. PTB warns about this intentional mixed callback/text design.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="If 'per_message=False'.*",
            category=PTBUserWarning,
        )
        conversation = ConversationHandler(
            entry_points=[
                CommandHandler("start", start),
                CommandHandler("help", help_command),
                CommandHandler("cancel", cancel_command),
                MessageHandler(private_menu_choice, menu),
            ],
            states={
                ADMIN_BUTTONS: [
                    CallbackQueryHandler(
                        admin["button"], pattern=r"^admin:(finish|message)$"
                    )
                ],
                SEND_FINISH: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND, admin["finish"]
                    )
                ],
                SEND_MESSAGE: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND, admin["message"]
                    )
                ],
                SEND_REQUEST: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, request)
                ],
                COMPLAINT_STATE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, complaint)
                ],
            },
            fallbacks=[
                CommandHandler("start", start),
                CommandHandler("help", help_command),
                CommandHandler("cancel", cancel_command),
            ],
            # Active states receive all text, even if it equals a menu button.
            allow_reentry=False,
        )
    application.add_handler(conversation, group=1)
    # This is deliberately in the same group and after ConversationHandler. It handles
    # unknown private-chat text only when no active state consumed the update.
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, menu),
        group=1,
    )

    async def error_handler(update: object, context: object) -> None:
        error = getattr(context, "error", None)
        if isinstance(error, (NetworkError, TimedOut)):
            logger.warning("Transient Telegram network error: %s", type(error).__name__)
        else:
            error_info = (
                (type(error), error, error.__traceback__)
                if isinstance(error, BaseException)
                else None
            )
            logger.error("Unhandled update error", exc_info=error_info)

    application.add_error_handler(error_handler)
    # One polling process owns scheduled delivery; the dashboard is web-only.
    if settings.shift_group_chat_id:
        if application.job_queue is None:
            raise ConfigurationError(
                "Install python-telegram-bot[job-queue] for shift reminders"
            )
        shift_store = ShiftStore(settings.database_path)
        shift_sheet = None
        if settings.shift_sheet_id:
            assert settings.shift_sheet_year is not None
            shift_sheet = SheetShiftSchedule(
                settings.shift_sheet_id, settings.google_application_credentials,
                sheet_name=settings.shift_sheet_tab,
                starting_year=settings.shift_sheet_year,
            )
            logger.info("Shift assignments will be read from the configured Google Sheet")

        async def shift_tick(context):
            try:
                await check_shift_reminder(
                    context, store=shift_store, group_id=settings.shift_group_chat_id,
                    timezone=settings.shift_timezone,
                    topic_id=settings.shift_topic_id,
                    reminder_time=settings.shift_reminder_time,
                    sheet_schedule=shift_sheet,
                )
            except Exception:
                logger.exception("Scheduled shift notification failed")

        application.job_queue.run_repeating(
            shift_tick, interval=60, first=10, name="hyperteam-shift-reminder"
        )

    if settings.catalog_reconcile_enabled and settings.google_sheet_id:
        if application.job_queue is None:
            raise ConfigurationError(
                "Install python-telegram-bot[job-queue] for reconciliation"
            )

        async def catalog_tick(context):
            try:
                catalog = GoogleSheetsCatalog(
                    settings.google_sheet_id, settings.google_application_credentials
                )
                result = await asyncio.to_thread(
                    reconcile_catalog, repository, catalog,
                    source_chat_id=settings.source_chat_id,
                    source_username=settings.source_chat_username or "",
                    bot_username=settings.bot_username,
                    auto_register=settings.catalog_auto_register,
                )
                if result["linked"] or result["errors"]:
                    logger.info("Catalog reconciliation: %s", result)
            except Exception:
                logger.exception("Catalog reconciliation failed")

        application.job_queue.run_repeating(
            catalog_tick, interval=300, first=90,
            name="hyperteam-sheet-reconciliation"
        )

    application.bot_data["settings"] = settings
    application.bot_data["repository"] = repository
    return application


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    configure_logging(settings.log_level)
    logger.info("Starting Hyper Team bot")
    build_application(settings).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
