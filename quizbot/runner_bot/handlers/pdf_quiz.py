"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from html import escape
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from quizbot.shared import config
from quizbot.shared.utils import is_premium_user
from quizbot.shared.utils.async_files import remove_file, write_temp_file

from ..ai_providers import gemini_page_questions, get_provider_key_single
from ..state import PDF_QUIZ_SESSIONS
from ..telegram_utils import safe_send_message
from .ai_quiz import _launch_ai_quiz

logger = logging.getLogger(__name__)


# ============================================================
# PDF QUIZ COMMAND
# ============================================================

async def pdfquiz_command(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Start PDF quiz creation by replying to a PDF."""

    if not update.message:
        return

    chat_id = update.message.chat_id

    try:
        user_id = update.message.from_user.id

        if not await is_premium_user(user_id):
            await safe_send_message(ctx, chat_id, "❌ Premium required.")
            return

        gemini_key = await get_provider_key_single(user_id, "gemini")

        if not gemini_key:
            await safe_send_message(
                ctx,
                chat_id,
                "🔑 <b>Gemini key required for PDF quiz</b>\n\n"
                "Get a free key at "
                "<a href='https://aistudio.google.com'>Google AI Studio</a>\n\n"
                "Then set it using:\n"
                "<code>/setkey gemini YOUR_KEY</code>",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return

        doc = None

        if update.message.reply_to_message:
            replied = update.message.reply_to_message.document

            if replied and replied.mime_type == "application/pdf":
                doc = replied

        if not doc:
            await safe_send_message(
                ctx,
                chat_id,
                "📄 <b>How to use /pdfquiz</b>\n\n"
                "Reply to a PDF file with:\n\n"
                "• <code>/pdfquiz</code> — select pages interactively\n"
                "• <code>/pdfquiz 1-10</code> — select pages 1 to 10\n"
                "• <code>/pdfquiz all</code> — select up to 20 pages\n\n"
                "⚠️ Requires a Gemini API key.",
                parse_mode=ParseMode.HTML,
            )
            return

        sm = await safe_send_message(ctx, chat_id, "📥 Downloading PDF...")

        file = await ctx.bot.get_file(doc.file_id)
        file_bytes = await file.download_as_bytearray()

        os.makedirs(config.TEMP_DIR, exist_ok=True)

        pdf_path = os.path.join(
            config.TEMP_DIR,
            f"pdfquiz_{user_id}_{int(time.time())}.pdf",
        )

        await write_temp_file(bytes(file_bytes), pdf_path)

        total_pages = await _count_pdf_pages(pdf_path)

        PDF_QUIZ_SESSIONS[user_id] = {
            "pdf_path": pdf_path,
            "gemini_key": gemini_key,
            "total_pages": total_pages,
            "step": "pages",
            "chat_id": chat_id,
            "chat_type": str(update.message.chat.type),
            "user_id": user_id,
        }

        page_info = f" ({total_pages} pages)" if total_pages else ""
        inline_range = " ".join(ctx.args).strip().lower() if ctx.args else ""

        if inline_range:
            try:
                start_p, end_p = _parse_range(inline_range, total_pages)
            except (ValueError, IndexError):
                await sm.edit_text(
                    "❌ Invalid page range.\n"
                    "Use <code>1-10</code> or <code>all</code>.",
                    parse_mode=ParseMode.HTML,
                )
                PDF_QUIZ_SESSIONS.pop(user_id, None)
                await remove_file(pdf_path)
                return

            sess = PDF_QUIZ_SESSIONS[user_id]
            sess["page_start"] = start_p
            sess["page_end"] = end_p
            sess["step"] = "count"

            await sm.edit_text(
                f"📄 <b>PDF ready{page_info}</b>\n\n"
                f"✅ Pages <b>{start_p}–{end_p}</b> selected "
                f"({end_p - start_p + 1} page(s))\n\n"
                "How many questions should I generate?",
                parse_mode=ParseMode.HTML,
                reply_markup=_count_kb(user_id),
            )
            return

        await sm.edit_text(
            f"📄 <b>PDF ready{page_info}</b>\n\n"
            "Send the page range to generate questions from:\n\n"
            "• <code>1-5</code> — pages 1 to 5\n"
            "• <code>3-10</code> — pages 3 to 10\n"
            "• <code>all</code> — all pages (maximum 20)\n\n"
            "<i>Tip: You can also use "
            "<code>/pdfquiz 1-10</code></i>\n\n"
            "🔍 Pages will be processed individually.",
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        logger.error("pdfquiz_command error: %s", e, exc_info=True)

        try:
            await safe_send_message(ctx, chat_id, f"❌ Error: {str(e)[:200]}")
        except Exception:
            pass


# ============================================================
# PAGE RANGE
# ============================================================

def _parse_range(text: str, total_pages: int) -> tuple[int, int]:
    text = text.strip().lower()

    if text == "all":
        if total_pages:
            return 1, min(20, total_pages)
        return 1, 20

    parts = text.split("-")

    if len(parts) != 2:
        raise ValueError("Invalid page range")

    start_p = int(parts[0].strip())
    end_p = int(parts[1].strip())

    if start_p < 1 or end_p < start_p:
        raise ValueError("Invalid page range")

    if total_pages and start_p > total_pages:
        raise ValueError("Page out of bounds")

    if total_pages:
        end_p = min(end_p, total_pages)

    end_p = min(end_p, start_p + 19)

    return start_p, end_p


# ============================================================
# QUESTION COUNT BUTTONS
# ============================================================

def _count_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    str(n),
                    callback_data=f"pdfq_count_{user_id}_{n}",
                )
                for n in (5, 10, 15, 20)
            ],
            [
                InlineKeyboardButton(
                    str(n),
                    callback_data=f"pdfq_count_{user_id}_{n}",
                )
                for n in (25, 30, 40, 50)
            ],
        ]
    )


# ============================================================
# PDF PAGE COUNTER
# ============================================================

async def _count_pdf_pages(pdf_path: str) -> int:
    try:
        import fitz
    except ImportError:
        return 0

    def _count() -> int:
        doc = fitz.open(pdf_path)
        try:
            return doc.page_count
        finally:
            doc.close()

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _count)


# ============================================================
# MESSAGE HANDLER — PAGE RANGE & QUIZ NAME
# ============================================================

async def pdfquiz_message_handler(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Handle page range and quiz-name text while setup is active."""

    try:
        if not update.message or not update.message.text:
            return False

        user_id = update.message.from_user.id
        chat_id = update.message.chat_id
        sess = PDF_QUIZ_SESSIONS.get(user_id)

        if not sess:
            return False

        # PAGE RANGE
        if sess.get("step") == "pages":
            text = update.message.text.strip().lower()

            try:
                start_p, end_p = _parse_range(
                    text, sess.get("total_pages", 0)
                )
            except (ValueError, IndexError):
                await safe_send_message(
                    ctx,
                    chat_id,
                    "❌ Invalid format. Send <code>1-10</code> or <code>all</code>.",
                    parse_mode=ParseMode.HTML,
                )
                return True

            sess["page_start"] = start_p
            sess["page_end"] = end_p
            sess["step"] = "count"

            await safe_send_message(
                ctx,
                chat_id,
                f"✅ Pages <b>{start_p}–{end_p}</b> selected "
                f"({end_p - start_p + 1} page(s))\n\n"
                "How many questions should I generate?",
                parse_mode=ParseMode.HTML,
                reply_markup=_count_kb(user_id),
            )
            return True

        # QUIZ NAME
        if sess.get("step") == "title":
            quiz_name = update.message.text.strip()

            if not quiz_name:
                await safe_send_message(
                    ctx,
                    chat_id,
                    "❌ Quiz name cannot be empty. Send a quiz name:",
                )
                return True

            if len(quiz_name) > 80:
                await safe_send_message(
                    ctx,
                    chat_id,
                    "❌ Quiz name must be 80 characters or fewer. Try again:",
                )
                return True

            approved = sess.get("questions", [])
            chat_id = sess.get("chat_id", user_id)
            chat_type = sess.get("chat_type", "private")
            pdf_path = sess.get("pdf_path", "")

            PDF_QUIZ_SESSIONS.pop(user_id, None)
            await remove_file(pdf_path)

            await safe_send_message(
                ctx,
                chat_id,
                f"✅ Quiz name: <b>{escape(quiz_name)}</b>\n"
                "🚀 Creating quiz...",
                parse_mode=ParseMode.HTML,
            )

            qid = await _launch_ai_quiz(
                user_id,
                ctx,
                approved,
                "PDF Quiz",
                20,
                0,
                1,
                False,
                False,
                shuffle_o_count=0,
                chat_id=chat_id,
                chat_type=chat_type,
                update=update,
                show_explanation=True,
                persist=True,
                quiz_name=quiz_name,
            )

            if qid:
                from quizbot.shared.quiz_links import (
                    player_quiz_link,
                    player_group_link,
                    player_share_link,
                )

                await safe_send_message(
                    ctx,
                    chat_id,
                    "✅ <b>PDF Quiz saved permanently!</b>\n\n"
                    "Your quiz is ready. Use the links below:",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "▶️ Start Quiz",
                                    url=player_quiz_link(qid),
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    "👥 Add Player Bot to Group",
                                    url=player_group_link(qid),
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    "🔗 Share Quiz",
                                    url=player_share_link(qid),
                                )
                            ],
                        ]
                    ),
                )

            return True

        return False

    except Exception as e:
        logger.error(
            "pdfquiz_message_handler error: %s",
            e,
            exc_info=True,
        )
        return False


async def pdfquiz_text_gate(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Ignore text outside an active PDF quiz setup flow."""
    await pdfquiz_message_handler(update, ctx)


# ============================================================
# REVIEW KEYBOARD
# ============================================================

def _review_kb(
    uid: int,
    index: int,
    total: int,
    skipped: set,
) -> InlineKeyboardMarkup:

    skip_label = (
        "↩️ Keep this question"
        if index in skipped
        else "⏭ Skip this question"
    )

    buttons = [
        [
            InlineKeyboardButton(
                "⏮ Previous",
                callback_data=f"pdfq_rv_{uid}_prev",
            ),
            InlineKeyboardButton(
                "Next ⏭",
                callback_data=f"pdfq_rv_{uid}_next",
            ),
        ],
        [
            InlineKeyboardButton(
                skip_label,
                callback_data=f"pdfq_rv_{uid}_toggle",
            )
        ],
        [
            InlineKeyboardButton(
                "✅ Approve & Create Quiz",
                callback_data=f"pdfq_rv_{uid}_finish",
            )
        ],
    ]

    return InlineKeyboardMarkup(buttons)


# ============================================================
# REVIEW TEXT
# ============================================================

def _review_text(sess: dict, index: int) -> str:
    questions = sess.get("questions", [])
    skipped = sess.get("skipped", set())

    if not questions:
        return "❌ No questions available."

    index = max(0, min(index, len(questions) - 1))
    q = questions[index]

    status = "⏭ SKIPPED" if index in skipped else "✅ KEEP"
    correct_ids = q.get("correct_option_id", [])

    if not isinstance(correct_ids, list):
        correct_ids = [correct_ids]

    question_text = escape(str(q.get("question", "")))

    text = (
        "📋 <b>PDF QUESTION REVIEW</b>\n"
        f"Question {index + 1}/{len(questions)}\n"
        f"Status: {status}\n\n"
        f"<b>{question_text}</b>\n\n"
    )

    options = q.get("options", [])

    for i, option in enumerate(options):
        mark = "✅" if i in correct_ids else "▫️"
        text += f"{mark} {escape(str(option))}\n"

    text += (
        "\nUse Previous/Next to review questions.\n"
        "Skipped questions will not be included."
    )

    return text


# ============================================================
# PDF QUIZ CALLBACK
# ============================================================

async def pdfquiz_callback(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle PDF quiz callbacks."""

    try:
        query = update.callback_query

        if not query:
            return

        data = query.data or ""
        parts = data.split("_", 3)

        if len(parts) < 3:
            await query.answer()
            return

        step = parts[1]
        uid_s = parts[2]
        value = parts[3] if len(parts) > 3 else None

        try:
            uid = int(uid_s)
        except ValueError:
            await query.answer()
            return

        if query.from_user.id != uid:
            await query.answer("❌ Not your session", show_alert=True)
            return

        sess = PDF_QUIZ_SESSIONS.get(uid)

        if not sess:
            await query.answer()
            try:
                await query.message.edit_text(
                    "❌ Session expired. Please start /pdfquiz again."
                )
            except Exception:
                pass
            return

        await query.answer()
        msg = query.message

        # ----------------------------------------------------
        # QUESTION REVIEW
        # ----------------------------------------------------

        if step == "rv":
            questions = sess.get("questions", [])

            if not questions:
                await msg.edit_text("❌ No questions available to review.")
                return

            skipped = sess.setdefault("skipped", set())
            index = sess.get("review_index", 0)

            if value == "prev":
                index = max(0, index - 1)

            elif value == "next":
                index = min(len(questions) - 1, index + 1)

            elif value == "toggle":
                if index in skipped:
                    skipped.remove(index)
                else:
                    skipped.add(index)

            elif value == "finish":
                approved = [
                    q for i, q in enumerate(questions)
                    if i not in skipped
                ]

                if not approved:
                    await msg.edit_text(
                        "❌ All questions are skipped.\n"
                        "Keep at least one question to continue."
                    )
                    return

                sess["questions"] = approved
                sess["step"] = "title"

                await msg.edit_text(
                    f"✅ Review approved!\n"
                    f"Questions kept: {len(approved)}\n"
                    f"Questions skipped: {len(skipped)}\n\n"
                    "📝 <b>Send a name for this quiz.</b>\n\n"
                    "Example: Kerala PSC – Indian Constitution",
                    parse_mode=ParseMode.HTML,
                )
                return

            index = max(0, min(index, len(questions) - 1))
            sess["review_index"] = index

            await msg.edit_text(
                _review_text(sess, index),
                parse_mode=ParseMode.HTML,
                reply_markup=_review_kb(
                    uid,
                    index,
                    len(questions),
                    skipped,
                ),
            )
            return

        # ----------------------------------------------------
        # QUESTION COUNT — START GENERATION
        # ----------------------------------------------------

        if step == "count":
            sess["count"] = int(value)

            # PDF quiz defaults
            sess["diff"] = "moderate"
            sess["exam"] = "oneway"
            sess["lang"] = "auto"
            sess["timer"] = 20
            sess["neg"] = 0
            sess["cm"] = 1
            sess["shuffle_q"] = False
            sess["shuffle_o"] = False
            sess["shuffle_o_count"] = 0
            sess["show_explanation"] = True

            pages_count = (
                sess["page_end"] - sess["page_start"] + 1
            )

            await msg.edit_text(
                "📄 <b>Starting PDF extraction...</b>\n\n"
                f"📄 Pages: {sess['page_start']}–{sess['page_end']} "
                f"({pages_count} pages)\n"
                f"🎯 Target: {sess['count']} questions\n\n"
                "🔍 Processing pages individually.\n"
                "⏳ Please wait...",
                parse_mode=ParseMode.HTML,
            )

            from ..state import tasks

            tasks.spawn(
                _pdfquiz_generate_flow(uid, ctx, msg, sess),
                name=f"pdfquiz_gen_{uid}",
            )
            return

    except Exception as e:
        logger.error("pdfquiz_callback error: %s", e, exc_info=True)


# ============================================================
# PDF QUESTION GENERATION FLOW
# ============================================================

async def _pdfquiz_generate_flow(
    uid: int,
    ctx: ContextTypes.DEFAULT_TYPE,
    msg: Any,
    sess: dict,
) -> None:
    """Process selected PDF pages and send questions to review."""

    pdf_path = sess["pdf_path"]
    start_p = sess["page_start"]
    end_p = sess["page_end"]
    total_q = sess["count"]
    diff = sess["diff"]
    exam = sess["exam"]
    lang = sess["lang"]
    gemini_key = sess["gemini_key"]

    try:
        import fitz
    except ImportError:
        await msg.edit_text(
            "❌ PyMuPDF is not installed.\n"
            "Admin must install: <code>pip install PyMuPDF</code>",
            parse_mode=ParseMode.HTML,
        )
        PDF_QUIZ_SESSIONS.pop(uid, None)
        await remove_file(pdf_path)
        return

    loop = asyncio.get_running_loop()
    src_doc = None

    def _open_doc():
        return fitz.open(pdf_path)

    def _extract_page_bytes(doc, page_idx: int) -> bytes:
        one_page = fitz.open()

        try:
            one_page.insert_pdf(
                doc,
                from_page=page_idx,
                to_page=page_idx,
            )
            return one_page.tobytes()
        finally:
            one_page.close()

    try:
        src_doc = await loop.run_in_executor(None, _open_doc)

        actual_end = min(end_p, src_doc.page_count)
        total_pages_in_range = actual_end - start_p + 1

        if total_pages_in_range <= 0:
            await msg.edit_text("❌ Invalid page range.")
            PDF_QUIZ_SESSIONS.pop(uid, None)
            await remove_file(pdf_path)
            return

        all_questions: list[dict] = []

        for page_idx in range(start_p - 1, actual_end):
            page_num = page_idx + 1
            pages_remaining = actual_end - page_idx
            questions_remaining = total_q - len(all_questions)

            if questions_remaining <= 0:
                break

            hint = max(
                1,
                (questions_remaining + pages_remaining - 1)
                // pages_remaining,
            )

            try:
                await msg.edit_text(
                    f"🔍 <b>Processing page {page_num}</b>\n"
                    f"({page_num - start_p + 1}/{total_pages_in_range})\n\n"
                    f"📋 Questions collected: "
                    f"<b>{len(all_questions)}</b> / {total_q}\n\n"
                    "⏳ Sending page to Gemini...",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

            page_bytes = await loop.run_in_executor(
                None,
                _extract_page_bytes,
                src_doc,
                page_idx,
            )

            page_b64 = base64.b64encode(page_bytes).decode("utf-8")

            try:
                page_questions = await gemini_page_questions(
                    gemini_key,
                    page_b64,
                    page_num,
                    hint,
                    diff,
                    exam,
                    lang,
                )

                if page_questions:
                    all_questions.extend(page_questions)

            except Exception as e:
                logger.warning(
                    "PDF quiz page %d generation failed: %s",
                    page_num,
                    e,
                )

            await asyncio.sleep(0.5)

        if not all_questions:
            await msg.edit_text(
                "❌ No questions generated.\n\n"
                "The PDF may be empty or unreadable. "
                "Try a different page range."
            )
            PDF_QUIZ_SESSIONS.pop(uid, None)
            await remove_file(pdf_path)
            return

        all_questions = all_questions[:total_q]

        # START REVIEW — QUIZ NOT CREATED YET
        sess["questions"] = all_questions
        sess["skipped"] = set()
        sess["review_index"] = 0
        sess["review_complete"] = False
        sess["step"] = "review"

        PDF_QUIZ_SESSIONS[uid] = sess

        await msg.edit_text(
            _review_text(sess, 0),
            parse_mode=ParseMode.HTML,
            reply_markup=_review_kb(
                uid,
                0,
                len(all_questions),
                sess["skipped"],
            ),
        )

    except Exception as e:
        logger.error(
            "_pdfquiz_generate_flow error: %s",
            e,
            exc_info=True,
        )

        try:
            await msg.edit_text(
                f"❌ Generation error: {escape(str(e)[:300])}",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

        PDF_QUIZ_SESSIONS.pop(uid, None)

    finally:
        if src_doc is not None:
            try:
                await loop.run_in_executor(None, src_doc.close)
            except Exception:
                pass

        if uid not in PDF_QUIZ_SESSIONS:
            await remove_file(pdf_path)


# ============================================================
# REGISTER HANDLERS
# ============================================================

def register(application: Application) -> None:
    application.add_handler(
        CommandHandler("pdfquiz", pdfquiz_command)
    )

    application.add_handler(
        CallbackQueryHandler(
            pdfquiz_callback,
            pattern=r"^pdfq_",
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & filters.ChatType.PRIVATE,
            pdfquiz_text_gate,
        )
    )
