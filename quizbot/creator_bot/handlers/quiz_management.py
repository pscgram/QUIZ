
"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.

PSCGram additions:
- Quiz leaderboard
- Unique participant list
- Participant attempt count and best score
- Participant pagination
"""

from __future__ import annotations

import asyncio
import logging

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from quizbot.database import QuizRepository, get_db
from quizbot.shared import config
from quizbot.shared.utils import is_premium_user

from .. import state
from ..ratelimit import ratelimit
from ..subscribe_gate import subscribe_gate

logger = logging.getLogger(__name__)

PARTICIPANTS_PER_PAGE = 5
LEADERBOARD_LIMIT = 10


def _is_owner_or_admin(user_id: int) -> bool:
    return user_id == config.OWNER_ID or user_id in config.ADMIN_IDS


def _safe_name(value, fallback="Unknown") -> str:
    """Keep user-provided names safe and readable in Telegram messages."""
    name = str(value or fallback)
    name = " ".join(name.replace("\n", " ").replace("\r", " ").split())
    return name[:60] or fallback


def _format_score(value) -> str:
    try:
        number = float(value or 0)
        return f"{number:.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(value or 0)


def _format_date(value) -> str:
    if not value:
        return "Unknown"
    return str(value).replace("T", " ")[:19]


def _normalise_options(value) -> set[str]:
    if value is None:
        return set()

    if not isinstance(value, (list, tuple, set)):
        value = [value]

    return {str(item) for item in value}


def _answer_stats(answers: dict, questions: list[dict]):
    """
    Calculate correct/wrong answers from the stored answer map.

    Returns None when the source question does not contain a usable
    correct_option_id/correct_option field.
    """
    if not isinstance(answers, dict):
        return None

    correct = 0
    wrong = 0
    checked = 0

    for key, selected in answers.items():
        try:
            q_index = int(str(key).lstrip("q"))
        except (TypeError, ValueError):
            continue

        if q_index < 0 or q_index >= len(questions):
            continue

        question = questions[q_index]
        correct_value = question.get(
            "correct_option_id",
            question.get("correct_option"),
        )

        if correct_value is None:
            continue

        selected_set = _normalise_options(selected)
        correct_set = _normalise_options(correct_value)

        if not selected_set:
            continue

        checked += 1

        if selected_set == correct_set:
            correct += 1
        else:
            wrong += 1

    if checked == 0:
        return None

    accuracy = (correct / checked) * 100

    return {
        "correct": correct,
        "wrong": wrong,
        "answered": checked,
        "accuracy": accuracy,
    }


async def _send_quiz_page(
    target,
    quizzes: list[dict],
    page: int,
    uid: int,
) -> None:
    """Display clickable quiz-selection buttons."""

    page_size = config.PAGE_SIZE
    start = page * page_size
    end = start + page_size
    chunk = quizzes[start:end]

    if not chunk:
        await target.edit_text("📋 No quizzes on this page.")
        return

    buttons = []

    for i, quiz in enumerate(chunk, start=start + 1):
        qid = str(quiz.get("qid", ""))
        name = str(quiz.get("quiz_name", "Unnamed"))[:40]

        buttons.append([
            InlineKeyboardButton(
                f"{i}. {name}",
                callback_data=f"myq_select:{qid}:{uid}",
            )
        ])

    nav = []

    if page > 0:
        nav.append(
            InlineKeyboardButton(
                "⬅️ Previous",
                callback_data=f"prev:{page}:{uid}",
            )
        )

    if end < len(quizzes):
        nav.append(
            InlineKeyboardButton(
                "Next ➡️",
                callback_data=f"next:{page}:{uid}",
            )
        )

    if nav:
        buttons.append(nav)

    buttons.append([
        InlineKeyboardButton(
            "❌ Close",
            callback_data=f"refresh:{uid}",
        )
    ])

    total_pages = (len(quizzes) + page_size - 1) // page_size

    text = (
        f"📋 **Your Quizzes (Page {page + 1}/{total_pages})**\n"
        f"Total: **{len(quizzes)}**\n\n"
        "👇 **Select a quiz below:**"
    )

    await target.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _get_leaderboard(qid: str) -> list[dict]:
    """Read saved first-completion leaderboard entries."""
    db = get_db()
    cursor = db.collection("leaderboard").find({"qid": qid})

    rows = [row async for row in cursor]

    # Rank by score. For equal scores, earlier completion comes first.
    rows.sort(
        key=lambda row: (
            -float(row.get("score", 0) or 0),
            str(row.get("completed_at", "")),
        )
    )

    return rows


async def _send_leaderboard(
    target,
    qid: str,
    uid: int,
    quiz: dict,
) -> None:
    """Display the top 10 saved leaderboard entries."""

    rows = await _get_leaderboard(qid)

    quiz_name = _safe_name(quiz.get("quiz_name"), "Quiz")

    if not rows:
        text = (
            f"🏆 **Leaderboard**\n\n"
            f"📚 **{quiz_name}**\n\n"
            "No completed quiz attempts have been recorded yet."
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data=f"myq_select:{qid}:{uid}",
                )
            ]
        ])

        await target.edit_text(text, reply_markup=keyboard)
        return

    lines = [
        "🏆 **LEADERBOARD**",
        "",
        f"📚 **{quiz_name}**",
        f"👥 Recorded players: **{len(rows)}**",
        "",
    ]

    medals = ["🥇", "🥈", "🥉"]

    for index, row in enumerate(rows[:LEADERBOARD_LIMIT]):
        name = _safe_name(
            row.get("user_name") or row.get("username"),
            f"Player {row.get('user_id', '')}",
        )

        medal = medals[index] if index < 3 else f"{index + 1}."

        score = _format_score(row.get("score", 0))
        total = row.get("total_questions", 0)

        lines.append(
            f"{medal} **{name}**\n"
            f"   🎯 Score: **{score}** / {total}\n"
            f"   🆔 ID: `{row.get('user_id', 'N/A')}`"
        )

        if index != min(len(rows), LEADERBOARD_LIMIT) - 1:
            lines.append("")

    if len(rows) > LEADERBOARD_LIMIT:
        lines.extend([
            "",
            f"_Showing top {LEADERBOARD_LIMIT} players._",
        ])

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 View Participants",
                callback_data=f"myq_participants:{qid}:{uid}:0",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data=f"myq_select:{qid}:{uid}",
            )
        ],
    ])

    await target.edit_text(
        "\n".join(lines),
        reply_markup=keyboard,
    )


async def _get_participants(qid: str, questions: list[dict]) -> list[dict]:
    """
    Load completed attempts and group them by unique Telegram user ID.

    Each participant receives an attempt count, best score, and latest
    completion date. Names are taken from the leaderboard collection.
    """
    db = get_db()

    attempt_cursor = (
        db.collection("quiz_attempts")
        .find({"qid": qid, "status": "completed"})
        .sort("time_ended", -1)
    )

    attempts = [row async for row in attempt_cursor]

    leaderboard_cursor = db.collection("leaderboard").find({"qid": qid})
    leaderboard_rows = [row async for row in leaderboard_cursor]

    names = {}

    for row in leaderboard_rows:
        user_id = row.get("user_id")
        if user_id is not None:
            names[user_id] = (
                row.get("user_name")
                or row.get("username")
                or f"Player {user_id}"
            )

    grouped = {}

    for attempt in attempts:
        user_id = attempt.get("user_id")

        if user_id is None:
            continue

        if user_id not in grouped:
            grouped[user_id] = {
                "user_id": user_id,
                "name": _safe_name(
                    names.get(user_id),
                    f"Player {user_id}",
                ),
                "attempts": 0,
                "best_attempt": attempt,
                "latest_attempt": attempt,
            }

        item = grouped[user_id]
        item["attempts"] += 1

        current_score = float(attempt.get("score", 0) or 0)
        best_score = float(
            item["best_attempt"].get("score", 0) or 0
        )

        if current_score > best_score:
            item["best_attempt"] = attempt

    participants = list(grouped.values())

    for item in participants:
        best = item["best_attempt"]
        stats = _answer_stats(
            best.get("answers", {}),
            questions,
        )

        item["best_score"] = best.get("score", 0)
        item["total_questions"] = best.get("total_questions", 0)
        item["played_at"] = item["latest_attempt"].get("time_ended")
        item["answer_stats"] = stats

    participants.sort(
        key=lambda item: (
            -float(item.get("best_score", 0) or 0),
            _safe_name(item.get("name")).lower(),
        )
    )

    return participants


async def _send_participants_page(
    target,
    qid: str,
    uid: int,
    page: int,
    quiz: dict,
) -> None:
    """Display a page of unique quiz participants."""

    questions = quiz.get("questions", [])
    participants = await _get_participants(qid, questions)

    quiz_name = _safe_name(quiz.get("quiz_name"), "Quiz")
    total = len(participants)

    if total == 0:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data=f"myq_select:{qid}:{uid}",
                )
            ]
        ])

        await target.edit_text(
            f"👥 **Quiz Participants**\n\n"
            f"📚 **{quiz_name}**\n\n"
            "No completed participants found yet.",
            reply_markup=keyboard,
        )
        return

    total_pages = (
        total + PARTICIPANTS_PER_PAGE - 1
    ) // PARTICIPANTS_PER_PAGE

    page = max(0, min(page, total_pages - 1))

    start = page * PARTICIPANTS_PER_PAGE
    chunk = participants[start:start + PARTICIPANTS_PER_PAGE]

    lines = [
        "👥 **QUIZ PARTICIPANTS**",
        "",
        f"📚 **{quiz_name}**",
        f"👤 Unique participants: **{total}**",
        f"📄 Page: **{page + 1}/{total_pages}**",
        "",
    ]

    for index, participant in enumerate(chunk, start=start + 1):
        name = _safe_name(participant.get("name"))
        user_id = participant.get("user_id")
        score = _format_score(participant.get("best_score"))
        attempts = participant.get("attempts", 0)
        total_questions = participant.get("total_questions", 0)

        lines.append(
            f"**{index}. {name}**\n"
            f"🆔 ID: `{user_id}`\n"
            f"🔁 Attempts: **{attempts}**\n"
            f"🏆 Best score: **{score}** / {total_questions}"
        )

        stats = participant.get("answer_stats")

        if stats:
            lines.append(
                f"✅ Correct: {stats['correct']}  "
                f"❌ Wrong: {stats['wrong']}\n"
                f"🎯 Accuracy: **{stats['accuracy']:.1f}%**"
            )

        lines.append(
            f"🕒 Last played: {_format_date(participant.get('played_at'))}"
        )
        lines.append("")

    buttons = []
    nav = []

    if page > 0:
        nav.append(
            InlineKeyboardButton(
                "⬅️ Previous",
                callback_data=f"myq_participants:{qid}:{uid}:{page - 1}",
            )
        )

    if page + 1 < total_pages:
        nav.append(
            InlineKeyboardButton(
                "Next ➡️",
                callback_data=f"myq_participants:{qid}:{uid}:{page + 1}",
            )
        )

    if nav:
        buttons.append(nav)

    buttons.append([
        InlineKeyboardButton(
            "🏆 Leaderboard",
            callback_data=f"myq_leaderboard:{qid}:{uid}",
        )
    ])

    buttons.append([
        InlineKeyboardButton(
            "⬅️ Back to Quiz",
            callback_data=f"myq_select:{qid}:{uid}",
        )
    ])

    await target.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def quiz_selection_cb(c: Client, cb: CallbackQuery) -> None:
    """Handle quiz selection, leaderboard, participants and management."""

    try:
        parts = cb.data.split(":")
        action = parts[0]
        qid = parts[1]
        owner_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
    except (ValueError, IndexError, AttributeError):
        await cb.answer("Invalid selection.", show_alert=True)
        return

    uid = cb.from_user.id

    if uid != owner_id:
        await cb.answer("This selection is not yours.", show_alert=True)
        return

    repo = QuizRepository(get_db())
    quiz = await repo.get(qid)

    if not quiz:
        await cb.answer("Quiz not found.", show_alert=True)
        return

    creator_id = quiz.get("creator_id")
    is_admin = _is_owner_or_admin(uid)

    if creator_id != uid and not is_admin:
        await cb.answer("You are not authorized.", show_alert=True)
        return

    if action == "myq_select":
        question_count = len(quiz.get("questions", []))

        text = (
            "✅ **Selected Quiz**\n\n"
            f"🏷️ **Name:** {str(quiz.get('quiz_name', 'Unnamed'))[:100]}\n"
            f"❓ **Questions:** {question_count}\n"
            f"👥 **Plays:** {quiz.get('total_participants', 0)}\n"
            f"💳 **Type:** {str(quiz.get('quiz_type', 'free')).title()}\n"
            f"⏱️ **Timer:** {quiz.get('timer', 'N/A')} seconds\n"
            f"🆔 **Quiz ID:** `{qid}`"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "▶️ Play Quiz",
                    url=f"https://t.me/Quizpscbot?start={qid}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🏆 Leaderboard",
                    callback_data=f"myq_leaderboard:{qid}:{uid}",
                )
            ],
            [
                InlineKeyboardButton(
                    "👥 View Participants",
                    callback_data=f"myq_participants:{qid}:{uid}:0",
                )
            ],
            [
                InlineKeyboardButton(
                    "✏️ Edit Quiz",
                    callback_data=f"myq_edit:{qid}:{uid}",
                ),
                InlineKeyboardButton(
                    "🗑️ Delete Quiz",
                    callback_data=f"myq_delete:{qid}:{uid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back to Quiz List",
                    callback_data=f"myq_back:{qid}:{uid}",
                )
            ],
        ])

        await cb.message.edit_text(
            text,
            reply_markup=keyboard,
        )
        await cb.answer()
        return

    if action == "myq_leaderboard":
        await _send_leaderboard(
            cb.message,
            qid,
            uid,
            quiz,
        )
        await cb.answer()
        return

    if action == "myq_participants":
        await _send_participants_page(
            cb.message,
            qid,
            uid,
            page,
            quiz,
        )
        await cb.answer()
        return

    if action == "myq_edit":
        if not await is_premium_user(uid):
            await cb.answer(
                "🔒 Premium is required to edit quizzes.",
                show_alert=True,
            )
            return

        from .quiz_editing import _quiz_info_text
        from ..keyboards import quiz_editor_main_kb

        state.edit_sessions[uid] = {
            "qid": qid,
            "page": 0,
            "field": None,
        }

        await cb.message.edit_text(
            f"✍️ **Quiz Editor**\n\n{_quiz_info_text(quiz)}",
            reply_markup=quiz_editor_main_kb(qid),
        )

        await cb.answer("Editor opened.")
        return

    if action == "myq_delete":
        if not await is_premium_user(uid):
            await cb.answer(
                "🔒 Premium is required to delete quizzes.",
                show_alert=True,
            )
            return

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Yes, Delete",
                    callback_data=f"myq_confirm:{qid}:{uid}",
                ),
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"myq_select:{qid}:{uid}",
                ),
            ]
        ])

        await cb.message.edit_text(
            "⚠️ **Confirm Quiz Deletion**\n\n"
            f"Delete **{quiz.get('quiz_name', 'this quiz')}**?\n\n"
            "This action cannot be undone.",
            reply_markup=keyboard,
        )

        await cb.answer()
        return

    if action == "myq_confirm":
        if not await is_premium_user(uid):
            await cb.answer(
                "🔒 Premium is required to delete quizzes.",
                show_alert=True,
            )
            return

        await repo.delete(qid)
        state.clear_quiz_list_cache(uid)

        await cb.message.edit_text(
            "🗑️ **Quiz Deleted Successfully**\n\n"
            f"Quiz: **{quiz.get('quiz_name', qid)}**"
        )

        await cb.answer("Quiz deleted.")
        return

    if action == "myq_back":
        cached = state.load_quiz_list_cache(uid)

        if not cached:
            await cb.answer(
                "Quiz list expired. Run /myquizzes again.",
                show_alert=True,
            )
            return

        quizzes = cached["data"]

        page = next(
            (
                i // config.PAGE_SIZE
                for i, item in enumerate(quizzes)
                if str(item.get("qid")) == qid
            ),
            0,
        )

        await _send_quiz_page(
            cb.message,
            quizzes,
            page,
            uid,
        )

        await cb.answer()
        return

    await cb.answer("Unknown action.", show_alert=True)


@ratelimit("strict")
async def myquizzes_cmd(c: Client, m: Message) -> None:
    """/myquizzes [term] -- list or search this creator's quizzes."""

    if await subscribe_gate(c, m):
        return

    uid = m.from_user.id

    if not await is_premium_user(uid):
        await m.reply("🔒 Purchase premium: /pay")
        return

    args = m.text.split(maxsplit=1)
    search_term = args[1].strip() if len(args) > 1 else None

    label = (
        f"🔍 Searching for **{search_term}**..."
        if search_term
        else "📋 Fetching..."
    )

    pending = await m.reply(label)

    quizzes = await QuizRepository(get_db()).list_by_creator(
        uid,
        query=search_term,
    )

    if not quizzes:
        msg = (
            f"🔍 No quizzes found for **{search_term}**"
            if search_term
            else "📋 No quizzes yet."
        )
        await pending.edit_text(msg)
        return

    state.save_quiz_list_cache(uid, quizzes)

    await _send_quiz_page(
        pending,
        quizzes,
        0,
        uid,
    )


async def pagination_cb(c: Client, cb: CallbackQuery) -> None:
    """Handle quiz-list pagination."""

    try:
        parts = cb.data.split(":")
        action = parts[0]
        uid = int(parts[-1])
    except (ValueError, IndexError):
        await cb.answer("Invalid pagination request.", show_alert=True)
        return

    if cb.from_user.id != uid:
        await cb.answer("This list is not yours.", show_alert=True)
        return

    if action == "refresh":
        state.clear_quiz_list_cache(uid)

        try:
            await cb.message.delete()
        except Exception:
            pass

        await cb.answer()
        return

    try:
        page = int(parts[1])
    except (ValueError, IndexError):
        await cb.answer("Invalid page.", show_alert=True)
        return

    cached = state.load_quiz_list_cache(uid)

    if not cached:
        await cb.answer(
            "⚠️ List expired. Run /myquizzes again.",
            show_alert=True,
        )
        return

    new_page = page - 1 if action == "prev" else page + 1

    await _send_quiz_page(
        cb.message,
        cached["data"],
        new_page,
        uid,
    )

    await cb.answer()


@ratelimit("default")
async def del_quiz_cmd(c: Client, m: Message) -> None:
    """/del <quiz_id> -- delete one of this creator's quizzes."""

    args = m.text.split()

    if len(args) != 2:
        await m.reply("⚠️ Usage: `/del <quiz_id>`")
        return

    qid, uid = args[1], m.from_user.id

    if not await is_premium_user(uid):
        await m.reply("🔒 Purchase premium: /pay")
        return

    repo = QuizRepository(get_db())
    quiz = await repo.get(qid)

    if not quiz:
        await m.reply("❌ Not found.")
        return

    if quiz["creator_id"] != uid:
        await m.reply("❌ Not authorized.")
        return

    await repo.delete(qid)
    await m.reply(f"🗑️ Deleted `{qid}`.")


async def delall_cmd(c: Client, m: Message) -> None:
    """/delall -- owner/admin only: delete every quiz."""

    if not _is_owner_or_admin(m.from_user.id):
        return

    repo = QuizRepository(get_db())
    quizzes = await repo.list_all(limit=1_000_000)

    for q in quizzes:
        await repo.delete(q["qid"])

    await m.reply(f"🗑️ Deleted {len(quizzes)} quizzes.")


@ratelimit("default")
async def convertall_cmd(c: Client, m: Message) -> None:
    """/convertall -- owner/admin only: convert paid quizzes to free."""

    if not _is_owner_or_admin(m.from_user.id):
        return

    status = await m.reply("🔄 Converting...")
    repo = QuizRepository(get_db())
    quizzes = await repo.list_all(limit=1_000_000)
    paid = [q for q in quizzes if q.get("quiz_type") == "paid"]

    for q in paid:
        await repo.update_field(q["qid"], "quiz_type", "free")

    await status.edit_text(
        f"✅ Converted {len(paid)} quizzes to free."
    )


@ratelimit("default")
async def info_cmd(c: Client, m: Message) -> None:
    """/info <quiz_id> -- show the creator of a quiz."""

    args = m.text.split()

    if len(args) < 2:
        await m.reply("⚠️ Usage: `/info <quiz_id>`")
        return

    quiz = await QuizRepository(get_db()).get(args[1])

    if not quiz:
        await m.reply("❌ Not found.")
        return

    creator_id = quiz.get("creator_id", "Unknown")

    try:
        creator = await c.get_users(creator_id)
        name = creator.first_name
    except Exception:
        name = "Unknown"

    await m.reply(
        f"🆔 Creator: {name} -- ID: `{creator_id}`"
    )


_SEARCH_PAGE_SIZE = 5


async def _send_search_page(
    c: Client,
    target,
    uid: int,
    term: str,
    offset: int,
) -> None:
    """Fetch one page of search results from the database."""

    repo = QuizRepository(get_db())

    chunk = await repo.search(
        term,
        limit=_SEARCH_PAGE_SIZE,
        offset=offset,
    )

    total = await repo.search_count(term)
    shown = min(offset + len(chunk), total)

    if not chunk:
        text = f"🔍 No quizzes found for **{term}**."

        try:
            await target.edit_text(text)
        except Exception:
            await target.reply(text)

        return

    me = await c.get_me()
    lines = []
    buttons = []

    for i, q in enumerate(chunk, start=offset + 1):
        name = q.get("quiz_name", "Unnamed")[:55]
        qid = q.get("qid", "")
        plays = q.get("total_participants", 0)
        quiz_type = q.get("quiz_type", "free")

        lines.append(
            f"**{i}.** {name}\n"
            f"    ID: `{qid}` | {plays} plays | {quiz_type}"
        )

        url = f"https://t.me/{me.username}?start={qid}"

        buttons.append([
            InlineKeyboardButton(
                f"{i}. {q.get('quiz_name', '?')[:28]}",
                url=url,
            )
        ])

    nav = []

    if offset + len(chunk) < total:
        next_offset = offset + _SEARCH_PAGE_SIZE

        nav.append(
            InlineKeyboardButton(
                f"⬇️ Load more ({shown}/{total})",
                callback_data=f"srch_more_{uid}_{next_offset}",
            )
        )

    if nav:
        buttons.append(nav)

    text = (
        f"🔍 **Search: {term}** ({shown}/{total} shown)\n\n"
        + "\n\n".join(lines)
    )

    kb = InlineKeyboardMarkup(buttons) if buttons else None

    try:
        await target.edit_text(
            text[:4000],
            reply_markup=kb,
        )
    except Exception:
        await target.reply(
            text[:4000],
            reply_markup=kb,
        )


@ratelimit("default")
async def search_cmd(c: Client, m: Message) -> None:
    """/search <term> or /quiz -- search public quizzes."""

    args = m.text.split(maxsplit=1)

    if len(args) < 2:
        await m.reply(
            "🔍 **Quiz Search**\n\n"
            "Usage: `/search <keyword>`\n"
            "Example: `/search science gk`\n\n"
            "Shows 5 results at a time -- tap Load More for the next batch."
        )
        return

    term = args[1].strip()

    if len(term) < 2:
        await m.reply(
            "⚠️ Search term too short (min 2 chars)."
        )
        return

    uid = m.from_user.id
    status = await m.reply(f"🔍 Searching **{term}**...")

    state.search_state[uid] = {"term": term}

    await _send_search_page(
        c,
        status,
        uid,
        term,
        0,
    )


async def search_more_cb(c: Client, cb: CallbackQuery) -> None:
    """Handle /search pagination."""

    try:
        parts = cb.data.split("_")
        uid = int(parts[2])
        offset = int(parts[3])
    except (ValueError, IndexError):
        await cb.answer("Invalid search request.", show_alert=True)
        return

    if cb.from_user.id != uid:
        await cb.answer("⚠️ Not yours.", show_alert=True)
        return

    session = state.search_state.get(uid)

    if not session:
        await cb.answer(
            "⚠️ Search expired -- run /search again.",
            show_alert=True,
        )
        return

    await _send_search_page(
        c,
        cb.message,
        uid,
        session["term"],
        offset,
    )

    await cb.answer()


@ratelimit("default")
async def setpromo_cmd(c: Client, m: Message) -> None:
    """/setpromo <text> -- set or clear a promo on all creator quizzes."""

    uid = m.from_user.id

    if not await is_premium_user(uid):
        await m.reply("🔒 Premium required: /pay")
        return

    args = m.text.split(maxsplit=1)

    if len(args) < 2:
        await m.reply(
            "📢 **Set Promo on All Your Quizzes**\n\n"
            "Usage: `/setpromo Your promo message here`\n"
            "Remove promo: `/setpromo none`"
        )
        return

    promo_raw = args[1].strip()

    promo = (
        None
        if promo_raw.lower() in ("none", "no", "remove", "clear")
        else promo_raw
    )

    status = await m.reply("🔄 Updating all quizzes...")

    count = await QuizRepository(get_db()).set_promo_for_creator(
        uid,
        promo or "",
    )

    if promo:
        await status.edit_text(
            f"✅ Promo set on **{count}** quizzes:\n\n_{promo}_"
        )
    else:
        await status.edit_text(
            f"✅ Promo removed from **{count}** quizzes."
        )


async def ban_cmd(c: Client, m: Message) -> None:
    """/ban <quiz_id> -- owner/admin only."""

    if not _is_owner_or_admin(m.from_user.id):
        return

    args = m.text.split()

    if len(args) < 2:
        await m.reply("⚠️ Usage: `/ban <quiz_id>`")
        return

    repo = QuizRepository(get_db())
    quiz = await repo.get(args[1])

    if not quiz:
        await m.reply("❌ Not found.")
        return

    creator_id = quiz.get("creator_id")

    if not creator_id:
        await m.reply("❌ No creator on this quiz.")
        return

    if config.CHANNEL_ID:
        try:
            await c.ban_chat_member(
                config.CHANNEL_ID,
                creator_id,
            )
            await m.reply(f"🚫 Banned {creator_id}.")
        except Exception as exc:
            await m.reply(f"⚠️ Ban failed: {exc}")

    try:
        creator_quizzes = await repo.list_by_creator(creator_id)

        for q in creator_quizzes:
            await repo.delete(q["qid"])

        await m.reply(
            f"🗑️ Deleted {len(creator_quizzes)} quizzes by {creator_id}."
        )

    except Exception as exc:
        logger.exception("ban_cmd deletion failed")
        await m.reply(f"⚠️ Deletion error: {exc}")


async def listquiz_cmd(c: Client, m: Message) -> None:
    """/listquiz -- list platform quizzes in the designated group."""

    if not config.BOT_GROUP or m.chat.id != config.BOT_GROUP:
        return

    quizzes = await QuizRepository(get_db()).list_all(limit=200)

    if not quizzes:
        await m.reply("📋 No quizzes.")
        return

    me = await c.get_me()

    for i, q in enumerate(quizzes):
        qid = q.get("qid")

        text = (
            f"📋 **Quiz {i + 1}**\n\n"
            f"Name: `{q.get('quiz_name', 'Unnamed')}`\n"
            f"Plays: `{q.get('total_participants', 0)}`\n"
            f"Timer: `{q.get('timer', 'N/A')}s`\n"
            f"ID: `{qid}`\n"
            f"Type: `{q.get('quiz_type', 'free')}`"
        )

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "▶️ Start",
                    url=f"https://t.me/{me.username}?start={qid}",
                )
            ]
        ])

        await m.reply(
            text,
            reply_markup=kb,
        )

        await asyncio.sleep(4)


def register(app: Client) -> None:
    """Register quiz management commands and callbacks."""

    app.on_message(
        filters.command("myquizzes") & filters.private
    )(myquizzes_cmd)

    app.on_callback_query(
        filters.regex(r"^(prev|next|refresh):")
    )(pagination_cb)

    app.on_callback_query(
        filters.regex(
            r"^myq_(select|edit|delete|confirm|back|leaderboard|participants):"
        )
    )(quiz_selection_cb)

    app.on_message(
        filters.command("del") & filters.private
    )(del_quiz_cmd)

    app.on_message(
        filters.command("delall") & filters.private
    )(delall_cmd)

    app.on_message(
        filters.command("convertall") & filters.private
    )(convertall_cmd)

    app.on_message(
        filters.command("info") & filters.private
    )(info_cmd)

    app.on_message(
        filters.command(["search", "quiz"]) & filters.private
    )(search_cmd)

    app.on_callback_query(
        filters.regex(r"^srch_more_")
    )(search_more_cb)

    app.on_message(
        filters.command("setpromo") & filters.private
    )(setpromo_cmd)

    app.on_message(
        filters.command("ban") & filters.private
    )(ban_cmd)

    app.on_message(
        filters.command("listquiz")
    )(listquiz_cmd)
