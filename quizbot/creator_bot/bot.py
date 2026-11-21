
"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import asyncio
import logging

from pyrogram import Client
from pyrogram.types import BotCommand

from quizbot.shared import config

logger = logging.getLogger(__name__)


def build_client() -> Client:
    """Construct (but do not start) the Creator Bot's Pyrogram client."""
    return Client(
        "creator_bot",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.CREATOR_BOT_TOKEN,
        in_memory=True,
        workers=50,
    )


async def run_creator_bot(stop_event: asyncio.Event | None = None) -> None:
    """Start the Creator Bot and block until the process is asked to stop."""

    app = build_client()

    from .handlers import register

    register(app)

    await app.start()

    # Register Telegram Menu commands.
    try:
        await app.set_bot_commands([
            BotCommand("start", "Start the bot"),
            BotCommand("help", "Command guide"),
            BotCommand("features", "View bot features"),
            BotCommand("create", "Create a quiz"),
            BotCommand("done", "Finish quiz creation"),
            BotCommand("cancel", "Cancel current operation"),
            BotCommand("myquizzes", "View your quizzes"),
            BotCommand("edit", "Edit a quiz"),
            BotCommand("del", "Delete a quiz"),
            BotCommand("delall", "Delete all quizzes"),
            BotCommand("pay", "View premium plans"),
            BotCommand("settings", "Creator settings"),
            BotCommand("batch", "View batches"),
            BotCommand("createbatch", "Create a batch"),
            BotCommand("searchbatch", "Search batches"),
            BotCommand("whtml", "HTML quiz report"),
            BotCommand("testseries", "Generate test series"),
            BotCommand("setkey", "Set AI API key"),
            BotCommand("mykeys", "View AI keys"),
            BotCommand("delkey", "Delete AI key"),
            BotCommand("auth", "Manage authorized users"),
            BotCommand("add", "Authorize a user"),
            BotCommand("rem", "Remove authorization"),
            BotCommand("remall", "Remove all authorizations"),
            BotCommand("limit", "Check usage limits"),
        ])
        logger.info("Creator Bot menu commands registered.")
    except Exception:
        logger.exception("Failed to register Creator Bot menu commands.")

    me = await app.get_me()
    logger.info("Creator Bot started as @%s", me.username)

    own_event = stop_event or asyncio.Event()
    try:
        await own_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Stopping Creator Bot...")
        await app.stop()
        logger.info("Creator Bot stopped.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_creator_bot())
