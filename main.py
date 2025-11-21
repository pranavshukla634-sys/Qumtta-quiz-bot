import io
import json
import asyncio
import logging
import threading
import os
import random
import sys
import aiohttp
import time
import requests
from pathlib import Path
from random import randint
from flask import Flask
from typing import List, Dict, Any, Set
from datetime import datetime, timezone, timedelta
from telegram import (
    Update,
    Poll,
    InputFile,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import PollType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    PollAnswerHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
# -----------------------------
# 🔒 HARD-CODED CONFIG
# -----------------------------
OWNER_ID = 7370025284
ADMIN_IDS: set[int] = {OWNER_ID}
GROUP_ID = -1002621279973
BOT_TOKEN = "8458622801:AAFWZDxnB8ZGoQEtrljhuPGA8GHzghytpLU"
HEALTH_URL = "https://qumtta-quiz-bot.onrender.com"
ACTIVE_GROUPS: Set[int] = {GROUP_ID} # Main group + auto-add new ones
# -----------------------------
# STATES
# -----------------------------
(
    TITLE,
    POLL_SETTINGS,
    QUESTIONS,
    CORRECT_ANSWERS,
) = range(4)

(
    POLL_TITLE,
    POLL_TIMER,
    POLL_COLLECT,
    POLL_CORRECT,
) = range(100, 104) 

# ================= GLOBAL STATE (must be at top) =================
quiz_store: Dict[str, Dict] = {}
poll_quiz_data: Dict[int, Dict] = {}
scheduled_quizzes: List[Dict[str, Any]] = []
active_quiz_state: Dict[int, Dict] = {}

MAX_RETRY_PER_QUESTION = 3
RETRY_WAIT_SECONDS = 2

active_users: Set[int] = set()
current_quiz: Dict[str, Any] = None

# --- THESE ARE MOST IMPORTANT ---
all_mode_tracking: Dict[str, Dict[str, Any]] = {}
poll_message_map: Dict[str, int] = {}
poll_sent_time: Dict[str, float] = {}      # poll_id → timestamp
poll_to_quiz: Dict[str, str] = {}          # poll_id → quiz_id
poll_to_group: Dict[str, int] = {}         # poll_id → group_id

awaiting_start_time: Dict[int, Dict[str, Any]] = {}
is_paused: bool = False 
paused_groups: Set[int] = set()
# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# -----------------------------
# HELPERS
# -----------------------------
from functools import wraps
def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("‼️Unauthorised Access‼️")
            return
        return await func(update, context)
    return wrapper

def _reset_poll_data(user_id: int):
    poll_quiz_data.pop(user_id, None)

async def get_group_name(bot, gid):
    try:
        chat = await bot.get_chat(gid)
        return chat.title or str(gid)
    except:
        return str(gid)

DB_FILE = "qumtta_db.json"

def load_db():
    global ACTIVE_GROUPS, active_users, ADMIN_IDS
    if not Path(DB_FILE).exists():
        save_db()  # पहली बार file बना देगा
        return
    
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            ACTIVE_GROUPS = set(data.get("groups", []))
            active_users  = set(data.get("users", []))
            ADMIN_IDS     = set(data.get("admins", [OWNER_ID]))  # OWNER हमेशा admin रहेगा
        logger.info(f"DB loaded: {len(ACTIVE_GROUPS)} groups, {len(active_users)} users, {len(ADMIN_IDS)} admins")
    except Exception as e:
        logger.error(f"DB load failed: {e} → Using empty sets")
        ACTIVE_GROUPS = {GROUP_ID}
        active_users = set()
        ADMIN_IDS = {OWNER_ID}

def save_db(context: ContextTypes.DEFAULT_TYPE = None):   # ← यहीं change
    data = {
        "groups": sorted(list(ACTIVE_GROUPS)),
        "users":  sorted(list(active_users)),
        "admins": sorted(list(ADMIN_IDS))
    }
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info("DB saved successfully (auto-save)")
    except Exception as e:
        logger.error(f"DB save failed: {e}")
@admin_only
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message:
        original = update.message.reply_to_message
        text = original.text or original.caption or ""
        entities = original.entities or original.caption_entities
        success, failed = 0, 0

        # Auto-detect parse mode
        parse_mode = "Markdown" if not entities else None

        # ===============================
        # 🖼️ PHOTO BROADCAST
        # ===============================
        if original.photo:
            photo = original.photo[-1].file_id
            for gid in ACTIVE_GROUPS:
                try:
                    sent_msg = await context.bot.send_photo(
                        gid,
                        photo=photo,
                        caption=text,
                        caption_entities=entities,
                        parse_mode=parse_mode
                    )
                    # ✅ Auto-pin with notification
                    try:
                        await context.bot.pin_chat_message(gid, sent_msg.message_id)
                    except Exception as e:
                        logger.warning(f"Pin failed in {gid}: {e}")
                    success += 1
                except Exception as e:
                    failed += 1
                    logger.error(f"Broadcast photo failed in {gid}: {e}")

            await update.message.reply_text(f"📸 Photo broadcast complete!\n✅ Sent: {success}\n❌ Failed: {failed}")
            return

        # ===============================
        # 🗳️ POLL / QUIZ BROADCAST
        # ===============================
        if original.poll:
            poll = original.poll
            question = poll.question
            options = [opt.text for opt in poll.options]
            is_anonymous = poll.is_anonymous
            allows_multiple = poll.allows_multiple_answers
            poll_type = poll.type  # "regular" or "quiz"
            correct_option = poll.correct_option_id if poll_type == "quiz" else None
            explanation = poll.explanation if poll_type == "quiz" else None

            context.bot_data["active_polls"] = {}

            for gid in ACTIVE_GROUPS:
                try:
                    poll_msg = await context.bot.send_poll(
                        gid,
                        question=question,
                        options=options,
                        is_anonymous=is_anonymous,
                        allows_multiple_answers=allows_multiple,
                        type=poll_type,
                        correct_option_id=correct_option,
                        explanation=explanation
                    )

                    # ✅ Auto-pin with notification (no disable_notification)
                    try:
                        await context.bot.pin_chat_message(gid, poll_msg.message_id)
                    except Exception as e:
                        logger.warning(f"Pin failed in {gid}: {e}")

                    context.bot_data["active_polls"][gid] = poll_msg.message_id
                    success += 1

                except Exception as e:
                    failed += 1
                    logger.error(f"Broadcast poll failed in {gid}: {e}")

            await update.message.reply_text(
                f"🧩 Poll/Quiz broadcast complete!\n✅ Sent: {success}\n❌ Failed: {failed}\n\nUse /stop_poll to collect results (for normal polls only)."
            )
            return

        # ===============================
        # 📝 TEXT BROADCAST
        # ===============================
        for gid in ACTIVE_GROUPS:
            try:
                sent_msg = await context.bot.send_message(
                    gid,
                    text=text,
                    entities=entities,
                    parse_mode=parse_mode
                )
                try:
                    await context.bot.pin_chat_message(gid, sent_msg.message_id)
                except Exception as e:
                    logger.warning(f"Pin failed in {gid}: {e}")
                success += 1
            except Exception as e:
                failed += 1
                logger.error(f"Broadcast text failed in {gid}: {e}")

        await update.message.reply_text(f"📝 Broadcast complete!\n✅ Sent: {success}\n❌ Failed: {failed}")
        return

    # ===============================
    # DIRECT /broadcast <message>
    # ===============================
    msg_parts = update.message.text.split(" ", 1)
    if len(msg_parts) == 1:
        await update.message.reply_text(
            "Usage:\n"
            "1️⃣ Reply to a message and type `/broadcast`\n"
            "2️⃣ Or `/broadcast <your message>` directly\n\n"
            "_Supports Markdown formatting!_",
            parse_mode="Markdown"
        )
        return

    message = msg_parts[1]
    success, failed = 0, 0

    for gid in ACTIVE_GROUPS:
        try:
            sent_msg = await context.bot.send_message(
                gid,
                text=message,
                parse_mode="Markdown"
            )
            try:
                await context.bot.pin_chat_message(gid, sent_msg.message_id)
            except Exception as e:
                logger.warning(f"Pin failed in {gid}: {e}")
            success += 1
        except Exception as e:
            failed += 1
            logger.error(f"Broadcast failed in {gid}: {e}")

    await update.message.reply_text(f"📢 Broadcast complete!\n✅ Sent: {success}\n❌ Failed: {failed}")

@admin_only
async def stop_poll_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active_polls = context.bot_data.get("active_polls", {})
    if not active_polls:
        await update.message.reply_text("❌ No active polls found to stop.")
        return

    combined_results = {}
    stopped = 0

    # हम सिर्फ "regular" polls को stop करेंगे
    for gid, mid in active_polls.items():
        try:
            poll = await context.bot.stop_poll(gid, mid)

            # अगर poll quiz है तो skip करो
            if poll.type == "quiz":
                logger.info(f"Skipped quiz poll in {gid}")
                continue

            stopped += 1
            for opt in poll.options:
                combined_results[opt.text] = combined_results.get(opt.text, 0) + opt.voter_count

        except Exception as e:
            logger.error(f"Stop poll failed in {gid}: {e}")

    if stopped == 0:
        await update.message.reply_text("ℹ️ No regular polls found to stop.")
        return

    # 🧾 Merged results (only once)
    result_text = "📊 *Merged Poll Results:*\n\n"
    sorted_results = sorted(combined_results.items(), key=lambda x: x[1], reverse=True)

    for opt, count in sorted_results:
        result_text += f"• {opt}: *{count} votes*\n"

    await update.message.reply_text(f"✅ {stopped} regular polls stopped successfully.")
    
    try:
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text=result_text,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Failed to send DM result: {e}")

    # reset data
    context.bot_data["active_polls"] = {}

def build_start_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("/start")], [KeyboardButton("/createviatxt")], [KeyboardButton("/createviapoll")], [KeyboardButton("/done")], [KeyboardButton("/cancel")]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )
def split_questions_from_text(text: str) -> List[str]:
    # Split by blank line (one or more empty lines)
    parts = [q.strip() for q in text.split("\n\n") if q.strip()]
    return parts

async def send_json_file_to_user(user_chat_id: int, context: ContextTypes.DEFAULT_TYPE, data: Dict[str, Any], filename: str = "quiz.json"):
    json_str = json.dumps(data, indent=4, ensure_ascii=False)
    bio = io.BytesIO(json_str.encode("utf-8"))
    bio.name = filename
    await context.bot.send_document(chat_id=user_chat_id, document=InputFile(bio, filename=filename))

# -----------------------------
# BOT COMMANDS / FLOW
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    active_users.add(user.id)
    save_db()
    chat = update.effective_chat
    if user.id in ADMIN_IDS:
        text = (
            "👋 नमस्ते! यह Quiz Bot है. नीचे दिए कमांड से शुरू करें:\n\n"
            "/createviatxt or /createviapoll — एक नया क्विज बनाएँ (DM में, केवल admin).\n"
            "/start_quiz — लोड किया हुआ क्विज ग्रुप में चलाएँ (केवल admin और configured group).\n"
            "/cancel — वर्तमान ऑपरेशन रद्द करें.\n\n"
            "क्विज बनाने का नया फ्लो:\n"
            "1️⃣ टाइटल पूछेगा.\n"
            "2️⃣ फिर Poll settings (तीन लाइनें): option_count, option_texts comma-separated, timer in seconds.\n"
            "3️⃣ प्रश्न भेजें — एक ही संदेश में कई प्रश्न भेज सकते हैं; प्रश्नों के बीच एक खाली लाइन रखें.\n"
            "4️⃣ /done के बाद correct answers comma-separated भेजिए.\n"
        )
        await update.message.reply_text(text, reply_markup=build_start_keyboard())
    else:
        # Non-admin (in private or group)
        group_link = "https://t.me/+e0yQys0Dvf5lNGRl" # ← यहां अपने Qumtta World ग्रुप का लिंक डालें
        welcome_text = (
            "‼️ *Welcome To Qumtta World!* ‼️\n\n"
            "This is the official quiz bot of Qumtta World.\n"
            "Join our group for daily quizzes and fun challenges!"
        )
        buttons = [
            [InlineKeyboardButton("🔗 Join Qumtta World", url=group_link)]
        ]
        await update.message.reply_text(
            welcome_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
async def create_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # only in private and only admin
    if update.effective_chat.type != 'private':
        return
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌Unauthorised Access.")
        return
    context.user_data.clear()
    context.user_data['questions'] = [] # list of question texts
    context.user_data['added_chunks'] = [] # to allow undo of last chunk
    await update.message.reply_text("📝 अच्छा — पहले क्विज का Title बताइए:")
    return TITLE
async def title_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text.strip()
    if not title:
        await update.message.reply_text("कृपया वैध title भेजें.")
        return TITLE
    context.user_data['title'] = title
    await update.message.reply_text(
        "अब Poll settings भेजिए (तीन लाइनें):\n"
        "पहली लाइन: 4 या 5\n"
        "दूसरी लाइन: option texts comma-separated (eg: A,B,C,D)\n"
        "तीसरी लाइन: timer in seconds (5-600)\n\n"
        "Example:\n4\nA,B,C,D\n10\n"
    )
    return POLL_SETTINGS
async def poll_settings_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [l.strip() for l in update.message.text.splitlines() if l.strip()]
    if len(lines) < 3:
        await update.message.reply_text("कृपया तीन लाइनें भेजें — option_count, option_texts, timer.")
        return POLL_SETTINGS
    try:
        option_count = int(lines[0])
        if option_count not in (2, 3, 4, 5):
            raise ValueError
    except ValueError:
        await update.message.reply_text("पहली लाइन में 2/3/4/5 में से एक संख्या भेजें (उदाहरण: 4).")
        return POLL_SETTINGS
    option_texts = [o.strip() for o in lines[1].split(',') if o.strip()]
    if len(option_texts) != option_count:
        await update.message.reply_text(
            f"दूसरी लाइन में {option_count} options चाहिए — आपने {len(option_texts)} दिए हैं."
        )
        return POLL_SETTINGS
    try:
        timer = int(lines[2])
        if not 5 <= timer <= 600:
            raise ValueError
    except ValueError:
        await update.message.reply_text("तीसरी लाइन में 5 से 600 सेकंड के बीच timer दें.")
        return POLL_SETTINGS
    context.user_data['option_count'] = option_count
    context.user_data['option_texts'] = option_texts
    context.user_data['timer'] = timer
    await update.message.reply_text(
        "अब प्रश्न भेजें — एक ही संदेश में कई प्रश्न भेज सकते हैं (प्रश्नों के बीच एक खाली लाइन)।\n"
        "हर बार प्रश्न भेजने पर मैं कुल प्रश्नों की संख्या बताऊंगा. पूरा होने पर /done भेजें. यदि आपने गलती से भेज दिया तो /cancel लिखिए ताकि आखिरी जोड़ा गया सेट हटे.")
    return QUESTIONS
async def questions_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    questions = split_questions_from_text(text)
    if not questions:
        await update.message.reply_text("कोई प्रश्न नहीं मिला — कृपया वैध प्रश्न भेजें (प्रश्नों के बीच एक खाली लाइन रखें).")
        return QUESTIONS
    context.user_data['questions'].extend(questions)
    context.user_data['added_chunks'].append(questions)
    total = len(context.user_data['questions'])
    await update.message.reply_text(
        f"✅ {len(questions)} प्रश्न जोड़ दिए गए. कुल: {total} प्रश्न.\n\n"
        "यदि और प्रश्न हैं तो भेजें, या /done लिखकर आगे बढ़ें. /cancel से आखिरी जोड़ा हटेगा (या पूरा रद्द)."
    )
    return QUESTIONS
async def cancel_or_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # If in conversation and have added_chunks, undo last added; else cancel conversation
    if 'added_chunks' in context.user_data and context.user_data['added_chunks']:
        last = context.user_data['added_chunks'].pop()
        for _ in last:
            if context.user_data['questions']:
                context.user_data['questions'].pop()
        total = len(context.user_data['questions'])
        await update.message.reply_text(f"🗑️ आखिरी जोड़ा हट गया. अब कुल प्रश्न: {total}.\nयदि और undo चाहिए तो /cancel फिर से भेजें, या /done करें.")
        return QUESTIONS
    else:
        context.user_data.clear()
        await update.message.reply_text("❌ ऑपरेशन रद्द कर दिया गया.")
        return ConversationHandler.END

async def done_questions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total = len(context.user_data.get('questions', []))
    if total == 0:
        await update.message.reply_text("कोई प्रश्न जोड़े नहीं गए — कृपया पहले प्रश्न भेजें.")
        return QUESTIONS
    await update.message.reply_text(
        f"📌 कुल {total} प्रश्न रजिस्टर हुए. अब सभी सही उत्तर comma-separated भेजिए (उदाहरण: B,A,C,D...).\n"
        "उत्तर यह मान कर भेजें कि आपने options दूसरी लाइन में जो दिए थे (उनके क्रम में)."
    )
    return CORRECT_ANSWERS

async def correct_answers_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("कृपया comma-separated correct answers भेजें.")
        return CORRECT_ANSWERS

    tokens = [t.strip() for t in text.split(',') if t.strip()]
    questions = context.user_data.get('questions', [])

    if len(tokens) != len(questions):
        await update.message.reply_text(
            f"प्रश्नों की संख्या {len(questions)} है पर आपने {len(tokens)} उत्तर दिए. दोनों बराबर होने चाहिए."
        )
        return CORRECT_ANSWERS

    option_texts = context.user_data['option_texts']

    def token_to_index(tok: str) -> int:
        # Match exact option text
        for i, opt in enumerate(option_texts):
            if tok.lower() == opt.lower():
                return i

        # A, B, C,...
        if len(tok) == 1 and tok.isalpha():
            idx = ord(tok.upper()) - ord('A')
            if 0 <= idx < len(option_texts):
                return idx

        # 1,2,3,...
        if tok.isdigit():
            n = int(tok)
            if 1 <= n <= len(option_texts):
                return n - 1

        raise ValueError(f"Cannot interpret token '{tok}' as option index")

    try:
        correct_indices = [token_to_index(t) for t in tokens]
    except ValueError as e:
        await update.message.reply_text(str(e) + " — कृपया सही फ़ॉर्मैट में भेजें.")
        return CORRECT_ANSWERS

    # ---- Build quiz ----
    quiz = {
        'title': context.user_data['title'],
        'option_count': context.user_data['option_count'],
        'option_texts': context.user_data['option_texts'],
        'timer': context.user_data['timer'],
        'questions': [],
    }

    for q_text, correct_idx in zip(context.user_data['questions'], correct_indices):
        quiz['questions'].append({
            'text': q_text,
            'options': context.user_data['option_texts'],
            'correct': correct_idx,
            'timer': context.user_data['timer']
        })

    # Unique ID
    quiz_id = str(int(datetime.now(tz=timezone.utc).timestamp()))
    quiz['quiz_id'] = quiz_id

    # --- Store in simple dictionary ---
    quiz_store[quiz_id] = quiz

    # Save JSON file for user
    safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in context.user_data['title'])
    filename = f"{safe_title}.json"

    await send_json_file_to_user(update.effective_chat.id, context, quiz, filename=filename)

    # Buttons
    buttons = [
        [
            InlineKeyboardButton("Start Quiz", callback_data=f"start_quiz:{quiz_id}"),
            InlineKeyboardButton("Start in All Groups", callback_data=f"start_all:{quiz_id}")
        ]
    ]

    await update.message.reply_text(
        "✅ Quiz saved. नीचे से आगे की कार्रवाई करें:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

    # Clear user data
    context.user_data.clear()
    return ConversationHandler.END
# -------------------------------------------------------------------------
# NEW COMMAND: /createviapoll – build a quiz by forwarding polls (normal or quiz)
# -------------------------------------------------------------------------
@admin_only
async def create_via_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the poll-based quiz creation."""
    if update.effective_chat.type != "private":
        await update.message.reply_text("This command works only in private chat.")
        return ConversationHandler.END
    _reset_poll_data(update.effective_user.id)
    await update.message.reply_text(
        "Poll-based Quiz Creator\n"
        "1. Send the **title** of the quiz."
    )
    return POLL_TITLE
# ---------- TITLE ----------
async def poll_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text.strip()
    if not title:
        await update.message.reply_text("Title cannot be empty.")
        return POLL_TITLE
    poll_quiz_data[update.effective_user.id] = {
        "title": title,
        "timer": None,
        "polls": [] # each entry: {question, options, correct_idx}
    }
    await update.message.reply_text(
        "2. Send the **timer** (5-600 seconds) that will be used for **every** question."
    )
    return POLL_TIMER
# ---------- TIMER ----------
async def poll_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        timer = int(update.message.text.strip())
        if not 5 <= timer <= 600:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please send a number between 5 and 600.")
        return POLL_TIMER
    poll_quiz_data[update.effective_user.id]["timer"] = timer
    await update.message.reply_text(
        f"Timer set to **{timer}s**.\n\n"
        "3. **Forward** (or send) the polls one by one.\n"
        "• **Quiz-poll** – correct answer is taken automatically.\n"
        "• **Normal poll** – after the poll I will ask you for the correct option.\n\n"
        "When you are done, type **/done**."
    )
    return POLL_COLLECT
# ---------- COLLECT POLLS ----------
async def poll_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Accept forwarded polls (quiz or normal)."""
    user_id = update.effective_user.id
    if update.message.poll:
        poll = update.message.poll
        data = poll_quiz_data[user_id]
        # ---- store the poll info ----
        entry = {
            "question": poll.question,
            "options": [opt.text for opt in poll.options],
        }
        if poll.type == PollType.QUIZ: # quiz-poll → correct known
            entry["correct_idx"] = poll.correct_option_id
            data["polls"].append(entry)
            await update.message.reply_text(
                f"Quiz-poll #{len(data['polls'])} added (correct = {entry['options'][entry['correct_idx']]})"
            )
        else: # normal poll → ask later
            entry["poll_id"] = poll.id
            data["polls"].append(entry)
            await update.message.reply_text(
                f"Normal poll #{len(data['polls'])} added – I will ask for the correct option next."
            )
            # go straight to asking correct for this poll
            context.user_data["awaiting_correct_for"] = len(data["polls"]) - 1
            await update.message.reply_text(
                "Which option is **correct**?\n"
                "Reply with the **letter** (A, B, C…) or the **full text** of the option."
            )
            return POLL_CORRECT
    else:
        await update.message.reply_text("Please forward a **poll** (quiz or normal).")
    return POLL_COLLECT
# ---------- ASK CORRECT FOR NORMAL POLL ----------
async def poll_correct_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = poll_quiz_data[user_id]
    idx = context.user_data.get("awaiting_correct_for")
    if idx is None or idx >= len(data["polls"]):
        await update.message.reply_text("Something went wrong – start over with /createviapoll.")
        return POLL_COLLECT
    text = update.message.text.strip()
    options = data["polls"][idx]["options"]
    # ---- resolve the answer ----
    correct_idx = None
    # 1. exact text match (case-insensitive)
    for i, opt in enumerate(options):
        if opt.lower() == text.lower():
            correct_idx = i
            break
    # 2. single letter A/B/C…
    if correct_idx is None and len(text) == 1 and text.isalpha():
        letter_idx = ord(text.upper()) - ord("A")
        if 0 <= letter_idx < len(options):
            correct_idx = letter_idx
    if correct_idx is None:
        await update.message.reply_text(
            "Could not recognise the answer.\n"
            "Reply with the **letter** (A, B, …) or the **full option text**."
        )
        return POLL_CORRECT
    data["polls"][idx]["correct_idx"] = correct_idx
    del context.user_data["awaiting_correct_for"]
    await update.message.reply_text(
        f"Correct answer for poll #{idx+1} set to **{options[correct_idx]}**.\n"
        "Continue forwarding more polls or type **/done**."
    )
    return POLL_COLLECT
# ---------- DONE → BUILD JSON (FIXED: अलग-अलग options per question) ----------
async def poll_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # No polls?
    if user_id not in poll_quiz_data or not poll_quiz_data[user_id]["polls"]:
        await update.message.reply_text("No polls were added – aborting.")
        _reset_poll_data(user_id)
        return ConversationHandler.END

    src = poll_quiz_data[user_id]
    timer = src["timer"]

    # Build questions list
    questions = []
    for p in src["polls"]:
        questions.append({
            "text": p["question"],
            "options": p["options"],   # unique options per poll
            "correct": p["correct_idx"],
            "timer": timer
        })

    # Final quiz JSON
    quiz = {
        "title": src["title"],
        "timer": timer,
        "questions": questions,
    }

    # unique quiz ID
    quiz_id = str(int(datetime.now(tz=timezone.utc).timestamp()))
    quiz["quiz_id"] = quiz_id

    # SAVE TO quiz_store (your new storage)
    global quiz_store
    quiz_store[quiz_id] = quiz

    # JSON file export
    safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in src["title"])

    await send_json_file_to_user(
        user_id, context, quiz, filename=f"{safe_title}.json"
    )

    # Buttons → Only Start Quiz
    buttons = [
        [
            InlineKeyboardButton("Start Quiz", callback_data=f"start_quiz:{quiz_id}"),
            InlineKeyboardButton("Start in All Groups", callback_data=f"start_all:{quiz_id}")
        ]
    ]

    await update.message.reply_text(
        "Quiz created from polls!\n"
        "JSON file sent above. Use the buttons to start or publish.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

    _reset_poll_data(user_id)
    return ConversationHandler.END

# ---------- CANCEL ----------
async def poll_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _reset_poll_data(update.effective_user.id)
    await update.message.reply_text("Poll-based quiz creation cancelled.")
    return ConversationHandler.END
# -----------------------------
# Handle uploaded JSON / TXT (FULL WORKING CODE)
# -----------------------------
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private':
        return
    if update.effective_user.id not in ADMIN_IDS:
        return

    document = update.message.document
    if not document:
        return

    file = await document.get_file()
    filename = document.file_name
    file_lower = filename.lower()

    # ========= JSON → QUIZ LOAD =========
    if file_lower.endswith('.json'):
        try:
            file_bytes = await file.download_as_bytearray()
            data = json.loads(file_bytes)

            # Prepare unique quiz_id
            quiz_id = data.get("quiz_id") or str(int(datetime.now(tz=timezone.utc).timestamp()))
            data["quiz_id"] = quiz_id

            # ====== Check if already exists in quiz_store ======
            exists = quiz_id in quiz_store

            # If not saved earlier → save
            if not exists:
                quiz_store[quiz_id] = data

            # Buttons
            buttons = [
                [
                    InlineKeyboardButton("Start Quiz", callback_data=f"start_quiz:{quiz_id}"),
                    InlineKeyboardButton("Start in All Groups", callback_data=f"start_all:{quiz_id}")
                ]
            ]

            if exists:
                msg = "♻️ Quiz uploaded.\nStart करने के लिए नीचे क्लिक करें:"
            else:
                msg = "📥 Quiz uploaded\nStart करने के लिए नीचे क्लिक करें:"

            await update.message.reply_text(
                msg,
                reply_markup=InlineKeyboardMarkup(buttons)
            )

        except Exception as e:
            await update.message.reply_text(f"Failed to load JSON: {str(e)}")
        return

    # ========= TXT → DB RESTORE =========
    if filename.startswith('qumtta_db_') and file_lower.endswith('.txt'):
        try:
            file_bytes = await file.download_as_bytearray()
            content = file_bytes.decode("utf-8")

            new_groups = set()
            new_users = set()
            section = None

            for line in content.splitlines():
                line = line.strip()

                if line == "=== GROUPS ===":
                    section = "groups"
                elif line == "=== USERS ===":
                    section = "users"
                elif (line and line[0].isdigit()) or line.startswith('-'):
                    try:
                        num = int(line.split()[0])
                        if section == "groups":
                            new_groups.add(num)
                        elif section == "users":
                            new_users.add(num)
                    except:
                        continue

            ACTIVE_GROUPS.clear()
            ACTIVE_GROUPS.update(new_groups)
            active_users.clear()
            active_users.update(new_users)

            await update.message.reply_text(
                f"DB RESTORED!\nGroups: {len(ACTIVE_GROUPS)}\nUsers: {len(active_users)}"
            )

        except Exception as e:
            await update.message.reply_text(f"Failed to restore DB: {str(e)}")
        return

    # ========= INVALID FILE =========
    await update.message.reply_text(
        "Unsupported file.\n"
        "• `.json` → Load Quiz\n"
        "• `qumtta_db_*.txt` → Restore DB",
        parse_mode="Markdown"
    )
# -----------------------------
# NEW: Handle admin-provided IST time replies (for scheduling)
# -----------------------------
async def start_quiz_button_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # callback format → start_quiz:<quiz_id>
    _, quiz_id = query.data.split(":", 1)

    # admin check
    if update.effective_user.id not in ADMIN_IDS:
        await query.answer("❌ Unauthorized!", show_alert=True)
        return

    quiz = quiz_store.get(quiz_id)
    if not quiz:
        await query.answer("⚠ Quiz data not found in storage!", show_alert=True)
        return

    # ask for start time (IST) for a SINGLE group
    awaiting_start_time[update.effective_user.id] = {
        "quiz_id": quiz_id,
        "mode": "single"
    }

    await query.edit_message_text(
        "📩 कृपया Start time भेजें (IST) — format *HH:MM* (24-hour).\n"
        "Bot उस समय group में quiz पोस्ट करके *auto-start* कर देगा.",
        parse_mode="Markdown"
    )

async def start_all_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if update.effective_user.id not in ADMIN_IDS:
        await query.answer("❌ Unauthorized!", show_alert=True)
        return

    _, quiz_id = query.data.split(":", 1)
    quiz = quiz_store.get(quiz_id)

    if not quiz:
        await query.answer("⚠ Quiz not found in storage!", show_alert=True)
        return

    # Ask for IST time for ALL groups mode
    awaiting_start_time[update.effective_user.id] = {
        "quiz_id": quiz_id,
        "mode": "all"
    }

    await query.edit_message_text(
        "📩 कृपया Start time भेजें (IST) — format *HH:MM* (24-hour).\n"
        "Bot उस समय *सभी active groups* में quiz post करके auto-start कर देगा.",
        parse_mode="Markdown"
    )

async def admin_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin sends HH:MM (IST) after choosing a quiz. Schedules quiz start cleanly."""
    if update.effective_user.id not in ADMIN_IDS:
        return
    if update.effective_chat.type != "private":
        return

    text = update.message.text.strip()

    # Check if we are awaiting this admin's time input
    if update.effective_user.id not in awaiting_start_time:
        return

    # --------- Parse HH:MM Time ---------
    try:
        hh, mm = map(int, text.split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except Exception:
        await update.message.reply_text(
            "Invalid time format. कृपया HH:MM (24-hour) में भेजें — example: 20:30"
        )
        return

    # Extract awaiting info
    info = awaiting_start_time.pop(update.effective_user.id)
    quiz_id = info['quiz_id']
    mode = info.get('mode', 'single')

    # --------- IST → UTC Conversion ---------
    now_utc = datetime.now(timezone.utc)
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = now_utc.astimezone(ist)

    target_ist = datetime(
        year=now_ist.year,
        month=now_ist.month,
        day=now_ist.day,
        hour=hh,
        minute=mm,
        tzinfo=ist
    )

    # If selected time already passed → schedule for next day
    if target_ist < now_ist:
        target_ist += timedelta(days=1)

    target_utc = target_ist.astimezone(timezone.utc)
    delay_seconds = (target_utc - now_utc).total_seconds()

    # --------- FETCH QUIZ FROM quiz_store ---------
    quiz = quiz_store.get(quiz_id, {})
    questions = quiz.get("questions", [])
    per_question_timer = quiz.get("timer", 30)

    total_questions = len(questions)
    estimated_duration_sec = total_questions * (per_question_timer + 5)

    # --------- SCHEDULE JOB ---------
    job = context.job_queue.run_once(
        start_scheduled_quiz,
        when=int(delay_seconds),
        data={'quiz_id': quiz_id, 'mode': mode, 'initiator': update.effective_user.id}
    )

    # --------- SAVE IN scheduled_quizzes ---------
    scheduled_quizzes.append({
        'quiz_id': quiz_id,
        'start_ist': target_ist,
        'mode': mode,
        'duration_sec': estimated_duration_sec,
        'title': quiz.get('title', 'Untitled Quiz'),
        'job': job
    })

    # --------- RESPONSE ---------
    if mode == 'single':
        await update.message.reply_text(
            f"Quiz scheduled for *{target_ist.strftime('%H:%M IST – %d %b')}*.\n"
            f"Estimated duration: ~`{estimated_duration_sec // 60}` min\n"
            "Bot will start the quiz in the selected group.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"Quiz scheduled for *ALL GROUPS* at *{target_ist.strftime('%H:%M IST – %d %b')}*.\n"
            f"Estimated duration: ~`{estimated_duration_sec // 60}` min\n"
            "Bot will start the quiz in all configured groups.",
            parse_mode="Markdown"
        )

# ========== UPDATED start_scheduled_quiz (major changes) ==========
async def start_scheduled_quiz(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data

    # Remove finished schedule
    for sch in scheduled_quizzes[:]:
        if sch['job'] == job:
            scheduled_quizzes.remove(sch)
            break

    quiz_id = data["quiz_id"]
    mode = data["mode"]  # 'single' or 'all'

    quiz = quiz_store.get(quiz_id)
    if not quiz:
        logger.error(f"Quiz not found in storage: {quiz_id}")
        return

    title = quiz.get("title", "Untitled")
    total_q = len(quiz.get("questions", []))
    timer = quiz.get("timer", 30)

    # MODE TAG below timer
    mode_tag = "Qumtta Wolrd Mode" if mode == "single" else "All Groups Mode"

    intro_text = (
        "‼️ *Welcome to Qumtta World!* ‼️\n"
        "⚜ *I am Your Qumtta Quiz Bot* ⚜\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📘 *Quiz Title:* {title}\n"
        f"❓ *Total Questions:* {total_q}\n"
        f"⏱ *Timer:* {timer} sec/question\n"
        f"🎛 *Mode:* {mode_tag}\n"
        f"🎛 *Quiz will start within 45 sec..*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "👇 *Join :- Qumtta World* 👇"
    )

    join_button = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Join Qumtta World", url="https://t.me/+e0yQys0Dvf5lNGRl")]
    ])

    # ======================================================
    # =============== SINGLE GROUP MODE ====================
    # ======================================================
    if mode == "single":

        # Intro send — retry + random delay
        await asyncio.sleep(random.uniform(1, 4))

        msg = None
        for attempt in range(3):
            try:
                msg = await context.bot.send_message(
                    chat_id=GROUP_ID,
                    text=intro_text,
                    parse_mode="Markdown",
                    reply_markup=join_button
                )
                break
            except Exception as e:
                wait = 2 + attempt
                logger.error(f"Intro failed in single mode, retrying in {wait}s… {e}")
                await asyncio.sleep(wait)

        if msg is None:
            logger.error("❌ Intro failed in single-group mode even after retries")
            return

        # Random delay 1–45 sec → DIRECT STT
        delay = random.randint(1, 45)
        await asyncio.sleep(delay)

        await _init_and_start_quiz_in_group(context, GROUP_ID, quiz)
        return

    # ======================================================
    # =============== MULTI GROUP MODE (ALL) ===============
    # ======================================================
    elif mode == "all":

        sent_messages = []

        try:
            expected_groups = set(ACTIVE_GROUPS)
            all_mode_tracking[quiz_id] = {
                'expected': expected_groups.copy(),
                'completed': set(),
                'data': {},
                'started_at': datetime.now(timezone.utc)
            }
        except Exception as e:
            logger.warning(f"Failed to register all_mode_tracking for {quiz_id}: {e}")

        # ===================== INTRO SEND ======================
        for gid in ACTIVE_GROUPS:

            # Random delay before intro (anti-flood)
            await asyncio.sleep(random.uniform(1, 4))

            msg = None
            for attempt in range(3):
                try:
                    msg = await context.bot.send_message(
                        chat_id=gid,
                        text=intro_text,
                        parse_mode="Markdown",
                        reply_markup=join_button
                    )
                    break
                except Exception as e:
                    wait = 2 + attempt
                    logger.error(f"Intro send failed to {gid}, retrying in {wait}s… {e}")
                    await asyncio.sleep(wait)

            if msg is None:
                logger.error(f"❌ Intro failed in {gid} even after retries")
                continue

            sent_messages.append((gid, msg.message_id))

        # ================== DELAY → DIRECT START ==================
        for gid, mid in sent_messages:

            delay = random.randint(1, 45)

            async def __delay_and_start(g=gid, d=delay):
                await asyncio.sleep(d)
                await _init_and_start_quiz_in_group(context, g, quiz)

            asyncio.create_task(__delay_and_start())

# -------- helper: initialize & start a quiz in a single group ----------
async def _init_and_start_quiz_in_group(context: ContextTypes.DEFAULT_TYPE, chat_id: int, quiz: dict):
   
    # prepare a local copy of questions and shuffle their order
    questions = quiz.get('questions', [])
    indices = list(range(len(questions)))
    random.shuffle(indices)

    active_quiz_state[chat_id] = {
        'quiz_id': quiz.get('quiz_id') or str(int(datetime.now(tz=timezone.utc).timestamp())),
        'questions_order': indices,
        'index': 0,
        'scores': {},  # user_id -> score
        'user_stats': {},  # user_id -> {'correct':.., 'incorrect':.., 'total_time':..}
        'started': True,
        'retry_count': {},  # question_index -> retry attempts
        'quiz_meta': quiz,  # keep pointer to quiz object (read-only)
    }

    # start sending first question (schedule immediately)
    await send_next_question(context, chat_id)

async def send_next_question(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    if chat_id in paused_groups:
        logger.info(f"Group {chat_id} paused — skipping next question")
        return
    state = active_quiz_state.get(chat_id)
    if not state or not state.get('started'):
        return

    quiz = state['quiz_meta']
    questions = quiz.get('questions', [])
    q_order = state['questions_order']
    q_index_local = state['index']

    # All questions sent? → end this group's quiz
    if q_index_local >= len(q_order):
        await _end_quiz_for_group(context, chat_id)
        return

    question_obj = questions[q_order[q_index_local]]
    # send text part (if any)
    try:
        if 'text' in question_obj:
            await context.bot.send_message(chat_id, f"Q{q_index_local + 1}. {question_obj['text']}")
    except Exception as e:
        logger.error(f"Text message failed in {chat_id}: {e}")

    # send poll (quiz type)
    try:
        message = await context.bot.send_poll(
            chat_id=chat_id,
            question="Choose correct option",
            options=question_obj['options'],
            type=PollType.QUIZ,
            correct_option_id=question_obj['correct'],
            open_period=question_obj.get('timer', quiz.get('timer', 30)),
            is_anonymous=False,
        )
        poll_id = message.poll.id
        poll_message_map[poll_id] = message.message_id  

        # record when poll sent for timing
        poll_sent_time[poll_id] = datetime.now(tz=timezone.utc).timestamp()

        # attach poll_id → chat mapping so poll_answer can find which group this poll belongs to
        poll_to_quiz[poll_id] = state['quiz_id']
        poll_to_group[poll_id] = chat_id

        # schedule next question for this group
        open_period = question_obj.get('timer', quiz.get('timer', 30))
        context.job_queue.run_once(
            next_question_callback,
            open_period + 2,
            data={'chat_id': chat_id},
            name=f"next_{chat_id}_{q_index_local}"
        )

        # reset retry counter for this question on success
        state['retry_count'].pop(q_index_local, None)

    except Exception as e:
        # safe retry logic: retry sending the same question a few times, then cancel group if unrecoverable
        logger.error(f"Poll failed in {chat_id}: {e}")
        retries = state['retry_count'].get(q_index_local, 0) + 1
        state['retry_count'][q_index_local] = retries
        if retries <= MAX_RETRY_PER_QUESTION:
            await asyncio.sleep(RETRY_WAIT_SECONDS)
            await send_next_question(context, chat_id)  # retry
        else:
            # cancel this group's quiz and inform OWNER_ID
            try:
                group_name = await get_group_name(context.bot, chat_id)
                await context.bot.send_message(OWNER_ID, f"⚠ Quiz cancelled in group *{group_name}* ({chat_id}) after {retries} failed attempts.", parse_mode="Markdown")
            except:
                pass
            # cleanup this group's state, but keep other groups unaffected
            active_quiz_state.pop(chat_id, None)
        return

async def next_question_callback(context: ContextTypes.DEFAULT_TYPE):
    # Extract chat_id from job data
    job_data = context.job.data
    if isinstance(job_data, dict):
        chat_id = job_data.get('chat_id')
    else:
        chat_id = job_data  # fallback

    if not chat_id:
        return

    state = active_quiz_state.get(chat_id)
    if not state or not state.get('started'):
        return

    # सबसे ज़रूरी चेक: अगर ग्रुप paused है तो index बिल्कुल मत बढ़ाओ!
    if chat_id in paused_groups:
        logger.info(f"next_question_callback blocked — group {chat_id} is paused. Next question delayed.")
        return

    # अब बेफिक्र होकर index बढ़ाओ और अगला सवाल भेजो
    state['index'] += 1
    await send_next_question(context, chat_id)

# -------------------------
# Poll answer handler
# -------------------------
async def poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:
        # Telegram object
        pa = update.poll_answer  # PollAnswer
        poll_id = pa.poll_id
        option_ids = pa.option_ids  # list (usually single element for quiz)
        user = pa.user
        user_id = user.id
        user_name = user.full_name or str(user_id)

        # identify group and quiz for this poll
        group_id = poll_to_group.get(poll_id)
        quiz_id = poll_to_quiz.get(poll_id)

        if group_id is None:
            # unknown poll -> ignore
            logger.debug(f"poll_answer: unknown poll_id {poll_id} by {user_id}")
            return

        state = active_quiz_state.get(group_id)
        if not state or not state.get('started'):
            # quiz not active / was cancelled -> ignore
            logger.info(f"poll_answer: received for group {group_id} but no active state")
            return

        # determine question index that this poll corresponded to
        # send_next_question used q_index_local = state['index'] at send time,
        # so the current question index for answers is state['index'] (same)
        question_idx = state.get('index', 0)

        # ensure we have an answers map to prevent duplicates:
        answers = state.setdefault('answers', {})  # question_idx -> { user_id: info }
        q_answers = answers.setdefault(question_idx, {})

        # if user already answered this question (double event), ignore
        if user_id in q_answers:
            logger.debug(f"poll_answer: user {user_id} already answered q{question_idx} in group {group_id}")
            return

        # time taken calc
        sent_ts = poll_sent_time.get(poll_id)
        now_ts = datetime.now(tz=timezone.utc).timestamp()
        time_taken = None
        if sent_ts is not None:
            time_taken = max(0.0, now_ts - sent_ts)
        else:
            # fallback if missing
            time_taken = 0.0

        # get question object & correctness
        quiz_meta = state.get('quiz_meta', {})
        questions = quiz_meta.get('questions', [])
        q_order = state.get('questions_order', [])
        # derive original question object safely
        try:
            q_obj = questions[q_order[question_idx]]
        except Exception:
            # fallback: try by index
            try:
                q_obj = questions[question_idx]
            except Exception:
                q_obj = {}

        correct_idx = q_obj.get('correct')

        # chosen option (handle multi-select defensively)
        chosen_idx = option_ids[0] if option_ids else None
        is_correct = (chosen_idx is not None and correct_idx is not None and chosen_idx == correct_idx)

        # initialize user_stats & scores
        user_stats = state.setdefault('user_stats', {})
        try:
            chat_info = await context.bot.get_chat(group_id)
            group_name = chat_info.title or "Unknown Group"
        except:
            group_name = "Unknown Group"
        user_rec = user_stats.setdefault(user_id, {
            'name': user_name,
            'correct': 0,
            'incorrect': 0,
            'total_time': 0.0,
            'last_answer_at': None,
            'user_group': group_name
        })

        # update counts
        if is_correct:
            user_rec['correct'] += 1
            # score: +1 per correct by default
            scores = state.setdefault('scores', {})
            scores[user_id] = scores.get(user_id, 0) + 1
        else:
            user_rec['incorrect'] += 1

        user_rec['total_time'] = user_rec.get('total_time', 0.0) + time_taken
        user_rec['last_answer_at'] = datetime.now(tz=timezone.utc).isoformat()

        # mark answered to prevent duplicates
        q_answers[user_id] = {
            'chosen': chosen_idx,
            'is_correct': is_correct,
            'time_taken': time_taken
        }

        logger.debug(f"poll_answer: group {group_id} q{question_idx} user {user_id} correct={is_correct} t={time_taken}")

    except Exception as e:
        logger.error(f"Exception in poll_answer: {e}")


async def _end_quiz_for_group(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    try:
        state = active_quiz_state.get(chat_id)
        if not state:
            return

        quiz_id = state.get("quiz_id")
        quiz_meta = state.get("quiz_meta", {})
        title = quiz_meta.get("title", "Untitled Quiz")

        # -----------------------------
        # BUILD this group's leaderboard
        # -----------------------------
        user_stats = state.get("user_stats", {}) or {}

        per_group_list = []
        for uid, rec in user_stats.items():
            per_group_list.append({
                "user_id": uid,
                "name": rec.get("name", str(uid)),
                "correct": rec.get("correct", 0),
                "incorrect": rec.get("incorrect", 0),
                "total_time": rec.get("total_time", 0.0),
                "user_group": rec.get("user_group", "Unknown")
            })

        # sort ranking (local)
        per_group_sorted = sorted(
            per_group_list,
            key=lambda r: (-r["correct"], r["total_time"])
        )

        # -------------------------
        # FUNCTION: Leaderboard UI
        # -------------------------
        def build_ui(title_str, data_list, multi_attempt_users=None):
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}

            text = (
                f"📊 *Qumtta-Leaderboard*\n"
                f"🏷️ *Quiz:* {title_str}\n\n"
            )

            if not data_list:
                return text + "No participants."

            for rank, e in enumerate(data_list, start=1):
                medal = medals.get(rank, f"#{rank}")
                text += (
                    f"{medal} *{e['name']}* _({e.get('user_group', 'Unknown')})_\n"
                    f"   ✅ {e['correct']}   ❌ {e['incorrect']}   ⏱️ {round(e['total_time'],1)}s\n\n"
                )

            if multi_attempt_users:
                text += "⚠️ *Multiple Attempts Detected:*\n"
                for u in multi_attempt_users:
                    text += f"• {u}\n"
                text += "\n"

            text += "— *Your Qumtta Quiz Bot* 🤖"
            return text

        # ==========================================================
        #       CHECK MODE → is this ALL MODE?
        # ==========================================================
        is_all_mode = quiz_id in all_mode_tracking

        # ========== SINGLE MODE ==========
        if not is_all_mode:

            # 1) Send leaderboard to this group
            try:
                ui = build_ui(title, per_group_sorted)
                await context.bot.send_message(chat_id, ui, parse_mode="Markdown")
            except Exception:
                pass

            # 2) Send OWNER summary
            try:
                chat_info = await context.bot.get_chat(chat_id)
                gname = chat_info.title or str(chat_id)
                summary = (
                    f"📌 *Quiz Summary (Single Mode)*\n"
                    f"🏷️ Quiz: *{title}*\n"
                    f"👥 Group: `{gname}`\n"
                    f"👤 Participants: *{len(per_group_list)}*\n"
                )
                await context.bot.send_message(OWNER_ID, summary, parse_mode="Markdown")
            except:
                pass

            # cleanup
            active_quiz_state.pop(chat_id, None)
            return

        # ========== ALL MODE ==============
        tracking = all_mode_tracking.get(quiz_id)
        if not tracking:
            active_quiz_state.pop(chat_id, None)
            return

        tracking["data"][chat_id] = user_stats
        tracking["completed"].add(chat_id)

        # delete group state
        active_quiz_state.pop(chat_id, None)

        # Notify group
        try:
            await context.bot.send_message(
                chat_id,
                "🎉 *Thank you, everyone!* 🎉\n\n"
                "Your enthusiasm made this quiz truly exciting!\n"
                "⌛ *Please wait…*\n"
                "Other groups are still finishing their quizzes.\n\n"
                "— *Your Qumtta Quiz Bot* 🤖",
                parse_mode="Markdown"
            )
        except:
            pass

        expected = tracking.get("expected", set())
        expected = {g for g in expected if g in ACTIVE_GROUPS}
        completed = tracking.get("completed", set())

        # If not all groups done → wait
        if completed < expected:
            return

        # ==========================================================
        #     ALL GROUPS finished → make COMBINED leaderboard
        # ==========================================================
        combined_map = {}
        user_group_count = {}

        for gid, gdata in tracking["data"].items():
            for uid, rec in gdata.items():
                if uid not in combined_map:
                    combined_map[uid] = {
                        "user_id": uid,
                        "name": rec["name"],
                        "correct": rec["correct"],
                        "incorrect": rec["incorrect"],
                        "total_time": rec["total_time"],
                        "user_group": rec.get("user_group", "Unknown")
                    }
                else:
                    # accumulate cross-group attempts
                    combined_map[uid]["correct"] += rec["correct"]
                    combined_map[uid]["incorrect"] += rec["incorrect"]
                    combined_map[uid]["total_time"] += rec["total_time"]

                user_group_count.setdefault(uid, set()).add(gid)

        # -------------------------
        # MULTIPLE ATTEMPT USERS
        # -------------------------
        multi_users = [ combined_map[u]["name"] for u, groups in user_group_count.items() if len(groups) > 1 ]

        # remove multi-users from ranking
        ranking_list = [
            v for u, v in combined_map.items()
            if len(user_group_count[u]) == 1
        ]

        ranking_sorted = sorted(
            ranking_list,
            key=lambda r: (-r["correct"], r["total_time"])
        )

        # FINAL UI (combined)
        combined_ui = build_ui(title, ranking_sorted, multi_attempt_users=multi_users)

        # send leaderboard to all groups
        for gid in expected:
            try:
                await context.bot.send_message(gid, combined_ui, parse_mode="Markdown")
            except:
                pass

        # OWNER SUMMARY MESSAGE
        try:
            summary = (
                f"📌 *Quiz Summary (ALL MODE)*\n"
                f"🏷️ Quiz: *{title}*\n\n"
                f"📍 *Group Participation:*\n"
            )
            total_unique = len(combined_map)

            for gid in expected:
                count = len(tracking["data"].get(gid, {}))

                chat_info = await context.bot.get_chat(gid)
                group_name = chat_info.title or str(gid)

                summary += f"• *{group_name}* → *{count}* participants\n"


            summary += f"\n👤 *Total Unique Users:* {total_unique}\n"
            summary += f"⚠️ *Multiple Attempts:* {len(multi_users)} Users"

            await context.bot.send_message(OWNER_ID, summary, parse_mode="Markdown")
        except:
            pass

        # cleanup
        all_mode_tracking.pop(quiz_id, None)

    except Exception as e:
        logger.error(f"_end_quiz_for_group ERROR: {e}")

# ========== UPDATED start_quiz_command: list quiz_store titles as inline buttons 
async def start_quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if user.id not in ADMIN_IDS or chat.id not in ACTIVE_GROUPS:
        return

    if not quiz_store:
        await update.message.reply_text("⚠️ No quiz loaded in quiz_store. Load a quiz first.")
        return

    # build buttons: one per quiz entry
    buttons = []
    for qid, q in quiz_store.items():
        title = q.get('title', qid)
        buttons.append([InlineKeyboardButton(title, callback_data=f"start_quiz_now:{qid}")])

    keyboard = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("Choose quiz to start (single mode):", reply_markup=keyboard)


# ========== NEW CALLBACK: start_quiz_now_cb ==========
async def start_quiz_now_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat     # ⚡ यही group इस्तेमाल होगा

    await query.answer()

    # 1) Admin check
    if user.id not in ADMIN_IDS:
        return await query.answer("❌ Unauthorized!", show_alert=True)

    # 2) Group must be ACTIVE
    if chat.id not in ACTIVE_GROUPS:
        return await query.answer("❌ This group is not authorized for quiz!", show_alert=True)

    # 3) Fetch quiz
    _, quiz_id = query.data.split(":", 1)
    quiz = quiz_store.get(quiz_id)
    if not quiz:
        return await query.answer("⚠ Quiz not found in storage!", show_alert=True)

    # Inform admin
    await query.edit_message_text("Starting quiz now in this group...")

    # Quiz metadata
    title = quiz.get("title", "Untitled")
    total_q = len(quiz.get("questions", []))
    timer = quiz.get("timer", 30)

    intro_text = (
        "‼️ *Welcome to Qumtta World!* ‼️\n"
        "⚜ *I am Your Qumtta Quiz Bot* ⚜\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📘 *Quiz Title:* {title}\n"
        f"❓ *Total Questions:* {total_q}\n"
        f"⏱ *Timer:* {timer} sec/question\n"
        f"🎛 *Mode:* Qumtta World Mode\n"
        f"🎛 *Quiz will start within 45 sec*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "👇 *Join :- Qumtta World* 👇"
    )

    join_button = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Join Qumtta World", url="https://t.me/+e0yQys0Dvf5lNGRl")]
    ])

    # ⭐ Intro goes to SAME GROUP where user tapped button
    try:
        msg = await context.bot.send_message(
            chat_id=chat.id,
            text=intro_text,
            parse_mode="Markdown",
            reply_markup=join_button
        )
    except Exception as e:
        logger.error(f"Failed to send start intro in group: {e}")
        return
    await asyncio.sleep(10)
    
    # ⭐ Start quiz in SAME ACTIVE GROUP
    await _init_and_start_quiz_in_group(context, chat.id, quiz)

async def refresh_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only command to safely restart the bot and confirm health."""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ You are not authorized to refresh the bot.")
        return
    await update.message.reply_text("♻️ Restarted bot.")
    def delayed_restart():
        time.sleep(3)
        os.execl(sys.executable, sys.executable, *sys.argv)
    threading.Thread(target=delayed_restart, daemon=True).start()

async def notify_admin_new_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """जब कोई नया यूजर प्राइवेट में /start करे तो Admin को डिटेल्स भेजो - सिर्फ़ पहली बार"""
    user = update.effective_user
    if update.effective_chat.type != "private":
        return
  
    # अगर पहले नोटिफाई कर चुके हैं तो दोबारा न भेजो
    if context.user_data.get('notified', False):
        return
    context.user_data['notified'] = True
  
    info_text = (
        "🔔 *नया यूजर ने Bot स्टार्ट किया!*\n\n"
        f"👤 नाम: {user.full_name}\n"
        f"🆔 User ID: `{user.id}`\n"
        f"📛 Username: @{user.username if user.username else 'None'}\n"
        f"🔗 प्रोфाइल: [यहाँ क्लिक करें](tg://user?id={user.id})\n"
        f"⏰ समय: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=info_text,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Failed to notify admin about new user: {e}")

async def notify_admin_new_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """जब Bot को किसी नए ग्रुप में जोड़ा जाए तो Admin को ग्रुप लिंक भेजो - सिर्फ़ Bot के लिए"""
    if not update.message or not update.message.new_chat_members:
        return
  
    bot_user = await context.bot.get_me()
    bot_added = any(member.id == bot_user.id for member in update.message.new_chat_members)
  
    if not bot_added:
        return # सिर्फ़ Bot को ऐड किया हो तभी नोटिफाई करो
  
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return
    ACTIVE_GROUPS.add(chat.id)
    save_db()
    # Invite link जनरेट करने की कोशिश
    try:
        invite_link = await context.bot.export_chat_invite_link(chat_id=chat.id)
    except Exception as e:
        invite_link = f"(लिंक नहीं मिला: {str(e)})"
    try:
        member_count = await chat.get_member_count()
    except:
        member_count = "N/A"
    info_text = (
        "🔔 *Bot को नए ग्रुप में जोड़ा गया!*\n\n"
        f"🏘️ ग्रुप नाम: {chat.title}\n"
        f"🆔 ग्रुप ID: `{chat.id}`\n"
        f"🔗 इनवाइट लिंक: {invite_link}\n"
        f"👥 मेंबर्स: {member_count}\n"
        f"⏰ समय: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=info_text,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Failed to notify admin about new group: {e}")

# 1. LIST ALL ACTIVE GROUPS
async def list_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Unauthorized")
        return
    if not ACTIVE_GROUPS:
        await update.message.reply_text("Koi active group nahi hai.")
        return
    text = "*Active Groups:*\n\n"
    for i, gid in enumerate(sorted(ACTIVE_GROUPS), 1):
        try:
            chat = await context.bot.get_chat(gid)
            member_count = await context.bot.get_chat_member_count(gid)
            text += f"{i}. `{gid}`\n ➤ {chat.title}\n ➤ Members: {member_count}\n\n"
        except:
            text += f"{i}. `{gid}` → (Access lost / deleted)\n\n"
  
    await update.message.reply_text(text, parse_mode="Markdown")

async def remove_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Unauthorized")
        return
  
    if not context.args:
        await update.message.reply_text("Usage: /rm_group <group_id>\nYa phir reply karke group id bhejo.")
        return
  
    try:
        group_id = int(context.args[0])
    except:
        await update.message.reply_text("Invalid group ID.")
        return
  
    if group_id not in ACTIVE_GROUPS:
        await update.message.reply_text("Ye group active list mein nahi hai.")
        return
  
    # Bot ko group se nikaalo
    try:
        await context.bot.leave_chat(group_id)
    except:
        pass
  
    ACTIVE_GROUPS.remove(group_id)
    save_db()
    await update.message.reply_text(f"Bot ne group chhoda aur list se hata diya:\n`{group_id}`")

@admin_only
async def pause_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_paused, paused_groups

    if is_paused:
        await update.message.reply_text("Quiz पहले से paused है!")
        return

    is_paused = True
    paused_groups.clear()

    active_count = 0
    for chat_id, state in active_quiz_state.items():
        if state.get('started'):
            paused_groups.add(chat_id)
            active_count += 1

    if active_count == 0:
        await update.message.reply_text("कोई quiz नहीं चल रहा।")
        is_paused = False
    else:
        await update.message.reply_text(
            f"⏸️ Quiz PAUSED!\n\n"
            f"जो सवाल अभी चल रहा है — वो पूरा होने दो।\n"
            f"उसके बाद कोई नया सवाल नहीं आएगा।\n"
            f"{active_count} ग्रुप्स affected।"
        )

        # सभी ग्रुप्स को मैसेज भेजो
        for chat_id in paused_groups:
            try:
                await context.bot.send_message(
                    chat_id,
                    "⏸️ *Admin ने Quiz Pause कर दिया है!*\n\n"
                    "वर्तमान सवाल का जवाब देने के बाद अगला सवाल नहीं आएगा।\n"
                    "जल्द ही Resume किया जाएगा...",
                    parse_mode="Markdown"
                )
            except:
                pass


@admin_only
async def resume_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_paused, paused_groups

    if not is_paused:
        await update.message.reply_text("Quiz paused नहीं है!")
        return

    is_paused = False
    resume_count = len(paused_groups)

    if resume_count == 0:
        await update.message.reply_text("कोई paused quiz नहीं मिला।")
        return

    # हर ग्रुप में resume मैसेज + अगला सवाल 5-8 सेकंड बाद
    for chat_id in paused_groups:
        try:
            delay = random.randint(5, 10)
            await context.bot.send_message(
                chat_id,
                f"Resume हो गया!\n\n"
                f"अगला सवाल {delay} सेकंड में आ रहा है...",
                parse_mode="Markdown"
            )

            # अगला सवाल schedule करो
            context.job_queue.run_once(
                next_question_callback,
                delay,
                data={'chat_id': chat_id},
                name=f"resume_next_{chat_id}_{int(time.time())}"
            )
        except Exception as e:
            logger.error(f"Resume failed in {chat_id}: {e}")

    paused_groups.clear()
    await update.message.reply_text(f"Resume सफल! {resume_count} ग्रुप्स में quiz दोबारा शुरू।")

# -------------------------------------------------
# ADMIN: /stats → total users + total groups
# -------------------------------------------------
@admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_groups = len(ACTIVE_GROUPS)
    total_users = len(active_users) # <-- NEW
    text = (
        "*BOT STATS*\n\n"
        f"*Total Active Groups:* `{total_groups}`\n"
        f"*Total Users (started bot):* `{total_users}`\n"
        "— Qumtta Quiz Bot"
    )
    await update.message.reply_text(text, parse_mode="Markdown")
# -------------------------------------------------
# ADMIN: /exdb → export DB (groups + users) to JSON
# -------------------------------------------------
@admin_only
async def export_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt_content = f"""QUMTTA BOT DB BACKUP
Timestamp: {int(datetime.now(tz=timezone.utc).timestamp())}
=== GROUPS ===
{chr(10).join(map(str, sorted(ACTIVE_GROUPS)))}
=== USERS ===
{chr(10).join(map(str, sorted(active_users)))}
"""
    bio = io.BytesIO(txt_content.encode("utf-8"))
    timestamp = int(datetime.now(tz=timezone.utc).timestamp())
    bio.name = f"qumtta_db_{timestamp}.txt"
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=InputFile(bio, filename=bio.name),
        caption="DB Backup (.txt) - Use /updb to restore"
    )

@admin_only
async def sch_quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not scheduled_quizzes:
        await update.message.reply_text("No quizzes are currently scheduled.")
        return
    now_ist = datetime.now(tz=timezone(timedelta(hours=5, minutes=30)))
    text = "*Scheduled Quizzes (IST)*\n\n"
    for i, sch in enumerate(sorted(scheduled_quizzes, key=lambda x: x['start_ist']), 1):
        start = sch['start_ist']
        mins_left = int((start - now_ist).total_seconds() // 60)
        duration_min = sch['duration_sec'] // 60
        status = "Starting soon" if mins_left <= 0 else f"{mins_left} min left"
        mode_display = "Qumtta World Mode" if sch['mode'] == "single" else "All Group Mode"
        text += (
            f"{i}. *{sch['title']}*\n"
            f" Start: `{start.strftime('%H:%M %d %b')}`\n"
            f" Mode: `{mode_display}`\n"
            f" Duration: ~`{duration_min}` min\n"
            f" Status: `{status}`\n\n"
        )
    text += "_-Your Qumtta Quiz Bot_ 🤖"
    await update.message.reply_text(text, parse_mode="Markdown")

async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Only OWNER can add admin
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Only the owner can add admins.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /add_admin <user_id>")
        return

    try:
        uid = int(context.args[0])
    except:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    ADMIN_IDS.add(uid)
    save_db()
    await update.message.reply_text(f"✅ User `{uid}` added as admin.", parse_mode="Markdown")

async def remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Only the owner can remove admins.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /rm_admin <user_id>")
        return

    try:
        uid = int(context.args[0])
    except:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    if uid in ADMIN_IDS:
        ADMIN_IDS.remove(uid)
        save_db()
        await update.message.reply_text(f"🗑️ Removed admin `{uid}`", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ That user is not an admin.")

def start_self_ping_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(self_ping())
   

app = Flask(__name__)

@app.route("/")
def home():
    return "Qumtta Quiz Bot is ALIVE!", 200

def run_flask():
    app.run(host="0.0.0.0", port=8081)
async def self_ping():
    url = "https://qumtta-quiz-bot.onrender.com/"
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    print("Self-Ping OK", resp.status)
        except Exception as e:
            print("Self-Ping failed:", e)
        
        await asyncio.sleep(600)
# ---------------------------
# TELEGRAM BOT (WEBHOOK MODE)
# ---------------------------
def main():
    """Start the bot — Webhook Mode"""
    from telegram.ext import ApplicationBuilder

    # ====================== BUILD APPLICATION ======================
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    load_db()   # ← ये लाइन जोड़ो
    application.job_queue.run_repeating(save_db, interval=300, first=10)

    # ====================== PUBLIC COMMANDS ======================
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, notify_admin_new_group))
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Command('start'), notify_admin_new_user), group=1)

    # ====================== TEXT-BASED QUIZ CREATOR ======================
    conv = ConversationHandler(
        entry_points=[CommandHandler('createviatxt', create_quiz)],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, title_received)],
            POLL_SETTINGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, poll_settings_received)],
            QUESTIONS: [
                MessageHandler(filters.Regex('^/done$') & filters.ChatType.PRIVATE, done_questions),
                MessageHandler(filters.Regex('^/cancel$') & filters.ChatType.PRIVATE, cancel_or_undo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, questions_received),
            ],
            CORRECT_ANSWERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, correct_answers_received)],
        },
        fallbacks=[CommandHandler('cancel', cancel_or_undo)],
        allow_reentry=True,
    )
    application.add_handler(conv)

    # ====================== POLL-BASED CREATOR ======================
    poll_conv = ConversationHandler(
        entry_points=[CommandHandler("createviapoll", create_via_poll)],
        states={
            POLL_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, poll_title)],
            POLL_TIMER: [MessageHandler(filters.TEXT & ~filters.COMMAND, poll_timer)],
            POLL_COLLECT: [
                MessageHandler(filters.POLL, poll_collect),
                MessageHandler(filters.Regex("^/done$"), poll_done),
            ],
            POLL_CORRECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, poll_correct_answer)],
        },
        fallbacks=[CommandHandler("cancel", poll_cancel)],
        allow_reentry=True,
    )
    application.add_handler(poll_conv)

    # ====================== ADMIN ONLY COMMANDS ======================
    application.add_handler(CommandHandler('start_quiz', admin_only(start_quiz_command)))
    application.add_handler(CommandHandler('pause', admin_only(pause_quiz)))
    application.add_handler(CommandHandler('resume', admin_only(resume_quiz)))
    application.add_handler(CommandHandler('refresh', admin_only(refresh_bot)))
    application.add_handler(CommandHandler('group', admin_only(list_groups)))
    application.add_handler(CommandHandler('rm_group', admin_only(remove_group)))
    application.add_handler(CommandHandler('stats', stats_command))
    application.add_handler(CommandHandler('broadcast', broadcast_command))
    application.add_handler(CommandHandler('exdb', export_db))
    application.add_handler(CommandHandler('sch_quiz', sch_quiz_command))

    # ====================== DOCUMENT HANDLER ======================
    application.add_handler(MessageHandler(
        filters.Document.ALL & filters.ChatType.PRIVATE,
        admin_only(handle_document)
    ))

    # ====================== CALLBACKS & OTHER HANDLERS ======================
    application.add_handler(PollAnswerHandler(poll_answer))
    application.add_handler(CallbackQueryHandler(start_quiz_button_cb, pattern=r'^start_quiz:'))
    application.add_handler(CallbackQueryHandler(start_all_cb, pattern=r'^start_all:'))
    application.add_handler(CommandHandler("stop_poll", stop_poll_command))
    application.add_handler(CommandHandler("add_admin", add_admin))
    application.add_handler(CommandHandler("rm_admin", remove_admin))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Regex(r'^\d{1,2}:\d{2}$'), admin_time_handler))
    application.add_handler(CallbackQueryHandler(start_quiz_now_cb, pattern=r"^start_quiz_now:"))
    logger.info("Qumtta Quiz Bot started in WEBHOOK mode...")

    # ====================== START WEBHOOK SERVER ======================
    application.run_webhook(
        listen="0.0.0.0",
        port=8080,  # Render main port
        url_path=BOT_TOKEN,
        webhook_url=f"https://qumtta-quiz-bot.onrender.com/{BOT_TOKEN}"
    )


# ---------------------------
# APP START POINT
# ---------------------------
if __name__ == "__main__":
    print("Starting Health Server (Flask on 8081)...")
    threading.Thread(target=run_flask, daemon=True).start()

    print("Starting Self-Ping Service...")
    threading.Thread(target=start_self_ping_loop, daemon=True).start()

    print("Starting Qumtta Quiz Bot in Webhook Mode...")
    main()


