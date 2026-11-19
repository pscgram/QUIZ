"""
PSCGram QuizBot - File Import / PDF Parser

Supports:
- TXT
- JSON
- Text-based PDF
- Scanned/image PDF
- English OCR
- Malayalam OCR
- Malayalam option labels
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
    """Remove common PDF/source noise without damaging Malayalam."""

    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x00", "")
    text = text.replace("\u00a0", " ")

    # Remove obvious page-number-only lines
    text = re.sub(
        r"(?im)^\s*(?:page\s*)?\d+\s*$",
        "",
        text,
    )

    noise_patterns = [
        r"(?im)^\s*@psc_?gram\s*$",
        r"(?im)^\s*@pscgram\s*$",
        r"(?im)^\s*psc\s*gram\s*$",
        r"(?im)^\s*telegram\s*:\s*@?psc_?gram\s*$",
        r"(?im)^\s*youtube\s*:\s*@?pscgram\s*$",
    ]

    for pattern in noise_patterns:
        text = re.sub(pattern, "", text)

    # Preserve line breaks.
    # Only collapse repeated horizontal spaces.
    text = re.sub(r"[ \t]+", " ", text)

    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def filter_words(text: str) -> str:
    """
    Light cleaning only.

    IMPORTANT:
    Malayalam Unicode must not be stripped.
    """

    if not text:
        return ""

    text = text.replace("\u00a0", " ")

    # Normalize unusual whitespace but preserve line structure.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# ============================================================
# MALAYALAM / ENGLISH OPTION HELPERS
# ============================================================

# Malayalam MCQ papers commonly use:
#
# A / B / C / D
# എ / ബി / സി / ഡി
#
# Sometimes OCR produces:
# (A), A), A., A:
# (എ), എ), എ., എ:
#
MALAYALAM_OPTION_MAP = {
    "എ": "A",
    "ബി": "B",
    "സി": "C",
    "ഡി": "D",
}

OPTION_RE = re.compile(
    r"^\s*"
    r"(?:"
    r"\(?([A-Da-d])\)?"
    r"|"
    r"\(?([എബി‌സിഡ])\)?"
    r")"
    r"\s*[\.\):\-–—]\s*"
    r"(.+?)"
    r"\s*$"
)

# Also support:
# A Option
# B Option
# എ Option
OPTION_SPACE_RE = re.compile(
    r"^\s*"
    r"(?:"
    r"([A-Da-d])"
    r"|"
    r"([എബി‌സിഡ])"
    r")"
    r"\s+"
    r"(.+?)"
    r"\s*$"
)


def _normalise_option_letter(letter: str) -> Optional[str]:
    if not letter:
        return None

    letter = letter.strip().upper()

    if letter in {"A", "B", "C", "D"}:
        return letter

    # Malayalam characters
    for mal, eng in MALAYALAM_OPTION_MAP.items():
        if letter == mal.upper():
            return eng

    return None


def _extract_option(line: str):
    """
    Extract A/B/C/D option.

    Supports:

        A. Option
        A) Option
        (A) Option
        A: Option
        A - Option

        എ. Option
        എ) Option
        (എ) Option
        എ: Option
        എ - Option

        A Option
        എ Option
    """

    if not line:
        return None

    line = line.strip()

    match = OPTION_RE.match(line)

    if match:
        letter = match.group(1) or match.group(2)
        value = match.group(3)

        letter = _normalise_option_letter(letter)

        if letter and value:
            return letter, value.strip()

    # Space-separated fallback
    match = OPTION_SPACE_RE.match(line)

    if match:
        letter = match.group(1) or match.group(2)
        value = match.group(3)

        letter = _normalise_option_letter(letter)

        if letter and value:
            return letter, value.strip()

    return None


# ============================================================
# ANSWER HELPERS
# ============================================================

ANSWER_RE = re.compile(
    r"^\s*"
    r"(?:"
    r"answer"
    r"|ans"
    r"|correct\s+answer"
    r"|correct\s+ans"
    r"|correct"
    r"|ഉത്തരം"
    r"|ശരിയുത്തരം"
    r"|ശരിയായ\s+ഉത്തരം"
    r"|ശരി\s*ഉത്തരം"
    r")"
    r"\s*[:\-\.]?\s*"
    r"[\(\[]?\s*"
    r"([A-Da-d]|[എബി‌സിഡ])"
    r"\s*[\)\]]?"
    r"\s*$",
    re.IGNORECASE,
)


def _extract_answer(line: str):
    """Extract answer letter from English or Malayalam answer lines."""

    if not line:
        return None

    line = line.strip()

    match = ANSWER_RE.match(line)

    if not match:
        return None

    answer = _normalise_option_letter(match.group(1))

    if answer in {"A", "B", "C", "D"}:
        return answer

    return None


# ============================================================
# QUESTION DETECTION
# ============================================================

# Examples:
#
# 1. Question
# 1) Question
# 1: Question
# Q1. Question
# Q 1. Question
#
QUESTION_RE = re.compile(
    r"^\s*"
    r"(?:Q(?:uestion)?\s*)?"
    r"(\d{1,4})"
    r"\s*[\.\):\-–—]\s*"
    r"(.+?)"
    r"\s*$",
    re.IGNORECASE,
)


def _is_question_start(line: str) -> bool:
    if not line:
        return False

    line = line.strip()

    return bool(QUESTION_RE.match(line))


def _extract_question_number(line: str):
    match = QUESTION_RE.match(line.strip())

    if not match:
        return None

    number = match.group(1)
    question = match.group(2).strip()

    return number, question


# ============================================================
# OCR LINE REPAIR
# ============================================================

def _repair_ocr_lines(lines: List[str]) -> List[str]:
    """
    Repair common OCR formatting problems.

    Examples:

        1.
        Question text

    becomes:

        1. Question text

    Also preserves option lines and answer lines.
    """

    repaired = []

    i = 0

    while i < len(lines):

        line = lines[i].strip()

        if not line:
            i += 1
            continue

        # Question number on its own:
        # 1.
        # 2)
        # 10.
        standalone_q = re.match(
            r"^\s*(\d{1,4})\s*[\.\):\-–—]\s*$",
            line,
        )

        if standalone_q and i + 1 < len(lines):

            next_line = lines[i + 1].strip()

            if next_line:
                repaired.append(
                    f"{standalone_q.group(1)}. {next_line}"
                )
                i += 2
                continue

        repaired.append(line)

        i += 1

    return repaired


# ============================================================
# PARSE ONE QUESTION BLOCK
# ============================================================

def parse_question_block(
    lines: List[str],
) -> Optional[Dict[str, Any]]:
    """
    Parse one MCQ block.

    Example:

    1. Question text
    A. Option one
    B. Option two
    C. Option three
    D. Option four
    ANSWER: A

    Malayalam:

    1. ചോദ്യം
    എ. ഓപ്ഷൻ ഒന്ന്
    ബി. ഓപ്ഷൻ രണ്ട്
    സി. ഓപ്ഷൻ മൂന്ന്
    ഡി. ഓപ്ഷൻ നാല്
    ഉത്തരം: എ
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

    # Repair OCR formatting
    cleaned = _repair_ocr_lines(cleaned)

    # --------------------------------------------------------
    # Find question line
    # --------------------------------------------------------

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

    options: Dict[str, str] = {}
    answer = None

    question_continuation = []

    # --------------------------------------------------------
    # Parse everything after question line
    # --------------------------------------------------------

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

        # Before first option, treat as question continuation
        if len(options) == 0:
            question_continuation.append(line)

    if question_continuation:
        question_text = (
            question_text
            + " "
            + " ".join(question_continuation)
        )

    question_text = re.sub(
        r"\s+",
        " ",
        question_text,
    ).strip()

    # --------------------------------------------------------
    # Require four options
    # --------------------------------------------------------

    if not question_text:
        return None

    if not all(
        letter in options
        for letter in ["A", "B", "C", "D"]
    ):
        return None

    # --------------------------------------------------------
    # Require answer
    # --------------------------------------------------------

    if answer not in {"A", "B", "C", "D"}:
        return None

    return {
        "question": question_text,
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

def _parse_text_mcqs(
    text: str,
    max_questions: int = 300,
) -> List[Dict[str, Any]]:

    text = strip_source_noise(text)
    text = filter_words(text)

    if not text:
        return []

    lines = [
        line.strip()
        for line in text.splitlines()
    ]

    lines = _repair_ocr_lines(lines)

    blocks = []
    current_block = []

    for line in lines:

        if not line:
            continue

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
            logger.exception(
                "Error parsing MCQ block"
            )

    return questions


# ============================================================
# PDF TEXT EXTRACTION
# ============================================================

def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract normal embedded text from PDF."""

    output = []

    document = None

    try:

        document = fitz.open(
            stream=pdf_bytes,
            filetype="pdf",
        )

        for page in document:

            try:

                text = page.get_text("text")

                if text:
                    output.append(text)

            except Exception:
                logger.exception(
                    "Failed extracting PDF page text"
                )

    finally:

        if document is not None:
            document.close()

    return "\n".join(output)


# ============================================================
# IMAGE PREPROCESSING
# ============================================================

def _prepare_ocr_image(image):
    """
    Prepare scanned page for OCR.

    Uses grayscale + contrast enhancement.
    """

    try:

        from PIL import ImageEnhance, ImageFilter

        # Convert to grayscale
        image = image.convert("L")

        # Improve contrast
        image = ImageEnhance.Contrast(
            image
        ).enhance(1.8)

        # Slight sharpening
        image = image.filter(
            ImageFilter.SHARPEN
        )

        return image

    except Exception:

        logger.exception(
            "OCR image preprocessing failed"
        )

        return image


# ============================================================
# OCR ONE PAGE
# ============================================================

def _ocr_page(image) -> str:
    """
    OCR one page using multiple page segmentation modes.

    We try both modes because Malayalam PDFs can have
    very different layouts.
    """

    import pytesseract

    image = _prepare_ocr_image(image)

    results = []

    for psm in (6, 11):

        try:

            text = pytesseract.image_to_string(
                image,
                lang="eng+mal",
                config=f"--oem 3 --psm {psm}",
            )

            if text and text.strip():

                results.append(text)

        except Exception:

            logger.exception(
                "Tesseract OCR failed for PSM %s",
                psm,
            )

    if not results:
        return ""

    # Prefer the result containing more useful text.
    results.sort(
        key=lambda value: len(value.strip()),
        reverse=True,
    )

    return results[0]


# ============================================================
# OCR PDF
# ============================================================

def _ocr_pdf(
    pdf_bytes: bytes,
    max_pages: int = 150,
) -> str:
    """
    OCR scanned PDF.

    Languages:
        English + Malayalam
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

        document = fitz.open(
            stream=pdf_bytes,
            filetype="pdf",
        )

        page_count = min(
            len(document),
            max_pages,
        )

        logger.info(
            "Starting Malayalam + English OCR: "
            "%s pages",
            page_count,
        )

        for page_number in range(page_count):

            try:

                page = document[page_number]

                # Higher resolution than the old OCR.
                matrix = fitz.Matrix(
                    3.0,
                    3.0,
                )

                pix = page.get_pixmap(
                    matrix=matrix,
                    alpha=False,
                )

                image = Image.frombytes(
                    "RGB",
                    [
                        pix.width,
                        pix.height,
                    ],
                    pix.samples,
                )

                text = _ocr_page(image)

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

        logger.exception(
            "Failed opening PDF for OCR"
        )

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

    Processing:

    1. Normal embedded text extraction.
    2. Parse normal text.
    3. OCR PDF using English + Malayalam.
    4. Parse OCR text.
    5. Keep whichever extraction produces more valid MCQs.

    OCR is NOT limited to only PDFs with fewer than 10
    detected questions. This improves scanned Malayalam PDFs
    where normal extraction may find some but not all questions.
    """

    if not content:
        return []

    # --------------------------------------------------------
    # Normal PDF text
    # --------------------------------------------------------

    try:

        normal_text = _extract_pdf_text(
            content
        )

    except Exception:

        logger.exception(
            "Normal PDF extraction failed"
        )

        normal_text = ""

    normal_questions = _parse_text_mcqs(
        normal_text,
        max_questions=max_questions,
    )

    logger.info(
        "Normal PDF extraction found %s MCQs",
        len(normal_questions),
    )

    # --------------------------------------------------------
    # OCR
    # --------------------------------------------------------

    try:

        ocr_text = _ocr_pdf(
            content,
            max_pages=150,
        )

    except Exception:

        logger.exception(
            "OCR processing failed"
        )

        ocr_text = ""

    ocr_questions = []

    if ocr_text:

        ocr_questions = _parse_text_mcqs(
            ocr_text,
            max_questions=max_questions,
        )

        logger.info(
            "English + Malayalam OCR found %s MCQs",
            len(ocr_questions),
        )

    # --------------------------------------------------------
    # Choose better result
    # --------------------------------------------------------

    if len(ocr_questions) > len(normal_questions):

        return ocr_questions[:max_questions]

    return normal_questions[:max_questions]


# ============================================================
# JSON SUPPORT
# ============================================================

def _normalise_json_question(
    item: Dict[str, Any],
):

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

        for letter in [
            "A",
            "B",
            "C",
            "D",
        ]:

            value = (
                options.get(letter)
                or options.get(letter.lower())
            )

            # Malayalam labels
            if value is None:

                mal_letter = {
                    "A": "എ",
                    "B": "ബി",
                    "C": "സി",
                    "D": "ഡി",
                }[letter]

                value = options.get(
                    mal_letter
                )

            if value is None:
                return None

            normalised_options.append(
                str(value)
            )

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

    # Answer can be integer
    if isinstance(answer, int):

        if 0 <= answer <= 3:
            answer = "ABCD"[answer]

        elif 1 <= answer <= 4:
            answer = "ABCD"[answer - 1]

        else:
            return None

    answer = str(
        answer
    ).strip().upper()

    # Malayalam answer
    answer = _normalise_option_letter(
        answer
    )

    if answer not in {
        "A",
        "B",
        "C",
        "D",
    }:
        return None

    return {
        "question": str(
            question
        ).strip(),

        "options": options,

        "answer": answer,
    }


def _parse_json_mcqs(
    content: bytes,
    max_questions: int = 300,
) -> List[Dict[str, Any]]:

    try:

        data = json.loads(
            content.decode(
                "utf-8-sig"
            )
        )

    except Exception:

        logger.exception(
            "Invalid JSON file"
        )

        return []

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

        question = _normalise_json_question(
            item
        )

        if question:
            questions.append(
                question
            )

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
    Process TXT, JSON or PDF.
    """

    filename_lower = (
        filename or ""
    ).lower()

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

            text = content.decode(
                "utf-8",
                errors="ignore",
            )

        return _parse_text_mcqs(
            text,
            max_questions=max_questions,
        )

    return []


def process_uploaded_file_sync(
    content: bytes,
    filename: str,
    max_questions: int = 300,
) -> List[Dict[str, Any]]:
    """
    Synchronous wrapper.
    """

    filename_lower = (
        filename or ""
    ).lower()

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

        text = content.decode(
            "utf-8-sig",
            errors="ignore",
        )

        return _parse_text_mcqs(
            text,
            max_questions=max_questions,
        )

    return []
