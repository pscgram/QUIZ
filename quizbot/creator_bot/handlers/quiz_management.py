
"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
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


def _is_owner_or_admin(user_id: int) -> bool:
    return user_id == config.OWNER_ID or user_id in config.ADMIN_IDS


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


async def quiz_selection_cb(c: Client, cb: CallbackQuery) -> None:
    """Handle quiz selection and its Play/Edit/Delete actions."""

    try:
        action, qid, owner_text = cb.data.split(":", 2)
        owner_id = int(owner_text)
    except (ValueError, AttributeError):
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
        filters.regex(r"^myq_(select|edit|delete|confirm|back):")
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
