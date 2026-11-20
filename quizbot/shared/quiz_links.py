
"""Canonical Telegram deep links for sharing quizzes."""

from __future__ import annotations

from urllib.parse import quote

from . import config


def player_quiz_link(qid: str) -> str:
    username = (config.RUNNER_BOT_USERNAME or "Quizpscbot").strip().lstrip("@")
    return f"https://t.me/{username}?start={quote(str(qid), safe='')}"


def player_group_link(qid: str) -> str:
    username = (config.RUNNER_BOT_USERNAME or "Quizpscbot").strip().lstrip("@")
    return f"https://t.me/{username}?startgroup={quote(str(qid), safe='')}"


def player_share_link(qid: str) -> str:
    quiz_url = player_quiz_link(qid)
    return f"https://t.me/share/url?url={quote(quiz_url, safe='')}"
