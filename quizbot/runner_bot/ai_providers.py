
"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
PDF extraction updated to prioritize verbatim source text.
"""

from __future__ import annotations

import asyncio
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

    # Walk the text and find the matching closing bracket while respecting
    # quoted strings and escaped characters.
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

    Important:
    - Do not truncate extracted question text or options.
    - Do not alter Malayalam/Unicode characters.
    - Do not normalize Unicode.
    - Keep the source strings intact, including internal spacing.
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

                    # Preserve option text; do not strip or truncate.
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

    # Legacy fallback parser (used by other quiz-generation flows).
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
# Topic-based quiz generation (existing functionality)
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
                diff=AIQUIZ_DIFFICULTY.get(
                    diff, "Moderate"
                ),
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
                diff=AIQUIZ_DIFFICULTY.get(
                    diff, "Moderate"
                ),
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
# PDF page question extraction
# ---------------------------------------------------------------------

async def gemini_page_questions(
    api_key: str,
    page_pdf_b64: str,
    page_num: int,
    questions_hint: int,
    diff: str,
    exam: str,
    lang: str,
) -> list[dict]:
    """
    Extract EXISTING MCQs from a PDF page.

    Language is detected from the PDF itself.
    Do not create, translate, or rewrite questions.
    Preserve original wording and Unicode characters.
    """

    prompt = f"""
TASK: Extract the EXISTING multiple-choice questions printed on this PDF page.

PDF page number: {page_num}
Requested maximum questions: {questions_hint}

LANGUAGE INSTRUCTION:
- AUTO-DETECT THE LANGUAGE DIRECTLY FROM THE PDF PAGE.
- KEEP THE EXACT ORIGINAL LANGUAGE AND SCRIPT.
- NEVER TRANSLATE TO ENGLISH OR ANY OTHER LANGUAGE.
- If the page contains Malayalam, output Malayalam exactly as printed.
- If the page contains English, output English exactly as printed.
- If the page contains multiple languages, preserve the original languages.

STRICT EXTRACTION RULES:

1. Extract questions that are ALREADY PRINTED on this page.
2. NEVER invent, generate, add, or compose a new question.
3. NEVER paraphrase, simplify, translate, correct, or rewrite the text.
4. Preserve Malayalam text exactly as visible.
5. Preserve the original spelling, punctuation, symbols, numbers, and characters.
6. Preserve the original question wording and option wording.
7. Do not convert Malayalam to English or English to Malayalam.
8. Do not replace Malayalam characters with similar-looking characters.
9. Do not silently fix spelling or grammar.
10. Do not add your own answer options.
11. If the question continues from another page, extract only what is visible.
12. If a question or option is unclear or cut off, do not guess missing text.
13. If you cannot confidently identify the correct answer from the PDF, set "c": [].
14. Extract up to {questions_hint} questions. If fewer are printed, return fewer.
15. Include the original question number inside the question text if it is printed.
16. Return only the questions present on this page.
17. Return a JSON array only, with no prose and no markdown.

JSON format:
[
  {{
    "q": "Question exactly as printed",
    "o": ["Option exactly as printed", "Option exactly as printed"],
    "c": [0],
    "e": null
  }}
]

For "q":
- Copy the question text faithfully.
- Do not truncate the question.
- Keep Malayalam and other Unicode characters as they appear.

For "o":
- Copy each printed option faithfully.
- Do not truncate options.
- Do not create missing options.
- Preserve option ordering.

For "c":
- Use zero-based option indexes.
- Use an empty array if the answer is not clearly available.
- Never guess the correct answer.

For "e":
- Do not invent explanations.
- Use null unless an explanation is printed on the page.
- If an explanation is printed, copy it exactly.

OUTPUT: JSON array only.
Start with [ and end with ].
"""

    payload = {
        "system_instruction": {
            "parts": [{
                "text": (
                    "You are a faithful document extraction system, not a quiz writer. "
                    "Copy existing text from the supplied PDF page in its original language "
                    "and original script. Detect the language from the document itself. "
                    "Never translate into English. Never rewrite, normalize, correct, "
                    "or invent text. Return only valid JSON."
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
            "maxOutputTokens": max(1024, questions_hint * 500),
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

    raw = data["candidates"][0]["content"]["parts"][0]["text"]
    raw = raw.strip()

    questions = parse_ai_questions(raw)

    logger.info(
        "PDF extraction page %s: extracted %d questions",
        page_num,
        len(questions),
    )

    return questions
