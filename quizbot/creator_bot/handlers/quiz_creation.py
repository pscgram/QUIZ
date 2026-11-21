
"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

from pyrogram import Client, filters
from pyrogram.enums import PollType
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from quizbot.database import CreatorSettingsRepository, QuizRepository, UserRepository, get_db
from quizbot.shared import config
from quizbot.shared.mini_app_link import mini_app_web_app_button
from quizbot.shared.utils import is_premium_user

from .. import state
from ..parsing import filter_words, parse_question_block, strip_source_noise
from ..ratelimit import ratelimit
from ..subscribe_gate import subscribe_gate
from .file_import import process_uploaded_file

logger = logging.getLogger(__name__)


def _poll_text(value) -> Optional[str]:
    """Convert poll fields, including FormattedText, into plain strings."""
    if value is None:
        return None

    text = getattr(value, "text", None)
    if text is not None:
        return str(text)

    return str(value)


_RESERVED_COMMANDS = [
    "start", "create", "myquizzes", "edit", "info", "ban", "done", "add", "rem",
    "remall", "del", "remove", "clearlist", "mywords", "help", "cancel", "quiz",
    "search", "auth", "pay", "setpromo", "setkey", "mykeys", "delkey", "settings",
    "batch", "createbatch", "searchbatch", "stopedit", "whtml", "testseries",
    "tsr", "mocktest", "features", "gcast", "stopcast", "statses", "testapi",
    "leaders", "aspirants", "limit", "listquiz", "removeuser",
]

MIN_QUESTIONS = 10
MAX_QUESTIONS = 300
MAX_QUESTIONS_OWNER = 3000


def _gen_qid() -> str:
    import random
    import string

    return "GGN" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


@ratelimit("create")
async def create_cmd(c: Client, m: Message) -> None:
    """/create -- start a new quiz-creation session."""
    if await subscribe_gate(c, m):
        return

    uid = m.from_user.id

    if not await is_premium_user(uid):
        await m.reply("🔒 Purchase premium: /pay")
        return

    if uid in state.quiz_creation:
        await m.reply("⚠️ Already creating a quiz. Use /done or /cancel.")
        return

    await m.reply("📝 **Send the quiz name.**")

    state.quiz_creation[uid] = {
        "questions": [],
        "timer": None,
        "quiz_name": None,
        "awaiting_name": True,
    }


@ratelimit("default")
async def cancel_cmd(c: Client, m: Message) -> None:
    """/cancel -- discard the quiz currently being created."""
    uid = m.from_user.id

    if uid in state.quiz_creation:
        state.quiz_creation.pop(uid, None)
        await m.reply("❌ Cancelled.")
    else:
        await m.reply("⚠️ Nothing to cancel.")


@ratelimit("create")
async def done_cmd(c: Client, m: Message) -> None:
    """/done -- finish and save the quiz being created."""
    uid = m.from_user.id

    if not await is_premium_user(uid):
        await m.reply("🔒 Purchase premium: /pay")
        return

    if uid not in state.quiz_creation:
        await m.reply("⚠️ Use /create first.")
        return

    total = len(state.quiz_creation[uid]["questions"])

    if total < MIN_QUESTIONS:
        await m.reply(f"⚠️ Need at least {MIN_QUESTIONS} questions. You have {total}.")
        return

    max_allowed = MAX_QUESTIONS_OWNER if uid == config.OWNER_ID else MAX_QUESTIONS

    if total > max_allowed:
        await m.reply(f"⚠️ Max {max_allowed} questions. You have {total}.")
        return

    settings_repo = CreatorSettingsRepository(get_db())
    settings = await settings_repo.get(uid)
    quiz_defaults = settings.get("quiz_defaults") or {}
    has_defaults = bool(quiz_defaults.get("type"))

    if has_defaults:
        typ = quiz_defaults.get("type", "free")
        promo = quiz_defaults.get("promo") or None
        section = quiz_defaults.get("section", "no")

        promo_preview = (
            (promo[:30] + "...") if promo and len(promo) > 30
            else (promo or "none")
        )

        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⚡ Use saved config", callback_data=f"qd_use_{uid}")],
                [InlineKeyboardButton("📝 Manual setup", callback_data=f"qd_manual_{uid}")],
            ]
        )

        state.quiz_creation[uid]["_qd"] = quiz_defaults

        await m.reply(
            f"⚡ **Quick Save available!**\n\n"
            f"Type: `{typ}`\n"
            f"Promo: `{promo_preview}`\n"
            f"Sections: `{section}`\n\n"
            f"Use the saved config, or set up manually?",
            reply_markup=kb,
        )
        return

    await m.reply("📚 Section quiz? yes/no")
    state.quiz_creation[uid]["awaiting_section_choice"] = True


async def _finalize_quiz(
    c: Client,
    reply_target,
    uid: int,
    quiz_type: str,
    promo: Optional[str],
    sections: list[dict],
    timer: int,
    from_user_name: str,
) -> None:
    """Save the quiz to the database and send the result."""

    is_message = isinstance(reply_target, Message)

    async def send_result(text: str, kb: Optional[InlineKeyboardMarkup] = None):
        if is_message:
            return await reply_target.reply(text, reply_markup=kb)

        try:
            return await reply_target.message.edit_text(text, reply_markup=kb)
        except Exception:
            return await reply_target.message.reply(text, reply_markup=kb)

    quiz_name = state.quiz_creation[uid]["quiz_name"]
    questions = state.quiz_creation[uid]["questions"]

    settings_repo = CreatorSettingsRepository(get_db())
    settings = await settings_repo.get(uid)

    # Respect the user's choice to skip default text.
    default_text = (
        None
        if state.quiz_creation[uid].get("_skip_default_text")
        else settings.get("default_text")
    )
    default_text_field = settings.get("default_text_field", "both")

    if default_text:
        for q in questions:
            if default_text_field in ("question", "both") and q.get("question"):
                q["question"] = q["question"].rstrip() + "\n" + default_text

            if default_text_field in ("explanation", "both"):
                q["explanation"] = (
                    (q.get("explanation") or "").rstrip() + "\n" + default_text
                    if q.get("explanation")
                    else default_text
                )

    qid = _gen_qid()
    quiz_repo = QuizRepository(get_db())

    quiz = await quiz_repo.create(
        creator_id=uid,
        quiz_name=quiz_name,
        questions=questions,
        qid=qid,
        sections=sections,
        timer=timer,
        quiz_type=quiz_type,
        negative_marks=0,
        promo_message=promo,
    )

    state.quiz_creation.pop(uid, None)

    if not quiz:
        await send_result(
            "⚠️ Quiz created but could not be re-fetched. Try /myquizzes."
        )
        return

    promo_flag = "Set" if promo else "None"

    text = (
        f"> 🎉 **Quiz Created!**\n\n"
        f"📝 **Name:** {quiz_name}\n"
        f"❓ **Questions:** {len(quiz['questions'])}\n"
        f"⏱️ **Timer:** {timer}s\n"
        f"🆔 **Quiz ID:** `{qid}`\n"
        f"🏷️ **Type:** `{quiz_type}`\n"
        f"🪭 **Promo:** {promo_flag}\n"
        f"👨‍💼 **Creator:** `{from_user_name}`"
    )

    if sections:
        text += "\n\n**Sections:**"

        for i, sec in enumerate(sections, 1):
            text += (
                f"\n\nSection {i}: {sec['name']}\n"
                f"  Questions: {sec['question_range'][0]} to {sec['question_range'][1]}\n"
                f"  Timer: {sec.get('timer', 'N/A')}s"
            )

    # Player bot links -- quizzes must open in Quizpscbot.
    me = await c.get_me()
    player_bot_username = "Quizpscbot"

    quiz_play_link = f"https://t.me/{player_bot_username}?start={qid}"

    share_link = (
        "https://t.me/share/url?url="
        + quote(quiz_play_link, safe="")
        + "&text="
        + quote(f"Play this quiz: {quiz_name}", safe="")
    )

    kb_buttons = [
        [InlineKeyboardButton("🚀 Start", url=quiz_play_link)],
        [
            InlineKeyboardButton(
                "👥 Add to Group",
                url=f"https://t.me/{player_bot_username}?startgroup={qid}",
            )
        ],
        [InlineKeyboardButton("🔗 Share", url=share_link)],
    ]

    # Mini App buttons, if configured.
    play_practice = mini_app_web_app_button(
        me.username, qid, "practice", "Play (Practice)"
    )
    play_exam = mini_app_web_app_button(
        me.username, qid, "exam", "Play (Exam)"
    )

    if play_practice and play_exam:
        kb_buttons.append([play_practice, play_exam])

    if quiz_type == "paid":
        kb_buttons.append(
            [
                InlineKeyboardButton(
                    "📦 Attach to Batch",
                    callback_data=f"bat_attachqz_{qid}_{uid}",
                )
            ]
        )

    kb = InlineKeyboardMarkup(kb_buttons)
    await send_result(text, kb)

    # Announce newly created quiz in configured group.
    if config.BOT_GROUP:
        try:
            announce_text = strip_source_noise(text) or text

            await c.send_message(
                config.BOT_GROUP,
                announce_text,
                reply_markup=InlineKeyboardMarkup(kb_buttons[:3]),
            )
        except Exception:
            logger.debug(
                "Failed to announce new quiz in BOT_GROUP",
                exc_info=True,
            )


async def quicksave_cb(c: Client, cb: CallbackQuery) -> None:
    """Handle Quick Save or Manual setup buttons."""
    uid = cb.from_user.id
    action = cb.data.split("_")[1]
    target = int(cb.data.split("_")[2])

    if uid != target:
        await cb.answer("❌ Not yours", show_alert=True)
        return

    if uid not in state.quiz_creation:
        await cb.answer("⚠️ Session expired", show_alert=True)
        return

    if action == "manual":
        try:
            await cb.message.edit_text("📚 Section quiz? yes/no")
        except Exception:
            await cb.message.reply("📚 Section quiz? yes/no")

        state.quiz_creation[uid]["awaiting_section_choice"] = True
        await cb.answer()
        return

    quiz_defaults = state.quiz_creation[uid].get("_qd", {})
    quiz_type = quiz_defaults.get("type", "free")
    promo = quiz_defaults.get("promo") or None
    section_choice = quiz_defaults.get("section", "no")
    timer = state.quiz_creation[uid].get("timer") or 20

    await cb.answer("⚡ Saving...")

    try:
        await cb.message.edit_text("🚀 Creating quiz...")
    except Exception:
        pass

    if section_choice == "yes":
        state.quiz_creation[uid]["section_wise"] = True
        state.quiz_creation[uid]["promo_message"] = promo
        state.quiz_creation[uid]["type_preset"] = quiz_type
        state.quiz_creation[uid]["awaiting_section_count"] = True

        try:
            await cb.message.edit_text("📚 How many sections? (>= 2)")
        except Exception:
            await cb.message.reply("📚 How many sections? (>= 2)")
        return

    name = cb.from_user.first_name if cb.from_user else str(uid)

    await _finalize_quiz(
        c, cb, uid, quiz_type, promo, [], timer, name
    )


async def handle_document(c: Client, m: Message) -> None:
    """Import questions from a .txt or .json file during quiz creation."""
    uid = m.from_user.id

    if not await is_premium_user(uid):
        return

    if uid not in state.quiz_creation:
        return

    allowed_types = ("text/plain", "application/json")

    if m.document.mime_type not in allowed_types:
        await m.reply("⚠️ Only .txt or .json files are supported for quiz questions.")
        return

    status = await m.reply("⏳ Processing...")

    file_bytes = await c.download_media(m.document.file_id, in_memory=True)

    if not file_bytes:
        await status.edit_text("❌ Could not download the file.")
        return

    file_bytes.seek(0)
    content = file_bytes.read()

    user = await UserRepository(get_db()).get_or_create(uid)
    remove_words = user.get("remove_words", [])

    count, error = process_uploaded_file(
        content,
        m.document.file_name or "upload.txt",
        state.quiz_creation[uid]["questions"],
        remove_words,
    )

    if error:
        await status.edit_text(f"❌ Error: {error}")
    else:
        total = len(state.quiz_creation[uid]["questions"])
        await status.edit_text(
            f"✅ {count} questions processed! Total: {total}\nSend more or /done"
        )


async def handle_creation_message(c: Client, m: Message) -> None:
    """Handle quiz name, sections, promo, quiz type, questions and polls."""
    uid, cid = m.from_user.id, m.chat.id

    if uid not in state.quiz_creation:
        return

    ud = state.quiz_creation[uid]

    # ── Forwarded quiz polls ────────────────────────────────────────
    if m.poll:
        poll = m.poll

        if poll.type != PollType.QUIZ:
            await m.reply("⚠️ Only quiz-type polls are supported.")
            return

        user = await UserRepository(get_db()).get_or_create(uid)
        remove_words = user.get("remove_words", [])

        question = strip_source_noise(
            filter_words(_poll_text(poll.question), remove_words)
        )

        options = [
            filter_words(_poll_text(o.text), remove_words)
            for o in poll.options
        ]

        # Correct answer handling for current Kurigram/Pyrogram poll objects.
        correct_id = getattr(poll, "correct_option_id", None)

        if correct_id is None:
            correct_ids = getattr(poll, "correct_option_ids", None) or []
            correct_id = correct_ids[0] if correct_ids else 0

        explanation = None

        if getattr(poll, "explanation", None):
            explanation = strip_source_noise(
                filter_words(_poll_text(poll.explanation), remove_words)
            )

        reply_msg = m.reply_to_message
        reply_text = reply_msg.text if reply_msg and reply_msg.text else None
        file_id = None

        if reply_msg and reply_msg.photo and config.BOT_GROUP:
            try:
                copied = await c.copy_message(
                    config.BOT_GROUP,
                    reply_msg.chat.id,
                    reply_msg.id,
                )
                file_id = copied.photo.file_id
            except Exception:
                logger.debug(
                    "Failed to copy poll's reply photo",
                    exc_info=True,
                )

        ud["questions"].append(
            {
                "question": question,
                "options": options,
                "correct_option_id": correct_id,
                "explanation": explanation,
                "file_id": file_id,
                "reply_text": reply_text,
            }
        )

        await m.reply(
            f"✅ {len(ud['questions'])} saved! Send more or /done"
        )
        return

    if not m.text:
        return

    # ── Quiz name ────────────────────────────────────────────────────
    if ud.get("awaiting_name"):
        name = m.text.strip()

        if not name:
            await m.reply("⚠️ Invalid name.")
            return

        ud["quiz_name"] = name
        ud["awaiting_name"] = False

        await m.reply(
            f"📝 Name: **{name}**\n"
            f"Send questions, forward quiz polls, or a .txt file. /cancel to abort."
        )
        return

    # ── Section quiz choice ──────────────────────────────────────────
    if ud.get("awaiting_section_choice"):
        choice = m.text.strip().lower()

        if choice not in ("yes", "no"):
            await m.reply("⚠️ Reply yes or no.")
            return

        ud["section_wise"] = choice == "yes"
        del ud["awaiting_section_choice"]

        if ud["section_wise"]:
            ud["awaiting_section_count"] = True
            await m.reply("📚 How many sections? (>1)")
        else:
            ud["timer"] = 20
            ud["awaiting_promo"] = True
            await m.reply(
                "📢 Send your promo message (shown periodically). "
                "Send 'skip' or 'no' to leave empty."
            )
        return

    # ── Section count ────────────────────────────────────────────────
    if ud.get("awaiting_section_count"):
        try:
            section_count = int(m.text.strip())

            if section_count < 2:
                raise ValueError

        except ValueError:
            await m.reply("⚠️ Enter a number >= 2.")
            return

        ud["section_count"] = section_count
        ud["sections"] = []
        ud["current_section"] = 1
        ud["last_range_end"] = 0

        del ud["awaiting_section_count"]
        ud["awaiting_section_name"] = True

        await m.reply("📚 Section 1 name:")
        return

    # ── Section name ────────────────────────────────────────────────
    if ud.get("awaiting_section_name"):
        name = m.text.strip()

        if not name:
            await m.reply("⚠️ Invalid name.")
            return

        ud["sections"].append({"name": name})

        del ud["awaiting_section_name"]
        ud["awaiting_question_range"] = True

        await m.reply(
            f"📚 Range for '{name}' (e.g. 1-5). Max: {len(ud['questions'])}"
        )
        return

    # ── Section question range ───────────────────────────────────────
    if ud.get("awaiting_question_range"):
        try:
            start, end = map(int, m.text.strip().split("-"))
            total = len(ud["questions"])

            if not (start >= 1 and end <= total and start <= end):
                raise ValueError

            if ud["last_range_end"] and start != ud["last_range_end"] + 1:
                raise ValueError

        except ValueError:
            await m.reply("⚠️ Invalid range.")
            return

        ud["sections"][-1]["question_range"] = (start, end)

        del ud["awaiting_question_range"]
        ud["last_range_end"] = end
        ud["awaiting_section_timer"] = True

        await m.reply("⏱️ Section timer in seconds (>10):")
        return

    # ── Section timer ────────────────────────────────────────────────
    if ud.get("awaiting_section_timer"):
        try:
            timer = int(m.text.strip())

            if timer <= 10:
                raise ValueError

        except ValueError:
            await m.reply("⚠️ Enter a number > 10.")
            return

        ud["sections"][-1]["timer"] = timer

        del ud["awaiting_section_timer"]

        if len(ud["sections"]) < ud["section_count"]:
            ud["current_section"] += 1
            ud["awaiting_section_name"] = True

            await m.reply(f"📚 Section {ud['current_section']} name:")
        else:
            ud["awaiting_promo"] = True
            await m.reply(
                "📢 Send your promo message (shown periodically). "
                "Send 'skip' or 'no' to leave empty."
            )
        return

    # ── Promo message ────────────────────────────────────────────────
    if ud.get("awaiting_promo"):
        promo_text = m.text.strip()

        ud["promo_message"] = (
            None
            if promo_text.lower() in ("skip", "no", "none", "/skip")
            else promo_text
        )

        del ud["awaiting_promo"]

        settings_repo = CreatorSettingsRepository(get_db())
        settings = await settings_repo.get(uid)

        default_text = settings.get("default_text")
        default_text_field = settings.get("default_text_field", "both")

        if default_text:
            field_labels = {
                "question": "questions",
                "explanation": "explanations",
                "both": "questions & explanations",
            }

            ud["awaiting_default_text_confirm"] = True
            ud["_dt'] = default_text
            ud["_dtf'] = default_text_field

            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Yes, add it",
                            callback_data=f"dtc_yes_{uid}",
                        ),
                        InlineKeyboardButton(
                            "⏭️ Skip",
                            callback_data=f"dtc_no_{uid}",
                        ),
                    ]
                ]
            )

            await m.reply(
                f"💡 **Add default text to "
                f"{field_labels.get(default_text_field, 'fields')}?**\n\n"
                f"`{default_text[:100]}`",
                reply_markup=kb,
            )
            return

        ud["awaiting_type"] = True
        await m.reply("📊 Type (free/paid)")
        return

    # ── Default-text callback pending ────────────────────────────────
    if ud.get("awaiting_default_text_confirm"):
        return

    # ── Quiz type ───────────────────────────────────────────────────
    if ud.get("awaiting_type"):
        quiz_type = m.text.strip().lower()

        if quiz_type not in ("free", "paid"):
            await m.reply("⚠️ Reply free or paid.")
            return

        timer = ud.get("timer") or 20
        sections = ud.get("sections", [])
        promo = ud.get("promo_message")
        name = m.from_user.first_name if m.from_user else str(uid)

        await _finalize_quiz(
            c, m, uid, quiz_type, promo, sections, timer, name
        )
        return

    # ── Free-text question paste ────────────────────────────────────
    blocks = m.text.split("\n\n")
    reply_msg = m.reply_to_message
    reply_text = reply_msg.text if reply_msg and reply_msg.text else None
    file_id = None

    if reply_msg and reply_msg.photo and config.BOT_GROUP:
        try:
            copied = await c.copy_message(
                config.BOT_GROUP,
                reply_msg.chat.id,
                reply_msg.id,
            )
            file_id = copied.photo.file_id
        except Exception:
            logger.debug(
                "Failed to copy pasted question's reply photo",
                exc_info=True,
            )

    parsed_any = False

    for block in blocks:
        if not block.strip():
            continue

        parsed = parse_question_block(block)

        if not parsed:
            await m.reply(
                "⚠️ Invalid format.\n\n"
                "Mark the correct option with a check-mark emoji. "
                "For multi-line questions, put a lone emoji on its own line "
                "before the options, or start each option with A) B) C) D)."
            )
            return

        parsed["file_id"] = file_id
        parsed["reply_text"] = reply_text
        ud["questions"].append(parsed)
        parsed_any = True

    if not parsed_any:
        await m.reply("⚠️ No valid question found.")
        return

    total = len(ud["questions"])
    suffix = " Consider stopping soon." if total > 200 else " Send more or /done"

    await m.reply(f"✅ {total} saved!{suffix}")


async def default_text_confirm_cb(c: Client, cb: CallbackQuery) -> None:
    """Handle the default-text confirmation buttons."""
    uid = cb.from_user.id
    action = cb.data.split("_")[1]

    if uid not in state.quiz_creation:
        await cb.answer("⚠️ Session expired", show_alert=True)
        return

    ud = state.quiz_creation[uid]
    ud.pop("awaiting_default_text_confirm", None)

    if action == "no":
        ud.pop("_dt", None)
        ud.pop("_dtf", None)
        ud["_skip_default_text"] = True
    else:
        ud["_skip_default_text"] = False

    ud["awaiting_type"] = True

    await cb.message.reply("📊 Type (free/paid)")
    await cb.answer()


def in_quiz_creation_filter():
    async def func(_, __, m: Message) -> bool:
        return bool(m.from_user) and m.from_user.id in state.quiz_creation

    return filters.create(func)


def register(app: Client) -> None:
    app.on_message(filters.command("create") & filters.private)(create_cmd)
    app.on_message(filters.command("done") & filters.private)(done_cmd)
    app.on_message(filters.command("cancel") & filters.private)(cancel_cmd)

    app.on_callback_query(
        filters.regex(r"^qd_(use|manual)_\d+$")
    )(quicksave_cb)

    app.on_callback_query(
        filters.regex(r"^dtc_(yes|no)_\d+$")
    )(default_text_confirm_cb)

    app.on_message(
        filters.document & filters.private & in_quiz_creation_filter()
    )(handle_document)

    app.on_message(
        (filters.text | filters.poll)
        & filters.private
        & in_quiz_creation_filter()
        & ~filters.command(_RESERVED_COMMANDS)
    )(handle_creation_message)
