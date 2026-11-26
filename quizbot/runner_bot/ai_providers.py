"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot

PDF extraction:
- High-resolution PDF page rendering (400 DPI).
- Original text and script preservation.
- Separate visual verification pass.
- Malayalam character preservation.
- Uncertain questions and answers are rejected.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
from typing import Any, Optional

from quizbot.database import AIKeyRepository, get_db
from quizbot.shared import config
from quizbot.shared.utils.http import request_json

from .state import last_working_ai

logger = logging.getLogger(__name__)

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

AIQUIZ_DIFFICULTY = {
    "moderate": "Moderate — standard competitive exam level",
    "hard": "Hard — advanced level, tricky options",
    "extreme": "Extreme Hard — UPSC Mains/IAS depth, statement-based",
}

AIQUIZ_EXAM = {
    "ssc": "SSC CGL/CHSL pattern — factual, direct, speed-based",
    "civil": "UPSC Civil Services — analytical, multi-statement, multi-correct possible",
    "oneway": "Common One Day Exam — mixed MCQ, standard pattern",
}

AIQUIZ_LANG = {
    "en": "English",
    "hi": "Hindi (Devanagari script)",
    "hinglish": "Hinglish (Hindi words in English script)",
    "bn": "Bengali (বাংলা script)",
    "te": "Telugu (తెలుగు script)",
    "mr": "Marathi (मराठी script)",
    "ta": "Tamil (தமிழ் script)",
    "gu": "Gujarati (ગુજરાતી script)",
    "kn": "Kannada (ಕನ್ನಡ script)",
    "pa": "Punjabi (ਪੰਜਾਬੀ script)",
    "ur": "Urdu (اردو script)",
    "or": "Odia/Oriya (ଓଡ଼ିଆ script)",
    "ml": "Malayalam (മലയാളം script)",
}

AIQUIZ_LANG_UI = {
    "en": "🇬🇧 English",
    "hi": "🇮🇳 Hindi",
    "hinglish": "🔀 Hinglish",
    "bn": "🟠 Bengali",
    "te": "🟣 Telugu",
    "mr": "🟤 Marathi",
    "ta": "🟡 Tamil",
    "gu": "🟢 Gujarati",
    "kn": "🔵 Kannada",
    "pa": "🟠 Punjabi",
    "ur": "🌙 Urdu",
    "or": "🔶 Odia",
    "ml": "🟢 Malayalam",
}

QUESTION_FORMAT = """[TASK]Generate {count} MCQ on: "{topic}"
[STYLE]{diff}|{exam}
[LANG]{lang}
[RULES]JSON array only. No prose. No markdown. Start with [ end with ]
Each item: {{"q":"<270ch","o":["A","B","C","D"],"c":[0],"e":"<150ch or null"}}
c = 0-based correct index array. Multi-correct allowed.
[OUTPUT]"""

QUESTION_FORMAT_BILINGUAL = """[TASK]Generate {count} MCQ on: "{topic}"
[STYLE]{diff}|{exam}
[LANG]Bilingual: {lang1} / {lang2}
[RULES]JSON array only. No prose. No markdown. Start with [ end with ]
Each item: {{"q":"<question in {lang1_short}> / <question in {lang2_short}>","o":["<A in {lang1_short}> / <A in {lang2_short}>","<B in {lang1_short}> / <B in {lang2_short}>","<C in {lang1_short}> / <C in {lang2_short}>","<D in {lang1_short}> / <D in {lang2_short}>"],"c":[0],"e":"<exp in {lang1_short}> / <exp in {lang2_short}> or null"}}
Use / as separator between the two languages in every text field.
Keep total q length <=270 chars, each option <=90 chars.
[OUTPUT]"""


# ---------------------------------------------------------------------
# AI PROVIDER KEY MANAGEMENT
# ---------------------------------------------------------------------

async def get_provider_keys(user_id: int, provider: str) -> list[dict]:
    repo = AIKeyRepository(get_db())
    return await repo.list_for_provider(user_id, provider)


async def get_provider_key_single(
    user_id: int, provider: str
) -> Optional[str]:
    keys = await get_provider_keys(user_id, provider)
    return keys[0]["api_key"] if keys else None


async def _mark_key(key_id: int, failed: bool) -> None:
    repo = AIKeyRepository(get_db())
    await repo.mark(key_id, failed)


# ---------------------------------------------------------------------
# AI PROVIDER REQUESTS
# ---------------------------------------------------------------------

async def _call_gemini(
    api_key: str, prompt: str, max_tokens: int = 4096
) -> str:
    payload = {
        "system_instruction": {
            "parts": [{
                "text": (
                    "You are an expert quiz question generator. "
                    "Output ONLY the formatted questions, nothing else."
                )
            }]
        },
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt}],
        }],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": 0.7,
        },
    }

    status, data = await request_json(
        "POST",
        config.GEMINI_URL,
        json_body=payload,
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
    )

    if status != 200:
        raise RuntimeError(f"Gemini {status}: {str(data)[:300]}")

    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise RuntimeError(
            f"Invalid Gemini response: {str(data)[:300]}"
        ) from exc


async def _call_groq(
    api_key: str, prompt: str, max_tokens: int = 4096
) -> str:
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an expert quiz question generator. "
                    "Output only the formatted questions."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }

    status, data = await request_json(
        "POST",
        config.GROQ_URL,
        json_body=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    if status != 200:
        raise RuntimeError(f"Groq {status}: {str(data)[:300]}")

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise RuntimeError(
            f"Invalid Groq response: {str(data)[:300]}"
        ) from exc


async def _call_openrouter(
    api_key: str, prompt: str, max_tokens: int = 4096
) -> str:
    payload = {
        "model": "openrouter/free",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Output ONLY a valid JSON array. "
                    "No prose, no markdown, no explanation."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }

    status, data = await request_json(
        "POST",
        OPENROUTER_CHAT_URL,
        json_body=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://t.me/",
            "X-Title": "Quiz Bot",
        },
    )

    if status == 200:
        try:
            content = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise RuntimeError(
                f"Invalid OpenRouter response: {str(data)[:300]}"
            ) from exc

        if content and content.startswith("["):
            return content

        raise RuntimeError(
            f"Non-JSON response from openrouter/free: {content[:200]}"
        )

    if status in (429, 503, 529):
        raise RuntimeError(
            f"OpenRouter free router rate-limited ({status}): "
            f"{str(data)[:300]}"
        )

    raise RuntimeError(f"OpenRouter {status}: {str(data)[:300]}")


async def _call_pollinations(
    prompt: str, max_tokens: int = 4096
) -> str:
    payload = {
        "model": "openai",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an expert quiz question generator. "
                    "Output ONLY the formatted questions, nothing else."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "max_tokens": max_tokens,
        "stream": False,
    }

    status, data = await request_json(
        "POST",
        config.POLLINATIONS_URL,
        json_body=payload,
    )

    if status != 200:
        raise RuntimeError(f"Pollinations {status}: {str(data)[:300]}")

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise RuntimeError(
            f"Invalid Pollinations response: {str(data)[:300]}"
        ) from exc


async def _try_call(
    provider: str, api_key: str, prompt: str, max_tokens: int
) -> str:
    if provider == "gemini":
        return await _call_gemini(api_key, prompt, max_tokens)

    if provider == "groq":
        return await _call_groq(api_key, prompt, max_tokens)

    if provider == "openrouter":
        return await _call_openrouter(api_key, prompt, max_tokens)

    raise ValueError(f"Unknown provider {provider}")


async def ai_generate(
    user_id: int, prompt: str, max_tokens: int = 4096
) -> str:
    """Try cached provider, user keys, shared OpenRouter keys, then Pollinations."""

    cached = last_working_ai.get(user_id)

    if cached:
        try:
            if cached["provider"] == "pollinations":
                result = await _call_pollinations(prompt, max_tokens)
            else:
                result = await _try_call(
                    cached["provider"],
                    cached["api_key"],
                    prompt,
                    max_tokens,
                )

            if cached.get("key_id"):
                await _mark_key(cached["key_id"], failed=False)

            return result

        except Exception:
            if cached.get("key_id"):
                await _mark_key(cached["key_id"], failed=True)

            last_working_ai.pop(user_id, None)

    for provider in ("gemini", "groq", "openrouter"):
        keys = await get_provider_keys(user_id, provider)

        for key in keys:
            try:
                result = await _try_call(
                    provider,
                    key["api_key"],
                    prompt,
                    max_tokens,
                )

                await _mark_key(key["id"], failed=False)

                last_working_ai[user_id] = {
                    "provider": provider,
                    "key_id": key["id"],
                    "api_key": key["api_key"],
                }

                return result

            except Exception:
                await _mark_key(key["id"], failed=True)

    for default_key in config.OPENROUTER_DEFAULT_KEYS:
        try:
            result = await _call_openrouter(
                default_key, prompt, max_tokens
            )

            last_working_ai[user_id] = {
                "provider": "openrouter",
                "key_id": None,
                "api_key": default_key,
            }

            return result

        except Exception:
            continue

    result = await _call_pollinations(prompt, max_tokens)

    last_working_ai[user_id] = {
        "provider": "pollinations",
        "key_id": None,
        "api_key": "",
    }

    return result


# ---------------------------------------------------------------------
# JSON PARSING
# ---------------------------------------------------------------------

def _extract_json_array(raw: str) -> Optional[str]:
    """Find a JSON array without greedily consuming unrelated trailing text."""

    cleaned = re.sub(r"```(?:json)?\s*", "", raw)
    cleaned = cleaned.replace("```", "").strip()

    start = cleaned.find("[")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False

    for pos in range(start, len(cleaned)):
        char = cleaned[pos]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1

            if depth == 0:
                return cleaned[start:pos + 1]

    return None


def parse_ai_questions(raw: str) -> list[dict]:
    """
    Parse AI JSON into quiz question dictionaries.

    Original question and option strings are preserved.
    Questions without confirmed correct answers are skipped.
    """

    questions: list[dict] = []
    json_text = _extract_json_array(raw)

    if json_text:
        try:
            data = json.loads(json_text)

            if isinstance(data, list):
                for obj in data:
                    if not isinstance(obj, dict):
                        continue

                    q = obj.get("q", "")
                    options_raw = obj.get("o", [])
                    correct_raw = obj.get("c", [])
                    explanation = obj.get("e")

                    if not isinstance(q, str):
                        continue

                    if not isinstance(options_raw, list):
                        continue

                    options = []
                    original_to_new = {}

                    for original_index, option in enumerate(options_raw):
                        if option is None:
                            continue

                        if not isinstance(option, str):
                            option = str(option)

                        if option == "":
                            continue

                        original_to_new[original_index] = len(options)
                        options.append(option)

                    if q == "" or len(options) < 2:
                        continue

                    if not isinstance(correct_raw, list):
                        correct_raw = [correct_raw]

                    valid_correct = []
                    invalid_correct = False

                    for index in correct_raw:
                        if type(index) is not int:
                            invalid_correct = True
                            break

                        if index not in original_to_new:
                            invalid_correct = True
                            break

                        mapped_index = original_to_new[index]

                        if mapped_index not in valid_correct:
                            valid_correct.append(mapped_index)

                    if invalid_correct or not valid_correct:
                        continue

                    correct_id = (
                        valid_correct[0]
                        if len(valid_correct) == 1
                        else valid_correct
                    )

                    if explanation is not None:
                        if not isinstance(explanation, str):
                            explanation = str(explanation)

                    questions.append({
                        "question": q,
                        "options": options,
                        "correct_option_id": correct_id,
                        "explanation": explanation,
                        "file_id": None,
                        "reply_text": None,
                    })

                if questions:
                    return questions

        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("AI JSON parsing failed: %s", exc)

    # Legacy fallback parser.
    blocks = re.split(r"\n{2,}", raw.strip())

    for block in blocks:
        block = block.strip()

        if not block:
            continue

        lines = [
            line.strip()
            for line in block.split("\n")
            if line.strip()
        ]

        if len(lines) < 3:
            continue

        question = lines[0]
        options = []
        correct_ids = []
        explanation = None

        for line in lines[1:]:
            if line.startswith("Ex:"):
                explanation = line[3:].strip()
                continue

            is_correct = "✅" in line

            option = re.sub(
                r"^[A-Da-d][.)]\s*",
                "",
                line.replace("✅", "").strip(),
            )

            if is_correct:
                correct_ids.append(len(options))

            if option:
                options.append(option)

        if not question or len(options) < 2 or not correct_ids:
            continue

        correct_id = (
            correct_ids[0]
            if len(correct_ids) == 1
            else correct_ids
        )

        questions.append({
            "question": question,
            "options": options,
            "correct_option_id": correct_id,
            "explanation": explanation,
            "file_id": None,
            "reply_text": None,
        })

    return questions


# ---------------------------------------------------------------------
# TOPIC-BASED QUIZ GENERATION
# ---------------------------------------------------------------------

async def generate_in_chunks(
    user_id: int,
    topic: str,
    total: int,
    lang: str,
    diff: str,
    exam: str,
    status_msg: Any,
    chunk_size: int = 25,
    bilingual_lang2: Optional[str] = None,
) -> list[dict]:

    all_questions: list[dict] = []
    consecutive_failures = 0
    chunk_number = 0

    lang1_name = AIQUIZ_LANG.get(lang, "English")
    lang2_name = (
        AIQUIZ_LANG.get(bilingual_lang2, "")
        if bilingual_lang2 else ""
    )

    def short_name(name: str) -> str:
        return name.split(" (")[0].split(" —")[0].strip()

    while len(all_questions) < total:
        needed = total - len(all_questions)
        chunk = min(chunk_size, needed)
        chunk_number += 1

        try:
            await status_msg.edit_text(
                f"🤖 <b>Generating...</b> "
                f"({len(all_questions)}/{total} ready)\n"
                f"📌 {topic} | Batch {chunk_number} — "
                f"fetching {chunk} questions"
                + (
                    f" | 🌐 {short_name(lang1_name)}/"
                    f"{short_name(lang2_name)}"
                    if bilingual_lang2 else ""
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

        if bilingual_lang2:
            prompt = QUESTION_FORMAT_BILINGUAL.format(
                count=chunk,
                topic=topic,
                diff=AIQUIZ_DIFFICULTY.get(diff, "Moderate"),
                exam=AIQUIZ_EXAM.get(
                    exam, "Common One Day Exam"
                ),
                lang1=lang1_name,
                lang2=lang2_name,
                lang1_short=short_name(lang1_name),
                lang2_short=short_name(lang2_name),
            )
        else:
            prompt = QUESTION_FORMAT.format(
                count=chunk,
                topic=topic,
                lang=lang1_name,
                diff=AIQUIZ_DIFFICULTY.get(diff, "Moderate"),
                exam=AIQUIZ_EXAM.get(
                    exam, "Common One Day Exam"
                ),
            )

        tokens_per_question = 350 if bilingual_lang2 else 220
        max_tokens = max(2048, chunk * tokens_per_question)

        try:
            raw = await ai_generate(
                user_id, prompt, max_tokens=max_tokens
            )

            parsed = parse_ai_questions(raw)

            if len(parsed) < chunk:
                retry_prompt = (
                    f"IMPORTANT: Return EXACTLY {chunk} items in the JSON array. "
                    f"You previously returned {len(parsed)}. "
                    "Generate the full requested count.\n\n"
                    + prompt
                )

                raw_retry = await ai_generate(
                    user_id,
                    retry_prompt,
                    max_tokens=max_tokens,
                )

                parsed_retry = parse_ai_questions(raw_retry)

                if len(parsed_retry) > len(parsed):
                    parsed = parsed_retry

            if parsed:
                all_questions.extend(parsed[:chunk])
                consecutive_failures = 0

            else:
                consecutive_failures += 1

                logger.error(
                    "Chunk %d empty after retry (%d/3)",
                    chunk_number,
                    consecutive_failures,
                )

                if consecutive_failures >= 3:
                    break

        except Exception as exc:
            consecutive_failures += 1

            logger.error(
                "Chunk %d exception: %s", chunk_number, exc
            )

            if consecutive_failures >= 3:
                break

        await asyncio.sleep(0.3)

    return all_questions[:total]


# ---------------------------------------------------------------------
# PDF PAGE PROCESSING
# ---------------------------------------------------------------------

def _decode_pdf(page_pdf_b64: str) -> bytes:
    """Decode a base64-encoded single-page PDF."""

    try:
        return base64.b64decode(page_pdf_b64, validate=True)
    except Exception as exc:
        raise ValueError("Invalid base64 PDF data") from exc


def _extract_pdf_page_text(page_pdf_b64: str) -> str:
    """
    Extract embedded text without changing its Unicode characters.
    """

    try:
        import fitz

        pdf_bytes = _decode_pdf(page_pdf_b64)

        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            if not doc.page_count:
                return ""

            return doc[0].get_text("text", sort=True)

    except Exception as exc:
        logger.warning("PDF text extraction failed: %s", exc)
        return ""


def _pdf_text_layer_is_usable(text: str) -> bool:
    """Basic quality check. A usable text layer is not proof of accuracy."""

    if not text or len(text.strip()) < 30:
        return False

    visible = [ch for ch in text if not ch.isspace()]

    if len(visible) < 20:
        return False

    bad_chars = sum(
        1 for ch in visible
        if ch == "\ufffd"
        or 0xE000 <= ord(ch) <= 0xF8FF
        or ord(ch) < 32
    )

    return bad_chars / len(visible) <= 0.02


def _render_pdf_page_image(
    page_pdf_b64: str,
    dpi: int = 400,
) -> tuple[str, str]:
    """
    Render the supplied one-page PDF at high resolution.

    Returns:
        (base64_image, mime_type)

    The rendered page is used as the visual source of truth.
    """

    try:
        import fitz

        pdf_bytes = _decode_pdf(page_pdf_b64)

        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            if not doc.page_count:
                raise ValueError("PDF contains no pages")

            page = doc[0]
            scale = dpi / 72.0

            pix = page.get_pixmap(
                matrix=fitz.Matrix(scale, scale),
                alpha=False,
                colorspace=fitz.csRGB,
            )

            image_bytes = pix.tobytes("png")

        return base64.b64encode(image_bytes).decode("ascii"), "image/png"

    except Exception as exc:
        logger.exception("PDF page rendering failed")
        raise RuntimeError(
            f"Could not render PDF page at {dpi} DPI: {exc}"
        ) from exc


def _pdf_question_comparison_key(value: str) -> str:
    """
    Temporary key for duplicate detection only.

    This does not modify the original question text.
    """

    return re.sub(r"\s+", " ", value).strip().casefold()


def _pdf_question_fingerprint(question: dict) -> str:
    stem = _pdf_question_comparison_key(
        str(question.get("question", ""))
    )

    options = question.get("options", [])

    if not isinstance(options, list):
        options = []

    option_keys = [
        _pdf_question_comparison_key(str(option))
        for option in options
    ]

    return stem + "||" + "||".join(option_keys)


def _filter_excluded_pdf_questions(
    questions: list[dict],
    exclude_questions: Optional[list[str]] = None,
) -> list[dict]:
    """Remove previously extracted questions and duplicates."""

    excluded_stems = {
        _pdf_question_comparison_key(str(item))
        for item in (exclude_questions or [])
        if str(item).strip()
    }

    seen_fingerprints: set[str] = set()
    filtered: list[dict] = []

    for question in questions:
        stem = str(question.get("question", ""))

        if _pdf_question_comparison_key(stem) in excluded_stems:
            continue

        fingerprint = _pdf_question_fingerprint(question)

        if not fingerprint or fingerprint in seen_fingerprints:
            continue

        seen_fingerprints.add(fingerprint)
        filtered.append(question)

    return filtered


# ---------------------------------------------------------------------
# GEMINI IMAGE REQUEST
# ---------------------------------------------------------------------

async def _gemini_image_request(
    api_key: str,
    image_b64: str,
    mime_type: str,
    prompt: str,
    max_tokens: int = 8192,
) -> str:
    """Send a rendered PDF page image and prompt to Gemini."""

    payload = {
        "system_instruction": {
            "parts": [{
                "text": (
                    "You are a precise document-reading system. "
                    "Read the supplied page image carefully. "
                    "Never invent, translate, rewrite or correct text. "
                    "Preserve the original script and characters."
                )
            }]
        },
        "contents": [{
            "role": "user",
            "parts": [
                {
                    "inline_data": {
                        "mime_type": mime_type,
                        "data": image_b64,
                    }
                },
                {"text": prompt},
            ],
        }],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": 0,
        },
    }

    status, data = await request_json(
        "POST",
        config.GEMINI_URL,
        json_body=payload,
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
    )

    if status != 200:
        raise RuntimeError(
            f"Gemini image request failed ({status}): {str(data)[:500]}"
        )

    try:
        parts = data["candidates"][0]["content"]["parts"]

        text_parts = [
            part["text"]
            for part in parts
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]

        if not text_parts:
            raise ValueError("Gemini returned no text")

        return "\n".join(text_parts).strip()

    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Invalid Gemini image response: {str(data)[:500]}"
        ) from exc


# ---------------------------------------------------------------------
# PDF EXTRACTION — FIRST PASS
# ---------------------------------------------------------------------

def _build_pdf_extraction_prompt(
    page_num: int,
    questions_hint: int,
    source_text: str,
    excluded_block: str,
) -> str:

    return f"""
TASK: TRANSCRIBE AND EXTRACT EXISTING MCQ QUESTIONS FROM THIS PAGE.

PDF PAGE: {page_num}
MAXIMUM ADDITIONAL QUESTIONS: {questions_hint}

The supplied page IMAGE is the primary source.
The PDF text below is only a secondary reference. It may contain
incorrect font mappings or OCR-like extraction errors.

----- BEGIN EMBEDDED PDF TEXT -----
{source_text}
----- END EMBEDDED PDF TEXT -----

----- QUESTIONS ALREADY EXTRACTED -----
{excluded_block}
----- END EXCLUDED QUESTIONS -----

STRICT TRANSCRIPTION RULES:

1. Extract only questions actually visible on this page.
2. Do not create new questions.
3. Do not use general knowledge to complete missing text.
4. Preserve the exact original question wording.
5. Preserve every option exactly as printed.
6. Preserve option order and labels.
7. Preserve original punctuation, numbers and symbols.
8. Do not translate or paraphrase.
9. Do not correct spelling or grammar.
10. Do not normalize Unicode.
11. Do not transliterate any language.
12. Do not silently replace similar-looking characters.
13. Do not invent missing options.
14. Do not invent explanations.
15. Do not repeat excluded questions.
16. Extract only additional questions, up to the requested limit.

MALAYALAM CHARACTER PRESERVATION:

If Malayalam appears on the page:

- Read the Malayalam directly from the page image.
- Preserve the exact Malayalam characters as printed.
- Do not convert Malayalam into English or Manglish.
- Do not replace a word with a synonym.
- Do not autocorrect Malayalam spelling.
- Do not remove or alter vowel signs.
- Do not alter chillus or conjunct characters.
- Do not change punctuation or numerals.
- Do not copy a corrupted character from the PDF text layer.
- If a Malayalam character cannot be read confidently, skip that
  question instead of guessing.

OTHER INDIAN SCRIPTS:

Apply the same exact-character preservation to Hindi, Bengali,
Tamil, Telugu, Kannada, Gujarati, Marathi, Punjabi, Urdu and Odia.

ANSWER RULES:

- The correct answer must be established by a printed answer key
  or an unambiguous answer indication on the page.
- Do not answer from your own knowledge.
- Do not guess an answer.
- Skip a question if its correct answer is not clearly confirmed.
- Use zero-based correct option indexes.
- For multiple-correct questions, include all confirmed indexes.
- Never return an empty correct-answer array.

EXTRACTION:

- Include the printed question number when visible.
- Do not add a question number that is not printed.
- If a question continues onto another page, extract only the
  visible portion. Do not invent the missing continuation.
- If fewer than {questions_hint} eligible questions exist, return
  only the questions that can be read and verified.
- Do not return excluded questions, including reworded duplicates.

OUTPUT:

Return valid JSON array only:

[
  {{
    "q": "Exact original question",
    "o": [
      "Exact option A",
      "Exact option B",
      "Exact option C",
      "Exact option D"
    ],
    "c": [0],
    "e": null
  }}
]

The q and o strings must preserve the original text.
The c array contains confirmed zero-based correct indexes.
The e field contains a printed explanation or null.

No markdown.
No code fences.
No commentary.
Return [] if no eligible questions can be extracted confidently.
"""


# ---------------------------------------------------------------------
# PDF EXTRACTION — SECOND VISUAL VERIFICATION PASS
# ---------------------------------------------------------------------

def _extract_verification_indexes(raw: str) -> set[int]:
    """
    Read the verified candidate indexes returned by the second pass.

    Expected format:
    {"verified": [0, 2]}
    """

    cleaned = re.sub(r"```(?:json)?\s*", "", raw)
    cleaned = cleaned.replace("```", "").strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start < 0 or end <= start:
        return set()

    try:
        data = json.loads(cleaned[start:end + 1])
    except (json.JSONDecodeError, TypeError):
        return set()

    if not isinstance(data, dict):
        return set()

    indexes = data.get("verified", [])

    if not isinstance(indexes, list):
        return set()

    result = set()

    for index in indexes:
        if type(index) is int and index >= 0:
            result.add(index)

    return result


async def _verify_pdf_questions(
    api_key: str,
    image_b64: str,
    mime_type: str,
    questions: list[dict],
    page_num: int,
) -> list[dict]:
    """
    Independently compare candidate questions against the page image.

    A question is retained only when the second pass confirms:
    - The question text matches the page.
    - Every option matches the page.
    - Option order is correct.
    - The correct answer is explicitly established on the page.
    """

    if not questions:
        return []

    candidates = []

    for index, question in enumerate(questions):
        candidates.append({
            "index": index,
            "q": question.get("question", ""),
            "o": question.get("options", []),
            "c": question.get("correct_option_id"),
        })

    candidates_json = json.dumps(
        candidates,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    prompt = f"""
SECOND-PASS VISUAL VERIFICATION.

You are checking candidate MCQs against the supplied original
PDF page image.

PAGE NUMBER: {page_num}

CANDIDATE QUESTIONS:
{candidates_json}

For each candidate, inspect the actual page image independently.

A candidate can be verified ONLY when all of the following are true:

1. The complete question text matches the page.
2. Every option matches the page.
3. No character, word, number or punctuation is changed.
4. The original option order is correct.
5. The correct answer is explicitly established on the page.
6. The supplied correct option index matches that evidence.
7. All relevant text is readable with confidence.

MALAYALAM AND INDIAN LANGUAGE CHECK:

- Inspect every character in the original script.
- Pay special attention to Malayalam vowel signs, chillus,
  conjuncts and visually similar characters.
- Do not approve a candidate if even one important character
  cannot be confirmed.
- Do not autocorrect or normalize any text.
- Do not assume the first extraction pass was correct.

ANSWER CHECK:

- Do not approve an answer based on general knowledge.
- The answer must be printed or unambiguously indicated on
  the supplied page.
- If an answer key is absent or unclear, do not approve it.

IMPORTANT:

Return only the indexes of candidates that pass EVERY check.
Do not rewrite the questions.
Do not return corrected text.
Do not guess.
If none pass, return an empty list.

OUTPUT JSON ONLY:

{{"verified": [0, 2]}}
"""


    try:
        raw = await _gemini_image_request(
            api_key=api_key,
            image_b64=image_b64,
            mime_type=mime_type,
            prompt=prompt,
            max_tokens=2048,
        )

        verified_indexes = _extract_verification_indexes(raw)

        verified_questions = [
            question
            for index, question in enumerate(questions)
            if index in verified_indexes
        ]

        logger.info(
            "PDF page %s verification: %d/%d passed",
            page_num,
            len(verified_questions),
            len(questions),
        )

        return verified_questions

    except Exception:
        logger.exception(
            "PDF page %s second-pass verification failed",
            page_num,
        )

        # Conservative behaviour: do not publish unverified questions.
        return []


# ---------------------------------------------------------------------
# MAIN PDF EXTRACTION FUNCTION
# ---------------------------------------------------------------------

async def gemini_page_questions(
    api_key: str,
    page_pdf_b64: str,
    page_num: int,
    questions_hint: int,
    diff: str,
    exam: str,
    lang: str,
    exclude_questions: Optional[list[str]] = None,
) -> list[dict]:
    """
    Extract and verify existing MCQs from a single-page PDF.

    Existing function signature is preserved for compatibility.

    Workflow:
    1. Extract embedded PDF text as secondary reference.
    2. Render the page at 400 DPI.
    3. Send the page image to Gemini.
    4. Extract existing questions without rewriting.
    5. Parse the candidate questions.
    6. Remove previously extracted questions.
    7. Run a second independent visual verification.
    8. Return only questions that pass verification.
    """

    if not api_key:
        raise ValueError("Gemini API key is missing")

    if questions_hint <= 0:
        return []

    # -------------------------------------------------------------
    # 1. Extract embedded text as a secondary reference.
    # -------------------------------------------------------------

    extracted_text = _extract_pdf_page_text(page_pdf_b64)

    text_layer_available = _pdf_text_layer_is_usable(
        extracted_text
    )

    if text_layer_available:
        source_text = extracted_text
    else:
        source_text = (
            "No reliable embedded PDF text layer is available. "
            "Read the original page image directly."
        )

    # -------------------------------------------------------------
    # 2. Prepare excluded questions.
    # -------------------------------------------------------------

    excluded: list[str] = []
    excluded_keys: set[str] = set()

    for item in exclude_questions or []:
        if not isinstance(item, str) or not item.strip():
            continue

        key = _pdf_question_comparison_key(item)

        if key not in excluded_keys:
            excluded_keys.add(key)
            excluded.append(item)

    if excluded:
        excluded_block = "\n".join(
            f"- {question}" for question in excluded
        )
    else:
        excluded_block = "None. First extraction attempt."

    # -------------------------------------------------------------
    # 3. Render the original page at 400 DPI.
    # -------------------------------------------------------------

    image_b64, mime_type = _render_pdf_page_image(
        page_pdf_b64,
        dpi=400,
    )

    logger.info(
        "PDF page %s rendered at 400 DPI; text_layer=%s",
        page_num,
        text_layer_available,
    )

    # -------------------------------------------------------------
    # 4. First pass: extract candidate questions.
    # -------------------------------------------------------------

    prompt = _build_pdf_extraction_prompt(
        page_num=page_num,
        questions_hint=questions_hint,
        source_text=source_text,
        excluded_block=excluded_block,
    )

    raw = await _gemini_image_request(
        api_key=api_key,
        image_b64=image_b64,
        mime_type=mime_type,
        prompt=prompt,
        max_tokens=max(4096, questions_hint * 600),
    )

    # -------------------------------------------------------------
    # 5. Parse candidate questions.
    # -------------------------------------------------------------

    parsed_questions = parse_ai_questions(raw)

    if not parsed_questions:
        logger.info(
            "PDF page %s: no eligible questions extracted",
            page_num,
        )
        return []

    # -------------------------------------------------------------
    # 6. Apply local duplicate filtering.
    # -------------------------------------------------------------

    candidates = _filter_excluded_pdf_questions(
        parsed_questions,
        exclude_questions,
    )

    if not candidates:
        return []

    # -------------------------------------------------------------
    # 7. Limit candidates to the requested number.
    # -------------------------------------------------------------

    candidates = candidates[:questions_hint]

    # -------------------------------------------------------------
    # 8. Second pass: visually verify each candidate.
    # -------------------------------------------------------------

    verified = await _verify_pdf_questions(
        api_key=api_key,
        image_b64=image_b64,
        mime_type=mime_type,
        questions=candidates,
        page_num=page_num,
    )

    # -------------------------------------------------------------
    # 9. Final duplicate filtering.
    # -------------------------------------------------------------

    final_questions = _filter_excluded_pdf_questions(
        verified,
        exclude_questions,
    )

    logger.info(
        (
            "PDF page %s completed: text_layer=%s, "
            "first_pass=%d, verified=%d, final=%d"
        ),
        page_num,
        text_layer_available,
        len(parsed_questions),
        len(verified),
        len(final_questions),
    )

    return final_questions
