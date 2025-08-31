import os
import requests
from telegram.ext import Application, CommandHandler

# Bot token from Render env vars
TOKEN = os.getenv("BOT_TOKEN")

# Piped API instance (you can change to another if rate limited)
PIPED_API = "https://pipedapi.kavin.rocks"


# /start command
async def start(update, context):
    await update.message.reply_text(
        "🎬 Send `/yt <YouTube Video ID>` or `/yt <YouTube URL>` to get a direct video link!",
        parse_mode="Markdown",
    )


# /yt command
async def yt(update, context):
    if len(context.args) == 0:
        await update.message.reply_text(
            "⚠️ Usage: `/yt <video_id or YouTube URL>`\nExample: `/yt dQw4w9WgXcQ`",
            parse_mode="Markdown",
        )
        return

    query = context.args[0].strip()

    # Extract video_id from full YouTube URL if needed
    if "youtube.com" in query or "youtu.be" in query:
        if "v=" in query:
            video_id = query.split("v=")[1].split("&")[0]
        else:
            video_id = query.split("/")[-1]
    else:
        video_id = query

    url = f"{PIPED_API}/streams/{video_id}"

    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()

        if "videoStreams" not in data or not data["videoStreams"]:
            await update.message.reply_text("❌ Couldn’t fetch video. Try another one.")
            return

        # Best video
        video_stream = max(data["videoStreams"], key=lambda v: v.get("qualityLabel", ""))
        video_url = video_stream["url"]

        # Best audio
        audio_stream = max(data.get("audioStreams", []), key=lambda a: a.get("bitrate", 0))
        audio_url = audio_stream["url"] if audio_stream else None

        msg = (
            f"✅ **YouTube Video Found**\n"
            f"🔗 https://youtube.com/watch?v={video_id}\n\n"
            f"🎥 [Video Link]({video_url})\n"
        )

        if audio_url:
            msg += f"🎵 [Audio Link]({audio_url})"

        await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")


def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("yt", yt))

    # ⚡ Use polling for dev, webhook if you deploy on Render
    app.run_polling()


if __name__ == "__main__":
    main()
