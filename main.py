import io
import json
import asyncio
import logging
import threading
import os
import sys
import time
import requests
from flask import Flask
from typing import List, Dict, Any, Set
from datetime import datetime, timezone
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
ADMIN_ID = 7370025284
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
# -----------------------------
# GLOBAL RUNTIME DATA
# -----------------------------
active_users: Set[int] = set()          # <-- NEW: every /start
quiz_completed = False
questions_sent_per_group: Dict[int, int] = {}
current_quiz: Dict[str, Any] = None
scores: Dict[int, int] = {}
correct_options: Dict[str, int] = {}
poll_sent_time: Dict[str, float] = {}
user_stats: Dict[int, Dict[str, Any]] = {}
readiness: Dict[str, Set[int]] = {}
readiness_message_ids: Dict[str, Dict[int, int]] = {} # quiz_id -> {group_id: message_id}
readiness_quiz_map: Dict[str, Dict[str, Any]] = {}
is_paused = False
is_stopped = False
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
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("‼️Unauthorised Access‼️")
            return
        return await func(update, context)
    return wrapper
# /stats — TOTAL USERS + GROUPS + QUIZZES
@admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_groups = len(ACTIVE_GROUPS)
    total_users = len(user_stats) # Jo log ne quiz khela
    text = (
        "*BOT STATS*\n\n"
        f"*Total Active Groups:* `{total_groups}`\n"
        f"*Total Unique Users:* `{total_users}`\n"
        "— Qumtta Quiz Bot"
    )
    await update.message.reply_text(text, parse_mode="Markdown")
# /broadcast <message> — SAB GROUPS MEIN MSG BHEJO
@admin_only
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <your message>")
        return
    message = " ".join(context.args)
    success = 0
    failed = 0
    for gid in ACTIVE_GROUPS:
        try:
            await context.bot.send_message(gid, f"*BROADCAST:*\n\n{message}", parse_mode="Markdown")
            success += 1
        except Exception as e:
            failed += 1
            logger.error(f"Broadcast failed in {gid}: {e}")
    await update.message.reply_text(
        f"Broadcast Complete!\n"
        f"Sent: `{success}` groups\n"
        f"Failed: `{failed}` groups"
    )
def build_start_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("/start")], [KeyboardButton("/createviatxt")], [KeyboardButton("/createviapoll")],  [KeyboardButton("/done")], [KeyboardButton("/cancel")]],
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
    chat = update.effective_chat
    if user.id == ADMIN_ID:
        text = (
            "👋 नमस्ते! यह Quiz Bot है. नीचे दिए कमांड से शुरू करें:\n\n"
            "/createviatxt or /createviapoll — एक नया क्विज बनाएँ (DM में, केवल admin).\n"
            "/start_quiz — लोड किया हुआ क्विज ग्रुप में चलाएँ (केवल admin और configured group).\n"
            "/cancel — वर्तमान ऑपरेशन रद्द करें.\n\n"
            "क्विज बनाने का नया फ्लो:\n"
            "1️⃣ टाइटल पूछेगा.\n"
            "2️⃣ फिर Poll settings (तीन लाइनें): option_count, option_texts comma-separated, timer in seconds.\n"
            "3️⃣ प्रश्न भेजें — एक ही संदेश में कई प्रश्न भेज सकते हैं; प्रश्नों के बीच एक खाली लाइन रखें.\n"
            "4️⃣ /done के बाद correct answers comma-separated भेजें.\n"
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
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌Unauthorised Access.")
        return
    # initialize storage
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
    # parse
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
    # store
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
    # add and keep chunk info for undo
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
        # remove last chunk from questions
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
        await update.message.reply_text(f"प्रश्नों की संख्या {len(questions)} है पर आपने {len(tokens)} उत्तर दिए. दोनों बराबर होने चाहिए.")
        return CORRECT_ANSWERS
    option_texts = context.user_data['option_texts']
    # Map tokens to indices. Accept tokens that are either exact option text (e.g., 'A') or letter labels like A,B,C
    def token_to_index(tok: str) -> int:
        # try match by exact option text (case-insensitive)
        for i, opt in enumerate(option_texts):
            if tok.lower() == opt.lower():
                return i
        # try letter label A,B,C... or numbers 1,2,3
        if len(tok) == 1 and tok.isalpha():
            idx = ord(tok.upper()) - ord('A')
            if 0 <= idx < len(option_texts):
                return idx
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
    # Build quiz structure
    quiz = {
        'title': context.user_data['title'],
        'option_count': context.user_data['option_count'],
        'option_texts': context.user_data['option_texts'],
        'timer': context.user_data['timer'],
        'questions': [],
        # will add leaderboard later
    }
    for q_text, correct_idx in zip(context.user_data['questions'], correct_indices):
        quiz['questions'].append({
            'text': q_text,
            'options': context.user_data['option_texts'],
            'correct': correct_idx,
            'timer': context.user_data['timer']
        })
    # Save to current_quiz (global) so it can be started in group
    global current_quiz
    quiz_id = str(int(datetime.now(tz=timezone.utc).timestamp()))
    quiz['quiz_id'] = quiz_id
    current_quiz = quiz
    # Save snapshot in readiness_quiz_map so it persists even after quiz run
    readiness_quiz_map[quiz_id] = quiz
    # send json file back AND send action message with buttons (Start Quiz / Publish Result)
    safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in context.user_data['title'])
    filename = f"{safe_title}.json"
    await send_json_file_to_user(update.effective_chat.id, context, quiz, filename=filename)
    # prepare inline buttons - keep these persistent (don't edit them away later)
    # Sirf last part change (4 buttons)
    buttons = [
        [
            InlineKeyboardButton("Start Quiz", callback_data=f"start_quiz:{quiz_id}"),
            InlineKeyboardButton("Publish Result", callback_data=f"publish_result:{quiz_id}")
        ],
        [
            InlineKeyboardButton("Start in All Groups", callback_data=f"start_all:{quiz_id}"),
            InlineKeyboardButton("Publish in All Groups", callback_data=f"publish_all:{quiz_id}")
        ]
    ]
    await update.message.reply_text(
        "✅ Quiz saved. नीचे से आगे की कार्रवाई करें:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    context.user_data.clear()
    return ConversationHandler.END
# -----------------------------
# Handle uploaded JSON (alternative flow)
# -----------------------------
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private':
        return
    if update.effective_user.id != ADMIN_ID:
        return

    document = update.message.document
    if not document.file_name.endswith('.json'):
        await update.message.reply_text("Please send a valid .json quiz file.")
        return

    # === SKIP DB BACKUP FILES ===
    if document.file_name.startswith("qumtta_db_"):
        await update.message.reply_text(
            "This is a DB backup file. Use /updb to restore.\n"
            "Quiz JSON files should contain 'title' and 'questions'."
        )
        return

    # === Proceed only if it's a quiz JSON ===
    file = await document.get_file()
    byte_array = await file.download_as_bytearray()
    try:
        data = json.loads(byte_array.decode("utf-8"))

        # Validate quiz structure
        if "title" not in data or "questions" not in data:
            await update.message.reply_text("Invalid quiz JSON: Missing 'title' or 'questions'.")
            return

        global current_quiz
        current_quiz = data
        if 'quiz_id' not in current_quiz:
            current_quiz['quiz_id'] = str(int(datetime.now(tz=timezone.utc).timestamp()))
        quiz_id = current_quiz['quiz_id']
        readiness_quiz_map[quiz_id] = current_quiz

        buttons = [
            [
                InlineKeyboardButton("Start Quiz", callback_data=f"start_quiz:{quiz_id}"),
                InlineKeyboardButton("Publish Result", callback_data=f"publish_result:{quiz_id}")
            ],
            [
                InlineKeyboardButton("Start in All Groups", callback_data=f"start_all:{quiz_id}"),
                InlineKeyboardButton("Publish in All Groups", callback_data=f"publish_all:{quiz_id}")
            ]
        ]
        await update.message.reply_text(
            "Quiz loaded from JSON. Use buttons to start or publish:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except json.JSONDecodeError:
        await update.message.reply_text("Invalid JSON file.")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

# -----------------------------
# CALLBACKS: Start Quiz flow / readiness
# -----------------------------
async def start_quiz_button_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() # acknowledge callback
    data = query.data # start_quiz:quiz_id
    _, quiz_id = data.split(':', 1)
    if update.effective_user.id != ADMIN_ID:
        # inform user via alert but DO NOT edit original admin message
        await query.answer(text="❌Unauthorised Access", show_alert=True)
        return
    quiz = readiness_quiz_map.get(quiz_id)
    if not quiz:
        # still inform admin but keep buttons visible
        await query.answer(text="Quiz data not found. Please upload or create quiz first.", show_alert=True)
        return
    # set current_quiz global so runner will use it
    global current_quiz
    current_quiz = quiz
    # send preparatory message to group
    title = quiz.get('title', 'Untitled')
    total_q = len(quiz.get('questions', []))
    timer = quiz.get('timer')
    text = (
        "‼️ *Welcome to Qumtta World!* ‼️\n"
        "⚜ *I am Your Qumtta Quiz Bot* ⚜\n\n"
        f"*Quiz Title:* {title}\n"
        f"*No. of Questions:* {total_q}\n"
        f"*Timer:* {timer} seconds\n\n"
        "*Note:-*\n"
        "1️⃣ Leaderboard will be prepared on the basis of your *first attempt only.*\n\n"
        "📢 *Quiz Timings:*\n"
        "💻 *English Vocab:*\n"
        "🕧 08:00 PM\n\n"
        "💻 *English Practice:*\n"
        "🕧 08:15 PM\n\n"
        "💻 *Computer:*\n"
        "🕧 08:45 PM\n\n"
        "👇 *Click below to start the Quiz!*\n"
        "_Minimum 2 participants required to start._"
    )
    # create 'I am ready' button with count
    readiness[quiz_id] = set()
    keyboard = [[InlineKeyboardButton(f"I am ready (0)", callback_data=f"ready:{quiz_id}")]]
    msg = await context.bot.send_message(GROUP_ID, text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    readiness_message_ids[quiz_id] = msg.message_id
    # schedule readiness check after 45 seconds
    context.job_queue.run_once(finalize_readiness, 45, data={'quiz_id': quiz_id, 'initiator': update.effective_user.id})
    # do NOT edit the original admin DM message (keep Start/Publish buttons visible).
    await query.answer(text="✅ Quiz start initiated and posted to group for readiness.")
async def ready_button_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data # ready:quiz_id
    _, quiz_id = data.split(':', 1)
    user_id = update.effective_user.id
    if quiz_id not in readiness:
        # maybe expired
        await query.answer(text="This readiness period has ended.", show_alert=True)
        return
    # toggle
    if user_id in readiness[quiz_id]:
        readiness[quiz_id].remove(user_id)
    else:
        readiness[quiz_id].add(user_id)
    count = len(readiness[quiz_id])
    # update the button label in group message
    message_id = readiness_message_ids.get(quiz_id)
    if message_id:
        try:
            keyboard = [[InlineKeyboardButton(f"I am ready ({count})", callback_data=f"ready:{quiz_id}")]]
            await context.bot.edit_message_reply_markup(chat_id=GROUP_ID, message_id=message_id, reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            logger.exception("Failed to update ready button: %s", e)
    await query.answer(text=f"Ready count: {count}")
async def finalize_readiness(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data
    quiz_id = data['quiz_id']
    initiator = data.get('initiator')
    count = len(readiness.get(quiz_id, set()))
    if count < 2:
        # notify group that not enough participants
        await context.bot.send_message(GROUP_ID, f"⚠️ पर्याप्त प्रतिभागी नहीं मिले ({count}). Quiz शुरू नहीं हुआ.")
        # cleanup readiness status but keep quiz snapshot for later
        readiness.pop(quiz_id, None)
        readiness_message_ids.pop(quiz_id, None)
        return
    # announce countdown
    for n in (3, 2, 1):
        await context.bot.send_message(GROUP_ID, f"{n}...")
        await asyncio.sleep(1)
    await context.bot.send_message(GROUP_ID, "Go! 🎯")
    # start the quiz questions loop
    await send_next_question(context, GROUP_ID)
# -----------------------------
# START QUIZ IN GROUP (direct command fallback)
# -----------------------------
async def start_quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if user.id != ADMIN_ID or chat.id != GROUP_ID:
        return
    global current_quiz, scores, correct_options
    if current_quiz is None:
        # try to pick from readiness_quiz_map if there's any (choose the latest)
        if readiness_quiz_map:
            # pick last inserted quiz
            quiz_id = list(readiness_quiz_map.keys())[-1]
            current_quiz = readiness_quiz_map[quiz_id]
        else:
            await update.message.reply_text("⚠️ No quiz loaded. Please load a quiz JSON in DM first or create one with /createviatxt or /createviapoll.")
            return
    # ensure that the readiness snapshot exists
    quiz_id = current_quiz.get('quiz_id') or str(int(datetime.now(tz=timezone.utc).timestamp()))
    readiness_quiz_map[quiz_id] = current_quiz
    # call start callback behavior: post preparatory message and start readiness
    title = current_quiz.get('title', 'Untitled')
    total_q = len(current_quiz.get('questions', []))
    timer = current_quiz.get('timer')
    text = (
        "‼️ *Welcome to Qumtta World!* ‼️\n"
        "⚜ *I am Your Qumtta Quiz Bot* ⚜\n\n"
        f"*Quiz Title:* {title}\n"
        f"*No. of Questions:* {total_q}\n"
        f"*Timer:* {timer} seconds\n\n"
        "*Note:-*\n"
        "1️⃣ Leaderboard will be prepared on the basis of your *first attempt only.*\n\n"
        "📢 *Quiz Timings:*\n"
        "💻 *English Vocab:*\n"
        "🕧 08:00 PM\n\n"
        "💻 *English Practice:*\n"
        "🕧 08:15 PM\n\n"
        "💻 *Computer:*\n"
        "🕧 08:45 PM\n\n"
        "👇 *Click below to start the Quiz!*\n"
        "_Minimum 2 participants required to start._"
    )
    readiness[quiz_id] = set()
    keyboard = [[InlineKeyboardButton(f"I am ready (0)", callback_data=f"ready:{quiz_id}")]]
    msg = await context.bot.send_message(GROUP_ID, text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    readiness_message_ids[quiz_id] = msg.message_id
    context.job_queue.run_once(finalize_readiness, 45, data={'quiz_id': quiz_id, 'initiator': user.id})
# -----------------------------
# QUESTIONS / POLLS
# -----------------------------
async def send_next_question(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    global correct_options, current_quiz, is_paused, is_stopped, questions_sent_per_group
    if is_stopped or is_paused or current_quiz is None:
        return
    # HAR GROUP KA APNA COUNTER — YE HI SAHI TARIKA HAI
    if chat_id not in questions_sent_per_group:
        questions_sent_per_group[chat_id] = 0
    q_index = questions_sent_per_group[chat_id]
    total_questions = len(current_quiz['questions'])
    # SAB QUESTIONS KHATAM? → END QUIZ
    if q_index >= total_questions:
        await end_quiz(context, chat_id)
        return
    q = current_quiz['questions'][q_index]
    # Question text
    try:
        await context.bot.send_message(chat_id, f"Q{q_index + 1}. {q['text']}")
    except Exception as e:
        logger.error(f"Text failed in {chat_id}: {e}")
    # Poll bhejo
    try:
        message = await context.bot.send_poll(
            chat_id=chat_id,
            question="Choose correct option",
            options=q['options'],
            type=PollType.QUIZ,
            correct_option_id=q['correct'],
            open_period=q['timer'],
            is_anonymous=False,
        )
        poll_id = message.poll.id
        correct_options[poll_id] = q['correct']
        poll_sent_time[poll_id] = datetime.now(tz=timezone.utc).timestamp()
        # COUNTER BADHAO
        questions_sent_per_group[chat_id] += 1
        # NEXT QUESTION SCHEDULE
        context.job_queue.run_once(
            next_question_callback,
            q['timer'] + 2,
            data={'chat_id': chat_id},
            name=f"next_{chat_id}_{q_index}"
        )
    except Exception as e:
        logger.error(f"Poll failed in {chat_id}: {e}")
async def next_question_callback(context: ContextTypes.DEFAULT_TYPE):
    global is_paused, is_stopped, current_quiz
    if is_stopped or is_paused or current_quiz is None:
        return
    # DATA SE CHAT_ID NIKALO (ye line sabse zaroori thi!)
    job_data = context.job.data
    if isinstance(job_data, dict):
        chat_id = job_data.get('chat_id')
    else:
        chat_id = job_data # fallback for old jobs
    if not chat_id:
        return
    # Ab agla question bhejo
    await send_next_question(context, chat_id)
async def poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    poll_id = answer.poll_id
    if poll_id not in correct_options:
        return
    if not answer.option_ids:
        return
    selected_option = answer.option_ids[0]
    correct = correct_options[poll_id]
    user_id = answer.user.id
    # Response time calculate karo
    sent_ts = poll_sent_time.get(poll_id)
    delta = 0.0
    if sent_ts:
        now_ts = datetime.now(tz=timezone.utc).timestamp()
        delta = max(0.0, now_ts - sent_ts)
    # User stats update
    if user_id not in user_stats:
        user_stats[user_id] = {'correct': 0, 'incorrect': 0, 'total_time': 0.0}
    if selected_option == correct:
        user_stats[user_id]['correct'] += 1
        scores[user_id] = scores.get(user_id, 0) + 1
    else:
        user_stats[user_id]['incorrect'] += 1
    user_stats[user_id]['total_time'] += delta
async def end_quiz(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    global current_quiz, scores, correct_options, quiz_completed
    if current_quiz is None or quiz_completed:
        return
    # SIRF EK BAAR HI SAB KUCH KAREGA (pehla group jo khatam karega)
    quiz_completed = True
    quiz_id = current_quiz.get('quiz_id')
    # ====== LEADERBOARD BANAYENGE ======
    previous_leaderboard = current_quiz.get('leaderboard', [])
    existing_users = {e['user_id'] for e in previous_leaderboard}
    entries = []
    for user_id, stats in user_stats.items():
        if user_id in existing_users:
            continue
        name = "Unknown User"
        for gid in ACTIVE_GROUPS:
            try:
                member = await context.bot.get_chat_member(gid, user_id)
                name = member.user.full_name
                break
            except:
                continue
        entries.append({
            'user_id': user_id,
            'name': name,
            'correct': stats['correct'],
            'incorrect': stats['incorrect'],
            'total_time': stats['total_time']
        })
    combined_entries = previous_leaderboard + entries
    combined_entries.sort(key=lambda x: (-x['correct'], x['total_time']))
    current_quiz['leaderboard'] = combined_entries
    readiness_quiz_map[quiz_id] = current_quiz
    # ====== SIRF EK BAAR JSON BHEJO ======
    try:
        await send_json_file_to_user(
            ADMIN_ID, context, current_quiz,
            filename=f"FINAL_RESULT_{quiz_id}.json"
        )
    except Exception as e:
        logger.error(f"JSON send failed: {e}")
    # ====== SIRF EK BAAR THANKS MESSAGE ======
    thanks_text = (
        "🎉 *Thank you, everyone!* 🎉\n\n"
            "Your enthusiasm made this quiz truly exciting!\n"
            "Stay tuned — more fun and challenging quizzes are coming soon. 🏆\n\n"
            "— *Your Qumtta Quiz Bot* 🤖"
    )
    for gid in ACTIVE_GROUPS:
        try:
            await context.bot.send_message(gid, thanks_text, parse_mode="Markdown")
        except:
            pass
    # ====== ADMIN KO FINAL BUTTONS ======
    try:
        buttons = [
            [InlineKeyboardButton("Start Quiz", callback_data=f"start_quiz:{quiz_id}"),
             InlineKeyboardButton("Publish Result", callback_data=f"publish_result:{quiz_id}")],
            [InlineKeyboardButton("Start in All Groups", callback_data=f"start_all:{quiz_id}"),
             InlineKeyboardButton("Publish in All Groups", callback_data=f"publish_all:{quiz_id}")]
        ]
        await context.bot.send_message(
            ADMIN_ID,
            "QUIZ KHATAM! Final leaderboard + JSON bheja gaya.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except:
        pass
    # ====== CLEANUP (ek hi baar) ======
    current_quiz = None
    scores.clear()
    correct_options.clear()
    poll_sent_time.clear()
    user_stats.clear()
    readiness.clear()
    readiness_message_ids.clear()
    questions_sent_per_group.clear() # YE LINE PEHLE SE HAI
    quiz_completed = False
    # YE NAZAR ANDAZ MAT KARNA — YE SABSE ZAROORI HAI
    questions_sent_per_group.clear() # Doosre group ke liye reset
# -----------------------------
# ADMIN CONTROL COMMANDS
# -----------------------------
async def pause_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_paused, is_stopped
    if update.effective_chat.id != GROUP_ID or update.effective_user.id != ADMIN_ID:
        return
    if is_stopped:
        await update.message.reply_text("⚠️ Quiz पहले ही बंद हो चुका है.")
        return
    if is_paused:
        await update.message.reply_text("⏸️ Quiz पहले ही pause है.")
        return
    is_paused = True
    await update.message.reply_text("⏸️ Quiz को pause कर दिया गया है.\nResume करने के लिए /resume भेजें.")
async def resume_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_paused, is_stopped
    if update.effective_chat.id != GROUP_ID or update.effective_user.id != ADMIN_ID:
        return
    if is_stopped:
        await update.message.reply_text("⚠️ Quiz पहले ही बंद किया जा चुका है.")
        return
    if not is_paused:
        await update.message.reply_text("⚠️ Quiz पहले से चल रहा है.")
        return
    is_paused = False
    await update.message.reply_text("▶️ Quiz फिर से शुरू कर दिया गया है.")
    await send_next_question(context, update.effective_chat.id)
# ✅ Publish Result callback — leaderboard with medals + clean JSON return
async def publish_result_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split(":")
    if len(data) < 2:
        await query.edit_message_text("❌ Invalid result data.")
        return
    quiz_id = data[1]
    quiz = readiness_quiz_map.get(quiz_id)
    if not quiz or "leaderboard" not in quiz:
        await query.edit_message_text("⚠️ No leaderboard found for this quiz.")
        return
    leaderboard = quiz["leaderboard"]
    chat_id = quiz.get("group_id", GROUP_ID)
    if not leaderboard:
        await query.edit_message_text("😕 No participants in this quiz.")
        return
    # 🏅 Medal icons
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    # 🏆 Build formatted leaderboard text
    text = (
        f"📊 *Qumtta-Leaderboard*\n"
        f"🏷️ *Quiz Name:* {quiz.get('title', 'Untitled Quiz')}\n\n"
    )
    for rank, e in enumerate(leaderboard, start=1):
        medal = medals.get(rank, f"#{rank}")
        text += (
            f"{medal} *{e['name']}*\n"
            f"✅ Correct: {e['correct']}\n"
            f"❌ Incorrect: {e['incorrect']}\n"
            f"⏱️ Time: {round(e['total_time'], 1)}s\n\n"
        )
    text += "— *Your Qumtta Quiz Bot* 🤖"
    try:
        # 📢 ग्रुप में leaderboard भेजो
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
        )
        # 🔄 JSON को साफ करना (participants डेटा हटाना)
        cleaned_quiz = {
            "quiz_id": quiz.get("quiz_id"),
            "title": quiz.get("title"),
            "questions": quiz.get("questions"),
            "options": quiz.get("options"),
            "timer": quiz.get("timer"),
            "created_by": quiz.get("created_by"),
            "created_at": quiz.get("created_at"),
        }
        # ✅ Admin को confirmation
        await query.edit_message_text("✅ Leaderboard published successfully!\n\nYour Qumtta Quiz Bot 🤖")
    except Exception as e:
        await query.edit_message_text(f"⚠️ Failed to publish leaderboard:\n{e}")
async def refresh_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only command to safely restart the bot and confirm health."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ You are not authorized to refresh the bot.")
        return
    await update.message.reply_text("♻️ Restarted bot.")
    def delayed_restart():
        time.sleep(3)
        os.execl(sys.executable, sys.executable, *sys.argv)
    threading.Thread(target=delayed_restart, daemon=True).start()
app = Flask(__name__)
@app.route('/')
def health():
    return "Qumtta Quiz Bot is ALIVE! 🤖", 200
def run_flask():
    app.run(host='0.0.0.0', port=8081)
def keep_alive():
    url = "https://qumtta-quiz-bot.onrender.com/" # अपना URL डालो
    while True:
        try:
            requests.get(url, timeout=10)
            print("Self-ping sent! Bot is awake.")
        except:
            print("Ping failed...")
        time.sleep(600) # 10 min
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
        f"🔗 प्रोफाइल: [यहाँ क्लिक करें](tg://user?id={user.id})\n"
        f"⏰ समय: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
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
            chat_id=ADMIN_ID,
            text=info_text,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Failed to notify admin about new group: {e}")
async def start_all_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if update.effective_user.id != ADMIN_ID:
        await query.answer("Unauthorized!", show_alert=True)
        return
    _, quiz_id = query.data.split(':', 1)
    quiz = readiness_quiz_map.get(quiz_id)
    if not quiz:
        await query.answer("Quiz not found!", show_alert=True)
        return
    global current_quiz
    current_quiz = quiz
    readiness[quiz_id] = set()
    readiness_message_ids[quiz_id] = {}
    total_groups = len(ACTIVE_GROUPS)
    success_count = 0
    title = quiz.get('title', 'Untitled')
    total_q = len(quiz.get('questions', []))
    timer = quiz.get('timer')
   
    text = (
        "‼️ *Welcome to Qumtta World!* ‼️\n"
        "⚜ *I am Your Qumtta Quiz Bot* ⚜\n\n"
        f"*Quiz Title:* {title}\n"
        f"*No. of Questions:* {total_q}\n"
        f"*Timer:* {timer} seconds\n\n"
        "*Note:-*\n"
        "1️⃣ Leaderboard will be prepared on the basis of your *first attempt only.*\n\n"
        "📢 *Quiz Timings:*\n"
        "💻 *English Vocab:*\n"
        "🕧 08:00 PM\n\n"
        "💻 *English Practice:*\n"
        "🕧 08:15 PM\n\n"
        "💻 *Computer:*\n"
        "🕧 08:45 PM\n\n"
        "👇 *Click below to start the Quiz!*\n"
        "_Minimum 2 participants required to start._"
    )
    for group_id in ACTIVE_GROUPS:
        try:
            keyboard = [[InlineKeyboardButton("I am ready (0)", callback_data=f"ready_all:{quiz_id}")]]
            msg = await context.bot.send_message(
                chat_id=group_id,
                text=text,
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            readiness_message_ids[quiz_id][group_id] = msg.message_id
            success_count += 1
        except Exception as e:
            logger.error(f"Failed to send to {group_id}: {e}")
    if success_count == 0:
        await query.edit_message_text("No groups available!")
        return
    context.job_queue.run_once(
        finalize_multi_readiness,
        50,
        data={'quiz_id': quiz_id, 'initiator': update.effective_user.id}
    )
    await query.edit_message_text(f"Quiz sent to {success_count}/{total_groups} groups!\nWaiting for players...")
async def ready_all_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, quiz_id = query.data.split(':', 1)
    user_id = update.effective_user.id
    if quiz_id not in readiness:
        readiness[quiz_id] = set()
    if user_id in readiness[quiz_id]:
        readiness[quiz_id].remove(user_id)
    else:
        readiness[quiz_id].add(user_id)
    total_ready = len(readiness[quiz_id])
    for group_id, msg_id in readiness_message_ids.get(quiz_id, {}).items():
        try:
            keyboard = [[InlineKeyboardButton(f"I am ready ({total_ready})", callback_data=f"ready_all:{quiz_id}")]]
            await context.bot.edit_message_reply_markup(
                chat_id=group_id,
                message_id=msg_id,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            logger.error(f"Ready button update failed in {group_id}: {e}")
    await query.answer(f"Total Ready: {total_ready}")
async def finalize_multi_readiness(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data
    quiz_id = data['quiz_id']
    total_ready = len(readiness.get(quiz_id, set()))
    if total_ready < 2:
        for group_id in readiness_message_ids.get(quiz_id, {}):
            try:
                await context.bot.send_message(group_id, f"Not enough players ({total_ready}). Quiz cancelled.")
            except:
                pass
        readiness.pop(quiz_id, None)
        readiness_message_ids.pop(quiz_id, None)
        return
    for n in (3, 2, 1):
        for group_id in readiness_message_ids.get(quiz_id, {}):
            try:
                await context.bot.send_message(group_id, f"{n}...")
            except:
                pass
        await asyncio.sleep(1)
    for group_id in readiness_message_ids.get(quiz_id, {}):
        try:
            await context.bot.send_message(group_id, "GO!")
        except:
            pass
    # SAB GROUPS MEIN QUIZ START KARNE KE LIYE
    for group_id in ACTIVE_GROUPS:
        try:
            # Pehla question turant bhejo
            await send_next_question(context, group_id)
            # Aur timer ke baad agla aayega automatically
        except Exception as e:
            logger.error(f"Failed to start quiz in {group_id}: {e}")
async def publish_all_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if update.effective_user.id != ADMIN_ID:
        await query.answer("Unauthorized!", show_alert=True)
        return
    _, quiz_id = query.data.split(':', 1)
    quiz = readiness_quiz_map.get(quiz_id)
    if not quiz or "leaderboard" not in quiz:
        await query.answer("No leaderboard found!", show_alert=True)
        return
    leaderboard = quiz["leaderboard"]
    if not leaderboard:
        await query.answer("No participants!", show_alert=True)
        return
        # 🏅 Medal icons
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    # 🏆 Build formatted leaderboard text
    text = (
        f"📊 *Qumtta-Leaderboard*\n"
        f"🏷️ *Quiz Name:* {quiz.get('title', 'Untitled Quiz')}\n\n"
    )
    for rank, e in enumerate(leaderboard, start=1):
        medal = medals.get(rank, f"#{rank}")
        text += (
            f"{medal} *{e['name']}*\n"
            f"✅ Correct: {e['correct']}\n"
            f"❌ Incorrect: {e['incorrect']}\n"
            f"⏱️ Time: {round(e['total_time'], 1)}s\n\n"
        )
    text += "— *Your Qumtta Quiz Bot* 🤖"
    success = 0
    failed = 0
    for group_id in ACTIVE_GROUPS:
        try:
            await context.bot.send_message(
                chat_id=group_id,
                text=text,
                parse_mode="Markdown",
            )
            success += 1
        except Exception as e:
            logger.error(f"Failed to publish in group {group_id}: {e}")
            failed += 1
    # Same confirmation message
    await query.edit_message_text(
        f" ✅Leaderboard published successfully in {success} group{'s' if success != 1 else ''}!\n"
        f"{failed} failed.\n\n"
        "Your Qumtta Quiz Bot 🤖"
    )
# 1. LIST ALL ACTIVE GROUPS
async def list_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
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
# 2. REMOVE GROUP + BOT LEFT
async def remove_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
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
    await update.message.reply_text(f"Bot ne group chhoda aur list se hata diya:\n`{group_id}`")
# 3. GLOBAL PAUSE (DM + Group dono se chalega)
async def pause_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_paused
    if update.effective_user.id != ADMIN_ID:
        return
   
    if is_stopped:
        await update.message.reply_text("Quiz band hai.")
        return
    if is_paused:
        await update.message.reply_text("Pehle se paused hai!")
        return
   
    is_paused = True
    await update.message.reply_text("QUIZ PAUSED IN ALL GROUPS!")
# 4. GLOBAL RESUME
async def resume_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_paused
    if update.effective_user.id != ADMIN_ID:
        return
   
    if not is_paused:
        await update.message.reply_text("Quiz chal raha hai!")
        return
   
    is_paused = False
    await update.message.reply_text("QUIZ RESUMED IN ALL GROUPS!")
   
    # Saare groups mein pending question bhejo
    for gid in list(questions_sent_per_group.keys()):
        if questions_sent_per_group.get(gid, 0) < len(current_quiz['questions']):
            context.job_queue.run_once(
                next_question_callback,
                2,
                data={'chat_id': gid},
                name=f"resume_{gid}"
            )
# -------------------------------------------------------------------------
# NEW COMMAND: /createviapoll – build a quiz by forwarding polls (normal or quiz)
# -------------------------------------------------------------------------
# ---------- STATE CONSTANTS ----------
(
    POLL_TITLE,
    POLL_TIMER,
    POLL_COLLECT,
    POLL_CORRECT,
) = range(100, 104) # new states – far away from the old ones
# ---------- GLOBAL STORAGE FOR THIS FLOW ----------
poll_quiz_data: Dict[int, Dict] = {} # user_id → {title, timer, polls:[]}
# ---------- HELPER ----------
def _reset_poll_data(user_id: int):
    poll_quiz_data.pop(user_id, None)
# ---------- ENTRY ----------
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
# ---------- DONE → BUILD JSON ----------
# ---------- DONE → BUILD JSON (FIXED: अलग-अलग options per question) ----------
async def poll_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in poll_quiz_data or not poll_quiz_data[user_id]["polls"]:
        await update.message.reply_text("No polls were added – aborting.")
        _reset_poll_data(user_id)
        return ConversationHandler.END
    src = poll_quiz_data[user_id]
    timer = src["timer"]
    questions = []
    for p in src["polls"]:
        # हर पोल के अपने options और correct_idx
        questions.append({
            "text": p["question"],
            "options": p["options"], # ← अब अलग-अलग
            "correct": p["correct_idx"],
            "timer": timer
        })
    # Final quiz structure
    quiz = {
        "title": src["title"],
        "timer": timer,
        "questions": questions,
        # option_count और option_texts अब जरूरी नहीं — हर प्रश्न में options हैं
    }
    # quiz_id generate + save globally
    quiz_id = str(int(datetime.now(tz=timezone.utc).timestamp()))
    quiz["quiz_id"] = quiz_id
    global current_quiz
    current_quiz = quiz
    readiness_quiz_map[quiz_id] = quiz
    # JSON file bhejo
    safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in src["title"])
    await send_json_file_to_user(
        user_id, context, quiz, filename=f"{safe_title}.json"
    )
    # Buttons
    buttons = [
        [
            InlineKeyboardButton("Start Quiz", callback_data=f"start_quiz:{quiz_id}"),
            InlineKeyboardButton("Publish Result", callback_data=f"publish_result:{quiz_id}")
        ],
        [
            InlineKeyboardButton("Start in All Groups", callback_data=f"start_all:{quiz_id}"),
            InlineKeyboardButton("Publish in All Groups", callback=f"publish_all:{quiz_id}")
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

# -------------------------------------------------
# ADMIN: /stats  →  total users + total groups
# -------------------------------------------------
@admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_groups = len(ACTIVE_GROUPS)
    total_users  = len(active_users)          # <-- NEW
    text = (
        "*BOT STATS*\n\n"
        f"*Total Active Groups:* `{total_groups}`\n"
        f"*Total Users (started bot):* `{total_users}`\n"
        "— Qumtta Quiz Bot"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# -------------------------------------------------
# ADMIN: /exdb  →  export DB (groups + users) to JSON
# -------------------------------------------------
@admin_only
async def export_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = {
        "groups": list(ACTIVE_GROUPS),
        "users": list(active_users)
    }
    json_str = json.dumps(data, indent=4, ensure_ascii=False)
    bio = io.BytesIO(json_str.encode("utf-8"))
    timestamp = int(datetime.now(tz=timezone.utc).timestamp())
    bio.name = f"qumtta_db_{timestamp}.json"  # ← YE ZAROORI HAI
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=InputFile(bio, filename=bio.name)
    )
    await update.message.reply_text("DB exported! Use /updb to restore.")

# -------------------------------------------------
# ADMIN: /updb  →  upload JSON and restore DB
# -------------------------------------------------
@admin_only
async def upload_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.document or not update.message.document.file_name.endswith('.json'):
        await update.message.reply_text("Please upload a *.json* file created with /exdb.")
        return

    file = await update.message.document.get_file()
    byte_data = await file.download_as_bytearray()
    try:
        payload = json.loads(byte_data.decode("utf-8"))

        # === DB VALIDATION: Sirf 'groups' aur 'users' hone chahiye ===
        if not isinstance(payload.get("groups"), list) or not isinstance(payload.get("users"), list):
            await update.message.reply_text("Invalid DB file: Missing 'groups' or 'users' list.")
            return

        if any(key not in ["groups", "users"] for key in payload.keys()):
            await update.message.reply_text("Invalid DB file: Contains unknown fields (only 'groups' and 'users' allowed).")
            return

        # === Restore ===
        restored_groups = 0
        restored_users = 0

        for gid in payload.get("groups", []):
            try:
                gid = int(gid)
                ACTIVE_GROUPS.add(gid)
                restored_groups += 1
            except:
                pass

        for uid in payload.get("users", []):
            try:
                uid = int(uid)
                active_users.add(uid)
                restored_users += 1
            except:
                pass

        await update.message.reply_text(
            f"DB restored successfully!\n"
            f"Groups: {restored_groups}\n"
            f"Users: {restored_users}\n"
            f"Total Active Groups: {len(ACTIVE_GROUPS)}\n"
            f"Total Users: {len(active_users)}"
        )

    except json.JSONDecodeError:
        await update.message.reply_text("Invalid JSON format.")
    except Exception as e:
        await update.message.reply_text(f"Import failed: {e}")
        
# -----------------------------
# MAIN (unchanged except for new end_quiz)
# -----------------------------
def main():
    """Start the bot — FULLY SECURED FOR ADMIN ONLY"""
    application = Application.builder().token(BOT_TOKEN).build()

    # ====================== PUBLIC COMMANDS ======================
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, notify_admin_new_group))
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Command('start'), notify_admin_new_user), group=1)

    # ====================== TEXT-BASED QUIZ CREATOR (/createviatxt) ======================
    conv = ConversationHandler(
        entry_points=[CommandHandler('createviatxt', create_quiz)],  # admin_only हटाओ
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

    # ====================== POLL-BASED QUIZ CREATOR (/createviapoll) ======================
    poll_conv = ConversationHandler(
        entry_points=[CommandHandler("createviapoll", create_via_poll)],  # admin_only हटाओ
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

    # ====================== ADMIN ONLY COMMANDS (बाहर से) ======================
    application.add_handler(CommandHandler('start_quiz', admin_only(start_quiz_command)))
    application.add_handler(CommandHandler('pause', admin_only(pause_quiz)))
    application.add_handler(CommandHandler('resume', admin_only(resume_quiz)))
    application.add_handler(CommandHandler('refresh', admin_only(refresh_bot)))
    application.add_handler(CommandHandler('group', admin_only(list_groups)))
    application.add_handler(CommandHandler('rm_group', admin_only(remove_group)))
    application.add_handler(CommandHandler('stats', stats_command))
    application.add_handler(CommandHandler('broadcast', broadcast_command))
    application.add_handler(CommandHandler('stats', stats_command))
    application.add_handler(CommandHandler('exdb', export_db))
    application.add_handler(CommandHandler('updb', admin_only(upload_db)))
    application.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, admin_only(upload_db)))
    # ====================== OTHER HANDLERS ======================
    application.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, admin_only(handle_document)))
    application.add_handler(PollAnswerHandler(poll_answer))
    application.add_handler(CallbackQueryHandler(start_quiz_button_cb, pattern=r'^start_quiz:'))
    application.add_handler(CallbackQueryHandler(publish_result_cb, pattern=r'^publish_result:'))
    application.add_handler(CallbackQueryHandler(ready_button_cb, pattern=r'^ready:'))
    application.add_handler(CallbackQueryHandler(start_all_cb, pattern=r'^start_all:'))
    application.add_handler(CallbackQueryHandler(publish_all_cb, pattern=r'^publish_all:'))
    application.add_handler(CallbackQueryHandler(ready_all_cb, pattern=r'^ready_all:'))
    # ====================== START BOT ======================
    logger.info("Qumtta Quiz Bot started in WEBHOOK mode...")

    # ---- WEBHOOK MODE (Render) ----
    application.run_webhook(
        listen="0.0.0.0",
        port=8080,
        url_path=BOT_TOKEN,  # /8458622801:AAFW...
        webhook_url=f"https://qumtta-quiz-bot.onrender.com/{BOT_TOKEN}"
    )

if __name__ == "__main__":
    print("Starting Health Server...")
    threading.Thread(target=run_flask, daemon=True).start()
    
    print("Starting Self-Ping (every 5 min)...")
    threading.Thread(target=keep_alive, daemon=True).start()
    
    print("Starting Qumtta Quiz Bot in Webhook Mode...")
    main()


