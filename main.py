import logging
import os
import yt_dlp
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# Start command
@dp.message_handler(commands=["start"])
async def start(msg: types.Message):
    await msg.reply("🎬 Send me a YouTube link and I'll download the video for you.")

# Handle YouTube links
@dp.message_handler(lambda m: m.text and "youtube.com" in m.text or "youtu.be" in m.text)
async def handle_youtube(msg: types.Message):
    url = msg.text.strip()
    await msg.reply("⏳ Downloading... please wait.")

    # Download video with yt-dlp
    ydl_opts = {
        "outtmpl": "downloads/%(title).50s.%(ext)s",
        "format": "mp4",  # best mp4
        "quiet": True,
    }

    os.makedirs("downloads", exist_ok=True)
    file_path = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = ydl.prepare_filename(info)

        # Send file back
        with open(file_path, "rb") as video:
            await bot.send_video(msg.chat.id, video, caption=f"✅ {info['title']}")
    except Exception as e:
        await msg.reply(f"❌ Error: {e}")

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
