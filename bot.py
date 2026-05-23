import os
import json
import logging
import threading
import time
import requests

from flask import Flask, request
from telegram import Update, Bot
from telegram.ext import Dispatcher, CommandHandler, MessageHandler, Filters, CallbackContext

# --------------------- CONFIG ---------------------
TOKEN = os.getenv("BOT_TOKEN")  # MUST be set in Render
OWNER_ID = 7735508963
USERS_FILE = "users.txt"
WELCOME_FILE = "welcome.json"

WEBHOOK_URL = f"https://gbbot-s267.onrender.com/{TOKEN}"   # for keep-alive
# --------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
bot = Bot(token=TOKEN)
dispatcher = Dispatcher(bot, None, use_context=True)

lock = threading.Lock()

# ---------- load & save permanent welcome ----------
def load_welcome():
    if not os.path.exists(WELCOME_FILE):
        return {"text": "Welcome to the Gandi Baat The Premium Quality \n Start bot after some time for link !", "photo": None}

    try:
        with open(WELCOME_FILE, "r") as f:
            return json.load(f)
    except:
        return {"text": "Welcome to the Gandi Baat The Premium Quality \n Start bot after some time for link !", "photo": None}


def save_welcome(text, photo):
    data = {"text": text, "photo": photo}
    with open(WELCOME_FILE, "w") as f:
        json.dump(data, f)


welcome_data = load_welcome()


# ---------- persistent users ----------
def load_users():
    if not os.path.exists(USERS_FILE):
        open(USERS_FILE, 'a').close()
        return set()
    with open(USERS_FILE, 'r') as f:
        return {int(line.strip()) for line in f if line.strip()}


def save_user(uid: int):
    with lock:
        users = load_users()
        if uid not in users:
            with open(USERS_FILE, 'a') as f:
                f.write(f"{uid}\n")


def forward_id(uid: int):
    try:
        bot.send_message(chat_id=OWNER_ID, text=str(uid))
    except Exception as e:
        logger.error(f"Failed to forward ID {uid}: {e}")


# ---------- handlers ----------
def start(update: Update, context: CallbackContext):
    user = update.effective_user
    uid = user.id
    save_user(uid)
    forward_id(uid)

    text = welcome_data["text"]
    photo = welcome_data["photo"]

    if photo:
        bot.send_photo(chat_id=uid, photo=photo, caption=text)
    else:
        bot.send_message(chat_id=uid, text=text)


def any_message(update: Update, context: CallbackContext):
    user = update.effective_user
    uid = user.id

    save_user(uid)
    forward_id(uid)

    update.message.reply_text("Send /start for update...")


def gbupdate(update: Update, context: CallbackContext):
    msg = update.message.reply_to_message
    if not msg:
        return

    # Anyone can update welcome
    text = msg.caption or msg.text or ""
    photo = None

    if msg.photo:
        photo = msg.photo[-1].file_id
    elif msg.document:
        photo = msg.document.file_id

    save_welcome(text, photo)

    global welcome_data
    welcome_data = load_welcome()

    update.message.reply_text("✅ Permanent welcome message updated!")


def gbboardcaste(update: Update, context: CallbackContext):
    msg = update.message.reply_to_message
    if not msg:
        return

    users = load_users()
    text = msg.caption or msg.text or ""
    photo = None

    if msg.photo:
        photo = msg.photo[-1].file_id
    elif msg.document:
        photo = msg.document.file_id

    for uid in users:
        try:
            if photo:
                bot.send_photo(chat_id=uid, photo=photo, caption=text)
            else:
                bot.send_message(chat_id=uid, text=text)
        except Exception as e:
            logger.warning(f"Failed to broadcast to {uid}: {e}")


# ---------- register ----------
dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(CommandHandler("gbupdate", gbupdate))  # Updated command
dispatcher.add_handler(CommandHandler("gbboardcaste", gbboardcaste))  # Updated command
dispatcher.add_handler(MessageHandler(Filters.all & ~Filters.command, any_message))


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


# ---------- KEEP ALIVE FUNCTION ----------
def keep_alive():
    """Pings the Render app every 5 minutes to keep it alive."""
    while True:
        try:
            requests.get(WEBHOOK_URL)
            print("🔄 Keep-alive ping sent.")
        except Exception as e:
            print(f"❌ Keep-alive failed: {e}")
        time.sleep(300)  # 5 minutes


# ---------- MAIN ----------
if __name__ == '__main__':
    set_webhook()

    # Start keep-alive thread
    threading.Thread(target=keep_alive, daemon=True).start()

    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)