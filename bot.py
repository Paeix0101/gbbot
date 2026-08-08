import os
import time
import logging
import threading

from flask import Flask, request
from telegram import Update, Bot
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Dispatcher,
    CommandHandler,
    Filters,
    CallbackContext,
)

# --------------------- CONFIG ---------------------
TOKEN = os.getenv("BOT_TOKEN")  # MUST be set in Render env vars
BASE_URL = os.getenv("WEBHOOK_URL", "https://your-app.onrender.com").rstrip("/")
WEBHOOK_URL = f"{BASE_URL}/{TOKEN}"

# CHAT_MAP env format: "source_id1:dest_id1,source_id2:dest_id2"
# Example: "-1001111111111:-1002222222222,-1003333333333:-1004444444444"
def parse_chat_map(raw: str):
    mapping = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        src, dst = pair.split(":")
        mapping[int(src)] = int(dst)
    return mapping


CHAT_MAP = parse_chat_map(os.getenv("CHAT_MAP", ""))

# AUTHORIZED_USERS env format: "123456789,987654321"
AUTHORIZED_USERS = {
    int(uid.strip())
    for uid in os.getenv("AUTHORIZED_USERS", "").split(",")
    if uid.strip()
}

FORWARD_DELAY = float(os.getenv("FORWARD_DELAY", "1.2"))  # seconds between each forward, tune if flood errors happen
# --------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
bot = Bot(token=TOKEN)
dispatcher = Dispatcher(bot, None, use_context=True)

# session[(user_id, chat_id)] = start_message_id
sessions = {}


def is_authorized(user_id: int) -> bool:
    return user_id in AUTHORIZED_USERS


def is_media(msg) -> bool:
    return any([
        msg.photo,
        msg.video,
        msg.document,
        msg.audio,
        msg.animation,
        msg.voice,
        msg.video_note,
        msg.sticker,
    ])


# ---------- handlers ----------
def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "👋 Range Forward Bot\n\n"
        "Source group/channel mein kisi media pe reply karke /here1 bhejo, "
        "phir end wale media pe reply karke /here2 bhejo. "
        "Beech ke saare media messages destination pe forward ho jayenge.\n\n"
        "/myid — apna Telegram user ID dekho\n"
        "/chatid — is chat ki ID dekho\n"
        "/cancel — pending selection cancel karo"
    )


def myid(update: Update, context: CallbackContext):
    update.message.reply_text(f"Your user ID: {update.effective_user.id}")


def chatid(update: Update, context: CallbackContext):
    update.message.reply_text(f"This chat's ID: {update.effective_chat.id}")


def cancel(update: Update, context: CallbackContext):
    key = (update.effective_user.id, update.effective_chat.id)
    if key in sessions:
        del sessions[key]
        update.message.reply_text("❌ Pending selection cancel ho gayi.")
    else:
        update.message.reply_text("Koi pending selection nahi hai.")


def here1(update: Update, context: CallbackContext):
    user = update.effective_user
    chat = update.effective_chat

    if not is_authorized(user.id):
        return  # silently ignore unauthorized users

    if chat.id not in CHAT_MAP:
        update.message.reply_text("⚠️ Ye chat kisi configured source/destination pair mein nahi hai.")
        return

    replied = update.message.reply_to_message
    if not replied:
        update.message.reply_text("❌ Kisi media message pe reply karke /here1 bhejo.")
        return

    sessions[(user.id, chat.id)] = replied.message_id
    update.message.reply_text(
        f"✅ Start point mark ho gaya (ID {replied.message_id}).\n"
        f"Ab end wale media pe reply karke /here2 bhejo."
    )


def here2(update: Update, context: CallbackContext):
    user = update.effective_user
    chat = update.effective_chat

    if not is_authorized(user.id):
        return

    key = (user.id, chat.id)
    if key not in sessions:
        update.message.reply_text("❌ Pehle kisi media pe reply karke /here1 bhejo.")
        return

    replied = update.message.reply_to_message
    if not replied:
        update.message.reply_text("❌ Kisi media message pe reply karke /here2 bhejo.")
        return

    start_id = sessions.pop(key)
    end_id = replied.message_id
    lo, hi = min(start_id, end_id), max(start_id, end_id)
    dest_chat_id = CHAT_MAP[chat.id]

    update.message.reply_text(
        f"⏳ ID {lo} se {hi} tak ({hi - lo + 1} messages) check karke media forward kar raha hoon. "
        f"Bade range mein time lag sakta hai, done hone pe DM karunga."
    )

    threading.Thread(
        target=forward_range,
        args=(chat.id, dest_chat_id, lo, hi, user.id),
        daemon=True,
    ).start()


def forward_one(source_chat_id: int, dest_chat_id: int, message_id: int):
    """Forward a single message; delete it from destination if it isn't media.
    Returns 'forwarded' / 'skipped' / 'error'."""
    try:
        msg = bot.forward_message(
            chat_id=dest_chat_id,
            from_chat_id=source_chat_id,
            message_id=message_id,
        )
    except RetryAfter as e:
        time.sleep(e.retry_after + 1)
        return forward_one(source_chat_id, dest_chat_id, message_id)
    except TelegramError:
        return "error"

    if is_media(msg):
        return "forwarded"

    try:
        bot.delete_message(chat_id=dest_chat_id, message_id=msg.message_id)
    except TelegramError:
        pass
    return "skipped"


def forward_range(source_chat_id: int, dest_chat_id: int, lo: int, hi: int, notify_user_id: int):
    forwarded = skipped = errors = 0

    for mid in range(lo, hi + 1):
        result = forward_one(source_chat_id, dest_chat_id, mid)
        if result == "forwarded":
            forwarded += 1
        elif result == "skipped":
            skipped += 1
        else:
            errors += 1
        time.sleep(FORWARD_DELAY)

    try:
        bot.send_message(
            chat_id=notify_user_id,
            text=(
                "✅ Forwarding complete!\n\n"
                f"📦 Media forwarded: {forwarded}\n"
                f"⏭️ Skipped (non-media): {skipped}\n"
                f"⚠️ Errors (deleted/missing msgs): {errors}"
            ),
        )
    except TelegramError as e:
        logger.error(f"Failed to send completion notice: {e}")


# ---------- register ----------
dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(CommandHandler("myid", myid))
dispatcher.add_handler(CommandHandler("chatid", chatid))
dispatcher.add_handler(CommandHandler("cancel", cancel))
dispatcher.add_handler(CommandHandler("here1", here1, filters=Filters.reply))
dispatcher.add_handler(CommandHandler("here2", here2, filters=Filters.reply))


# ---------- webhook ----------
@app.route('/' + TOKEN, methods=['POST'])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dispatcher.process_update(update)
    return '', 200


@app.route('/')
def index():
    return 'Bot is alive!'


def set_webhook():
    current = bot.get_webhook_info()
    if current.url != WEBHOOK_URL:
        bot.set_webhook(url=WEBHOOK_URL)
        logger.info(f"Webhook set to {WEBHOOK_URL}")


def keep_alive():
    """Pings the Render app every 5 minutes to keep the free-tier instance awake."""
    import requests
    while True:
        try:
            requests.get(WEBHOOK_URL)
            print("🔄 Keep-alive ping sent.")
        except Exception as e:
            print(f"❌ Keep-alive failed: {e}")
        time.sleep(300)


# ---------- MAIN ----------
if __name__ == '__main__':
    set_webhook()
    threading.Thread(target=keep_alive, daemon=True).start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)