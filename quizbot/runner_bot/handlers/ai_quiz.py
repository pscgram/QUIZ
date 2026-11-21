"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from quizbot.shared import config
from quizbot.shared.utils import is_premium_user
from quizbot.database import QuizRepository, get_db

from ..ai_providers import AIQUIZ_LANG_UI, generate_in_chunks, get_provider_keys
from ..state import AI_QUIZ_SESSIONS, last_working_ai, session_mgr, tasks
from ..telegram_utils import safe_send_message

logger = logging.getLogger(__name__)


def _kb(step: str, uid: int) -> InlineKeyboardMarkup:
    def p(s: str, v: Any) -> str:
        return f"aiq_{s}_{uid}_{v}"

    if step == "shufflecount":
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("First 2", callback_data=p("shufflecount", 2)),
                InlineKeyboardButton("First 4", callback_data=p("shufflecount", 4)),
            ],
            [InlineKeyboardButton("All", callback_data=p("shufflecount", "all"))],
        ])

    if step == "count":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(str(n), callback_data=p("count", n)) for n in (5, 10, 15, 20)],
            [InlineKeyboardButton(str(n), callback_data=p("count", n)) for n in (25, 30, 40, 50)],
            [InlineKeyboardButton(str(n), callback_data=p("count", n)) for n in (60, 75, 100)],
        ])

    if step == "lang":
        items = list(AIQUIZ_LANG_UI.items())
        rows = [
            [
                InlineKeyboardButton(label, callback_data=p("lang", code))
                for code, label in items[i:i + 2]
            ]
            for i in range(0, len(items), 2)
        ]
        return InlineKeyboardMarkup(rows)

    if step == "bilingual":
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Yes — Bilingual",
                    callback_data=p("bilingual", "yes"),
                ),
                InlineKeyboardButton(
                    "❌ No — Single Lang",
                    callback_data=p("bilingual", "no"),
                ),
            ],
        ])

    if step == "lang2":
        sess = AI_QUIZ_SESSIONS.get(uid, {})
        lang1 = sess.get("lang", "en")
        items = [(c, l) for c, l in AIQUIZ_LANG_UI.items() if c != lang1]
        rows = [
            [
                InlineKeyboardButton(label, callback_data=p("lang2", code))
                for code, label in items[i:i + 2]
            ]
            for i in range(0, len(items), 2)
        ]
        return InlineKeyboardMarkup(rows)

    if step == "diff":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🟡 Moderate", callback_data=p("diff", "moderate"))],
            [InlineKeyboardButton("🔴 Hard", callback_data=p("diff", "hard"))],
            [InlineKeyboardButton("💀 Extreme Hard", callback_data=p("diff", "extreme"))],
        ])

    if step == "exam":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 SSC Type", callback_data=p("exam", "ssc"))],
            [InlineKeyboardButton("🏛 Civil Services", callback_data=p("exam", "civil"))],
            [InlineKeyboardButton("📝 Common One Day", callback_data=p("exam", "oneway"))],
        ])

    if step == "timer":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{t}s", callback_data=p("timer", t)) for t in (10, 15, 20)],
            [InlineKeyboardButton(f"{t}s", callback_data=p("timer", t)) for t in (25, 30, 45)],
        ])

    if step == "neg":
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("None", callback_data=p("neg", "0")),
                InlineKeyboardButton("1/4", callback_data=p("neg", "0.25")),
                InlineKeyboardButton("1/3", callback_data=p("neg", "0.333")),
            ],
            [
                InlineKeyboardButton("1/2", callback_data=p("neg", "0.5")),
                InlineKeyboardButton("1", callback_data=p("neg", "1.0")),
            ],
        ])

    if step == "cm":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(str(n), callback_data=p("cm", n)) for n in (1, 2, 3, 4)]
        ])

    if step == "shuffle":
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔀 Questions", callback_data=p("shuffle", "q")),
                InlineKeyboardButton("🔀 Options", callback_data=p("shuffle", "o")),
                InlineKeyboardButton("🔀 Both", callback_data=p("shuffle", "b")),
            ],
            [InlineKeyboardButton("⏭ No Shuffle", callback_data=p("shuffle", "none"))],
        ])

    return InlineKeyboardMarkup([])


async def _edit(msg, text: str, kb: InlineKeyboardMarkup = None) -> None:
    try:
        await msg.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception:
        pass


# ============================================================
# QUIZ LAUNCHER — supports normal AI quiz and persistent PDF quiz
# ============================================================

async def _launch_ai_quiz(
    uid: int,
    ctx: ContextTypes.DEFAULT_TYPE,
    questions: list[dict],
    topic: str,
    timer: int,
    neg: float,
    cm: int,
    shuffle_q: bool = False,
    shuffle_o: bool = False,
    shuffle_o_count: int = 0,
    chat_id: int = None,
    chat_type: str = "private",
    update: Any = None,
    show_explanation: bool = False,
    persist: bool = False,
) -> str:
    """Launch a generated quiz; optionally persist it for deep-link sharing."""

    from .quiz_play import run_group_quiz, start_private_quiz

    chat_id = chat_id or uid
    is_private = str(chat_type) in ("private", "ChatType.PRIVATE")

    # Prevent overlapping quizzes in the same chat.
    if session_mgr.get(chat_id):
        await safe_send_message(
            ctx,
            chat_id,
            "⚠️ A quiz is already running. /stop it first.",
        )
        return ""

    quiz_name = f"AI: {topic[:50]}"
    qid = f"AI{int(time.time())}{random.randint(100, 999)}"

    # Save permanently when requested (PDF quiz sharing).
    if persist:
        saved = await QuizRepository(get_db()).create(
            creator_id=uid,
            quiz_name=quiz_name,
            questions=questions,
            qid=qid,
            sections=[],
            timer=timer,
            quiz_type="free",
            negative_marks=neg,
            correct_marks=cm,
            shuffle_questions=shuffle_q,
            shuffle_options=shuffle_o,
            promo_message=None,
        )

        if not saved:
            await safe_send_message(
                ctx,
                chat_id,
                "⚠️ Quiz could not be saved; share links were not created.",
            )
            return ""

    quiz_obj = {
        "question_set_id": qid,
        "quiz_name": quiz_name,
        "questions": questions,
        "timer": timer,
        "negative_marking": neg,
        "correct_mark": cm,
        "shuffle": shuffle_q,
        "shuffle_options": shuffle_o,
        "shuffle_options_count": shuffle_o_count,
        "show_explanation": show_explanation,
        "sections": [],
        "promo_message": None,
        "quiz_type": "free",
        "creator_id": uid,
    }

    # Private quiz.
    if is_private:
        await start_private_quiz(
            chat_id,
            ctx,
            questions,
            quiz_obj,
            qid,
        )
        # Return qid so PDF handler can build share links.
        return qid

    # Group quiz.
    if shuffle_q and not quiz_obj.get("sections"):
        random.shuffle(questions)

    session_data = {
        "quiz_id": qid,
        "quiz_data": quiz_obj,
        "current_index": 0,
        "paused": False,
        "polls": {},
        "participants": {},
        "is_private": False,
        "section_msgs": [],
        "modified_timer_offset": 0,
    }

    await session_mgr.create(chat_id, session_data)

    class _FakeMsg:
        def __init__(self, cid: int) -> None:
            self.chat_id = cid
            self.chat = type("C", (), {"id": cid, "type": "group"})()

    class _FakeUpdate:
        def __init__(self, cid: int) -> None:
            self.message = _FakeMsg(cid)
            self._chat_id = cid

    tasks.spawn(
        run_group_quiz(
            chat_id,
            ctx,
            questions,
            quiz_obj,
            False,
            _FakeUpdate(chat_id),
            0,
        ),
        name=f"quiz_{chat_id}_ai",
    )

    return qid


# ============================================================
# /AIQUIZ COMMAND
# ============================================================

async def aiquiz_command(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    """`/aiquiz <topic>` -- starts the wizard."""

    chat_id = update.message.chat_id

    try:
        user_id = update.message.from_user.id

        if not await is_premium_user(user_id):
            await safe_send_message(ctx, chat_id, "❌ Premium required.")
            return

        if not ctx.args:
            await safe_send_message(
                ctx,
                chat_id,
                "🤖 <b>AI Quiz Generator</b>\n\n"
                "Usage: <code>/aiquiz &lt;topic&gt;</code>\n"
                "Example: <code>/aiquiz Indian History</code>\n\n"
                "No DB storage — questions generated live, quiz starts immediately!\n"
                "Add your own AI key via <code>/setkey gemini AIzaSy...</code> "
                "for faster, higher-quality results.\n"
                "Without a key, a free fallback provider is used automatically.",
                parse_mode=ParseMode.HTML,
            )
            return

        topic = " ".join(ctx.args).strip()
        chat_type = update.message.chat.type

        AI_QUIZ_SESSIONS[user_id] = {
            "topic": topic,
            "step": "count",
            "chat_id": chat_id,
            "chat_type": chat_type,
            "user_id": user_id,
        }

        msg = await safe_send_message(
            ctx,
            chat_id,
            f"🤖 <b>AI Quiz: {topic}</b>\n\nHow many questions?",
            parse_mode=ParseMode.HTML,
            reply_markup=_kb("count", user_id),
        )

        if msg:
            AI_QUIZ_SESSIONS[user_id]["msg_id"] = msg.message_id

    except Exception as e:
        logger.error("aiquiz_command error: %s", e, exc_info=True)


# ============================================================
# CALLBACK HANDLER
# ============================================================

async def aiquiz_callback(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle all `aiq_`-prefixed callbacks."""

    try:
        query = update.callback_query
        await query.answer()

        data = query.data
        parts = data.split("_", 3)

        if len(parts) < 3:
            return

        step, uid_s = parts[1], parts[2]
        value = parts[3] if len(parts) > 3 else None
        uid = int(uid_s)

        if query.from_user.id != uid:
            await query.answer("❌ Not your session", show_alert=True)
            return

        sess = AI_QUIZ_SESSIONS.get(uid)

        if not sess:
            await query.message.edit_text("❌ Session expired. /aiquiz again")
            return

        msg = query.message
        topic = sess["topic"]

        if step == "count":
            count_val = int(value)

            if not (1 <= count_val <= 100):
                await query.answer(
                    "❌ Choose between 1–100 questions",
                    show_alert=True,
                )
                return

            sess["count"] = count_val

            await _edit(
                msg,
                f"🤖 <b>AI Quiz: {topic}</b>\n"
                f"📋 Questions: {value}\n\nLanguage?",
                _kb("lang", uid),
            )

        elif step == "lang":
            sess["lang"] = value
            lbl = AIQUIZ_LANG_UI.get(value, value)

            await _edit(
                msg,
                f"🤖 <b>AI Quiz: {topic}</b>\n"
                f"📋 {sess['count']} | 🌐 {lbl}\n\n"
                "🌐 <b>Bilingual mode?</b>\n"
                "Show questions in 2 languages (lang1 / lang2 format)",
                _kb("bilingual", uid),
            )

        elif step == "bilingual":
            if value == "yes":
                sess["bilingual"] = True
                lbl = AIQUIZ_LANG_UI.get(sess.get("lang", "en"), "English")

                await _edit(
                    msg,
                    f"🤖 <b>AI Quiz: {topic}</b>\n"
                    f"📋 {sess['count']} | 🌐 {lbl} + ?\n\n"
                    "🌐 <b>Select 2nd language:</b>",
                    _kb("lang2", uid),
                )
            else:
                sess["bilingual"] = False
                sess["lang2"] = None
                lbl = AIQUIZ_LANG_UI.get(sess.get("lang", "en"), "English")

                await _edit(
                    msg,
                    f"🤖 <b>AI Quiz: {topic}</b>\n"
                    f"📋 {sess['count']} | 🌐 {lbl}\n\nDifficulty?",
                    _kb("diff", uid),
                )

        elif step == "lang2":
            sess["lang2"] = value
            l1 = AIQUIZ_LANG_UI.get(sess.get("lang", "en"), "Lang1")
            l2 = AIQUIZ_LANG_UI.get(value, value)

            await _edit(
                msg,
                f"🤖 <b>AI Quiz: {topic}</b>\n"
                f"📋 {sess['count']} | 🌐 {l1} / {l2}\n\nDifficulty?",
                _kb("diff", uid),
            )

        elif step == "diff":
            sess["diff"] = value

            dlbl = {
                "moderate": "🟡 Moderate",
                "hard": "🔴 Hard",
                "extreme": "💀 Extreme",
            }.get(value, value)

            await _edit(
                msg,
                f"🤖 <b>AI Quiz: {topic}</b>\n"
                f"📋 {sess['count']} | {dlbl}\n\nExam style?",
                _kb("exam", uid),
            )

        elif step == "exam":
            sess["exam"] = value

            elbl = {
                "ssc": "📋 SSC",
                "civil": "🏛 Civil",
                "oneway": "📝 One Day",
            }.get(value, value)

            has_gemini = bool(await get_provider_keys(uid, "gemini"))
            has_groq = bool(await get_provider_keys(uid, "groq"))
            has_openrouter = bool(await get_provider_keys(uid, "openrouter"))
            has_default_or = bool(config.OPENROUTER_DEFAULT_KEYS)

            if has_gemini:
                ai_label = "Gemini"
            elif has_groq:
                ai_label = "Groq"
            elif has_openrouter:
                ai_label = "OpenRouter (your key)"
            elif has_default_or:
                ai_label = "OpenRouter (shared key)"
            else:
                ai_label = "Pollinations (free fallback)"

            bilingual_info = ""

            if sess.get("bilingual") and sess.get("lang2"):
                l1 = AIQUIZ_LANG_UI.get(sess.get("lang", "en"), "")
                l2 = AIQUIZ_LANG_UI.get(sess["lang2"], "")
                bilingual_info = f"\n🌐 <b>Bilingual:</b> {l1} / {l2}"

            await _edit(
                msg,
                f"🤖 <b>AI Quiz: {topic}</b>\n"
                f"📋 {sess['count']} | {elbl} | 🤖 {ai_label}"
                f"{bilingual_info}\n\n"
                "⏳ Generating questions in chunks of 25...\n"
                "This may take 30–120 seconds.",
            )

            tasks.spawn(
                _aiquiz_generate_flow(uid, ctx, msg, sess),
                name=f"aiquiz_gen_{uid}",
            )

        elif step == "timer":
            sess["timer"] = int(value)

            await _edit(
                msg,
                f"✅ Timer: {value}s\n\n➖ <b>Negative marking?</b>",
                _kb("neg", uid),
            )

        elif step == "neg":
            sess["neg"] = float(value)

            await _edit(
                msg,
                f"✅ Negative: {value}\n\n"
                "✅ <b>Correct mark per question?</b>",
                _kb("cm", uid),
            )

        elif step == "cm":
            sess["cm"] = int(value)

            await _edit(
                msg,
                f"✅ Correct mark: +{value}\n\n🔀 <b>Shuffle?</b>",
                _kb("shuffle", uid),
            )

        elif step == "shuffle":
            sess["shuffle_q"] = value in ("q", "b")
            sess["shuffle_o"] = value in ("o", "b")

            slbl = {
                "q": "Questions",
                "o": "Options",
                "b": "Both",
                "none": "None",
            }.get(value, "None")

            if sess["shuffle_o"]:
                await _edit(
                    msg,
                    f"✅ Shuffle: {slbl}\n\n"
                    "🔀 <b>Shuffle how many options?</b>",
                    _kb("shufflecount", uid),
                )
            else:
                sess["shuffle_o_count"] = 0

                await _edit(
                    msg,
                    f"✅ Shuffle: {slbl}\n\n"
                    "💡 Show explanation after each question?",
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "✅ Yes",
                            callback_data=f"aiq_ex_{uid}_yes",
                        ),
                        InlineKeyboardButton(
                            "❌ No (default)",
                            callback_data=f"aiq_ex_{uid}_no",
                        ),
                    ]]),
                )

        elif step == "shufflecount":
            sess["shuffle_o_count"] = (
                0 if value == "all" else int(value)
            )

            label = "All" if value == "all" else f"First {value}"

            await _edit(
                msg,
                f"✅ Options shuffle range: {label}\n\n"
                "💡 Show explanation after each question?",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "✅ Yes",
                        callback_data=f"aiq_ex_{uid}_yes",
                    ),
                    InlineKeyboardButton(
                        "❌ No (default)",
                        callback_data=f"aiq_ex_{uid}_no",
                    ),
                ]]),
            )

        elif step == "ex":
            sess["show_explanation"] = value == "yes"

            questions = sess.get("questions", [])
            topic = sess.get("topic", "AI Quiz")

            neg_str = (
                "None" if sess["neg"] == 0 else f"-{sess['neg']}"
            )

            slbl = (
                "Both"
                if (sess.get("shuffle_q") and sess.get("shuffle_o"))
                else "Questions"
                if sess.get("shuffle_q")
                else "Options"
                if sess.get("shuffle_o")
                else "None"
            )

            await _edit(
                msg,
                f"🤖 <b>AI Quiz Ready!</b>\n\n"
                f"📌 <b>Topic:</b> {topic}\n"
                f"📋 <b>Questions:</b> {len(questions)}\n"
                f"⏱ <b>Timer:</b> {sess['timer']}s\n"
                f"✅ <b>Correct:</b> +{sess['cm']}\n"
                f"➖ <b>Negative:</b> {neg_str}\n"
                f"🔀 <b>Shuffle:</b> {slbl}\n"
                f"💡 <b>Explanation:</b> "
                f"{'✅ Yes' if sess['show_explanation'] else '❌ No'}\n\n"
                "▶️ Starting quiz now...",
            )

            chat_id = sess.get("chat_id", uid)
            chat_type = sess.get("chat_type", "private")
            show_expl = sess.get("show_explanation", False)

            AI_QUIZ_SESSIONS.pop(uid, None)

            await asyncio.sleep(1)

            await _launch_ai_quiz(
                uid,
                ctx,
                questions,
                topic,
                sess["timer"],
                sess["neg"],
                sess["cm"],
                sess.get("shuffle_q", False),
                sess.get("shuffle_o", False),
                shuffle_o_count=sess.get("shuffle_o_count", 0),
                chat_id=chat_id,
                chat_type=chat_type,
                update=update,
                show_explanation=show_expl,
            )

    except Exception as e:
        logger.error("aiquiz_callback error: %s", e, exc_info=True)


# ============================================================
# GENERATION FLOW
# ============================================================

async def _aiquiz_generate_flow(
    uid: int,
    ctx: ContextTypes.DEFAULT_TYPE,
    msg: Any,
    sess: dict,
) -> None:
    """Background task: generate questions, then move to timer settings."""

    try:
        topic = sess["topic"]
        count = sess["count"]
        lang = sess["lang"]
        diff = sess["diff"]
        exam = sess["exam"]
        lang2 = sess.get("lang2")

        questions = await generate_in_chunks(
            uid,
            topic,
            count,
            lang,
            diff,
            exam,
            msg,
            bilingual_lang2=lang2,
        )

        if not questions:
            await _edit(
                msg,
                "❌ Failed to generate questions. Check your AI key or try a different topic.\n"
                "Add a key: <code>/setkey gemini YOUR_KEY</code>",
            )
            AI_QUIZ_SESSIONS.pop(uid, None)
            last_working_ai.pop(uid, None)
            return

        got = len(questions)

        shortfall_note = (
            f"\n⚠️ Got {got}/{count} — starting with available questions."
            if got < count
            else ""
        )

        bilingual_tag = ""

        if lang2:
            l1 = AIQUIZ_LANG_UI.get(lang, lang)
            l2 = AIQUIZ_LANG_UI.get(lang2, lang2)
            bilingual_tag = f"\n🌐 <b>Bilingual:</b> {l1} / {l2}"

        preview = (
            f"✅ <b>{got} questions generated!</b>{shortfall_note}\n\n"
            f"📌 <b>Topic:</b> {topic}{bilingual_tag}\n"
            "<b>Preview (first 3):</b>\n"
        )

        for i, q in enumerate(questions[:3], 1):
            cid = q["correct_option_id"]
            cids = cid if isinstance(cid, list) else [cid]

            preview += (
                f"\n<b>Q{i}.</b> {q['question'][:80]}"
                f"{'...' if len(q['question']) > 80 else ''}\n"
            )

            for j, opt in enumerate(q["options"]):
                mark = "✅" if j in cids else "▪️"
                preview += (
                    f"  {mark} {opt[:45]}"
                    f"{'...' if len(opt) > 45 else ''}\n"
                )

        preview += "\n⏱ <b>Timer per question?</b>"

        sess["questions"] = questions
        AI_QUIZ_SESSIONS[uid] = sess

        await _edit(msg, preview, _kb("timer", uid))

    except Exception as e:
        logger.error("_aiquiz_generate_flow error: %s", e, exc_info=True)

        await _edit(
            msg,
            f"❌ Generation error: {str(e)[:200]}",
        )

        AI_QUIZ_SESSIONS.pop(uid, None)


# ============================================================
# REGISTER HANDLERS
# ============================================================

def register(application: Application) -> None:
    application.add_handler(CommandHandler("aiquiz", aiquiz_command))
    application.add_handler(
        CallbackQueryHandler(aiquiz_callback, pattern="^aiq_")
    )
