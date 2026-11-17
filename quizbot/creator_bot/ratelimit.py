"""
PSCGram QuizBot — Creator Bot Rate Limiter
Owner gets unlimited access.
Normal users keep the existing rate limits.
"""

from __future__ import annotations

import functools
import logging
from typing import Awaitable, Callable

from pyrogram.types import Message

from quizbot.shared import config
from . import state

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[None]]


def ratelimit(bucket: str = "default") -> Callable[[Handler], Handler]:
    def decorator(fn: Handler) -> Handler:

        @functools.wraps(fn)
        async def wrapper(client, message: Message, *args, **kwargs):

            user = message.from_user

            if user is None:
                return await fn(client, message, *args, **kwargs)

            # ============================================================
            # OWNER BYPASS
            # ============================================================
            # The Telegram user whose numeric ID is in OWNER_ID gets
            # unlimited access to all rate-limited commands.
            #
            # This bypasses:
            #   - General commands
            #   - /create + /done
            #   - /myquizzes + /edit
            # ============================================================

            if config.OWNER_ID and user.id == config.OWNER_ID:
                return await fn(client, message, *args, **kwargs)

            # ============================================================
            # NORMAL USERS
            # ============================================================

            wait_minutes = state.check_rate_limit(user.id, bucket)

            if wait_minutes is None:
                return await fn(client, message, *args, **kwargs)

            if wait_minutes == -1:
                # Already warned during this rate-limit window.
                return None

            try:
                await message.reply(
                    f"Rate limit hit for this command. "
                    f"Try again in ~{wait_minutes} min. "
                    "Use /limit to check your usage."
                )
            except Exception:
                logger.debug(
                    "Failed to send rate-limit notice",
                    exc_info=True
                )

            return None

        return wrapper

    return decorator
