
"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
PDF extraction updated to prioritize verbatim source text.
"""

from __future__ import annotations

import asyncio
import base64
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
Keep total q length ≤270 chars, each option ≤90 chars.
[OUTPUT]"""


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
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
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

    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


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

    return data["choices"][0]["message"]["content"].strip()


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
        content = data["choices"][0]["message"]["content"].strip()
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
        "POST", config.POLLINATIONS_URL, json_body=payload
    )

    if status != 200:
        raise RuntimeError(f"Pollinations {status}: {str(data)[:300]}")

    return data["choices"][0]["message"]["content"].strip()


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
                    provider, key["api_key"], prompt, max_tokens
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
# JSON parsing
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

    Do not normalize Unicode or rewrite extracted text.
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
                        q = str(q)

                    if not isinstance(options_raw, list):
                        continue

                    options = []
                    original_to_new = {}

                    for original_index, option in enumerate(options_raw):
                        if option is None:
                            continue

                        option_text = (
                            option if isinstance(option, str) else str(option)
                        )

                        if option_text == "":
                            continue

                        original_to_new[original_index] = len(options)
                        options.append(option_text)

                    if q == "" or len(options) < 2:
                        continue

                    if not isinstance(correct_raw, list):
                        correct_raw = [correct_raw]

                    valid_correct = []

                    for index in correct_raw:
                        if isinstance(index, bool):
                            continue
                        if isinstance(index, (int, float)):
                            old_index = int(index)
                            if old_index in original_to_new:
                                valid_correct.append(
                                    original_to_new[old_index]
                                )

                    if not valid_correct:
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

        lines = [line.strip() for line in block.split("\n") if line.strip()]
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
# Topic-based quiz generation
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
                    logger.error(
                        "Three consecutive empty chunks; stopping."
                    )
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
# PDF page question extraction — VERBATIM TEXT PRIORITY
# ---------------------------------------------------------------------

def _extract_pdf_page_text(page_pdf_b64: str) -> str:
    """
    Extract embedded text from a single-page PDF.

    No Unicode normalization or spelling correction is performed.
    """

    try:
        import fitz

        pdf_bytes = base64.b64decode(page_pdf_b64)

        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            if not doc.page_count:
                return ""

            return doc[0].get_text("text", sort=True)

    except Exception as exc:
        logger.warning(
            "PDF text-layer extraction failed: %s", exc
        )
        return ""


def _pdf_text_layer_is_usable(text: str) -> bool:
    """
    Basic quality check for embedded PDF text.

    This cannot prove that a font's Unicode mapping is correct.
    The original page must still be checked visually.
    """

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

    if bad_chars / len(visible) > 0.02:
        return False

    return True


def _pdf_question_comparison_key(value: str) -> str:
    """
    Create a temporary comparison key.

    This is used only for duplicate detection.
    Original question text is never modified.
    """

    return re.sub(r"\s+", " ", value).strip().casefold()


def _pdf_question_fingerprint(question: dict) -> str:
    """Build a comparison fingerprint from question and options."""

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
    """
    Remove questions already extracted during an earlier attempt.

    Only comparison keys are normalized. Stored question and option
    text remains exactly as returned by the parser.
    """

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
    Extract existing MCQs from a PDF page.

    Strategy:
    1. Extract embedded PDF text without modifying it.
    2. Send the original PDF page to Gemini.
    3. Ask Gemini to compare the text layer against the page.
    4. Preserve original wording and Unicode characters.
    5. Never guess unreadable text.
    6. Exclude questions already extracted in previous attempts.
    """

    extracted_text = _extract_pdf_page_text(page_pdf_b64)

    text_layer_available = _pdf_text_layer_is_usable(
        extracted_text
    )

    if text_layer_available:
        source_text = extracted_text
    else:
        source_text = (
            "No reliable embedded text layer was extracted. "
            "Read the original PDF page carefully."
        )

    # Prepare exclusion list for this retry.
    # The original question strings are not changed.
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
        excluded_block = "None. This is the first extraction attempt."

    prompt = f"""
You are a DOCUMENT TRANSCRIPTION AND MCQ EXTRACTION SYSTEM.

Extract EXISTING multiple-choice questions from the supplied
original PDF page.

PDF PAGE: {page_num}
MAXIMUM ADDITIONAL QUESTIONS: {questions_hint}

QUESTIONS ALREADY EXTRACTED FROM THIS SAME PAGE:

----- BEGIN EXCLUDED QUESTIONS -----

{excluded_block}

----- END EXCLUDED QUESTIONS -----


IMPORTANT DUPLICATE PREVENTION RULE:

- Do NOT return any question listed in the excluded section.
- Do NOT return the same question with slightly different wording.
- Do NOT return an excluded question with only punctuation changes.
- Do NOT return an excluded question with different option formatting.
- Extract only additional questions that are visibly printed on this page.
- The excluded list is provided only to prevent duplicate extraction.
- Do not count excluded questions toward the requested additional count.

IMPORTANT SOURCE TEXT:

The following text was extracted directly from the PDF's embedded
text layer. It may be accurate, or it may contain font-encoding
errors. Compare it carefully with the original PDF page.

----- BEGIN PDF TEXT LAYER -----

{source_text}

----- END PDF TEXT LAYER -----


STRICT LANGUAGE AND CHARACTER RULES:

1. Detect the original language and script from the PDF page.
2. Preserve the original language.
3. Never translate.
4. Never paraphrase.
5. Never simplify.
6. Never correct spelling or grammar.
7. Never normalize or transliterate Unicode characters.
8. Never replace Malayalam characters with similar-looking characters.
9. Preserve original punctuation, numbers and symbols.
10. Preserve the original question and option wording.

MALAYALAM PRESERVATION:

If the page contains Malayalam:

- Preserve every Malayalam character as printed.
- Do not convert Malayalam into English.
- Do not convert Malayalam into Manglish.
- Do not replace Malayalam words with synonyms.
- Do not silently correct spelling.
- Do not remove or alter vowel signs, chillus, conjuncts or punctuation.
- Compare the embedded text with the original page before using it.
- If the embedded text contains corrupted characters, do not copy
  those corrupted characters as if they were correct.
- If the original character cannot be identified confidently,
  do not guess it.

EXTRACTION RULES:

1. Extract only questions actually printed on this page.
2. Do not generate new questions.
3. Do not add questions from general knowledge.
4. Do not invent missing options.
5. Preserve the original option order.
6. Include the printed question number when available.
7. Extract only the visible part of a question that continues
   onto another page.
8. Do not invent explanations.
9. Use null for explanation unless it is printed on the page.
10. Extract no more than {questions_hint} additional questions.
11. If fewer additional questions are printed, return fewer.
12. Do not duplicate a question.
13. Do not guess unreadable words or characters.
14. Do not return any question listed in the excluded section.

TEXT-LAYER VERIFICATION:

- Compare the extracted PDF text with the original PDF page.
- If the text layer agrees with the page, use its exact wording.
- If the text layer disagrees with the page, trust only text that
  can be read confidently from the original page.
- Never assume that a text layer is correct merely because it
  contains readable characters.
- If a word or character is unclear in both sources, do not guess.

ANSWER RULES:

- Use zero-based option indexes.
- Include the correct answer only when clearly established
  by the printed answer key or an unambiguous indication on the page.
- If the answer is not printed or cannot be identified confidently,
  return an empty array for "c".
- Never invent an answer.
- Never add an explanation that is not printed.

OUTPUT FORMAT:

Return a valid JSON array only.

[
  {{
    "q": "Original question exactly as printed",
    "o": [
      "Original option A",
      "Original option B",
      "Original option C",
      "Original option D"
    ],
    "c": [0],
    "e": null
  }}
]

The "q" field must contain the original question.
The "o" field must contain the original options.
The "c" field must contain zero-based correct option indexes.
The "e" field must contain the printed explanation or null.

No markdown.
No code fences.
No additional commentary.

Return [] if no additional questions can be extracted confidently.

OUTPUT: JSON ARRAY ONLY.
"""

    payload = {
        "system_instruction": {
            "parts": [{
                "text": (
                    "You are a faithful PDF transcription and extraction "
                    "system, not a quiz writer. Preserve original text "
                    "and scripts. The original PDF page is the visual "
                    "source of truth. Use embedded text only after "
                    "checking it against the page. Never translate, "
                    "rewrite, normalize, correct or invent text. "
                    "Never guess unclear characters. Do not repeat "
                    "questions from the excluded list. Return valid JSON only."
                )
            }]
        },
        "contents": [{
            "role": "user",
            "parts": [
                {
                    "inline_data": {
                        "mime_type": "application/pdf",
                        "data": page_pdf_b64,
                    }
                },
                {"text": prompt},
            ],
        }],
        "generationConfig": {
            "maxOutputTokens": max(2048, questions_hint * 500),
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
            f"Gemini {status}: {str(data)[:300]}"
        )

    try:
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            f"Invalid Gemini response: {str(data)[:300]}"
        ) from exc

    raw = raw.strip()

    parsed_questions = parse_ai_questions(raw)

    # Apply local filtering as well. Do not rely only on Gemini
    # following the duplicate-prevention instructions.
    questions = _filter_excluded_pdf_questions(
        parsed_questions,
        exclude_questions,
    )

    logger.info(
        (
            "PDF extraction page %s: text_layer=%s, "
            "received=%d, excluded=%d, final=%d"
        ),
        page_num,
        text_layer_available,
        len(parsed_questions),
        len(parsed_questions) - len(questions),
        len(questions),
    )

    return questions
