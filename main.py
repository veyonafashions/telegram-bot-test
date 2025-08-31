import os
import requests
from telegram.ext import Application, CommandHandler

# Telegram bot token (set in Render environment variables)
TOKEN = os.getenv("BOT_TOKEN")

# Public Piped API (Invidious alternative)
PIPED_API = "https://pipedapi.kavin.rocks"

async def start(update, context):
    await update.message.reply_text("🎬 Send /yt <YouTube Video ID> to get the video link!")

async def yt(update, context):
    if len(context.args) == 0:
        await update.message.reply_text("⚠️ Usage: /yt <video_id>\nExample: /yt dQw4w9WgXcQ")
        return

    video_id = context.args[0].strip()
    url = f"{PIPED_API}/streams/{video_id}"

    try:
        resp = requests.get(url)
        data = resp.json()

        if "videoStreams" not in data or not data["videoStreams"]:
            await update.message.reply_text("❌ Couldn’t fetch video. Try another ID.")
            return

        # Pick best quality video
        video_stream = max(data["videoStreams"], key=lambda v: v.get("qualityLabel", ""))
        video_url = video_stream["url"]

        # Reply with download link
        await update.message.reply_text(
            f"✅ Video: https://youtube.com/watch?v={video_id}\n"
            f"🎥 Direct Link: {video_url}"
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("yt", yt))
    app.run_polling()

if __name__ == "__main__":
    main()
