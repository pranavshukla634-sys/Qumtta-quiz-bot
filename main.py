import io
import json
import asyncio
import logging
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
GROUP_ID = -1003122522619
BOT_TOKEN = "8458622801:AAFWZDxnB8ZGoQEtrljhuPGA8GHzghytpLU"

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
current_quiz: Dict[str, Any] = None
scores: Dict[int, int] = {}
correct_options: Dict[str, int] = {}  # poll_id -> correct_index
poll_sent_time: Dict[str, float] = {}  # poll_id -> timestamp (seconds)
user_stats: Dict[int, Dict[str, Any]] = {}  # user_id -> {correct, incorrect, total_time}

# readiness tracking for pre-start
readiness: Dict[str, Set[int]] = {}  # quiz_id -> set(user_ids who clicked ready)
readiness_message_ids: Dict[str, int] = {}  # quiz_id -> message_id of ready message in group
readiness_quiz_map: Dict[str, Dict[str, Any]] = {}  # quiz_id -> quiz object snapshot (archive)

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -----------------------------
# HELPERS
# -----------------------------


def build_start_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("/create_quiz")], [KeyboardButton("/start_quiz")], [KeyboardButton("/cancel")]],
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
    text = (
        "👋 नमस्ते! यह Quiz Bot है. नीचे दिए कमांड से शुरू करें:\n\n"
        "/create_quiz — एक नया क्विज बनाएँ (DM में, केवल admin).\n"
        "/start_quiz — लोड किया हुआ क्विज ग्रुप में चलाएँ (केवल admin और configured group).\n"
        "/cancel — वर्तमान ऑपरेशन रद्द करें.\n\n"
        "क्विज बनाने का नया फ्लो:\n"
        "1) टाइटल पूछेगा.\n"
        "2) फिर Poll settings (तीन लाइनें): option_count, option_texts comma-separated, timer in seconds.\n"
        "3) प्रश्न भेजें — एक ही संदेश में कई प्रश्न भेज सकते हैं; प्रश्नों के बीच एक खाली लाइन रखें.\n"
        "4) /done के बाद correct answers comma-separated भेजें.\n"
    )
    if update.effective_chat.type == 'private':
        await update.message.reply_text(text, reply_markup=build_start_keyboard())
    else:
        await update.message.reply_text("Use /create_quiz in DM (admin only) or /start_quiz in group if a quiz is already loaded.")



async def create_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # only in private and only admin
    if update.effective_chat.type != 'private':
        return
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ केवल admin क्विज बना सकता है.")
        return

    # initialize storage
    context.user_data.clear()
    context.user_data['questions'] = []  # list of question texts
    context.user_data['added_chunks'] = []  # to allow undo of last chunk

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
        quiz['questions'].append({'text': q_text, 'options': context.user_data['option_texts'], 'correct': correct_idx, 'timer': context.user_data['timer']})

    # Save to current_quiz (global) so it can be started in group
    global current_quiz
    quiz_id = str(int(datetime.now(tz=timezone.utc).timestamp()))
    quiz['quiz_id'] = quiz_id
    current_quiz = quiz

    # Save snapshot in readiness_quiz_map so it persists even after quiz run
    readiness_quiz_map[quiz_id] = quiz

    # send json file back AND send action message with buttons (Start Quiz / Publish Result)
    await send_json_file_to_user(update.effective_chat.id, context, quiz, filename=f"quiz_{quiz_id}.json")

    # prepare inline buttons - keep these persistent (don't edit them away later)
    buttons = [
        [InlineKeyboardButton("Start Quiz", callback_data=f"start_quiz:{quiz_id}"),
         InlineKeyboardButton("Publish Result", callback_data=f"publish_result:{quiz_id}")]
    ]
    await update.message.reply_text("✅ Quiz saved. नीचे से आगे की कार्रवाई करें:", reply_markup=InlineKeyboardMarkup(buttons))

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

    file = await document.get_file()
    byte_array = await file.download_as_bytearray()
    try:
        global current_quiz
        current_quiz = json.loads(byte_array)
        # ensure quiz_id
        if 'quiz_id' not in current_quiz:
            current_quiz['quiz_id'] = str(int(datetime.now(tz=timezone.utc).timestamp()))
        quiz_id = current_quiz['quiz_id']
        readiness_quiz_map[quiz_id] = current_quiz

        # send action buttons (do NOT edit/remove these later)
        buttons = [
            [InlineKeyboardButton("Start Quiz", callback_data=f"start_quiz:{quiz_id}"),
             InlineKeyboardButton("Publish Result", callback_data=f"publish_result:{quiz_id}")]
        ]
        await update.message.reply_text("✅ Quiz loaded from JSON. नीचे से आगे की कार्रवाई करें:", reply_markup=InlineKeyboardMarkup(buttons))
    except json.JSONDecodeError:
        await update.message.reply_text("Invalid JSON file.")


# -----------------------------
# CALLBACKS: Start Quiz flow / readiness
# -----------------------------
async def start_quiz_button_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # acknowledge callback
    data = query.data  # start_quiz:quiz_id
    _, quiz_id = data.split(':', 1)
    if update.effective_user.id != ADMIN_ID:
        # inform user via alert but DO NOT edit original admin message
        await query.answer(text="❌ केवल admin ही यह कर सकता है.", show_alert=True)
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
        "🎉 Welcome to Qumtta World\n"
        "I am Your Qumtta Quiz Bot\n\n"
        f"*Quiz Title:* {title}\n"
        f"*No of Questions:* {total_q}\n"
        f"*Timer:* {timer} seconds\n\n"
        "Click below when you are ready. Minimum 2 participants required to start."
    )

    # create 'I am ready' button with count
    readiness[quiz_id] = set()
    keyboard = [[InlineKeyboardButton(f"I am ready (0)", callback_data=f"ready:{quiz_id}")]]
    msg = await context.bot.send_message(GROUP_ID, text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    readiness_message_ids[quiz_id] = msg.message_id

    # schedule readiness check after 15 seconds
    context.job_queue.run_once(finalize_readiness, 15, data={'quiz_id': quiz_id, 'initiator': update.effective_user.id})

    # do NOT edit the original admin DM message (keep Start/Publish buttons visible).
    await query.answer(text="✅ Quiz start initiated and posted to group for readiness.")


async def ready_button_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data  # ready:quiz_id
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
            await update.message.reply_text("⚠️ No quiz loaded. Please load a quiz JSON in DM first or create one with /create_quiz.")
            return

    # ensure that the readiness snapshot exists
    quiz_id = current_quiz.get('quiz_id') or str(int(datetime.now(tz=timezone.utc).timestamp()))
    readiness_quiz_map[quiz_id] = current_quiz
    # call start callback behavior: post preparatory message and start readiness
    title = current_quiz.get('title', 'Untitled')
    total_q = len(current_quiz.get('questions', []))
    timer = current_quiz.get('timer')
    text = (
        "🎉 Welcome to Qumtta World\n"
        "I am Your Qumtta Quiz Bot\n\n"
        f"*Quiz Title:* {title}\n"
        f"*No of Questions:* {total_q}\n"
        f"*Timer:* {timer} seconds\n\n"
        "Click below when you are ready. Minimum 2 participants required to start."
    )
    readiness[quiz_id] = set()
    keyboard = [[InlineKeyboardButton(f"I am ready (0)", callback_data=f"ready:{quiz_id}")]]
    msg = await context.bot.send_message(GROUP_ID, text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    readiness_message_ids[quiz_id] = msg.message_id
    context.job_queue.run_once(finalize_readiness, 15, data={'quiz_id': quiz_id, 'initiator': user.id})


# -----------------------------
# QUESTIONS / POLLS
# -----------------------------
async def send_next_question(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    global correct_options, current_quiz
    # when called from job, context is available and current_quiz should be set
    if current_quiz is None:
        return

    # compute q_index as number of polls already sent (use correct_options length)
    q_index = len(correct_options)
    if q_index >= len(current_quiz['questions']):
        await end_quiz(context, chat_id)
        return

    q = current_quiz['questions'][q_index]

    # 1) send question text as normal message
    await context.bot.send_message(chat_id, f"Q{q_index+1}. {q['text']}")

    # 2) send poll with placeholder question text
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

    # record poll sent time
    poll_sent_time[poll_id] = datetime.now(tz=timezone.utc).timestamp()

    # schedule next question
    context.job_queue.run_once(next_question_callback, q['timer'] + 1, data=chat_id)


async def next_question_callback(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
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

    # compute response time
    sent_ts = poll_sent_time.get(poll_id)
    if sent_ts:
        now_ts = datetime.now(tz=timezone.utc).timestamp()
        delta = max(0.0, now_ts - sent_ts)
    else:
        delta = 0.0

    # init stats
    if user_id not in user_stats:
        user_stats[user_id] = {'correct': 0, 'incorrect': 0, 'total_time': 0.0}

    if selected_option == correct:
        user_stats[user_id]['correct'] += 1
    else:
        user_stats[user_id]['incorrect'] += 1

    user_stats[user_id]['total_time'] += delta

    # also track scores for leaderboard numeric sorting
    if selected_option == correct:
        scores[user_id] = scores.get(user_id, 0) + 1


async def end_quiz(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    global current_quiz, scores, correct_options

    if current_quiz is None:
        return

    quiz_id = current_quiz.get('quiz_id')
    previous_leaderboard = current_quiz.get('leaderboard', [])  # 🟡 पहले के attempts

    # Store existing users (first attempt users)
    existing_users = {e['user_id']: e for e in previous_leaderboard}

    entries = []
    for user_id, stats in user_stats.items():
        if user_id in existing_users:
            continue  # 🔹 पहले attempt वाले को skip करो

        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
            name = member.user.full_name
        except Exception:
            name = f"User {user_id}"

        entries.append({
            'user_id': user_id,
            'name': name,
            'correct': stats['correct'],
            'incorrect': stats['incorrect'],
            'total_time': stats['total_time']
        })

    # merge with old leaderboard (keep old ones + new ones)
    combined_entries = previous_leaderboard + entries

    # sort combined leaderboard
    combined_entries.sort(key=lambda x: (-x['correct'], x['total_time']))

    # build leaderboard text
    text = "🏁 *Quiz Ended! Leaderboard:*\n\n"
    if not combined_entries:
        text += "No participants."
    else:
        for rank, e in enumerate(combined_entries, start=1):
            text += f"{rank}. {e['name']} — ✅ {e['correct']}  ❌ {e['incorrect']}  ⏱️ {round(e['total_time'],1)}s\n"

    current_quiz['leaderboard'] = combined_entries
    readiness_quiz_map[quiz_id] = current_quiz

    # ✅ send updated JSON and "Thanks" message + Start/Publish buttons
    try:
        await send_json_file_to_user(ADMIN_ID, context, current_quiz, filename=f"quiz_{quiz_id}.json")

        buttons = [
            [InlineKeyboardButton("Start Quiz", callback_data=f"start_quiz:{quiz_id}"),
             InlineKeyboardButton("Publish Result", callback_data=f"publish_result:{quiz_id}")]
        ]
        await context.bot.send_message(ADMIN_ID, "✅ Quiz finished. Leaderboard updated and file sent.", reply_markup=InlineKeyboardMarkup(buttons))
    except Exception as e:
        logger.exception("Failed to send updated JSON to admin: %s", e)

    # 🟢 Send “Thanks” message to group
    try:
        await context.bot.send_message(chat_id, "🙏 Thank you everyone for participating in the quiz!\nStay tuned for more quizzes soon! 🎉")
    except Exception:
        pass

    # Cleanup runtime
    current_quiz = None
    scores = {}
    correct_options = {}
    poll_sent_time.clear()
    user_stats.clear()
    readiness.clear()
    readiness_message_ids.clear()
    # keep readiness_quiz_map intact for later publish

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
    text = f"🏆 *Final Leaderboard for:* {quiz.get('title', 'Untitled Quiz')}\n\n"
    for rank, e in enumerate(leaderboard, start=1):
        medal = medals.get(rank, f"#{rank}")
        text += (
            f"{medal} *{e['name']}* — "
            f"✅ {e['correct']}  ❌ {e['incorrect']}  ⏱️ {round(e['total_time'], 1)}s\n"
        )

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

        # 📨 Cleaned JSON वापस admin को भेजना
        json_str = json.dumps(cleaned_quiz, indent=4, ensure_ascii=False)
        bio = io.BytesIO(json_str.encode("utf-8"))
        bio.name = f"cleaned_quiz_{quiz_id}.json"
        await context.bot.send_document(
            chat_id=ADMIN_ID,
            document=InputFile(bio, filename=bio.name),
            caption="🧹 Cleaned quiz data (participants removed).",
        )

        # ✅ Admin को confirmation
        await query.edit_message_text("✅ Leaderboard published and cleaned JSON sent to admin!")

    except Exception as e:
        await query.edit_message_text(f"⚠️ Failed to publish leaderboard:\n{e}")

# -----------------------------
# MAIN (unchanged except for new end_quiz)
# -----------------------------
def main():
    application = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler('create_quiz', create_quiz)],
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

    application.add_handler(CommandHandler('start', start))
    application.add_handler(conv)
    application.add_handler(CommandHandler('start_quiz', start_quiz_command))
    application.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, handle_document))
    application.add_handler(PollAnswerHandler(poll_answer))
    application.add_handler(CallbackQueryHandler(start_quiz_button_cb, pattern=r'^start_quiz:'))
    application.add_handler(CallbackQueryHandler(publish_result_cb, pattern=r'^publish_result:'))
    application.add_handler(CallbackQueryHandler(ready_button_cb, pattern=r'^ready:'))

    logger.info("🤖 Bot is running with extended features (Thanks msg + first-attempt leaderboard)...")
    application.run_polling()


if __name__ == '__main__':
    main()
