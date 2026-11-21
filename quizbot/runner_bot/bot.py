
"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import BotCommand, Update
from telegram.ext import Application

from quizbot.shared import config

from . import handlers
from .handlers.admin import watchdog_loop
from .handlers.scheduling import init_schedule_manager
from .state import tasks

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def post_init(application: Application) -> None:
    """Runs once after Application.initialize() -- starts the scheduler and
    the background watchdog task."""
    init_schedule_manager(scheduler)
    scheduler.start()
    tasks.spawn(watchdog_loop(), name="watchdog")
    logger.info("Runner Bot post_init complete (scheduler + watchdog started).")


async def post_shutdown(application: Application) -> None:
    """Runs once during Application.shutdown() -- tears down the scheduler."""
    scheduler.shutdown(wait=False)
    logger.info("Runner Bot post_shutdown complete.")


def build_application() -> Application:
    """Construct the PTB Application with every handler registered."""
    application = (
        Application.builder()
        .token(config.RUNNER_BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    handlers.register(application)
    return application


async def run_runner_bot() -> None:
    """Run the Runner Bot non-blocking, as one of two concurrent asyncio
    tasks sharing a single event loop with the Creator Bot.
    """
    application = build_application()

    await application.initialize()

    # Register Telegram Menu commands.
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "Start and play a quiz"),
            BotCommand("help", "Help and instructions"),
            BotCommand("aiquiz", "Create an AI quiz"),
            BotCommand("pdfquiz", "Create quiz from PDF"),
            BotCommand("pollquiz", "Create a poll quiz"),
            BotCommand("mix", "Create a mixed quiz"),
            BotCommand("html", "Get HTML report"),
            BotCommand("pdf", "Get PDF report"),
            BotCommand("schedule", "Schedule a quiz"),
            BotCommand("viewschedule", "View scheduled quizzes"),
            BotCommand("cancelschedule", "Cancel scheduled quiz"),
            BotCommand("trans", "Translate quiz"),
            BotCommand("pause", "Pause a quiz"),
            BotCommand("resume", "Resume a quiz"),
            BotCommand("stop", "Stop a quiz"),
            BotCommand("leaderboard", "View leaderboard"),
        ])
        logger.info("Runner Bot menu commands registered.")
    except Exception:
        logger.exception("Failed to register Runner Bot menu commands.")

    await application.start()
    await application.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
    logger.info("Runner Bot polling started.")

    try:
        # Block here until this task is cancelled by the launcher.
        await asyncio.Future()
    finally:
        logger.info("Runner Bot shutting down...")
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
