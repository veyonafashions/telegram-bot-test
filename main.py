#!/usr/bin/env python3
"""
Telegram YouTube Downloader Bot (Optimized for python-telegram-bot v13.x)
- Uses yt-dlp + FFmpeg to fetch available formats and download
- Lets users choose: audio (very high quality) or video with multiple quality options
- Progress updates, file-size checks (2 GB Telegram limit for bots), thumbnails & metadata
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Callable

import humanize
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
    Bot,
)
from telegram.ext import (
    Updater,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackContext,
)

# ---------------------------- Config ---------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_MAX_FILESIZE = 2 * 1024 * 1024 * 1024  # 2 GB limit for bot uploads

DEFAULT_MAX_VIDEO_HEIGHT = 1080
DEFAULT_AUDIO_PROFILE = "best"

# ---------------------------------------------------------------------
YOUTUBE_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=[^&\s]+|shorts/[^\s/?#&]+)|youtu\.be/[^\s/?#&]+)"
)

# Cookie file path
COOKIES_FILE = "cookies.txt"


@dataclass
class UserSettings:
    max_video_height: int = DEFAULT_MAX_VIDEO_HEIGHT
    audio_profile: str = DEFAULT_AUDIO_PROFILE


@dataclass
class JobState:
    tempdir: Path
    url: str
    selection: Optional[str] = None
    message_id: Optional[int] = None
    progress_msg_id: Optional[int] = None
    info: Optional[dict] = None
    options: Optional[List[dict]] = None
    file_path: Optional[Path] = None


# Global stores (replace with a DB for production)
USER_SETTINGS: Dict[int, UserSettings] = {}
ACTIVE_JOBS: Dict[Tuple[int, int], JobState] = {}
CHAT_DATA_STORE: Dict[int, Dict[str, any]] = {}

# ---------------------------- yt-dlp helpers -------------------------
from yt_dlp import YoutubeDL

def pretty_size(num: Optional[int]) -> str:
    if not num or num <= 0:
        return "?"
    return humanize.naturalsize(num, binary=True)

def pick_audio_postprocessors(profile: str, embed_thumbnail: bool = True):
    pps = []
    if profile == "best":
        pass
    elif profile == "mp3_320":
        pps.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320",
        })
    elif profile == "opus_160":
        pps.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": "opus",
            "preferredquality": "160",
        })
    elif profile == "flac":
        pps.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": "flac",
        })
    if embed_thumbnail:
        pps.extend([
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ])
    return pps

def list_formats(url: str) -> Tuple[dict, List[dict], List[dict]]:
    ydl_opts = {
        "cookiefile": COOKIES_FILE,  # ADDED: Pass cookies to yt-dlp
        "quiet": True,
        "skip_download": True,
        "no_warnings": True,
        "writesubtitles": False,
        "logger": logging.getLogger("yt-dlp-probe"),
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    fmts = info.get("formats", [])
    audio = []
    video_prog = []
    video_only = []

    for f in fmts:
        if f.get("acodec") != "none" and f.get("vcodec") == "none":
            audio.append(f)
        elif f.get("acodec") != "none" and f.get("vcodec") != "none":
            video_prog.append(f)
        elif f.get("acodec") == "none" and f.get("vcodec") != "none":
            video_only.append(f)

    audio.sort(key=lambda x: (x.get("tbr") or 0, x.get("abr") or 0, x.get("filesize") or 0), reverse=True)
    video_prog.sort(key=lambda x: (x.get("height") or 0, x.get("tbr") or 0), reverse=True)
    video_only.sort(key=lambda x: (x.get("height") or 0, x.get("tbr") or 0), reverse=True)

    video_options = []
    for f in video_prog:
        label = f"🎬 {f.get('ext','?').upper()} {f.get('height','?')}p (prog) — {pretty_size(f.get('filesize') or f.get('filesize_approx'))}"
        video_options.append({
            "selector": f"{f['format_id']}",
            "label": label,
            "est_size": f.get("filesize") or f.get("filesize_approx") or 0,
        })
    if video_only:
        best_a = audio[0] if audio else None
        for f in video_only:
            if not best_a:
                continue
            est = (f.get("filesize") or f.get("filesize_approx") or 0) + (best_a.get("filesize") or best_a.get("filesize_approx") or 0)
            label = (
                f"🎞️ {f.get('ext','?').upper()} {f.get('height','?')}p + bestaudio "
                f"— ~{pretty_size(est)}"
            )
            selector = f"{f['format_id']}+bestaudio/best"
            video_options.append({
                "selector": selector,
                "label": label,
                "est_size": est,
            })
    audio_options = []
    for f in audio:
        label = f"🎵 {f.get('ext','?').upper()} {int(f.get('abr') or f.get('tbr') or 0)}kbps — {pretty_size(f.get('filesize') or f.get('filesize_approx'))}"
        audio_options.append({
            "format_id": f["format_id"],
            "label": label,
            "est_size": f.get("filesize") or f.get("filesize_approx") or 0,
        })
    return info, audio_options, video_options

def download_with_ytdlp(
    *,
    url: str,
    tempdir: Path,
    selector: Optional[str] = None,
    audio_profile: Optional[str] = None,
    on_progress=None,
) -> Tuple[Path, dict]:
    outtmpl = str(tempdir / "%(title).200B [%(id)s].%(ext)s")
    ydl_opts = {
        "cookiefile": COOKIES_FILE, # ADDED: Pass cookies to yt-dlp
        "outtmpl": {"default": outtmpl},
        "noprogress": True,
        "quiet": True,
        "ignoreerrors": False,
        "writethumbnail": True,
        "postprocessors": [],
        "merge_output_format": "mp4",
        "logger": logging.getLogger("yt-dlp-dl"),
        "progress_hooks": [on_progress] if on_progress else [],
    }
    if selector:
        ydl_opts["format"] = selector
    else:
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = pick_audio_postprocessors(audio_profile or DEFAULT_AUDIO_PROFILE)
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg not found in PATH. Please install FFmpeg.")
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        file_path = Path(ydl.prepare_filename(info))
        if not file_path.exists():
            vid = info.get("id")
            cand = list(tempdir.glob(f"*[{vid}].*"))
            if cand:
                file_path = cand[0]
    return file_path, info

# ---------------------------- Telegram bot ---------------------------
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ytbot")

def get_user_settings(user_id: int) -> UserSettings:
    if user_id not in USER_SETTINGS:
        USER_SETTINGS[user_id] = UserSettings()
    return USER_SETTINGS[user_id]

def get_chat_data(chat_id: int) -> Dict[str, any]:
    if chat_id not in CHAT_DATA_STORE:
        CHAT_DATA_STORE[chat_id] = {}
    return CHAT_DATA_STORE[chat_id]

def start_cmd(update: Update, context: CallbackContext):
    text = (
        "Send me a YouTube link and I'll fetch the quality options!\n\n"
        "• /audio — Force audio flow (choose profile)\n"
        "• /settings — Tweak defaults (max video height, audio profile)\n"
        "• Tip: I won't upload files over 2 GB."
    )
    update.message.reply_text(text)

def help_cmd(update: Update, context: CallbackContext):
    start_cmd(update, context)

def build_settings_kb(user_id: int) -> InlineKeyboardMarkup:
    s = get_user_settings(user_id)
    rows = [
        [InlineKeyboardButton(f"Max video: {s.max_video_height}p", callback_data="noop")],
        [
            InlineKeyboardButton("2160p", callback_data="set_h_2160"),
            InlineKeyboardButton("1440p", callback_data="set_h_1440"),
            InlineKeyboardButton("1080p", callback_data="set_h_1080"),
            InlineKeyboardButton("720p", callback_data="set_h_720"),
        ],
        [InlineKeyboardButton(f"Audio: {s.audio_profile}", callback_data="noop")],
        [
            InlineKeyboardButton("best", callback_data="set_a_best"),
            InlineKeyboardButton("mp3_320", callback_data="set_a_mp3_320"),
            InlineKeyboardButton("opus_160", callback_data="set_a_opus_160"),
            InlineKeyboardButton("flac", callback_data="set_a_flac"),
        ],
    ]
    return InlineKeyboardMarkup(rows)

def settings_cmd(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    update.message.reply_text("Settings:", reply_markup=build_settings_kb(user_id))

def settings_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    if data.startswith("set_h_"):
        val = int(data.split("_")[-1])
        get_user_settings(user_id).max_video_height = val
        query.answer(f"Max height set to {val}p")
        query.edit_message_reply_markup(build_settings_kb(user_id))
    elif data.startswith("set_a_"):
        val = data.split("set_a_")[-1]
        get_user_settings(user_id).audio_profile = val
        query.answer(f"Audio profile: {val}")
        query.edit_message_reply_markup(build_settings_kb(user_id))
    else:
        query.answer()

def audio_cmd(update: Update, context: CallbackContext):
    update.message.reply_text(
        "Send a YouTube link and I'll prepare top audio options and \n"
        "let you choose extraction profile (best/mp3/opus/flac)."
    )

def chunk_buttons(options: List[dict], prefix: str, max_rows: int = 10) -> List[List[InlineKeyboardButton]]:
    rows: List[List[InlineKeyboardButton]] = []
    for i, opt in enumerate(options[: max_rows * 3]):
        if i % 3 == 0:
            rows.append([])
        rows[-1].append(InlineKeyboardButton(opt["label"][:64], callback_data=f"{prefix}:{i}"))
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return rows

def on_url_message(update: Update, context: CallbackContext):
    message = update.message
    text = message.text.strip()
    m = YOUTUBE_URL_RE.search(text)
    if not m:
        return
    url = m.group(0)
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Check for an active job
    job_key = (chat_id, message.message_id)
    if job_key in ACTIVE_JOBS:
        message.reply_text("An active job already exists in this chat. Please wait.")
        return

    try:
        # Blocking call to list formats
        info, audio_opts, video_opts = list_formats(url)
        title = info.get("title", "(no title)")
        thumb = info.get("thumbnail")

        max_h = get_user_settings(user_id).max_video_height
        filtered = []
        for o in video_opts:
            h = None
            m_h = re.search(r"(\d{3,4})p", o["label"])
            if m_h:
                h = int(m_h.group(1))
            if h is None or h <= max_h:
                filtered.append(o)

        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🎵 Audio options", callback_data=f"show_audio|{message.message_id}"),
              InlineKeyboardButton("🎬 Video options", callback_data=f"show_video|{message.message_id}")]]
        )
        caption = (
            f"<b>{title}</b>\nChoose what to download:"
            f"\n\n🎵 Audio: pick source stream (we'll convert per your profile)."
            f"\n🎬 Video: pick a specific quality."
        )
        sent = message.reply_html(caption, reply_markup=kb, disable_web_page_preview=False)
        
        # Store job state and options in global stores
        job_state = JobState(tempdir=Path(tempfile.mkdtemp(prefix="ytbot-")), url=url, message_id=sent.message_id, info=info)
        ACTIVE_JOBS[job_key] = job_state
        chat_data = get_chat_data(chat_id)
        chat_data[f"opts:{message.message_id}:audio"] = audio_opts
        chat_data[f"opts:{message.message_id}:video"] = filtered
        chat_data[f"job:{message.message_id}"] = job_key
        chat_data[f"title:{message.message_id}"] = title
        chat_data[f"thumb:{message.message_id}"] = thumb
        
    except Exception as e:
        logger.exception("Probe failed")
        message.reply_text(f"Failed to probe formats: {e}")
        # Clean up if a tempdir was created
        if job_key in ACTIVE_JOBS and ACTIVE_JOBS[job_key].tempdir:
            shutil.rmtree(ACTIVE_JOBS[job_key].tempdir, ignore_errors=True)
        ACTIVE_JOBS.pop(job_key, None)

def show_lists_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data
    _, msg_id_s = data.split("|")
    msg_id = int(msg_id_s)
    chat_id = query.effective_chat.id
    chat_data = get_chat_data(chat_id)

    if data.startswith("show_audio|"):
        options = chat_data.get(f"opts:{msg_id}:audio", [])
        kb = InlineKeyboardMarkup(chunk_buttons(options, prefix=f"pick_a|{msg_id}"))
        query.edit_message_reply_markup(kb)
        query.answer("Audio sources")
    elif data.startswith("show_video|"):
        options = chat_data.get(f"opts:{msg_id}:video", [])
        kb = InlineKeyboardMarkup(chunk_buttons(options, prefix=f"pick_v|{msg_id}"))
        query.edit_message_reply_markup(kb)
        query.answer("Video qualities")

def cancel_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer("Cancelled")
    query.edit_message_reply_markup(None)
    # Clean up job state
    job_key = (query.effective_chat.id, query.message.message_id)
    if job_key in ACTIVE_JOBS:
        shutil.rmtree(ACTIVE_JOBS[job_key].tempdir, ignore_errors=True)
        ACTIVE_JOBS.pop(job_key)
    chat_data = get_chat_data(query.effective_chat.id)
    chat_data.pop(f"opts:{query.message.message_id}:audio", None)
    chat_data.pop(f"opts:{query.message.message_id}:video", None)
    chat_data.pop(f"job:{query.message.message_id}", None)

def pick_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data
    kind_msg, idx_s = data.split(":")
    kind, msg_id_s = kind_msg.split("|")
    idx = int(idx_s)
    msg_id = int(msg_id_s)
    chat_id = query.effective_chat.id
    chat_data = get_chat_data(chat_id)

    options = chat_data.get(f"opts:{msg_id}:{'audio' if kind=='pick_a' else 'video'}", [])
    if idx < 0 or idx >= len(options):
        query.answer("Option out of range", show_alert=True)
        return

    job_key = chat_data.get(f"job:{msg_id}")
    if not job_key or job_key not in ACTIVE_JOBS:
        query.answer("Job not found", show_alert=True)
        return

    job = ACTIVE_JOBS[job_key]
    selection = options[idx]

    if kind == "pick_a":
        query.edit_message_text(
            f"Downloading audio: {selection['label']}\nProfile: {get_user_settings(query.from_user.id).audio_profile}"
        )
        query.answer("Audio download started…")
        run_download_flow(update, context, job, audio_selection=selection)
    else:
        query.edit_message_text(
            f"Downloading video: {selection['label']}"
        )
        query.answer("Video download started…")
        run_download_flow(update, context, job, video_selection=selection)

def run_download_flow(
    update: Update,
    context: CallbackContext,
    job: JobState,
    audio_selection: Optional[dict] = None,
    video_selection: Optional[dict] = None,
):
    chat = query.message.chat
    chat_id = chat.id

    
    def progress_hook(d):
        try:
            if d.get('status') == 'downloading':
                p = d.get('downloaded_bytes', 0)
                t = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                if t:
                    pct = int(p * 100 / t)
                    text = f"⬇️ Downloading… {pct}% ({pretty_size(p)}/{pretty_size(t)})"
                else:
                    text = f"⬇️ Downloading… {pretty_size(p)}"
                if job.progress_msg_id:
                    context.bot.edit_message_text(chat_id=chat.id, message_id=job.progress_msg_id, text=text)
                else:
                    m = context.bot.send_message(chat_id=chat.id, text=text)
                    job.progress_msg_id = m.message_id
            elif d.get('status') == 'finished':
                if job.progress_msg_id:
                    context.bot.edit_message_text(chat_id=chat.id, message_id=job.progress_msg_id, text="✅ Processing…")
        except Exception as e:
            logger.error(f"Progress hook failed: {e}")

    try:
        if audio_selection:
            est = audio_selection.get("est_size", 0)
            if est and est > TELEGRAM_MAX_FILESIZE:
                chat.send_message("File likely exceeds Telegram limit (2 GB). Try a lower-bitrate option.")
                return
            
            file_path, info = download_with_ytdlp(
                url=job.url,
                tempdir=job.tempdir,
                selector=None,
                audio_profile=get_user_settings(update.effective_user.id).audio_profile,
                on_progress=progress_hook
            )
            job.file_path = file_path
            job.info = info
            send_audio(chat, job.file_path, job.info)
            
        elif video_selection:
            est = video_selection.get("est_size", 0)
            if est and est > TELEGRAM_MAX_FILESIZE:
                chat.send_message("Selected quality likely exceeds Telegram's 2 GB limit. Choose a smaller quality.")
                return

            file_path, info = download_with_ytdlp(
                url=job.url,
                tempdir=job.tempdir,
                selector=video_selection["selector"],
                audio_profile=None,
                on_progress=progress_hook
            )
            job.file_path = file_path
            job.info = info
            send_video(chat, job.file_path, job.info)
        else:
            chat.send_message("Nothing selected.")
            return

    except Exception as e:
        logger.exception("Download failed")
        chat.send_message(f"❌ Download failed: {e}")
    finally:
        if job.progress_msg_id:
            try:
                context.bot.delete_message(chat_id=chat.id, message_id=job.progress_msg_id)
            except Exception:
                pass
        shutil.rmtree(job.tempdir, ignore_errors=True)
        job_key = (chat.id, job.message_id)
        ACTIVE_JOBS.pop(job_key, None)

def send_audio(chat, file_path: Path, info: dict):
    title = info.get("title") or file_path.stem
    uploader = info.get("uploader") or info.get("channel")
    duration = info.get("duration")
    caption = f"<b>{title}</b>\n{uploader or ''}"
    try:
        chat.send_audio(
            audio=open(file_path, "rb"),
            caption=caption,
            parse_mode="HTML",
            duration=duration if isinstance(duration, int) else None,
            title=title,
            performer=uploader,
        )
    except Exception as e:
        chat.send_message(f"Could not send audio: {e}")

def send_video(chat, file_path: Path, info: dict):
    title = info.get("title") or file_path.stem
    duration = info.get("duration")
    try:
        chat.send_video(
            video=open(file_path, "rb"),
            caption=f"<b>{title}</b>",
            parse_mode="HTML",
            supports_streaming=True,
            duration=duration if isinstance(duration, int) else None,
        )
    except Exception as e:
        chat.send_message(f"Could not send video: {e}")

def error_handler(update: Update, context: CallbackContext) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

def main():
    if not BOT_TOKEN:
        raise SystemExit("Please set BOT_TOKEN env var.")

    updater = Updater(BOT_TOKEN, use_context=True)
    dispatcher = updater.dispatcher

    # Handlers
    dispatcher.add_handler(CommandHandler("start", start_cmd))
    dispatcher.add_handler(CommandHandler("help", help_cmd))
    dispatcher.add_handler(CommandHandler("settings", settings_cmd))
    dispatcher.add_handler(CommandHandler("audio", audio_cmd))

    dispatcher.add_handler(CallbackQueryHandler(settings_cb, pattern=r"^set_(h|a)_"))
    dispatcher.add_handler(CallbackQueryHandler(show_lists_cb, pattern=r"^(show_audio|show_video)\|"))
    dispatcher.add_handler(CallbackQueryHandler(pick_cb, pattern=r"^(pick_a|pick_v)\|"))
    dispatcher.add_handler(CallbackQueryHandler(cancel_cb, pattern=r"^cancel$"))

    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, on_url_message))

    dispatcher.add_error_handler(error_handler)

    logger.info("Bot starting...")

    # --- Decide whether to use webhook or polling ---
    if "RENDER_EXTERNAL_HOSTNAME" in os.environ:  # Running on Render/Heroku
        port = int(os.environ.get("PORT", 8443))
        webhook_url = f"https://{os.environ['RENDER_EXTERNAL_HOSTNAME']}/{BOT_TOKEN}"

        logger.info(f"Starting webhook on port {port}, url={webhook_url}")
        updater.start_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=BOT_TOKEN,
            webhook_url=webhook_url,
        )
    else:  # Local development
        logger.info("Starting polling (local mode)")
        updater.start_polling()

    updater.idle()


if __name__ == "__main__":
    main()
