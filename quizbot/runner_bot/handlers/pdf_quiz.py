"""
No-AI PDF -> Quiz handler.

Sends a PDF directly to the Runner Bot.
The bot extracts existing MCQs from the PDF:
Question -> A/B/C/D -> ANSWER
No AI/API key required.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

from telegram import Update
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
from quizbot.shared.utils.async_files import remove_file, write_temp_file
from ..telegram_utils import safe_send_message
from .ai_quiz import _launch_ai_quiz

logger = logging.getLogger(__name__)


# ---------------------------------------------------------
# PDF TEXT EXTRACTION
# ---------------------------------------------------------

async def _extract_pdf_text(pdf_path: str) -> str:
    """Extract text from the complete PDF using PyMuPDF."""

    def _extract() -> str:
        import fitz

        doc = fitz.open(pdf_path)
        try:
            pages = []

            for page in doc:
                text = page.get_text("text")
                if text:
                    pages.append(text)

            return "\n".join(pages)
        finally:
            doc.close()

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _extract)


# ---------------------------------------------------------
# CLEAN PDF TEXT
# ---------------------------------------------------------

def _clean_pdf_text(text: str) -> str:
    """Remove common PSCGram/PDF header and footer noise."""

    # Remove Telegram handles / branding
    text = re.sub(r"@psc_gram", "", text, flags=re.I)
    text = re.sub(r"@pscgram", "", text, flags=re.I)

    # Remove common page/header noise
    text = re.sub(
        r"^\s*PSCGRAM\s*(?:\||·|-)?\s*\d*\s*$",
        "",
        text,
        flags=re.I | re.M,
    )

    # Remove source-page metadata
    text = re.sub(
        r"^\s*.*?Source PDF page\s*\d+.*$",
        "",
        text,
        flags=re.I | re.M,
    )

    # Remove empty trailing spaces
    lines = []

    for line in text.splitlines():
        line = line.strip()

        if not line:
            lines.append("")
            continue

        lines.append(line)

    return "\n".join(lines)


# ---------------------------------------------------------
# MCQ PARSER
# ---------------------------------------------------------

def _parse_mcqs(text: str, max_questions: int = 300) -> list[dict]:
    """
    Parse MCQs having this general structure:

    1. Question text

    A) Option one
    B) Option two
    C) Option three
    D) Option four

    ANSWER B) Option two

    Also supports:
    1) Question
    A) ...
    """

    text = _clean_pdf_text(text)

    # Normalize common PDF spacing
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Normalize tabs
    text = text.replace("\t", " ")

    # Collapse excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Find question blocks.
    #
    # Example:
    # 1. Question...
    # 2. Question...
    #
    question_matches = list(
        re.finditer(
            r"(?m)^\s*(\d{1,4})\s*[\.\)]\s+",
            text,
        )
    )

    questions: list[dict] = []

    for i, match in enumerate(question_matches):

        if len(questions) >= max_questions:
            break

        start = match.start()
        end = (
            question_matches[i + 1].start()
            if i + 1 < len(question_matches)
            else len(text)
        )

        block = text[start:end].strip()

        # Ignore very short blocks
        if len(block) < 20:
            continue

        parsed = _parse_single_mcq(block)

        if parsed:
            questions.append(parsed)

    return questions


def _parse_single_mcq(block: str) -> dict | None:
    """Parse one numbered MCQ."""

    # Find ANSWER line.
    answer_match = re.search(
        r"(?im)^\s*ANSWER\s*[:\-]?\s*([A-D])\s*[\)\.\:]?",
        block,
    )

    # Some PDFs place ANSWER in the middle of a line.
    if not answer_match:
        answer_match = re.search(
            r"(?i)\bANSWER\s*[:\-]?\s*([A-D])\s*[\)\.\:]?",
            block,
        )

    if not answer_match:
        return None

    answer_letter = answer_match.group(1).upper()

    # Everything before ANSWER is the question + options.
    content = block[: answer_match.start()].strip()

    # Remove question number.
    content = re.sub(
        r"^\s*\d{1,4}\s*[\.\)]\s*",
        "",
        content,
        count=1,
    ).strip()

    # Find A/B/C/D markers.
    option_matches = list(
        re.finditer(
            r"(?<![A-Za-z0-9])([A-D])\s*[\)\.]\s*",
            content,
        )
    )

    # Need at least 2 options, preferably 4.
    if len(option_matches) < 4:
        return None

    # Only use the first A/B/C/D sequence.
    option_matches = option_matches[:4]

    # Ensure labels are A B C D.
    labels = [m.group(1).upper() for m in option_matches]

    if labels != ["A", "B", "C", "D"]:
        return None

    question_text = content[: option_matches[0].start()].strip()

    if not question_text:
        return None

    options = []

    for j, opt_match in enumerate(option_matches):

        opt_start = opt_match.end()

        if j + 1 < len(option_matches):
            opt_end = option_matches[j + 1].start()
        else:
            opt_end = len(content)

        option_text = content[opt_start:opt_end].strip()

        # Clean line breaks inside options.
        option_text = re.sub(r"\s*\n\s*", " ", option_text)
        option_text = re.sub(r"\s{2,}", " ", option_text).strip()

        if not option_text:
            return None

        options.append(option_text)

    # Remove obvious page/header noise from question.
    question_text = re.sub(
        r"\s+",
        " ",
        question_text,
    ).strip()

    if len(question_text) < 3:
        return None

    correct_index = ord(answer_letter) - ord("A")

    if correct_index < 0 or correct_index > 3:
        return None

    return {
        "question": question_text,
        "options": options,
        "correct_option_id": correct_index,
        "explanation": None,
    }


# ---------------------------------------------------------
# DIRECT PDF HANDLER
# ---------------------------------------------------------

async def direct_pdf_handler(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Automatically process a PDF sent directly to the bot.

    No /pdfquiz command required.
    No AI required.
    """

    message = update.message

    if not message or not message.document:
        return

    document = message.document
    filename = document.file_name or ""

    # Only PDF
    if not filename.lower().endswith(".pdf"):
        return

    user_id = message.from_user.id
    chat_id = message.chat_id

    pdf_path = ""

    try:

        status = await message.reply_text(
            "📥 <b>PDF received</b>\n\n"
            "⏳ Downloading PDF...",
            parse_mode=ParseMode.HTML,
        )

        # Download
        telegram_file = await ctx.bot.get_file(document.file_id)

        file_bytes = await telegram_file.download_as_bytearray()

        pdf_path = os.path.join(
            config.TEMP_DIR,
            f"direct_pdf_{user_id}_{int(time.time())}.pdf",
        )

        await write_temp_file(
            bytes(file_bytes),
            pdf_path,
        )

        await status.edit_text(
            "📄 <b>PDF downloaded</b>\n\n"
            "🔎 Extracting questions...",
            parse_mode=ParseMode.HTML,
        )

        # Extract PDF text
        text = await _extract_pdf_text(pdf_path)

        if not text.strip():
            await status.edit_text(
                "❌ <b>No readable text found.</b>\n\n"
                "This PDF may be scanned/image-only.\n"
                "This version does not use OCR or AI.",
                parse_mode=ParseMode.HTML,
            )
            return

        # Parse MCQs
        questions = _parse_mcqs(
            text,
            max_questions=300,
        )

        if not questions:
            await status.edit_text(
                "❌ <b>No MCQs detected.</b>\n\n"
                "The PDF must contain questions with:\n"
                "A) ...\n"
                "B) ...\n"
                "C) ...\n"
                "D) ...\n"
                "ANSWER A/B/C/D",
                parse_mode=ParseMode.HTML,
            )
            return

        # Limit to Telegram-safe quiz size
        questions = questions[:300]

        await status.edit_text(
            f"✅ <b>{len(questions)} questions detected!</b>\n\n"
            "🚀 Starting quiz...",
            parse_mode=ParseMode.HTML,
        )

        # Launch using existing quiz engine
        await _launch_ai_quiz(
            user_id,
            ctx,
            questions,
            "PDF Quiz",
            20,          # timer
            0,           # negative marking
            1,           # correct mark
            False,       # shuffle questions
            False,       # shuffle options
            shuffle_o_count=0,
            chat_id=chat_id,
            chat_type=str(message.chat.type),
            update=update,
            show_explanation=False,
        )

    except Exception as e:

        logger.exception(
            "Direct PDF processing failed"
        )

        try:
            await message.reply_text(
                f"❌ <b>PDF processing error</b>\n\n"
                f"<code>{str(e)[:500]}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    finally:

        if pdf_path:
            try:
                await remove_file(pdf_path)
            except Exception:
                pass


# ---------------------------------------------------------
# OLD /pdfquiz COMMAND
# ---------------------------------------------------------

async def pdfquiz_command(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    await safe_send_message(
        ctx,
        update.message.chat_id,
        "📄 <b>Automatic PDF Quiz</b>\n\n"
        "Just send the PDF directly to this bot.\n\n"
        "The bot will extract existing MCQs automatically.\n\n"
        "❌ No AI\n"
        "❌ No API key\n"
        "❌ No Telegram channel required.",
        parse_mode=ParseMode.HTML,
    )


def register(application: Application) -> None:

    # /pdfquiz information
    application.add_handler(
        CommandHandler(
            "pdfquiz",
            pdfquiz_command,
        )
    )

    # DIRECT PDF -> QUIZ
    application.add_handler(
        MessageHandler(
            filters.Document.PDF & filters.ChatType.PRIVATE,
            direct_pdf_handler,
        )
    )
