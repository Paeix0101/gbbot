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

# AUTHORIZED_USERS env format: "123456789,987654321"
# These stay permanently authorized (never expire on restart), same as anyone granted via /grant.
PERMANENT_USERS = {
    int(uid.strip())
    for uid in os.getenv("AUTHORIZED_USERS", "").split(",")
    if uid.strip()
}

# In-memory only — resets automatically whenever Render restarts the service (e.g. every 30 days).
granted_users = set()

FORWARD_DELAY = float(os.getenv("FORWARD_DELAY", "1.2"))  # seconds between each forward, tune if flood errors happen
# --------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
bot = Bot(token=TOKEN)
dispatcher = Dispatcher(bot, None, use_context=True)

# sessions[user_id] = {"source_chat": int, "start": int, "end": int|None}
sessions = {}


def is_authorized(user_id: int) -> bool:
    return user_id in PERMANENT_USERS or user_id in granted_users


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
        "1️⃣ Source group/channel mein pehle media pe reply karke /here1 bhejo\n"
        "2️⃣ Last media pe reply karke /here2 bhejo\n"
        "3️⃣ Jis bhi group/channel mein bhejna hai, wahan jaake /bhejde bhejo (koi reply nahi chahiye)\n\n"
        "/myid — apna Telegram user ID dekho\n"
        "/chatid — is chat ki ID dekho\n"
        "/cancel — pending selection cancel karo"
    )


def myid(update: Update, context: CallbackContext):
    update.message.reply_text(f"Your user ID: {update.effective_user.id}")


def chatid(update: Update, context: CallbackContext):
    update.message.reply_text(f"This chat's ID: {update.effective_chat.id}")


def cancel(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id in sessions:
        del sessions[user_id]
        update.message.reply_text("❌ Pending selection cancel ho gayi.")
    else:
        update.message.reply_text("Koi pending selection nahi hai.")


def grant(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("Usage: /grant <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        update.message.reply_text("❌ Valid numeric user ID do. Example: /grant 123456789")
        return

    granted_users.add(target_id)
    update.message.reply_text(
        f"✅ User {target_id} authorize ho gaya.\n"
        f"⚠️ Ye grant sirf tab tak valid hai jab tak bot restart nahi hota "
        f"(Render ~30 din mein restart karta hai) — uske baad phir se /grant karna padega."
    )


def revoke(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("Usage: /revoke <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        update.message.reply_text("❌ Valid numeric user ID do.")
        return

    granted_users.discard(target_id)
    update.message.reply_text(f"🚫 User {target_id} ka access hata diya.")


def listauth(update: Update, context: CallbackContext):
    if not granted_users:
        update.message.reply_text("Abhi koi temporarily granted user nahi hai.")
        return

    lines = "\n".join(str(uid) for uid in granted_users)
    update.message.reply_text(f"Temporarily granted users:\n{lines}")


def here1(update: Update, context: CallbackContext):
    user = update.effective_user
    chat = update.effective_chat

    if not is_authorized(user.id):
        return  # silently ignore unauthorized users

    replied = update.message.reply_to_message
    if not replied:
        update.message.reply_text("❌ Kisi media message pe reply karke /here1 bhejo.")
        return

    sessions[user.id] = {
        "source_chat": chat.id,
        "start": replied.message_id,
        "end": None,
    }
    update.message.reply_text(
        f"✅ Start point mark ho gaya (ID {replied.message_id}).\n"
        f"Ab isi chat mein end wale media pe reply karke /here2 bhejo."
    )


def here2(update: Update, context: CallbackContext):
    user = update.effective_user
    chat = update.effective_chat

    if not is_authorized(user.id):
        return

    session = sessions.get(user.id)
    if not session or session["source_chat"] != chat.id:
        update.message.reply_text("❌ Pehle isi chat mein kisi media pe reply karke /here1 bhejo.")
        return

    replied = update.message.reply_to_message
    if not replied:
        update.message.reply_text("❌ Kisi media message pe reply karke /here2 bhejo.")
        return

    session["end"] = replied.message_id
    update.message.reply_text(
        "✅ End point bhi mark ho gaya.\n\n"
        "Ab jis bhi group/channel mein media bhejna hai, wahan jaake sirf /bhejde likh do "
        "(is bot ko us group/channel mein admin hona chahiye)."
    )


def bhejde(update: Update, context: CallbackContext):
    user = update.effective_user
    dest_chat = update.effective_chat

    if not is_authorized(user.id):
        return

    session = sessions.get(user.id)
    if not session or session["end"] is None:
        update.message.reply_text(
            "❌ Pehle source group/channel mein /here1 aur /here2 use karo, "
            "phir yahan aake /bhejde bhejo."
        )
        return

    source_chat_id = session["source_chat"]
    lo, hi = sorted([session["start"], session["end"]])
    del sessions[user.id]

    update.message.reply_text(
        f"⏳ ID {lo} se {hi} tak ({hi - lo + 1} messages) check karke media is chat mein forward kar raha hoon. "
        f"Bade range mein time lag sakta hai, done hone pe DM karunga."
    )

    threading.Thread(
        target=forward_range,
        args=(source_chat_id, dest_chat.id, lo, hi, user.id),
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
dispatcher.add_handler(CommandHandler("bhejde", bhejde))
dispatcher.add_handler(CommandHandler("grant", grant))
dispatcher.add_handler(CommandHandler("revoke", revoke))
dispatcher.add_handler(CommandHandler("listauth", listauth))


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