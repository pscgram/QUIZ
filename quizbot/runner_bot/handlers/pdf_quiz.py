
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
                "✏️ Edit Question / Options",
                callback_data=f"pdfq_ed_{uid}_menu",
            )
        ],
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
# EDIT MENU KEYBOARD
# ============================================================

def _edit_menu_kb(uid: int, question: dict) -> InlineKeyboardMarkup:
    options = question.get("options", [])

    buttons = [
        [
            InlineKeyboardButton(
                "📝 Edit Question Text",
                callback_data=f"pdfq_ed_{uid}_question",
            )
        ]
    ]

    for i, option in enumerate(options):
        buttons.append([
            InlineKeyboardButton(
                f"✏️ Edit Option {i + 1}",
                callback_data=f"pdfq_ed_{uid}_opt_{i}",
            ),
            InlineKeyboardButton(
                f"🗑 Remove {i + 1}",
                callback_data=f"pdfq_ed_{uid}_del_{i}",
            ),
        ])

    if len(options) < 10:
        buttons.append([
            InlineKeyboardButton(
                "➕ Add New Option",
                callback_data=f"pdfq_ed_{uid}_add",
            )
        ])

    buttons.extend([
        [
            InlineKeyboardButton(
                "✅ Change Correct Answer",
                callback_data=f"pdfq_ed_{uid}_answer",
            )
        ],
        [
            InlineKeyboardButton(
                "↩️ Back to Review",
                callback_data=f"pdfq_ed_{uid}_back",
            )
        ],
    ])

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
        "You can edit the question, options and correct answer.\n"
        "Skipped questions will not be included."
    )

    return text


# ============================================================
# REFRESH REVIEW MESSAGE
# ============================================================

async def _show_review_again(
    ctx: ContextTypes.DEFAULT_TYPE,
    uid: int,
    sess: dict,
) -> None:
    """Restore the review screen after an edit."""

    questions = sess.get("questions", [])
    if not questions:
        return

    index = sess.get("review_index", 0)
    index = max(0, min(index, len(questions) - 1))

    chat_id = sess.get("review_chat_id", sess.get("chat_id", uid))
    message_id = sess.get("review_message_id")

    text = _review_text(sess, index)
    markup = _review_kb(
        uid,
        index,
        len(questions),
        sess.setdefault("skipped", set()),
    )

    if message_id:
        try:
            await ctx.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
            return
        except Exception as exc:
            logger.warning("Could not restore review message: %s", exc)

    sent = await safe_send_message(
        ctx,
        chat_id,
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
    )

    if sent:
        sess["review_message_id"] = sent.message_id
        sess["review_chat_id"] = sent.chat_id


# ============================================================
# MESSAGE HANDLER — PAGE RANGE, EDITING & QUIZ NAME
# ============================================================

async def pdfquiz_message_handler(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Handle page range, edit inputs and quiz-name text."""

    try:
        if not update.message or not update.message.text:
            return False

        user_id = update.message.from_user.id
        chat_id = update.message.chat_id
        sess = PDF_QUIZ_SESSIONS.get(user_id)

        if not sess:
            return False

        # ----------------------------------------------------
        # QUESTION EDIT INPUTS
        # ----------------------------------------------------

        edit_step = sess.get("step")

        if edit_step in {
            "edit_question",
            "edit_option",
            "edit_add_option",
            "edit_answer",
        }:
            value = update.message.text.strip()
            questions = sess.get("questions", [])
            index = sess.get("review_index", 0)

            if not questions or not (0 <= index < len(questions)):
                sess["step"] = "review"
                return True

            q = questions[index]
            options = q.setdefault("options", [])

            if edit_step == "edit_question":
                if not value:
                    await safe_send_message(
                        ctx,
                        chat_id,
                        "❌ Question cannot be empty. Send the corrected question:",
                    )
                    return True

                q["question"] = value

            elif edit_step == "edit_option":
                option_index = sess.get("editing_option_index", -1)

                if not (0 <= option_index < len(options)):
                    sess["step"] = "review"
                    await safe_send_message(
                        ctx, chat_id, "❌ Invalid option. Please try again."
                    )
                    return True

                if not value:
                    await safe_send_message(
                        ctx,
                        chat_id,
                        "❌ Option cannot be empty. Send the corrected option:",
                    )
                    return True

                options[option_index] = value

            elif edit_step == "edit_add_option":
                if not value:
                    await safe_send_message(
                        ctx,
                        chat_id,
                        "❌ Option cannot be empty. Send the option again:",
                    )
                    return True

                if len(options) >= 10:
                    await safe_send_message(
                        ctx, chat_id, "❌ Maximum 10 options are allowed."
                    )
                    return True

                options.append(value)

            elif edit_step == "edit_answer":
                try:
                    answer_ids = sorted({
                        int(part.strip()) - 1
                        for part in value.split(",")
                    })

                    if (
                        not answer_ids
                        or any(
                            answer_id < 0 or answer_id >= len(options)
                            for answer_id in answer_ids
                        )
                    ):
                        raise ValueError

                    q["correct_option_id"] = answer_ids

                except (TypeError, ValueError):
                    await safe_send_message(
                        ctx,
                        chat_id,
                        "❌ Invalid answer number.\n\n"
                        "Send the correct option number.\n"
                        "Example: <code>2</code>\n"
                        "For multiple correct answers: <code>2,4</code>",
                        parse_mode=ParseMode.HTML,
                    )
                    return True

            sess["step"] = "review"
            sess.pop("editing_option_index", None)

            await _show_review_again(ctx, user_id, sess)

            await safe_send_message(
                ctx,
                chat_id,
                "✅ Changes saved successfully.",
            )
            return True

        # ----------------------------------------------------
        # PAGE RANGE
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # QUIZ NAME
        # ----------------------------------------------------

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
        # EDIT QUESTION / OPTIONS
        # ----------------------------------------------------

        if step == "ed":
            questions = sess.get("questions", [])
            index = sess.get("review_index", 0)

            if not questions or not (0 <= index < len(questions)):
                await msg.edit_text("❌ Question not found.")
                return

            q = questions[index]
            options = q.setdefault("options", [])

            sess["review_message_id"] = msg.message_id
            sess["review_chat_id"] = msg.chat_id

            # EDIT MENU
            if value == "menu":
                sess["step"] = "review"

                await msg.edit_text(
                    "✏️ <b>EDIT QUESTION</b>\n\n"
                    "Choose what you want to change.\n"
                    "You can edit the question, individual options, "
                    "or the correct answer.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=_edit_menu_kb(uid, q),
                )
                return

            # BACK TO REVIEW
            if value == "back":
                sess["step"] = "review"
                await msg.edit_text(
                    _review_text(sess, index),
                    parse_mode=ParseMode.HTML,
                    reply_markup=_review_kb(
                        uid,
                        index,
                        len(questions),
                        sess.setdefault("skipped", set()),
                    ),
                )
                return

            # EDIT QUESTION TEXT
            if value == "question":
                sess["step"] = "edit_question"

                await msg.edit_text(
                    "📝 <b>EDIT QUESTION TEXT</b>\n\n"
                    "Send the corrected question text.\n\n"
                    "Malayalam and other languages are supported.\n"
                    "Your text will be saved exactly as you send it.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "↩️ Cancel",
                            callback_data=f"pdfq_ed_{uid}_back",
                        )
                    ]]),
                )
                return

            # EDIT AN INDIVIDUAL OPTION
            if value and value.startswith("opt_"):
                try:
                    option_index = int(value.split("_", 1)[1])
                except ValueError:
                    return

                if not (0 <= option_index < len(options)):
                    await query.answer("Invalid option.", show_alert=True)
                    return

                sess["step"] = "edit_option"
                sess["editing_option_index"] = option_index

                await msg.edit_text(
                    f"✏️ <b>EDIT OPTION {option_index + 1}</b>\n\n"
                    f"Current option:\n"
                    f"<code>{escape(str(options[option_index]))}</code>\n\n"
                    "Send the replacement option text.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "↩️ Cancel",
                            callback_data=f"pdfq_ed_{uid}_back",
                        )
                    ]]),
                )
                return

            # ADD AN OPTION
            if value == "add":
                if len(options) >= 10:
                    await query.answer(
                        "Maximum 10 options are allowed.",
                        show_alert=True,
                    )
                    return

                sess["step"] = "edit_add_option"

                await msg.edit_text(
                    "➕ <b>ADD NEW OPTION</b>\n\n"
                    "Send the new option text.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "↩️ Cancel",
                            callback_data=f"pdfq_ed_{uid}_back",
                        )
                    ]]),
                )
                return

            # REMOVE AN OPTION
            if value and value.startswith("del_"):
                try:
                    option_index = int(value.split("_", 1)[1])
                except ValueError:
                    return

                if not (0 <= option_index < len(options)):
                    await query.answer("Invalid option.", show_alert=True)
                    return

                if len(options) <= 2:
                    await query.answer(
                        "At least 2 options must remain.",
                        show_alert=True,
                    )
                    return

                removed_option = options.pop(option_index)

                old_correct = q.get("correct_option_id", [])
                if not isinstance(old_correct, list):
                    old_correct = [old_correct]

                new_correct = []

                for correct_id in old_correct:
                    if not isinstance(correct_id, int):
                        continue

                    if correct_id == option_index:
                        continue

                    if correct_id > option_index:
                        correct_id -= 1

                    new_correct.append(correct_id)

                q["correct_option_id"] = new_correct
                sess["step"] = "review"

                await msg.edit_text(
                    "🗑 <b>Option removed.</b>\n\n"
                    f"Removed: {escape(str(removed_option))}\n\n"
                    + _review_text(sess, index),
                    parse_mode=ParseMode.HTML,
                    reply_markup=_review_kb(
                        uid,
                        index,
                        len(questions),
                        sess.setdefault("skipped", set()),
                    ),
                )

                if not new_correct:
                    await safe_send_message(
                        ctx,
                        msg.chat_id,
                        "⚠️ The removed option was marked as correct.\n"
                        "Please use Edit Question / Options → "
                        "Change Correct Answer to select the new answer.",
                    )
                return

            # CHANGE CORRECT ANSWER
            if value == "answer":
                sess["step"] = "edit_answer"

                option_lines = "\n".join(
                    f"{i + 1}. {escape(str(option))}"
                    for i, option in enumerate(options)
                )

                await msg.edit_text(
                    "✅ <b>CHANGE CORRECT ANSWER</b>\n\n"
                    f"{option_lines}\n\n"
                    "Send the correct option number.\n"
                    "Example: <code>2</code>\n\n"
                    "For multiple correct answers, use commas:\n"
                    "<code>2,4</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "↩️ Cancel",
                            callback_data=f"pdfq_ed_{uid}_back",
                        )
                    ]]),
                )
                return

            return

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

                # Do not include skipped questions.
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
            sess["review_message_id"] = msg.message_id
            sess["review_chat_id"] = msg.chat_id
            sess["step"] = "review"

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
            try:
                count = int(value)
            except (TypeError, ValueError):
                await query.answer("Invalid question count.", show_alert=True)
                return

            if count not in (5, 10, 15, 20, 25, 30, 40, 50):
                await query.answer("Invalid question count.", show_alert=True)
                return

            sess["count"] = count

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
        sess["review_message_id"] = msg.message_id
        sess["review_chat_id"] = msg.chat_id

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
