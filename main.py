#!/usr/bin/env python3
"""
A Telegram bot that fetches direct video and audio links from YouTube
using a Piped API instance. This version is optimized for webhook deployment on
platforms like Render.
"""

import os
import re
import logging
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

# --- Configuration & Constants ---

# Enable logging for better debugging and monitoring
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot token from environment variables. Crucial for operation.
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("FATAL: No BOT_TOKEN found in environment variables")

# Piped API instance (you can change to another if this one is down or rate-limited)
PIPED_API_INSTANCE = "https://pipedapi.kavin.rocks"

# A robust regex to find a YouTube video ID from various URL formats
YOUTUBE_ID_REGEX = re.compile(
    r"(?:youtube\.com\/(?:[^\/]+\/.+\/|(?:v|e(?:mbed)?)\/|.*[?&]v=)|youtu\.be\/|youtube\.com\/shorts\/)([a-zA-Z0-9_-]{11})"
)

# --- Helper Functions ---

def extract_video_id(text: str) -> str | None:
    """Extracts a YouTube video ID from a string (URL or plain ID)."""
    match = YOUTUBE_ID_REGEX.search(text)
    if match:
        return match.group(1)
    # If no URL match, assume the text is a valid 11-character video ID
    if len(text) == 11 and re.match(r"^[a-zA-Z0-9_-]+$", text):
        return text
    return None

# --- Command Handlers ---

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
            "⚠️ **Usage:** `/yt <YouTube URL or video_id>`\n"
            "**Example:** `/yt https://www.youtube.com/watch?v=dQw4w9WgXcQ`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    query = " ".join(context.args)
    video_id = extract_video_id(query)

    if not video_id:
        await update.message.reply_text("❌ Couldn't find a valid YouTube Video ID in your message.")
        return

    processing_message = await update.message.reply_text("⏳ Fetching video info, please wait...")

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

        # Find the best available MP4 video stream with sound for easy playback
        video_streams = [
            s for s in data.get("videoStreams", [])
            if s.get("quality") and s.get("url") and s.get("videoOnly") is False and s.get("mimeType") == "video/mp4"
        ]

        if not video_streams:
             await processing_message.edit_text("❌ No direct video streams with audio found. The video might be music or protected.")
             return

        best_video_stream = max(video_streams, key=lambda s: int(s["quality"].replace("p", "")))
        
        # Find the best audio stream for separate download
        audio_streams = [s for s in data.get("audioStreams", []) if s.get("bitrate") and s.get("url")]
        best_audio_stream = max(audio_streams, key=lambda s: s["bitrate"]) if audio_streams else None

        video_url = best_video_stream["url"]
        video_quality = best_video_stream["quality"]

        message_text = (
            f"✅ **{data.get('title', 'YouTube Video')}**\n\n"
            f"🔗 *Source:* `https://youtube.com/watch?v={video_id}`\n\n"
            f"🎥 [**Direct Video Link ({video_quality})**]({video_url})"
        )

        if best_audio_stream:
            audio_url = best_audio_stream["url"]
            message_text += f"\n🎵 [**Direct Audio Only**]({audio_url})"

        await processing_message.edit_text(
            message_text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True
        )

    except httpx.RequestError as e:
        logger.error(f"HTTP request failed for ID {video_id}: {e}")
        await processing_message.edit_text("❌ Network error: Could not connect to the API.")
    except KeyError as e:
        logger.error(f"Invalid API response for ID {video_id}: Missing key {e}")
        await processing_message.edit_text("❌ Error: Received an invalid response from the API.")
    except Exception as e:
        logger.error(f"An unexpected error occurred for ID {video_id}: {e}", exc_info=True)
        await processing_message.edit_text("❌ An unexpected error occurred. Please try again later.")

# --- Error Handler ---

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error for debugging purposes."""
    logger.error("Exception while handling an update:", exc_info=context.error)

# --- Main Execution ---

# This `app` variable is what `uvicorn` will look for.
app = Application.builder().token(TOKEN).build()

def main() -> None:
    """Configures and runs the bot in webhook mode."""
    # Read environment variables for webhook setup.
    # Render provides the PORT variable automatically.
    PORT = int(os.environ.get('PORT', 8443))
    # You MUST set WEBHOOK_URL in your Render environment variables.
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    if not WEBHOOK_URL:
        raise ValueError("FATAL: No WEBHOOK_URL found in environment variables")

    # Register all handlers
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("yt", yt))

    # The `main()` function now just sets up the webhook.
    # The actual web server (uvicorn) will run the `app` object.
    # We use a part of the token as a secret path to ensure that only Telegram is sending updates.
    secret_path = TOKEN.split(':')[-1]
    
    logger.info(f"Setting webhook for URL: {WEBHOOK_URL}/{secret_path}")
    
    # This coroutine sets the webhook and starts an internal webserver.
    # It needs to be awaited, which `uvicorn` handles automatically when it runs `app`.
    # We run this setup logic within the `if __name__ == "__main__"` block
    # to avoid it running when uvicorn imports the file.
    # Uvicorn will run the `app` object directly.
    # The setup of the webhook is done by the library itself.
    # All we need to do is provide the run_webhook arguments when the app starts.

if __name__ == "__main__":
    PORT = int(os.environ.get('PORT', 8443))
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    if not WEBHOOK_URL:
        raise ValueError("FATAL: No WEBHOOK_URL found in environment variables")

    # Register handlers inside the main execution block
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("yt", yt))
    
    secret_path = TOKEN.split(':')[-1]

    # Start the bot
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=secret_path,
        webhook_url=f"{WEBHOOK_URL}/{secret_path}"
    )
