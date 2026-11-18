"""
PSCGram Quiz Bot — Creator Quiz Creation Handler

Supports:
- /create wizard
- Manual MCQ creation
- Quiz poll importing
- TXT / JSON importing
- DIRECT PDF -> MCQ -> QUIZ
- English + Malayalam OCR through file_import.py
- No AI required for PDF parsing
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from pyrogram import Client, filters
from pyrogram.enums import PollType
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from quizbot.database import (
    CreatorSettingsRepository,
    QuizRepository,
    UserRepository,
    get_db,
)
from quizbot.shared import config
from quizbot.shared.mini_app_link import mini_app_web_app_button
from quizbot.shared.utils import is_premium_user

from .. import state
from ..parsing import filter_words, parse_question_block, strip_source_noise
from ..ratelimit import ratelimit
from ..subscribe_gate import subscribe_gate
from .file_import import process_uploaded_file


logger = logging.getLogger(__name__)


# ============================================================
# CONSTANTS
# ============================================================

MIN_QUESTIONS = 10
MAX_QUESTIONS = 300
MAX_QUESTIONS_OWNER = 3000

# IMPORTANT:
# The Creator Bot creates/manages quizzes.
# The Runner Bot opens/plays the quizzes.
RUNNER_BOT_USERNAME = "Quizpscbot"


_RESERVED_COMMANDS = [
    "start",
    "create",
    "myquizzes",
    "edit",
    "info",
    "ban",
    "done",
    "add",
    "rem",
    "remall",
    "del",
    "remove",
    "clearlist",
    "mywords",
    "help",
    "cancel",
    "quiz",
    "search",
    "auth",
    "pay",
    "setpromo",
    "setkey",
    "mykeys",
    "delkey",
    "settings",
    "batch",
    "createbatch",
    "searchbatch",
    "stopedit",
    "whtml",
    "testseries",
    "tsr",
    "mocktest",
    "features",
    "gcast",
    "stopcast",
    "statses",
    "testapi",
    "leaders",
    "aspirants",
    "limit",
    "listquiz",
    "removeuser",
]


# ============================================================
# HELPERS
# ============================================================

def _poll_text(value) -> Optional[str]:
    """
    Convert Pyrogram/Kurigram FormattedText or string into plain text.
    """

    if value is None:
        return None

    text = getattr(value, "text", None)

    if text is not None:
        return str(text)

    return str(value)


def _gen_qid() -> str:
    import random
    import string

    return "GGN" + "".join(
        random.choices(
            string.ascii_uppercase + string.digits,
            k=6,
        )
    )


def _safe_filename(filename: str) -> str:
    """
    Convert PDF filename into a reasonable quiz name.
    """

    name = os.path.basename(filename or "PDF Quiz")

    if "." in name:
        name = name.rsplit(".", 1)[0]

    name = name.replace("_", " ")
    name = name.replace("-", " ")

    name = " ".join(name.split()).strip()

    if not name:
        name = "PDF Quiz"

    return name[:150]


# ============================================================
# /CREATE
# ============================================================

@ratelimit("create")
async def create_cmd(c: Client, m: Message) -> None:
    """
    /create -- start manual quiz creation.
    """

    if await subscribe_gate(c, m):
        return

    uid = m.from_user.id

    if not await is_premium_user(uid):
        await m.reply("🔒 Purchase premium: /pay")
        return

    if uid in state.quiz_creation:
        await m.reply(
            "⚠️ Already creating a quiz.\n"
            "Use /done or /cancel."
        )
        return

    await m.reply(
        "📝 **Send the quiz name.**"
    )

    state.quiz_creation[uid] = {
        "questions": [],
        "timer": None,
        "quiz_name": None,
        "awaiting_name": True,
    }


# ============================================================
# /CANCEL
# ============================================================

@ratelimit("default")
async def cancel_cmd(c: Client, m: Message) -> None:
    """
    /cancel -- discard current quiz creation.
    """

    uid = m.from_user.id

    if uid in state.quiz_creation:
        state.quiz_creation.pop(uid, None)
        await m.reply("❌ Cancelled.")
    else:
        await m.reply("⚠️ Nothing to cancel.")


# ============================================================
# /DONE
# ============================================================

@ratelimit("create")
async def done_cmd(c: Client, m: Message) -> None:
    """
    /done -- finish manual quiz creation.
    """

    uid = m.from_user.id

    if not await is_premium_user(uid):
        await m.reply("🔒 Purchase premium: /pay")
        return

    if uid not in state.quiz_creation:
        await m.reply("⚠️ Use /create first.")
        return

    total = len(
        state.quiz_creation[uid]["questions"]
    )

    if total < MIN_QUESTIONS:
        await m.reply(
            f"⚠️ Need at least {MIN_QUESTIONS} questions.\n"
            f"You have {total}."
        )
        return

    max_allowed = (
        MAX_QUESTIONS_OWNER
        if uid == config.OWNER_ID
        else MAX_QUESTIONS
    )

    if total > max_allowed:
        await m.reply(
            f"⚠️ Maximum {max_allowed} questions.\n"
            f"You have {total}."
        )
        return

    settings_repo = CreatorSettingsRepository(get_db())
    settings = await settings_repo.get(uid)

    quiz_defaults = settings.get("quiz_defaults") or {}

    has_defaults = bool(
        quiz_defaults.get("type")
    )

    if has_defaults:

        typ = quiz_defaults.get(
            "type",
            "free",
        )

        promo = (
            quiz_defaults.get("promo")
            or None
        )

        section = quiz_defaults.get(
            "section",
            "no",
        )

        promo_preview = (
            promo[:30] + "..."
            if promo and len(promo) > 30
            else promo or "none"
        )

        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⚡ Use saved config",
                        callback_data=f"qd_use_{uid}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📝 Manual setup",
                        callback_data=f"qd_manual_{uid}",
                    )
                ],
            ]
        )

        state.quiz_creation[uid]["_qd"] = (
            quiz_defaults
        )

        await m.reply(
            f"⚡ **Quick Save available!**\n\n"
            f"Type: `{typ}`\n"
            f"Promo: `{promo_preview}`\n"
            f"Sections: `{section}`\n\n"
            f"Use the saved config, or set up manually?",
            reply_markup=kb,
        )

        return

    await m.reply(
        "📚 Section quiz? yes/no"
    )

    state.quiz_creation[uid][
        "awaiting_section_choice"
    ] = True


# ============================================================
# FINALIZE QUIZ
# ============================================================

async def _finalize_quiz(
    c: Client,
    reply_target,
    uid: int,
    quiz_type: str,
    promo: Optional[str],
    sections: list[dict],
    timer: int,
    from_user_name: str,
) -> None:

    is_message = isinstance(
        reply_target,
        Message,
    )

    async def send_result(
        text: str,
        kb: Optional[InlineKeyboardMarkup] = None,
    ):
        if is_message:
            return await reply_target.reply(
                text,
                reply_markup=kb,
            )

        try:
            return await reply_target.message.edit_text(
                text,
                reply_markup=kb,
            )
        except Exception:
            return await reply_target.message.reply(
                text,
                reply_markup=kb,
            )

    if uid not in state.quiz_creation:
        await send_result(
            "⚠️ Quiz creation session expired."
        )
        return

    quiz_name = state.quiz_creation[uid][
        "quiz_name"
    ]

    questions = state.quiz_creation[uid][
        "questions"
    ]

    # --------------------------------------------------------
    # Apply default text
    # --------------------------------------------------------

    settings_repo = CreatorSettingsRepository(
        get_db()
    )

    settings = await settings_repo.get(uid)

    default_text = settings.get(
        "default_text"
    )

    default_text_field = settings.get(
        "default_text_field",
        "both",
    )

    if default_text:

        for q in questions:

            if (
                default_text_field
                in ("question", "both")
                and q.get("question")
            ):
                q["question"] = (
                    q["question"].rstrip()
                    + "\n"
                    + default_text
                )

            if (
                default_text_field
                in ("explanation", "both")
            ):

                if q.get("explanation"):
                    q["explanation"] = (
                        q["explanation"].rstrip()
                        + "\n"
                        + default_text
                    )
                else:
                    q["explanation"] = (
                        default_text
                    )

    # --------------------------------------------------------
    # Create quiz
    # --------------------------------------------------------

    qid = _gen_qid()

    quiz_repo = QuizRepository(
        get_db()
    )

    quiz = await quiz_repo.create(
        creator_id=uid,
        quiz_name=quiz_name,
        questions=questions,
        qid=qid,
        sections=sections,
        timer=timer,
        quiz_type=quiz_type,
        negative_marks=0,
        promo_message=promo,
    )

    state.quiz_creation.pop(
        uid,
        None,
    )

    if not quiz:

        await send_result(
            "⚠️ Quiz created but could not be re-fetched.\n"
            "Try /myquizzes."
        )

        return

    promo_flag = (
        "Set"
        if promo
        else "None"
    )

    text = (
        f"> 🎉 **Quiz Created!**\n\n"
        f"📝 **Name:** {quiz_name}\n"
        f"❓ **Questions:** {len(quiz['questions'])}\n"
        f"⏱️ **Timer:** {timer}s\n"
        f"🆔 **Quiz ID:** `{qid}`\n"
        f"🏷 **Type:** `{quiz_type}`\n"
        f"🪭 **Promo:** {promo_flag}\n"
        f"👨‍💼 **Creator:** `{from_user_name}`"
    )

    if sections:

        text += "\n\n**Sections:**"

        for i, sec in enumerate(
            sections,
            1,
        ):

            text += (
                f"\n\nSection {i}: "
                f"{sec['name']}\n"
                f"  Questions: "
                f"{sec['question_range'][0]} "
                f"to "
                f"{sec['question_range'][1]}\n"
                f"  Timer: "
                f"{sec.get('timer', 'N/A')}s"
            )

    # --------------------------------------------------------
    # Buttons
    # --------------------------------------------------------

    me = await c.get_me()

    # IMPORTANT:
    # Play/Add-to-Group links point to Runner Bot.
    # Creator Bot is only used for creating/managing quizzes.

    quiz_url = (
        f"https://t.me/{RUNNER_BOT_USERNAME}?start={qid}"
    )

    # Telegram's native share URL.
    share_url = (
        "https://t.me/share/url"
        f"?url={quiz_url}"
        f"&text={quiz_name}"
    )

    kb_buttons = [
        [
            InlineKeyboardButton(
                "🚀 Start Quiz",
                url=quiz_url,
            )
        ],
        [
            InlineKeyboardButton(
                "👥 Add to Group",
                url=(
                    f"https://t.me/"
                    f"{RUNNER_BOT_USERNAME}"
                    f"?startgroup={qid}"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "🔗 Share Quiz",
                url=share_url,
            )
        ],
    ]

    # Mini App buttons still use the Creator Bot's
    # existing Mini App configuration.
    play_practice = (
        mini_app_web_app_button(
            me.username,
            qid,
            "practice",
            "Play (Practice)",
        )
    )

    play_exam = (
        mini_app_web_app_button(
            me.username,
            qid,
            "exam",
            "Play (Exam)",
        )
    )

    if play_practice and play_exam:

        kb_buttons.append(
            [
                play_practice,
                play_exam,
            ]
        )

    if quiz_type == "paid":

        kb_buttons.append(
            [
                InlineKeyboardButton(
                    "📦 Attach to Batch",
                    callback_data=(
                        f"bat_attachqz_"
                        f"{qid}_{uid}"
                    ),
                )
            ]
        )

    kb = InlineKeyboardMarkup(
        kb_buttons
    )

    await send_result(
        text,
        kb,
    )

    # --------------------------------------------------------
    # Optional group announcement
    # --------------------------------------------------------

    if config.BOT_GROUP:

        try:

            announce_text = (
                strip_source_noise(text)
                or text
            )

            await c.send_message(
                config.BOT_GROUP,
                announce_text,
                reply_markup=(
                    InlineKeyboardMarkup(
                        kb_buttons[:3]
                    )
                ),
            )

        except Exception:

            logger.debug(
                "Failed to announce new quiz",
                exc_info=True,
            )


# ============================================================
# QUICK SAVE CALLBACK
# ============================================================

async def quicksave_cb(
    c: Client,
    cb: CallbackQuery,
) -> None:

    uid = cb.from_user.id

    parts = cb.data.split("_")

    action = parts[1]
    target = int(parts[2])

    if uid != target:

        await cb.answer(
            "❌ Not yours",
            show_alert=True,
        )

        return

    if uid not in state.quiz_creation:

        await cb.answer(
            "⚠️ Session expired",
            show_alert=True,
        )

        return

    if action == "manual":

        try:
            await cb.message.edit_text(
                "📚 Section quiz? yes/no"
            )
        except Exception:
            await cb.message.reply(
                "📚 Section quiz? yes/no"
            )

        state.quiz_creation[uid][
            "awaiting_section_choice"
        ] = True

        await cb.answer()

        return

    quiz_defaults = (
        state.quiz_creation[uid]
        .get("_qd", {})
    )

    quiz_type = quiz_defaults.get(
        "type",
        "free",
    )

    promo = (
        quiz_defaults.get("promo")
        or None
    )

    section_choice = quiz_defaults.get(
        "section",
        "no",
    )

    timer = (
        state.quiz_creation[uid]
        .get("timer")
        or 20
    )

    await cb.answer(
        "⚡ Saving..."
    )

    try:
        await cb.message.edit_text(
            "🚀 Creating quiz..."
        )
    except Exception:
        pass

    if section_choice == "yes":

        state.quiz_creation[uid][
            "section_wise"
        ] = True

        state.quiz_creation[uid][
            "promo_message"
        ] = promo

        state.quiz_creation[uid][
            "type_preset"
        ] = quiz_type

        state.quiz_creation[uid][
            "awaiting_section_count"
        ] = True

        try:
            await cb.message.edit_text(
                "📚 How many sections? (>= 2)"
            )
        except Exception:
            await cb.message.reply(
                "📚 How many sections? (>= 2)"
            )

        return

    name = (
        cb.from_user.first_name
        if cb.from_user
        else str(uid)
    )

    await _finalize_quiz(
        c,
        cb,
        uid,
        quiz_type,
        promo,
        [],
        timer,
        name,
    )


# ============================================================
# DIRECT PDF IMPORT
# ============================================================

async def handle_pdf_direct(
    c: Client,
    m: Message,
) -> None:
    """
    DIRECT PDF -> QUESTIONS -> QUIZ

    User sends a PDF directly to the creator bot.

    No /create required.

    No Telegram channel required.

    No AI required.
    """

    uid = m.from_user.id

    # --------------------------------------------------------
    # Premium check
    # --------------------------------------------------------

    if not await is_premium_user(uid):

        await m.reply(
            "🔒 PDF → Quiz creation is a premium feature.\n\n"
            "Purchase premium: /pay"
        )

        return

    # --------------------------------------------------------
    # Check PDF
    # --------------------------------------------------------

    filename = (
        m.document.file_name
        or "quiz.pdf"
    )

    mime_type = (
        m.document.mime_type
        or ""
    )

    if (
        not filename.lower().endswith(".pdf")
        and mime_type != "application/pdf"
    ):

        await m.reply(
            "⚠️ Please send a PDF file."
        )

        return

    # --------------------------------------------------------
    # Prevent simultaneous creation
    # --------------------------------------------------------

    if uid in state.quiz_creation:

        await m.reply(
            "⚠️ You already have a quiz creation session running.\n\n"
            "Use /done or /cancel first."
        )

        return

    # --------------------------------------------------------
    # Download PDF
    # --------------------------------------------------------

    status = await m.reply(
        "📥 **Downloading PDF...**"
    )

    try:

        file_bytes = await c.download_media(
            m.document.file_id,
            in_memory=True,
        )

        if file_bytes is None:

            await status.edit_text(
                "❌ Could not download the PDF."
            )

            return

        file_bytes.seek(0)

        content = file_bytes.read()

    except Exception as e:

        logger.exception(
            "PDF download failed"
        )

        await status.edit_text(
            "❌ PDF download failed.\n\n"
            f"`{str(e)[:200]}`"
        )

        return

    # --------------------------------------------------------
    # Parse PDF
    # --------------------------------------------------------

    try:

        await status.edit_text(
            "🔍 **Reading PDF...**\n"
            "Extracting existing MCQs..."
        )

        questions = await process_uploaded_file(
            content,
            filename,
            max_questions=(
                MAX_QUESTIONS_OWNER
                if uid == config.OWNER_ID
                else MAX_QUESTIONS
            ),
        )

    except Exception as e:

        logger.exception(
            "PDF parsing failed"
        )

        await status.edit_text(
            "❌ Could not process the PDF.\n\n"
            f"`{str(e)[:200]}`"
        )

        return

    # --------------------------------------------------------
    # No questions
    # --------------------------------------------------------

    if not questions:

        await status.edit_text(
            "❌ **No MCQs found in this PDF.**\n\n"
            "The PDF should contain questions and options "
            "in a recognizable MCQ format.\n\n"
            "Example:\n\n"
            "1. Question?\n"
            "A) Option 1\n"
            "B) Option 2\n"
            "C) Option 3\n"
            "D) Option 4\n"
            "ANSWER: A\n\n"
            "🔎 The bot automatically tries "
            "English + Malayalam OCR for "
            "scanned/image-based PDFs."
        )

        return

    # --------------------------------------------------------
    # Minimum question check
    # --------------------------------------------------------

    if len(questions) < MIN_QUESTIONS:

        await status.edit_text(
            f"⚠️ Only **{len(questions)} MCQs** were found.\n\n"
            f"At least **{MIN_QUESTIONS} questions** "
            f"are required to create a quiz."
        )

        return

    # --------------------------------------------------------
    # Owner / normal limit
    # --------------------------------------------------------

    max_allowed = (
        MAX_QUESTIONS_OWNER
        if uid == config.OWNER_ID
        else MAX_QUESTIONS
    )

    if len(questions) > max_allowed:

        questions = questions[:max_allowed]

    # --------------------------------------------------------
    # Set quiz name
    # --------------------------------------------------------

    quiz_name = _safe_filename(
        filename
    )

    # --------------------------------------------------------
    # Initialize creation state
    # --------------------------------------------------------

    state.quiz_creation[uid] = {
        "questions": questions,
        "timer": 20,
        "quiz_name": quiz_name,
        "awaiting_name": False,
    }

    # --------------------------------------------------------
    # AUTOMATIC QUIZ CREATION
    #
    # Defaults:
    # type    = free
    # timer   = 20 sec
    # promo   = none
    # section = none
    # --------------------------------------------------------

    await status.edit_text(
        f"✅ **PDF processed successfully!**\n\n"
        f"📝 Quiz: **{quiz_name}**\n"
        f"❓ Questions found: **{len(questions)}**\n\n"
        f"🚀 Creating quiz..."
    )

    try:

        name = (
            m.from_user.first_name
            if m.from_user
            else str(uid)
        )

        await _finalize_quiz(
            c,
            m,
            uid,
            "free",
            None,
            [],
            20,
            name,
        )

    except Exception as e:

        logger.exception(
            "Automatic PDF quiz creation failed"
        )

        state.quiz_creation.pop(
            uid,
            None,
        )

        await status.edit_text(
            "❌ PDF was read, but quiz creation failed.\n\n"
            f"`{str(e)[:300]}`"
        )


# ============================================================
# MANUAL DOCUMENT IMPORT
# ============================================================

async def handle_document(
    c: Client,
    m: Message,
) -> None:
    """
    Handle TXT / JSON / PDF files while /create wizard
    is active.

    PDF direct mode is handled separately.
    """

    uid = m.from_user.id

    if not await is_premium_user(uid):
        return

    if uid not in state.quiz_creation:
        return

    filename = (
        m.document.file_name
        or ""
    )

    mime_type = (
        m.document.mime_type
        or ""
    )

    # Direct PDF should normally be handled by
    # handle_pdf_direct before this handler.
    if (
        filename.lower().endswith(".pdf")
        or mime_type == "application/pdf"
    ):

        await m.reply(
            "⚠️ A PDF is already handled automatically.\n"
            "Please send it outside the /create session, "
            "or use /cancel first."
        )

        return

    # --------------------------------------------------------
    # TXT / JSON only in manual wizard
    # --------------------------------------------------------

    allowed = (
        filename.lower().endswith(".txt")
        or filename.lower().endswith(".json")
        or mime_type.startswith("text/")
        or mime_type == "application/json"
        or mime_type.endswith("+json")
    )

    if not allowed:

        await m.reply(
            "⚠️ Supported files:\n"
            "• PDF\n"
            "• TXT\n"
            "• JSON"
        )

        return

    # --------------------------------------------------------
    # Download
    # --------------------------------------------------------

    status = await m.reply(
        "⏳ Processing file..."
    )

    try:

        file_bytes = await c.download_media(
            m.document.file_id,
            in_memory=True,
        )

        if file_bytes is None:

            await status.edit_text(
                "❌ Could not download the file."
            )

            return

        file_bytes.seek(0)

        content = file_bytes.read()

    except Exception as e:

        logger.exception(
            "File download failed"
        )

        await status.edit_text(
            "❌ File download failed."
        )

        return

    # --------------------------------------------------------
    # Parse
    # --------------------------------------------------------

    try:

        parsed = await process_uploaded_file(
            content,
            filename,
            max_questions=(
                MAX_QUESTIONS_OWNER
                if uid == config.OWNER_ID
                else MAX_QUESTIONS
            ),
        )

    except Exception as e:

        logger.exception(
            "File processing failed"
        )

        await status.edit_text(
            f"❌ Processing error:\n`{str(e)[:300]}`"
        )

        return

    if not parsed:

        await status.edit_text(
            "❌ No valid questions found in the file."
        )

        return

    # --------------------------------------------------------
    # Add to current quiz
    # --------------------------------------------------------

    state.quiz_creation[uid][
        "questions"
    ].extend(parsed)

    total = len(
        state.quiz_creation[uid][
            "questions"
        ]
    )

    await status.edit_text(
        f"✅ **{len(parsed)} questions imported!**\n\n"
        f"Total questions: **{total}**\n\n"
        f"Send more or use `/done`."
    )


# ============================================================
# CREATION MESSAGE HANDLER
# ============================================================

async def handle_creation_message(
    c: Client,
    m: Message,
) -> None:

    uid = m.from_user.id
    cid = m.chat.id

    if uid not in state.quiz_creation:
        return

    ud = state.quiz_creation[uid]

    # --------------------------------------------------------
    # FORWARDED QUIZ POLL
    # --------------------------------------------------------

    if m.poll:

        poll = m.poll

        if poll.type != PollType.QUIZ:

            await m.reply(
                "⚠️ Only quiz-type polls are supported."
            )

            return

        user = await UserRepository(
            get_db()
        ).get_or_create(uid)

        remove_words = user.get(
            "remove_words",
            [],
        )

        question = strip_source_noise(
            filter_words(
                _poll_text(
                    poll.question
                ),
                remove_words,
            )
        )

        options = [
            filter_words(
                _poll_text(o.text),
                remove_words,
            )
            for o in poll.options
        ]

        correct_id = getattr(
            poll,
            "correct_option_id",
            None,
        )

        if correct_id is None:

            correct_ids = getattr(
                poll,
                "correct_option_ids",
                None,
            ) or []

            correct_id = (
                correct_ids[0]
                if correct_ids
                else 0
            )

        explanation = None

        if getattr(
            poll,
            "explanation",
            None,
        ):

            explanation = strip_source_noise(
                filter_words(
                    _poll_text(
                        poll.explanation
                    ),
                    remove_words,
                )
            )

        reply_msg = m.reply_to_message

        reply_text = (
            reply_msg.text
            if reply_msg and reply_msg.text
            else None
        )

        file_id = None

        if (
            reply_msg
            and reply_msg.photo
            and config.BOT_GROUP
        ):

            try:

                copied = await c.copy_message(
                    config.BOT_GROUP,
                    reply_msg.chat.id,
                    reply_msg.id,
                )

                file_id = (
                    copied.photo.file_id
                )

            except Exception:

                logger.debug(
                    "Failed to copy poll reply photo",
                    exc_info=True,
                )

        ud["questions"].append(
            {
                "question": question,
                "options": options,
                "correct_option_id": correct_id,
                "explanation": explanation,
                "file_id": file_id,
                "reply_text": reply_text,
            }
        )

        await m.reply(
            f"✅ {len(ud['questions'])} saved!\n"
            "Send more or /done"
        )

        return

    # --------------------------------------------------------
    # TEXT
    # --------------------------------------------------------

    if not m.text:
        return

    # --------------------------------------------------------
    # QUIZ NAME
    # --------------------------------------------------------

    if ud.get("awaiting_name"):

        name = m.text.strip()

        if not name:

            await m.reply(
                "⚠️ Invalid name."
            )

            return

        ud["quiz_name"] = name

        ud["awaiting_name"] = False

        await m.reply(
            f"📝 Name: **{name}**\n\n"
            "Send questions, forward quiz polls, "
            "or send a TXT/JSON file.\n\n"
            "/cancel to abort."
        )

        return

    # --------------------------------------------------------
    # SECTION CHOICE
    # --------------------------------------------------------

    if ud.get(
        "awaiting_section_choice"
    ):

        choice = (
            m.text.strip().lower()
        )

        if choice not in (
            "yes",
            "no",
        ):

            await m.reply(
                "⚠️ Reply yes or no."
            )

            return

        ud["section_wise"] = (
            choice == "yes"
        )

        del ud[
            "awaiting_section_choice"
        ]

        if ud["section_wise"]:

            ud[
                "awaiting_section_count"
            ] = True

            await m.reply(
                "📚 How many sections? (>1)"
            )

        else:

            ud["timer"] = 20

            ud["awaiting_promo"] = True

            await m.reply(
                "📢 Send your promo message.\n"
                "Send `skip` or `no` to leave empty."
            )

        return

    # --------------------------------------------------------
    # SECTION COUNT
    # --------------------------------------------------------

    if ud.get(
        "awaiting_section_count"
    ):

        try:

            section_count = int(
                m.text.strip()
            )

            if section_count < 2:
                raise ValueError

        except ValueError:

            await m.reply(
                "⚠️ Enter a number >= 2."
            )

            return

        ud["section_count"] = (
            section_count
        )

        ud["sections"] = []

        ud["current_section"] = 1

        ud["last_range_end"] = 0

        del ud[
            "awaiting_section_count"
        ]

        ud[
            "awaiting_section_name"
        ] = True

        await m.reply(
            "📚 Section 1 name:"
        )

        return

    # --------------------------------------------------------
    # SECTION NAME
    # --------------------------------------------------------

    if ud.get(
        "awaiting_section_name"
    ):

        name = m.text.strip()

        if not name:

            await m.reply(
                "⚠️ Invalid name."
            )

            return

        ud["sections"].append(
            {
                "name": name
            }
        )

        del ud[
            "awaiting_section_name"
        ]

        ud[
            "awaiting_question_range"
        ] = True

        await m.reply(
            f"📚 Range for '{name}' "
            f"(e.g. 1-5).\n"
            f"Max: {len(ud['questions'])}"
        )

        return

    # --------------------------------------------------------
    # QUESTION RANGE
    # --------------------------------------------------------

    if ud.get(
        "awaiting_question_range"
    ):

        try:

            start, end = map(
                int,
                m.text.strip().split("-"),
            )

            total = len(
                ud["questions"]
            )

            if not (
                start >= 1
                and end <= total
                and start <= end
            ):
                raise ValueError

            if (
                ud["last_range_end"]
                and start
                != ud["last_range_end"] + 1
            ):
                raise ValueError

        except ValueError:

            await m.reply(
                "⚠️ Invalid range."
            )

            return

        ud["sections"][-1][
            "question_range"
        ] = (
            start,
            end,
        )

        del ud[
            "awaiting_question_range"
        ]

        ud["last_range_end"] = end

        ud[
            "awaiting_section_timer"
        ] = True

        await m.reply(
            "⏱️ Section timer in seconds (>10):"
        )

        return

    # --------------------------------------------------------
    # SECTION TIMER
    # --------------------------------------------------------

    if ud.get(
        "awaiting_section_timer"
    ):

        try:

            timer = int(
                m.text.strip()
            )

            if timer <= 10:
                raise ValueError

        except ValueError:

            await m.reply(
                "⚠️ Enter a number > 10."
            )

            return

        ud["sections"][-1][
            "timer"
        ] = timer

        del ud[
            "awaiting_section_timer"
        ]

        if (
            len(ud["sections"])
            < ud["section_count"]
        ):

            ud[
                "current_section"
            ] += 1

            ud[
                "awaiting_section_name"
            ] = True

            await m.reply(
                f"📚 Section "
                f"{ud['current_section']} name:"
            )

        else:

            ud[
                "awaiting_promo"
            ] = True

            await m.reply(
                "📢 Send your promo message.\n"
                "Send `skip` or `no` to leave empty."
            )

        return

    # --------------------------------------------------------
    # PROMO
    # --------------------------------------------------------

    if ud.get(
        "awaiting_promo"
    ):

        promo_text = m.text.strip()

        ud["promo_message"] = (
            None
            if promo_text.lower()
            in (
                "skip",
                "no",
                "none",
                "/skip",
            )
            else promo_text
        )

        del ud[
            "awaiting_promo"
        ]

        settings_repo = (
            CreatorSettingsRepository(
                get_db()
            )
        )

        settings = await settings_repo.get(
            uid
        )

        default_text = settings.get(
            "default_text"
        )

        default_text_field = settings.get(
            "default_text_field",
            "both",
        )

        if default_text:

            field_labels = {
                "question": "questions",
                "explanation": "explanations",
                "both": "questions & explanations",
            }

            ud[
                "awaiting_default_text_confirm"
            ] = True

            ud["_dt"] = default_text

            ud["_dtf"] = (
                default_text_field
            )

            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Yes, add it",
                            callback_data=(
                                f"dtc_yes_{uid}"
                            ),
                        ),
                        InlineKeyboardButton(
                            "⏭️ Skip",
                            callback_data=(
                                f"dtc_no_{uid}"
                            ),
                        ),
                    ]
                ]
            )

            await m.reply(
                "💡 **Add default text to "
                f"{field_labels.get(default_text_field, 'fields')}?**\n\n"
                f"`{default_text[:100]}`",
                reply_markup=kb,
            )

            return

        ud[
            "awaiting_type"
        ] = True

        await m.reply(
            "📊 Type (free/paid)"
        )

        return

    # --------------------------------------------------------
    # DEFAULT TEXT CONFIRMATION
    # --------------------------------------------------------

    if ud.get(
        "awaiting_default_text_confirm"
    ):

        return

    # --------------------------------------------------------
    # QUIZ TYPE
    # --------------------------------------------------------

    if ud.get(
        "awaiting_type"
    ):

        quiz_type = (
            m.text.strip().lower()
        )

        if quiz_type not in (
            "free",
            "paid",
        ):

            await m.reply(
                "⚠️ Reply free or paid."
            )

            return

        timer = (
            ud.get("timer")
            or 20
        )

        sections = ud.get(
            "sections",
            [],
        )

        promo = ud.get(
            "promo_message"
        )

        name = (
            m.from_user.first_name
            if m.from_user
            else str(uid)
        )

        await _finalize_quiz(
            c,
            m,
            uid,
            quiz_type,
            promo,
            sections,
            timer,
            name,
        )

        return

    # --------------------------------------------------------
    # FREE TEXT QUESTIONS
    # --------------------------------------------------------

    blocks = m.text.split(
        "\n\n"
    )

    reply_msg = (
        m.reply_to_message
    )

    reply_text = (
        reply_msg.text
        if reply_msg
        and reply_msg.text
        else None
    )

    file_id = None

    if (
        reply_msg
        and reply_msg.photo
        and config.BOT_GROUP
    ):

        try:

            copied = await c.copy_message(
                config.BOT_GROUP,
                reply_msg.chat.id,
                reply_msg.id,
            )

            file_id = (
                copied.photo.file_id
            )

        except Exception:

            logger.debug(
                "Failed to copy pasted question photo",
                exc_info=True,
            )

    parsed_any = False

    for block in blocks:

        if not block.strip():
            continue

        parsed = parse_question_block(
            block
        )

        if not parsed:

            await m.reply(
                "⚠️ Invalid format.\n\n"
                "Use:\n"
                "1. Question?\n"
                "A) Option 1\n"
                "B) Option 2\n"
                "C) Option 3\n"
                "D) Option 4\n"
                "ANSWER: A"
            )

            return

        parsed["file_id"] = file_id
        parsed["reply_text"] = reply_text

        ud["questions"].append(
            parsed
        )

        parsed_any = True

    if not parsed_any:

        await m.reply(
            "⚠️ No valid question found."
        )

        return

    total = len(
        ud["questions"]
    )

    suffix = (
        " Consider stopping soon."
        if total > 200
        else " Send more or /done"
    )

    await m.reply(
        f"✅ {total} saved!{suffix}"
    )


# ============================================================
# DEFAULT TEXT CALLBACK
# ============================================================

async def default_text_confirm_cb(
    c: Client,
    cb: CallbackQuery,
) -> None:

    uid = cb.from_user.id

    action = cb.data.split("_")[1]

    if uid not in state.quiz_creation:

        await cb.answer(
            "⚠️ Session expired",
            show_alert=True,
        )

        return

    ud = state.quiz_creation[uid]

    ud.pop(
        "awaiting_default_text_confirm",
        None,
    )

    if action == "no":

        ud.pop(
            "_dt",
            None,
        )

        ud.pop(
            "_dtf",
            None,
        )

    ud[
        "awaiting_type"
    ] = True

    await cb.message.reply(
        "📊 Type (free/paid)"
    )

    await cb.answer()


# ============================================================
# FILTER
# ============================================================

def in_quiz_creation_filter():

    async def func(
        _,
        __,
        m: Message,
    ):

        return (
            bool(m.from_user)
            and m.from_user.id
            in state.quiz_creation
        )

    return filters.create(func)


# ============================================================
# REGISTER HANDLERS
# ============================================================

def register(app: Client) -> None:

    # Commands
    app.on_message(
        filters.command("create")
        & filters.private
    )(create_cmd)

    app.on_message(
        filters.command("done")
        & filters.private
    )(done_cmd)

    app.on_message(
        filters.command("cancel")
        & filters.private
    )(cancel_cmd)

    # --------------------------------------------------------
    # Callbacks
    # --------------------------------------------------------

    app.on_callback_query(
        filters.regex(
            r"^qd_(use|manual)_\d+$"
        )
    )(quicksave_cb)

    app.on_callback_query(
        filters.regex(
            r"^dtc_(yes|no)_\d+$"
        )
    )(default_text_confirm_cb)

    # --------------------------------------------------------
    # IMPORTANT:
    # DIRECT PDF HANDLER
    #
    # This is registered BEFORE the wizard document handler.
    # --------------------------------------------------------

    app.on_message(
        filters.document
        & filters.private
        & filters.regex(
            r"(?i)\.pdf$",
            "document.file_name",
        )
    )(handle_pdf_direct)

    # MIME fallback for PDFs whose filename does not end in .pdf
    app.on_message(
        filters.document
        & filters.private
        & filters.regex(
            r"^application/pdf$",
            "document.mime_type",
        )
    )(handle_pdf_direct)

    # --------------------------------------------------------
    # TXT / JSON inside /create
    # --------------------------------------------------------

    app.on_message(
        filters.document
        & filters.private
        & in_quiz_creation_filter()
    )(handle_document)

    # --------------------------------------------------------
    # Text / poll creation wizard
    # --------------------------------------------------------

    app.on_message(
        (filters.text | filters.poll)
        & filters.private
        & in_quiz_creation_filter()
        & ~filters.command(
            _RESERVED_COMMANDS
        )
    )(handle_creation_message)
