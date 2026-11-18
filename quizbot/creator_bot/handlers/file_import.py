from __future__ import annotations

import io
import json
import re
from typing import Any, Dict, List, Optional

import fitz  # PyMuPDF


# ============================================================
# BASIC CLEANING
# ============================================================

def strip_source_noise(text: str) -> str:
    """
    Remove common PSCGram/PDF header and footer noise.
    """
    if not text:
        return ""

    lines = []

    for line in text.splitlines():
        line = line.strip()

        if not line:
            continue

        # PSCGram branding / Telegram handles
        if re.fullmatch(
            r"(?i)(@psc_gram|@pscgram|pscgram|psc\s*gram)",
            line,
        ):
            continue

        # Common page/header/footer patterns
        if re.match(r"(?i)^page\s+\d+(\s+of\s+\d+)?$", line):
            continue

        if re.match(r"(?i)^page\s*[:\-]?\s*\d+$", line):
            continue

        if re.match(r"(?i)^source\s+pdf\s+p\.?\s*\d+", line):
            continue

        if re.match(r"(?i)^pscgram\s*\|", line):
            continue

        lines.append(line)

    return "\n".join(lines)


def filter_words(text: str) -> str:
    """
    Basic cleanup while preserving question content.
    """
    if not text:
        return ""

    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")

    # Remove excessive spaces
    text = re.sub(r"[ \t]+", " ", text)

    # Remove excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# ============================================================
# TEXT / JSON IMPORT
# ============================================================

def parse_question_block(block: str) -> Optional[Dict[str, Any]]:
    """
    Parse a single MCQ block.

    Supported examples:

    1. What is the capital of Kerala?
    A) Thiruvananthapuram
    B) Kochi
    C) Kollam
    D) Kannur
    ANSWER: A

    Also supports:
    Q1. ...
    1) ...
    1. ...
    """

    if not block:
        return None

    block = filter_words(strip_source_noise(block))

    if not block:
        return None

    # --------------------------------------------------------
    # Question number
    # --------------------------------------------------------

    q_match = re.search(
        r"(?im)^\s*(?:Q(?:uestion)?\s*)?(\d{1,4})[\.\):\-]\s*(.+)",
        block,
    )

    if not q_match:
        return None

    question_number = q_match.group(1)
    first_question_line = q_match.group(2).strip()

    # --------------------------------------------------------
    # Find answer
    # --------------------------------------------------------

    answer_match = re.search(
        r"(?im)^\s*(?:ANSWER|ANS|CORRECT\s*ANSWER|KEY)\s*"
        r"[:\-]?\s*[\(\[]?\s*([A-D])\s*[\)\]]?",
        block,
    )

    if not answer_match:
        # Sometimes answer appears as:
        # ANSWER A
        answer_match = re.search(
            r"(?im)\b(?:ANSWER|ANS|KEY)\s*[:\-]?\s*([A-D])\b",
            block,
        )

    if not answer_match:
        return None

    correct_letter = answer_match.group(1).upper()

    # --------------------------------------------------------
    # Remove answer portion
    # --------------------------------------------------------

    question_part = block[:answer_match.start()].strip()

    # Remove question number from first line
    question_part = re.sub(
        r"^\s*(?:Q(?:uestion)?\s*)?\d{1,4}[\.\):\-]\s*",
        "",
        question_part,
        count=1,
        flags=re.I,
    ).strip()

    # --------------------------------------------------------
    # Extract options
    # --------------------------------------------------------

    option_pattern = re.compile(
        r"(?im)^\s*([A-D])[\.\):\-]\s*(.+?)(?=\n\s*[A-D][\.\):\-]\s*|\Z)",
        re.S,
    )

    option_matches = list(option_pattern.finditer(question_part))

    options: List[str] = []

    if option_matches:
        # Text before A) is the actual question
        question_text = question_part[: option_matches[0].start()].strip()

        for match in option_matches:
            option_text = match.group(2).strip()
            option_text = re.sub(r"\s+", " ", option_text)

            if option_text:
                options.append(option_text)

    else:
        # Alternative parser for inline options:
        # A) xxx B) yyy C) zzz D) aaa
        inline_pattern = re.compile(
            r"(?i)(?:^|\s)([A-D])[\.\):\-]\s*"
        )

        inline_matches = list(inline_pattern.finditer(question_part))

        if len(inline_matches) >= 2:
            question_text = question_part[: inline_matches[0].start()].strip()

            for i, match in enumerate(inline_matches):
                start = match.end()

                if i + 1 < len(inline_matches):
                    end = inline_matches[i + 1].start()
                else:
                    end = len(question_part)

                option_text = question_part[start:end].strip()
                option_text = re.sub(r"\s+", " ", option_text)

                if option_text:
                    options.append(option_text)
        else:
            return None

    # Need at least 2 choices
    if len(options) < 2:
        return None

    # Limit to four choices
    options = options[:4]

    # Correct answer index
    correct_index = ord(correct_letter) - ord("A")

    if correct_index < 0 or correct_index >= len(options):
        return None

    question_text = re.sub(r"\s+", " ", question_text).strip()

    if not question_text:
        return None

    # --------------------------------------------------------
    # Build result
    # --------------------------------------------------------

    return {
        "question": question_text,
        "options": options,
        "correct_option_id": correct_index,
        "answer": correct_letter,
        "explanation": "",
        "reply_text": question_text,
        "file_id": None,
        "question_number": question_number,
    }


# ============================================================
# TEXT PARSER
# ============================================================

def _parse_text_mcqs(text: str, max_questions: int = 300) -> List[Dict[str, Any]]:
    """
    Parse MCQs from plain text.
    """

    text = filter_words(strip_source_noise(text))

    if not text:
        return []

    # --------------------------------------------------------
    # Find question starts.
    #
    # Supports:
    # 1.
    # 1)
    # Q1.
    # Q1)
    # Question 1.
    # --------------------------------------------------------

    question_start = re.compile(
        r"(?im)^\s*"
        r"(?:Q(?:uestion)?\s*)?"
        r"(\d{1,4})"
        r"[\.\):\-]\s*"
    )

    matches = list(question_start.finditer(text))

    if not matches:
        return []

    questions: List[Dict[str, Any]] = []

    for i, match in enumerate(matches):
        if len(questions) >= max_questions:
            break

        start = match.start()

        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            end = len(text)

        block = text[start:end].strip()

        parsed = parse_question_block(block)

        if parsed:
            questions.append(parsed)

    return questions


# ============================================================
# PDF PARSER
# ============================================================

def process_pdf_mcqs(
    content: bytes,
    max_questions: int = 300,
) -> List[Dict[str, Any]]:
    """
    Extract existing MCQs from a text-based PDF.

    IMPORTANT:
    This does NOT use AI.

    It expects the PDF to already contain:
      Question
      A) option
      B) option
      C) option
      D) option
      ANSWER: A

    Scanned/image-only PDFs will not work without OCR.
    """

    if not content:
        return []

    try:
        pdf = fitz.open(stream=content, filetype="pdf")
    except Exception:
        return []

    all_pages: List[str] = []

    try:
        for page in pdf:
            try:
                page_text = page.get_text("text")
            except Exception:
                page_text = ""

            if page_text:
                all_pages.append(page_text)
    finally:
        pdf.close()

    full_text = "\n".join(all_pages)

    if not full_text.strip():
        return []

    return _parse_text_mcqs(
        full_text,
        max_questions=max_questions,
    )


# ============================================================
# JSON PARSER
# ============================================================

def _normalise_json_question(item: Any) -> Optional[Dict[str, Any]]:
    """
    Convert common JSON MCQ formats into the bot's internal format.
    """

    if not isinstance(item, dict):
        return None

    question = (
        item.get("question")
        or item.get("text")
        or item.get("question_text")
    )

    options = (
        item.get("options")
        or item.get("choices")
        or item.get("answers")
    )

    if not question or not options:
        return None

    if isinstance(options, dict):
        # Example:
        # {"A": "...", "B": "...", "C": "...", "D": "..."}
        ordered = []

        for key in ("A", "B", "C", "D"):
            if key in options:
                ordered.append(str(options[key]))

        options = ordered

    if not isinstance(options, list):
        return None

    options = [str(x).strip() for x in options if str(x).strip()]

    if len(options) < 2:
        return None

    options = options[:4]

    answer = (
        item.get("answer")
        or item.get("correct_answer")
        or item.get("correct")
        or item.get("answer_index")
        or item.get("correct_option_id")
    )

    correct_index: Optional[int] = None

    if isinstance(answer, int):
        correct_index = answer

    elif isinstance(answer, str):
        answer_clean = answer.strip().upper()

        # A/B/C/D
        if answer_clean in ("A", "B", "C", "D"):
            correct_index = ord(answer_clean) - ord("A")
        else:
            # Try exact option match
            for idx, option in enumerate(options):
                if option.strip().lower() == answer.strip().lower():
                    correct_index = idx
                    break

    if correct_index is None:
        return None

    if correct_index < 0 or correct_index >= len(options):
        return None

    return {
        "question": str(question).strip(),
        "options": options,
        "correct_option_id": correct_index,
        "answer": chr(ord("A") + correct_index),
        "explanation": str(item.get("explanation", "") or ""),
        "reply_text": str(question).strip(),
        "file_id": item.get("file_id"),
    }


def _parse_json_mcqs(
    content: bytes,
    max_questions: int = 300,
) -> List[Dict[str, Any]]:
    try:
        raw = json.loads(content.decode("utf-8"))
    except Exception:
        return []

    if isinstance(raw, dict):
        # Common containers
        raw = (
            raw.get("questions")
            or raw.get("quiz")
            or raw.get("items")
            or []
        )

    if not isinstance(raw, list):
        return []

    questions: List[Dict[str, Any]] = []

    for item in raw:
        if len(questions) >= max_questions:
            break

        parsed = _normalise_json_question(item)

        if parsed:
            questions.append(parsed)

    return questions


# ============================================================
# MAIN FILE IMPORT FUNCTION
# ============================================================

async def process_uploaded_file(
    content: bytes,
    filename: str = "",
    mime_type: str = "",
    max_questions: int = 300,
) -> List[Dict[str, Any]]:
    """
    Main importer used by the creator bot.

    Supported:
      .txt
      .json
      .pdf

    PDF is parsed without AI.
    """

    filename_lower = (filename or "").lower().strip()
    mime_lower = (mime_type or "").lower().strip()

    # --------------------------------------------------------
    # PDF
    # --------------------------------------------------------

    if (
        filename_lower.endswith(".pdf")
        or mime_lower == "application/pdf"
    ):
        return process_pdf_mcqs(
            content,
            max_questions=max_questions,
        )

    # --------------------------------------------------------
    # JSON
    # --------------------------------------------------------

    if (
        filename_lower.endswith(".json")
        or mime_lower == "application/json"
        or mime_lower.endswith("+json")
    ):
        return _parse_json_mcqs(
            content,
            max_questions=max_questions,
        )

    # --------------------------------------------------------
    # TXT
    # --------------------------------------------------------

    if (
        filename_lower.endswith(".txt")
        or mime_lower.startswith("text/")
    ):
        try:
            text = content.decode("utf-8", errors="ignore")
        except Exception:
            return []

        return _parse_text_mcqs(
            text,
            max_questions=max_questions,
        )

    return []


# ============================================================
# SYNCHRONOUS HELPER
# ============================================================

def process_uploaded_file_sync(
    content: bytes,
    filename: str = "",
    mime_type: str = "",
    max_questions: int = 300,
) -> List[Dict[str, Any]]:
    """
    Synchronous helper for handlers that don't need await.
    """

    filename_lower = (filename or "").lower().strip()
    mime_lower = (mime_type or "").lower().strip()

    if (
        filename_lower.endswith(".pdf")
        or mime_lower == "application/pdf"
    ):
        return process_pdf_mcqs(
            content,
            max_questions=max_questions,
        )

    if (
        filename_lower.endswith(".json")
        or mime_lower == "application/json"
        or mime_lower.endswith("+json")
    ):
        return _parse_json_mcqs(
            content,
            max_questions=max_questions,
        )

    if (
        filename_lower.endswith(".txt")
        or mime_lower.startswith("text/")
    ):
        try:
            text = content.decode("utf-8", errors="ignore")
        except Exception:
            return []

        return _parse_text_mcqs(
            text,
            max_questions=max_questions,
        )

    return []
