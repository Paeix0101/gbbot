import os
import re
import logging
import threading
import time
from io import BytesIO

import requests
import instaloader
from flask import Flask, request
from telegram import Update, Bot
from telegram.ext import Dispatcher, CommandHandler, MessageHandler, Filters, CallbackContext

# --------------------- CONFIG ---------------------
TOKEN = os.getenv("BOT_TOKEN")  # MUST be set in Render env vars
WEBHOOK_URL = f"https://gbbot-s267.onrender.com/{TOKEN}"  # change to your own Render URL
# --------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
bot = Bot(token=TOKEN)
dispatcher = Dispatcher(bot, None, use_context=True)

# instaloader context - only used for fetching PUBLIC post metadata, no login
L = instaloader.Instaloader(
    download_pictures=False,
    download_videos=False,
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    compress_json=False,
)

INSTAGRAM_URL_RE = re.compile(
    r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)"
)


def extract_shortcode(text: str):
    match = INSTAGRAM_URL_RE.search(text)
    return match.group(1) if match else None


def fetch_media_urls(shortcode: str):
    """
    Returns a list of dicts: [{"type": "video"/"photo", "url": "..."}]
    Handles single posts, reels, and carousels (multiple items).
    """
    post = instaloader.Post.from_shortcode(L.context, shortcode)
    items = []

    if post.typename == "GraphSidecar":
        for node in post.get_sidecar_nodes():
            if node.is_video:
                items.append({"type": "video", "url": node.video_url})
            else:
                items.append({"type": "photo", "url": node.display_url})
    else:
        if post.is_video:
            items.append({"type": "video", "url": post.video_url})
        else:
            items.append({"type": "photo", "url": post.url})

    return items


def download_bytes(url: str) -> BytesIO:
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    buf = BytesIO(resp.content)
    buf.seek(0)
    return buf


# ---------- handlers ----------
def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "👋 Instagram Downloader Bot\n\n"
        "Bas kisi bhi public Instagram post, reel, ya carousel ka link bhej do, "
        "main uska media download karke bhej dunga."
    )


def handle_message(update: Update, context: CallbackContext):
    text = update.message.text or ""
    shortcode = extract_shortcode(text)

    if not shortcode:
        update.message.reply_text(
            "❌ Ye valid Instagram post/reel link nahi lag raha. "
            "Example: https://www.instagram.com/reel/XXXXXXXXX/"
        )
        return

    update.message.reply_text("⏳ Downloading, thoda ruko...")

    try:
        items = fetch_media_urls(shortcode)
    except Exception as e:
        logger.error(f"Failed to fetch post {shortcode}: {e}")
        update.message.reply_text(
            "⚠️ Download nahi ho paya. Ho sakta hai post private ho, "
            "delete ho gaya ho, ya Instagram ne rate-limit lagaya ho."
        )
        return

    for item in items:
        try:
            file_bytes = download_bytes(item["url"])
            if item["type"] == "video":
                update.message.reply_video(video=file_bytes)
            else:
                update.message.reply_photo(photo=file_bytes)
        except Exception as e:
            logger.error(f"Failed to send media: {e}")
            update.message.reply_text("⚠️ Ek media file bhejne mein error aaya.")


# ---------- register ----------
dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))


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