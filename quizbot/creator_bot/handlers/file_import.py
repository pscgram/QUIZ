"""
PSCGram QuizBot - File Import / PDF Parser

Supports:
- TXT
- JSON
- Text-based PDF
- Scanned/image PDF using OCR
- English OCR
- Malayalam OCR
- Automatic MCQ extraction
- No AI required
"""

import io
import json
import re
import logging
from typing import Any, Dict, List, Optional

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)


# ============================================================
# BASIC CLEANING
# ============================================================

def strip_source_noise(text: str) -> str:
    """Remove common PDF/source noise without changing MCQ content."""

    if not text:
        return ""

    # Normalize line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Remove null characters
    text = text.replace("\x00", "")

    # Remove repeated spaces
    text = re.sub(r"[ \t]+", " ", text)

    # Remove obvious page-number-only lines
    text = re.sub(
        r"(?im)^\s*(?:page\s*)?\d+\s*$",
        "",
        text,
    )

    # Remove common PSCGram branding/noise
    noise_patterns = [
        r"(?im)^\s*@psc_?gram\s*$",
        r"(?im)^\s*@pscgram\s*$",
        r"(?im)^\s*psc\s*gram\s*$",
        r"(?im)^\s*telegram\s*:\s*@?psc_?gram\s*$",
        r"(?im)^\s*youtube\s*:\s*@?pscgram\s*$",
    ]

    for pattern in noise_patterns:
        text = re.sub(pattern, "", text)

    # Remove excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def filter_words(text: str) -> str:
    """Clean OCR/PDF text while preserving Malayalam and English."""

    if not text:
        return ""

    # Fix common OCR spacing problems
    text = text.replace("\u00a0", " ")

    # Normalize spaces
    text = re.sub(r"[ \t]+", " ", text)

    # Normalize blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# ============================================================
# OPTION / ANSWER HELPERS
# ============================================================

OPTION_RE = re.compile(
    r"^\s*(?:"
    r"\(?([A-Da-d])\)?"
    r"[\.\):\-]\s*"
    r"|"
    r"([A-Da-d])\s+"
    r")(.+?)\s*$"
)

ANSWER_RE = re.compile(
    r"^\s*"
    r"(?:"
    r"answer"
    r"|ans"
    r"|correct\s+answer"
    r"|correct\s+ans"
    r"|"
    r"ഉത്തരം"
    r"|ശരിയുത്തരം"
    r"|ശരിയായ\s+ഉത്തരം"
    r")"
    r"\s*[:\-\.]?\s*"
    r"[\(\[]?\s*([A-Da-d])\s*[\)\]]?"
    r"\s*$",
    re.IGNORECASE,
)


def _extract_option(line: str):
    """
    Extract A/B/C/D option.

    Supports:
        A. Option
        A) Option
        (A) Option
        A: Option
        A - Option
    """

    if not line:
        return None

    match = OPTION_RE.match(line)

    if not match:
        return None

    letter = match.group(1) or match.group(2)
    value = match.group(3)

    if not letter or not value:
        return None

    letter = letter.upper()
    value = value.strip()

    if letter not in {"A", "B", "C", "D"}:
        return None

    return letter, value


def _extract_answer(line: str):
    """Extract answer letter from English or Malayalam answer lines."""

    if not line:
        return None

    match = ANSWER_RE.match(line.strip())

    if not match:
        return None

    answer = match.group(1).upper()

    if answer in {"A", "B", "C", "D"}:
        return answer

    return None


# ============================================================
# QUESTION DETECTION
# ============================================================

QUESTION_RE = re.compile(
    r"^\s*"
    r"(?:"
    r"Q(?:uestion)?\s*"
    r")?"
    r"(\d{1,4})"
    r"\s*[\.\):\-]\s*"
    r"(.+?)"
    r"\s*$",
    re.IGNORECASE,
)


def _is_question_start(line: str) -> bool:
    """Return True if line looks like a numbered question."""

    if not line:
        return False

    line = line.strip()

    return bool(QUESTION_RE.match(line))


def _extract_question_number(line: str):
    """Extract question number and text."""

    match = QUESTION_RE.match(line.strip())

    if not match:
        return None

    number = match.group(1)
    question = match.group(2).strip()

    return number, question


# ============================================================
# PARSE ONE QUESTION BLOCK
# ============================================================

def parse_question_block(lines: List[str]) -> Optional[Dict[str, Any]]:
    """
    Parse one MCQ block.

    Expected structure:

    1. Question text
    A. Option one
    B. Option two
    C. Option three
    D. Option four
    ANSWER: A

    Malayalam is also supported.
    """

    if not lines:
        return None

    cleaned = []

    for line in lines:
        line = line.strip()

        if line:
            cleaned.append(line)

    if not cleaned:
        return None

    # Find question line
    question_index = None
    question_text = ""

    for i, line in enumerate(cleaned):
        result = _extract_question_number(line)

        if result:
            question_index = i
            _, question_text = result
            break

    if question_index is None:
        return None

    options = {}
    answer = None

    # Everything after question line
    for line in cleaned[question_index + 1:]:

        # Answer
        detected_answer = _extract_answer(line)

        if detected_answer:
            answer = detected_answer
            continue

        # Option
        option = _extract_option(line)

        if option:
            letter, value = option
            options[letter] = value
            continue

        # Sometimes the question continues over multiple lines
        if len(options) == 0:
            if question_text:
                question_text += " " + line

    # Must have four options
    if not question_text:
        return None

    if not all(letter in options for letter in ["A", "B", "C", "D"]):
        return None

    # If answer is missing, try to find answer from text
    if answer not in {"A", "B", "C", "D"}:
        return None

    return {
        "question": question_text.strip(),
        "options": [
            options["A"],
            options["B"],
            options["C"],
            options["D"],
        ],
        "answer": answer,
    }


# ============================================================
# TEXT MCQ PARSER
# ============================================================

def _parse_text_mcqs(text: str, max_questions: int = 300) -> List[Dict[str, Any]]:
    """
    Parse MCQs from extracted PDF/OCR text.
    """

    text = strip_source_noise(text)
    text = filter_words(text)

    if not text:
        return []

    lines = [line.strip() for line in text.splitlines()]

    blocks = []
    current_block = []

    for line in lines:

        if not line:
            continue

        # New question
        if _is_question_start(line):

            if current_block:
                blocks.append(current_block)

            current_block = [line]

        else:

            if current_block:
                current_block.append(line)

    if current_block:
        blocks.append(current_block)

    questions = []

    for block in blocks:

        if len(questions) >= max_questions:
            break

        try:
            question = parse_question_block(block)

            if question:
                questions.append(question)

        except Exception:
            logger.exception("Error parsing MCQ block")

    return questions


# ============================================================
# PDF TEXT EXTRACTION
# ============================================================

def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract normal embedded text from PDF."""

    output = []

    document = None

    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")

        for page in document:
            try:
                text = page.get_text("text")

                if text:
                    output.append(text)

            except Exception:
                logger.exception("Failed extracting text from PDF page")

    finally:
        if document is not None:
            document.close()

    return "\n".join(output)


# ============================================================
# OCR
# ============================================================

def _ocr_pdf(
    pdf_bytes: bytes,
    max_pages: int = 100,
) -> str:
    """
    OCR scanned PDF pages.

    Languages:
        eng = English
        mal = Malayalam

    Requires:
        tesseract-ocr
        tesseract-ocr-eng
        tesseract-ocr-mal
        pytesseract
        Pillow
    """

    try:
        import pytesseract
        from PIL import Image

    except ImportError:
        logger.exception(
            "OCR dependencies are missing. "
            "Install pytesseract and Pillow."
        )
        return ""

    output = []

    document = None

    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")

        page_count = min(len(document), max_pages)

        logger.info(
            "Starting OCR: %s pages, languages=eng+mal",
            page_count,
        )

        for page_number in range(page_count):

            try:
                page = document[page_number]

                # Render page at higher resolution for better OCR
                matrix = fitz.Matrix(2.5, 2.5)

                pix = page.get_pixmap(
                    matrix=matrix,
                    alpha=False,
                )

                image = Image.frombytes(
                    "RGB",
                    [pix.width, pix.height],
                    pix.samples,
                )

                text = pytesseract.image_to_string(
                    image,
                    lang="eng+mal",
                    config="--psm 6",
                )

                if text:
                    output.append(text)

                logger.info(
                    "OCR completed page %s/%s",
                    page_number + 1,
                    page_count,
                )

            except Exception:
                logger.exception(
                    "OCR failed on page %s",
                    page_number + 1,
                )

    except Exception:
        logger.exception("Failed opening PDF for OCR")

    finally:
        if document is not None:
            document.close()

    return "\n".join(output)


# ============================================================
# PDF MCQ PROCESSING
# ============================================================

def process_pdf_mcqs(
    content: bytes,
    max_questions: int = 300,
) -> List[Dict[str, Any]]:
    """
    Convert PDF bytes into MCQs.

    Processing order:

    1. Extract normal PDF text.
    2. Try MCQ parser.
    3. If insufficient MCQs are found,
       automatically run English + Malayalam OCR.
    4. Parse OCR result.
    """

    if not content:
        return []

    # --------------------------------------------------------
    # STEP 1: Normal PDF text extraction
    # --------------------------------------------------------

    try:
        normal_text = _extract_pdf_text(content)

    except Exception:
        logger.exception("Normal PDF extraction failed")
        normal_text = ""

    questions = _parse_text_mcqs(
        normal_text,
        max_questions=max_questions,
    )

    logger.info(
        "Normal PDF extraction found %s MCQs",
        len(questions),
    )

    # If enough questions were found, don't waste time on OCR
    if len(questions) >= 10:
        return questions[:max_questions]

    # --------------------------------------------------------
    # STEP 2: OCR fallback
    # --------------------------------------------------------

    logger.info(
        "Normal extraction found only %s MCQs. "
        "Starting English + Malayalam OCR.",
        len(questions),
    )

    try:
        ocr_text = _ocr_pdf(
            content,
            max_pages=100,
        )

    except Exception:
        logger.exception("OCR processing failed")
        ocr_text = ""

    if ocr_text:

        ocr_questions = _parse_text_mcqs(
            ocr_text,
            max_questions=max_questions,
        )

        logger.info(
            "OCR extraction found %s MCQs",
            len(ocr_questions),
        )

        # Prefer OCR result if it found more questions
        if len(ocr_questions) > len(questions):
            questions = ocr_questions

    return questions[:max_questions]


# ============================================================
# JSON SUPPORT
# ============================================================

def _normalise_json_question(item: Dict[str, Any]):
    """Convert common JSON MCQ structures into bot format."""

    if not isinstance(item, dict):
        return None

    question = (
        item.get("question")
        or item.get("Question")
        or item.get("text")
        or item.get("q")
    )

    options = (
        item.get("options")
        or item.get("Options")
        or item.get("choices")
        or item.get("Choices")
    )

    answer = (
        item.get("answer")
        or item.get("Answer")
        or item.get("correct")
        or item.get("correct_answer")
    )

    if not question or not options or answer is None:
        return None

    # Dictionary options
    if isinstance(options, dict):

        normalised_options = []

        for letter in ["A", "B", "C", "D"]:

            value = (
                options.get(letter)
                or options.get(letter.lower())
            )

            if value is None:
                return None

            normalised_options.append(str(value))

        options = normalised_options

    # List options
    elif isinstance(options, list):

        if len(options) < 4:
            return None

        options = [
            str(options[0]),
            str(options[1]),
            str(options[2]),
            str(options[3]),
        ]

    else:
        return None

    # Answer can be letter or index
    if isinstance(answer, int):

        if 0 <= answer <= 3:
            answer = "ABCD"[answer]

        elif 1 <= answer <= 4:
            answer = "ABCD"[answer - 1]

        else:
            return None

    answer = str(answer).strip().upper()

    if answer not in {"A", "B", "C", "D"}:
        return None

    return {
        "question": str(question).strip(),
        "options": options,
        "answer": answer,
    }


def _parse_json_mcqs(
    content: bytes,
    max_questions: int = 300,
) -> List[Dict[str, Any]]:
    """Parse JSON MCQs."""

    try:
        data = json.loads(
            content.decode("utf-8-sig")
        )

    except Exception:
        logger.exception("Invalid JSON file")
        return []

    # Support:
    # [...]
    # {"questions": [...]}
    # {"mcqs": [...]}
    if isinstance(data, dict):

        data = (
            data.get("questions")
            or data.get("mcqs")
            or data.get("data")
            or []
        )

    if not isinstance(data, list):
        return []

    questions = []

    for item in data:

        if len(questions) >= max_questions:
            break

        question = _normalise_json_question(item)

        if question:
            questions.append(question)

    return questions


# ============================================================
# FILE PROCESSOR
# ============================================================

async def process_uploaded_file(
    content: bytes,
    filename: str,
    max_questions: int = 300,
) -> List[Dict[str, Any]]:
    """
    Process uploaded TXT, JSON or PDF.

    PDF:
        Text extraction -> OCR fallback

    JSON:
        JSON MCQ parser

    TXT:
        Text MCQ parser
    """

    filename_lower = (filename or "").lower()

    # --------------------------------------------------------
    # PDF
    # --------------------------------------------------------

    if filename_lower.endswith(".pdf"):

        return process_pdf_mcqs(
            content,
            max_questions=max_questions,
        )

    # --------------------------------------------------------
    # JSON
    # --------------------------------------------------------

    if filename_lower.endswith(".json"):

        return _parse_json_mcqs(
            content,
            max_questions=max_questions,
        )

    # --------------------------------------------------------
    # TXT
    # --------------------------------------------------------

    if filename_lower.endswith(".txt"):

        try:
            text = content.decode(
                "utf-8-sig",
                errors="ignore",
            )

        except Exception:
            return []

        return _parse_text_mcqs(
            text,
            max_questions=max_questions,
        )

    # Unsupported file
    return []


# ============================================================
# SYNCHRONOUS HELPER
# ============================================================

def process_uploaded_file_sync(
    content: bytes,
    filename: str,
    max_questions: int = 300,
) -> List[Dict[str, Any]]:
    """
    Synchronous version for places where async processing
    is not required.
    """

    filename_lower = (filename or "").lower()

    if filename_lower.endswith(".pdf"):

        return process_pdf_mcqs(
            content,
            max_questions=max_questions,
        )

    if filename_lower.endswith(".json"):

        return _parse_json_mcqs(
            content,
            max_questions=max_questions,
        )

    if filename_lower.endswith(".txt"):

        try:
            text = content.decode(
                "utf-8-sig",
                errors="ignore",
            )

        except Exception:
            return []

        return _parse_text_mcqs(
            text,
            max_questions=max_questions,
        )

    return []


# ============================================================
# TEST / DEBUG
# ============================================================

if __name__ == "__main__":

    print(
        "PSCGram file_import.py loaded successfully."
    )
