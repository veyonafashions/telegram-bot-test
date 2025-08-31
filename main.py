#!/usr/bin/env python3
"""
A Telegram bot that fetches direct video and audio links from YouTube
using a Piped API instance. This version is optimized for webhook deployment on
platforms like Render using a Uvicorn server.
"""

import os
import re
import logging
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

# --- Configuration & Constants ---

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("FATAL: No BOT_TOKEN found in environment variables")

WEBHOOK_URL = os.getenv("WEBHOOK_URL")
if not WEBHOOK_URL:
    raise ValueError("FATAL: No WEBHOOK_URL found in environment variables")

PORT = int(os.environ.get('PORT', 8443))
PIPED_API_INSTANCE = "https://pipedapi.kavin.rocks"
YOUTUBE_ID_REGEX = re.compile(
    r"(?:youtube\.com\/(?:[^\/]+\/.+\/|(?:v|e(?:mbed)?)\/|.*[?&]v=)|youtu\.be\/|youtube\.com\/shorts\/)([a-zA-Z0-9_-]{11})"
)

# --- Helper Functions & Command Handlers --- (No changes here)

def extract_video_id(text: str) -> str | None:
    """Extracts a YouTube video ID from a string (URL or plain ID)."""
    match = YOUTUBE_ID_REGEX.search(text)
    if match: return match.group(1)
    if len(text) == 11 and re.match(r"^[a-zA-Z0-9_-]+$", text): return text
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message when the /start command is issued."""
    await update.message.reply_text(
        "🎬 Welcome! Send `/yt <YouTube Video URL or ID>` to get direct links.",
        parse_mode=ParseMode.MARKDOWN,
    )

async def yt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetches and sends direct video/audio links for a given YouTube video."""
    if not context.args:
        await update.message.reply_text(
            "⚠️ **Usage:** `/yt <YouTube URL or video_id>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    query = " ".join(context.args)
    video_id = extract_video_id(query)
    if not video_id:
        await update.message.reply_text("❌ Couldn't find a valid YouTube Video ID in your message.")
        return
    processing_message = await update.message.reply_text("⏳ Fetching video info...")
    api_url = f"{PIPED_API_INSTANCE}/streams/{video_id}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(api_url)
            response.raise_for_status()
            data = response.json()
        if "error" in data:
            logger.warning(f"Piped API error for ID {video_id}: {data['error']}")
            await processing_message.edit_text(f"❌ API Error: {data['error']}")
            return
        video_streams = [s for s in data.get("videoStreams", []) if s.get("quality") and s.get("url") and not s.get("videoOnly") and s.get("mimeType") == "video/mp4"]
        if not video_streams:
            await processing_message.edit_text("❌ No direct video streams with audio found.")
            return
        best_video_stream = max(video_streams, key=lambda s: int(s["quality"].replace("p", "")))
        audio_streams = [s for s in data.get("audioStreams", []) if s.get("bitrate") and s.get("url")]
        best_audio_stream = max(audio_streams, key=lambda s: s["bitrate"]) if audio_streams else None
        message_text = (
            f"✅ **{data.get('title', 'YouTube Video')}**\n\n"
            f"🔗 *Source:* `https://youtube.com/watch?v={video_id}`\n\n"
            f"🎥 [**Direct Video Link ({best_video_stream['quality']})**]({best_video_stream['url']})"
        )
        if best_audio_stream:
            message_text += f"\n🎵 [**Direct Audio Only**]({best_audio_stream['url']})"
        await processing_message.edit_text(message_text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    except httpx.RequestError as e:
        logger.error(f"HTTP request failed for ID {video_id}: {e}")
        await processing_message.edit_text("❌ Network error: Could not connect to the API.")
    except Exception as e:
        logger.error(f"An unexpected error occurred for ID {video_id}: {e}", exc_info=True)
        await processing_message.edit_text("❌ An unexpected error occurred.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors caused by updates."""
    logger.error("Exception while handling an update:", exc_info=context.error)

# --- Uvicorn Setup ---

# The `app` object is created at the module level. Uvicorn will look for this.
# We configure the webhook settings on this object directly.
secret_path = TOKEN.split(':')[-1]
app = (
    Application.builder()
    .token(TOKEN)
    .webhook_url(f"{WEBHOOK_URL}/{secret_path}")
    .listen_port(PORT)
    .build()
)

# Register all handlers on the app object
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("yt", yt))
app.add_error_handler(error_handler)
