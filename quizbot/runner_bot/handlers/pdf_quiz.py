"""
PSCGram Runner Bot
Direct PDF -> MCQ Quiz

No AI required.

The PDF must contain existing MCQs with:
1. Question
A) Option
B) Option
C) Option
D) Option
ANSWER: A
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

from quizbot.shared import config

from quizbot.creator_bot.handlers.file_import import (
    process_pdf_mcqs,
)

from .ai_quiz import _launch_ai_quiz


logger = logging.getLogger(__name__)


# ============================================================
# SETTINGS
# ============================================================

MIN_QUESTIONS = 1
MAX_QUESTIONS = 300


# ============================================================
# DIRECT PDF HANDLER
# ============================================================

async def handle_pdf(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Receive a PDF directly from the user and create
    a playable quiz from existing MCQs.

    No AI.
    No channel.
    """

    if not update.message:
        return

    document = update.message.document

    if not document:
        return

    user = update.effective_user

    if not user:
        return

    uid = user.id

    filename = (
        document.file_name
        or "quiz.pdf"
    )

    mime_type = (
        document.mime_type
        or ""
    )

    # --------------------------------------------------------
    # Verify PDF
    # --------------------------------------------------------

    if (
        not filename.lower().endswith(".pdf")
        and mime_type != "application/pdf"
    ):
        return

    # --------------------------------------------------------
    # Status
    # --------------------------------------------------------

    status = await update.message.reply_text(
        "📥 **Downloading PDF...**",
        parse_mode="Markdown",
    )

    # --------------------------------------------------------
    # Download
    # --------------------------------------------------------

    try:

        tg_file = await context.bot.get_file(
            document.file_id
        )

        content = await tg_file.download_as_bytearray()

    except Exception as e:

        logger.exception(
            "PDF download failed"
        )

        await status.edit_text(
            "❌ Could not download the PDF."
        )

        return

    # --------------------------------------------------------
    # Parse PDF
    # --------------------------------------------------------

    try:

        await status.edit_text(
            "🔍 **Reading PDF...**\n\n"
            "Extracting existing MCQs...",
            parse_mode="Markdown",
        )

        questions = process_pdf_mcqs(
            bytes(content),
            max_questions=MAX_QUESTIONS,
        )

    except Exception as e:

        logger.exception(
            "PDF parsing failed"
        )

        await status.edit_text(
            "❌ Could not read the PDF."
        )

        return

    # --------------------------------------------------------
    # No questions
    # --------------------------------------------------------

    if not questions:

        await status.edit_text(
            "❌ **No MCQs found.**\n\n"
            "The PDF must contain text-based MCQs "
            "with options and answers.\n\n"
            "Example:\n"
            "1. What is the capital of Kerala?\n"
            "A) Kochi\n"
            "B) Kollam\n"
            "C) Thiruvananthapuram\n"
            "D) Kannur\n"
            "ANSWER: C\n\n"
            "⚠️ Scanned/image-only PDFs are not supported "
            "by this no-AI version.",
            parse_mode="Markdown",
        )

        return

    # --------------------------------------------------------
    # Minimum
    # --------------------------------------------------------

    if len(questions) < MIN_QUESTIONS:

        await status.edit_text(
            f"⚠️ Only {len(questions)} question(s) found."
        )

        return

    # --------------------------------------------------------
    # Quiz name
    # --------------------------------------------------------

    quiz_name = filename

    if quiz_name.lower().endswith(".pdf"):
        quiz_name = quiz_name[:-4]

    quiz_name = quiz_name.replace(
        "_",
        " ",
    )

    quiz_name = quiz_name.replace(
        "-",
        " ",
    )

    quiz_name = " ".join(
        quiz_name.split()
    )

    # --------------------------------------------------------
    # Launch
    # --------------------------------------------------------

    await status.edit_text(
        f"✅ **PDF processed!**\n\n"
        f"📝 Quiz: **{quiz_name}**\n"
        f"❓ Questions: **{len(questions)}**\n\n"
        f"🚀 Starting quiz...",
        parse_mode="Markdown",
    )

    try:

        # ----------------------------------------------------
        # Convert imported questions into runner format
        # ----------------------------------------------------

        runner_questions = []

        for q in questions:

            runner_questions.append(
                {
                    "question": q.get(
                        "question",
                        "",
                    ),
                    "options": q.get(
                        "options",
                        [],
                    ),
                    "correct_option_id": q.get(
                        "correct_option_id",
                        0,
                    ),
                    "explanation": q.get(
                        "explanation",
                        "",
                    ),
                }
            )

        # ----------------------------------------------------
        # Launch using existing quiz engine
        # ----------------------------------------------------

        await _launch_ai_quiz(
            uid=uid,
            ctx=context,
            questions=runner_questions,
            topic=quiz_name,
            timer=20,
            neg=0,
            cm=None,
            shuffle_q=False,
            shuffle_o=False,
            shuffle_o_count=0,
            chat_id=update.effective_chat.id,
            chat_type=(
                update.effective_chat.type
                if update.effective_chat
                else "private"
            ),
            update=update,
            show_explanation=False,
        )

        try:
            await status.delete()
        except Exception:
            pass

    except Exception as e:

        logger.exception(
            "Failed to launch PDF quiz"
        )

        await status.edit_text(
            "❌ PDF was processed, "
            "but the quiz could not be started.\n\n"
            f"`{str(e)[:300]}`",
            parse_mode="Markdown",
        )


# ============================================================
# REGISTER
# ============================================================

def register(
    application: Application,
) -> None:

    application.add_handler(
        MessageHandler(
            filters.Document.PDF
            & filters.ChatType.PRIVATE,
            handle_pdf,
        )
    )
