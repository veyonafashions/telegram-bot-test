#!/usr/bin/env python3
"""
Telegram YouTube Downloader Bot
- Uses python-telegram-bot (PTB) v20+
- Uses yt-dlp + FFmpeg to fetch available formats and download
- Lets users choose: audio (very high quality) or video with multiple quality options
- Progress updates, file-size checks (2 GB Telegram limit for bots), thumbnails & metadata

Setup
-----
1) Python 3.10+
2) `pip install python-telegram-bot==20.7 yt-dlp==2025.1.1 humanize==4.9`  (adjust versions as you like)
3) Install FFmpeg & FFprobe on your system (required for audio extraction/merging).
4) Export your bot token:
   export BOT_TOKEN=123456:ABC-DEF...
5) Run:
   python3 main.py

Security & Legal
---------------
Only download or share content you have the rights to. Respect YouTube/website Terms of Service.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import humanize
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------- Config ---------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_MAX_FILESIZE = 2 * 1024 * 1024 * 1024  # 2 GB limit for bot uploads

# Tweak these defaults if you like
DEFAULT_MAX_VIDEO_HEIGHT = 1080
DEFAULT_AUDIO_PROFILE = "best"  # one of: best, mp3_320, opus_160, flac

# ---------------------------------------------------------------------
YOUTUBE_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=[^&\s]+|shorts/[^\s/?#&]+)|youtu\.be/[^\s/?#&]+)"
)


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


# In-memory stores (swap for a DB if needed)
USER_SETTINGS: Dict[int, UserSettings] = {}
ACTIVE_JOBS: Dict[Tuple[int, int], JobState] = {}
PER_CHAT_LOCKS: Dict[int, asyncio.Semaphore] = {}


# ---------------------------- yt-dlp helpers -------------------------
from yt_dlp import YoutubeDL


def pretty_size(num: Optional[int]) -> str:
    if not num or num <= 0:
        return "?"
    return humanize.naturalsize(num, binary=True)


def pick_audio_postprocessors(profile: str, embed_thumbnail: bool = True):
    """Return yt-dlp postprocessors for requested audio profile."""
    pps = []
    if profile == "best":
        # Keep best audio, container as m4a/webm as provided; still add metadata.
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
    # Always try to embed thumbnail & metadata when possible
    if embed_thumbnail:
        pps.extend([
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ])
    return pps


def list_formats(url: str) -> Tuple[dict, List[dict], List[dict]]:
    """
    Probe available formats with yt-dlp (no download).
    Returns: (info, audio_formats, video_options)
    - audio_formats: sorted by abr desc
    - video_options: merged options (video+audio) and adaptive combos, sorted
    """
    ydl_opts = {
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
    video_prog = []  # progressive (has audio)
    video_only = []  # video-only

    for f in fmts:
        if f.get("acodec") != "none" and f.get("vcodec") == "none":
            audio.append(f)
        elif f.get("acodec") != "none" and f.get("vcodec") != "none":
            video_prog.append(f)
        elif f.get("acodec") == "none" and f.get("vcodec") != "none":
            video_only.append(f)

    # Sorters
    audio.sort(key=lambda x: (x.get("tbr") or 0, x.get("abr") or 0, x.get("filesize") or 0), reverse=True)
    video_prog.sort(key=lambda x: (x.get("height") or 0, x.get("tbr") or 0), reverse=True)
    video_only.sort(key=lambda x: (x.get("height") or 0, x.get("tbr") or 0), reverse=True)

    # Build human-friendly choices
    video_options = []

    for f in video_prog:
        label = f"🎬 {f.get('ext','?').upper()} {f.get('height','?')}p (prog) — {pretty_size(f.get('filesize') or f.get('filesize_approx'))}"
        video_options.append({
            "selector": f"{f['format_id']}",
            "label": label,
            "est_size": f.get("filesize") or f.get("filesize_approx") or 0,
        })

    # For adaptive video-only, pair with bestaudio
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

    # Audio list, top-first
    audio_options = []
    for f in audio:
        label = f"🎵 {f.get('ext','?').upper()} {int(f.get('abr') or f.get('tbr') or 0)}kbps — {pretty_size(f.get('filesize') or f.get('filesize_approx'))}"
        audio_options.append({
            "format_id": f["format_id"],
            "label": label,
            "est_size": f.get("filesize") or f.get("filesize_approx") or 0,
        })

    return info, audio_options, video_options


async def download_with_ytdlp(
    *,
    url: str,
    tempdir: Path,
    selector: Optional[str] = None,
    audio_profile: Optional[str] = None,
    on_progress=None,
) -> Tuple[Path, dict]:
    """
    Download using yt-dlp. If selector is None and audio_profile provided, do audio-only.
    Returns (file_path, info_dict)
    """
    outtmpl = str(tempdir / "%(title).200B [%(id)s].%(ext)s")

    ydl_opts = {
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

    if selector:  # video (could be prog or adaptive combo)
        ydl_opts["format"] = selector
    else:  # audio path
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = pick_audio_postprocessors(audio_profile or DEFAULT_AUDIO_PROFILE)

    # Ensure ffmpeg is found
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg not found in PATH. Please install FFmpeg.")

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        file_path = Path(ydl.prepare_filename(info))

        # If postprocessing changed the extension, locate the final file
        if not file_path.exists():
            # Try to detect by scanning tempdir for matching video id
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


def get_lock(chat_id: int) -> asyncio.Semaphore:
    if chat_id not in PER_CHAT_LOCKS:
        PER_CHAT_LOCKS[chat_id] = asyncio.Semaphore(1)
    return PER_CHAT_LOCKS[chat_id]


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Send me a YouTube link and I\'ll fetch the quality options!\n\n"
        "• /audio — Force audio flow (choose profile)\n"
        "• /settings — Tweak defaults (max video height, audio profile)\n"
        "• Tip: I won\'t upload files over 2 GB."
    )
    await update.message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, context)


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


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(
        "Settings:", reply_markup=build_settings_kb(user_id)
    )


async def settings_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    if data.startswith("set_h_"):
        val = int(data.split("_")[-1])
        get_user_settings(user_id).max_video_height = val
        await query.answer(f"Max height set to {val}p")
        await query.edit_message_reply_markup(build_settings_kb(user_id))
        return
    if data.startswith("set_a_"):
        val = data.split("set_a_")[-1]
        get_user_settings(user_id).audio_profile = val
        await query.answer(f"Audio profile: {val}")
        await query.edit_message_reply_markup(build_settings_kb(user_id))
        return
    await query.answer()


async def audio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send a YouTube link and I\'ll prepare top audio options and \n"
        "let you choose extraction profile (best/mp3/opus/flac)."
    )


def chunk_buttons(options: List[dict], prefix: str, max_rows: int = 10) -> List[List[InlineKeyboardButton]]:
    rows: List[List[InlineKeyboardButton]] = []
    for i, opt in enumerate(options[: max_rows * 3]):  # 3 buttons per row
        if i % 3 == 0:
            rows.append([])
        rows[-1].append(InlineKeyboardButton(opt["label"][:64], callback_data=f"{prefix}:{i}"))
    # add cancel row
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return rows


async def on_url_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text = message.text.strip()

    m = YOUTUBE_URL_RE.search(text)
    if not m:
        return  # ignore non-YouTube messages

    url = m.group(0)
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    lock = get_lock(chat_id)

    if not lock.locked():
        await message.chat.send_action(ChatAction.TYPING)

    async with lock:
        tmp = Path(tempfile.mkdtemp(prefix="ytbot-"))
        job_key = (chat_id, message.message_id)
        ACTIVE_JOBS[job_key] = JobState(tempdir=tmp, url=url)

        try:
            info, audio_opts, video_opts = await asyncio.get_event_loop().run_in_executor(
                None, list_formats, url
            )
            title = info.get("title", "(no title)")
            thumb = info.get("thumbnail")

            # Filter video options by user max height
            max_h = get_user_settings(user_id).max_video_height
            filtered = []
            for o in video_opts:
                # Try parse height from label (e.g., "1080p")
                h = None
                m_h = re.search(r"(\d{3,4})p", o["label"])  # best effort
                if m_h:
                    h = int(m_h.group(1))
                if h is None or h <= max_h:
                    filtered.append(o)

            # Build keyboards
            audio_kb = chunk_buttons(audio_opts[:12], prefix=f"a|{message.message_id}")
            video_kb = chunk_buttons(filtered[:12], prefix=f"v|{message.message_id}")

            caption = (
                f"<b>{title}</b>\nChoose what to download:" \
                f"\n\n🎵 Audio: pick source stream (we\'ll convert per your profile)." \
                f"\n🎬 Video: pick a specific quality."
            )
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🎵 Audio options", callback_data=f"show_audio|{message.message_id}"),
                  InlineKeyboardButton("🎬 Video options", callback_data=f"show_video|{message.message_id}")]]
            )
            sent = await message.reply_html(caption, reply_markup=kb, disable_web_page_preview=False)
            ACTIVE_JOBS[job_key].message_id = sent.message_id

            # Store options in context for callbacks
            context.chat_data[f"opts:{message.message_id}:audio"] = audio_opts
            context.chat_data[f"opts:{message.message_id}:video"] = filtered
            context.chat_data[f"job:{message.message_id}"] = job_key
            context.chat_data[f"title:{message.message_id}"] = title
            context.chat_data[f"thumb:{message.message_id}"] = thumb
        except Exception as e:
            logger.exception("Probe failed")
            await message.reply_text(f"Failed to probe formats: {e}")
            shutil.rmtree(tmp, ignore_errors=True)
            ACTIVE_JOBS.pop(job_key, None)


async def show_lists_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    _, msg_id_s = data.split("|")
    msg_id = int(msg_id_s)

    if data.startswith("show_audio|"):
        options = context.chat_data.get(f"opts:{msg_id}:audio", [])
        kb = InlineKeyboardMarkup(chunk_buttons(options, prefix=f"pick_a|{msg_id}"))
        await query.edit_message_reply_markup(kb)
        await query.answer("Audio sources")
    elif data.startswith("show_video|"):
        options = context.chat_data.get(f"opts:{msg_id}:video", [])
        kb = InlineKeyboardMarkup(chunk_buttons(options, prefix=f"pick_v|{msg_id}"))
        await query.edit_message_reply_markup(kb)
        await query.answer("Video qualities")


async def cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Cancelled")
    await query.edit_message_reply_markup(None)


async def pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data  # e.g., "pick_a|12345:2" or "pick_v|12345:1"

    kind_msg, idx_s = data.split(":")
    kind, msg_id_s = kind_msg.split("|")
    idx = int(idx_s)
    msg_id = int(msg_id_s)

    options = context.chat_data.get(f"opts:{msg_id}:{'audio' if kind=='pick_a' else 'video'}", [])
    if idx < 0 or idx >= len(options):
        await query.answer("Option out of range", show_alert=True)
        return

    # Resolve job
    job_key = context.chat_data.get(f"job:{msg_id}")
    if not job_key or job_key not in ACTIVE_JOBS:
        await query.answer("Job not found", show_alert=True)
        return

    job = ACTIVE_JOBS[job_key]
    selection = options[idx]

    # Update UI & fire download
    if kind == "pick_a":
        await query.edit_message_text(
            f"Downloading audio: {selection['label']}\nProfile: {get_user_settings(query.from_user.id).audio_profile}"
        )
        await query.answer("Audio download started…")
        await run_download_flow(
            update, context, job, audio_selection=selection
        )
    else:
        await query.edit_message_text(
            f"Downloading video: {selection['label']}"
        )
        await query.answer("Video download started…")
        await run_download_flow(
            update, context, job, video_selection=selection
        )


async def run_download_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    job: JobState,
    audio_selection: Optional[dict] = None,
    video_selection: Optional[dict] = None,
):
    chat = update.effective_chat

    async def progress_hook(d):
        if d.get('status') == 'downloading':
            p = d.get('downloaded_bytes', 0)
            t = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            if t:
                pct = int(p * 100 / t)
                text = f"⬇️ Downloading… {pct}% ({pretty_size(p)}/{pretty_size(t)})"
            else:
                text = f"⬇️ Downloading… {pretty_size(p)}"
            try:
                if job.progress_msg_id:
                    await context.bot.edit_message_text(chat_id=chat.id, message_id=job.progress_msg_id, text=text)
                else:
                    m = await chat.send_message(text)
                    job.progress_msg_id = m.message_id
            except Exception:
                pass
        elif d.get('status') == 'finished':
            try:
                if job.progress_msg_id:
                    await context.bot.edit_message_text(chat_id=chat.id, message_id=job.progress_msg_id, text="✅ Processing…")
            except Exception:
                pass

    try:
        if audio_selection:
            # Estimate Telegram limit
            est = audio_selection.get("est_size", 0)
            if est and est > TELEGRAM_MAX_FILESIZE:
                await chat.send_message("File likely exceeds Telegram limit (2 GB). Try a lower-bitrate option.")
                return

            file_path, info = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: download_with_ytdlp(
                    url=job.url,
                    tempdir=job.tempdir,
                    selector=None,
                    audio_profile=get_user_settings(update.effective_user.id).audio_profile,
                    on_progress=lambda d: asyncio.run(asyncio.create_task(progress_hook(d)))
                ),
            )

            await send_audio(chat, file_path, info, context)
        elif video_selection:
            est = video_selection.get("est_size", 0)
            if est and est > TELEGRAM_MAX_FILESIZE:
                await chat.send_message("Selected quality likely exceeds Telegram\'s 2 GB limit. Choose a smaller quality.")
                return

            file_path, info = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: download_with_ytdlp(
                    url=job.url,
                    tempdir=job.tempdir,
                    selector=video_selection["selector"],
                    audio_profile=None,
                    on_progress=lambda d: asyncio.run(asyncio.create_task(progress_hook(d)))
                ),
            )

            await send_video(chat, file_path, info, context)
        else:
            await chat.send_message("Nothing selected.")
            return
    except Exception as e:
        logger.exception("Download failed")
        await chat.send_message(f"❌ Download failed: {e}")
    finally:
        # Clean up progress message
        if job.progress_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat.id, message_id=job.progress_msg_id)
            except Exception:
                pass
        # Cleanup files
        shutil.rmtree(job.tempdir, ignore_errors=True)
        ACTIVE_JOBS.pop((chat.id, job.message_id or 0), None)


async def send_audio(chat, file_path: Path, info: dict, context):
    title = info.get("title") or file_path.stem
    uploader = info.get("uploader") or info.get("channel")
    duration = info.get("duration")
    caption = f"<b>{title}</b>\n{uploader or ''}"

    try:
        await chat.send_action(ChatAction.UPLOAD_AUDIO)
        with file_path.open("rb") as f:
            await chat.send_audio(
                audio=InputFile(f, filename=file_path.name),
                caption=caption,
                parse_mode="HTML",
                duration=duration if isinstance(duration, int) else None,
                title=title,
                performer=uploader,
                thumbnail=None,  # yt-dlp may have embedded; Telegram will show it if present
            )
    except Exception as e:
        await chat.send_message(f"Could not send audio: {e}")


async def send_video(chat, file_path: Path, info: dict, context):
    title = info.get("title") or file_path.stem
    duration = info.get("duration")

    try:
        await chat.send_action(ChatAction.UPLOAD_VIDEO)
        with file_path.open("rb") as f:
            await chat.send_video(
                video=InputFile(f, filename=file_path.name),
                caption=f"<b>{title}</b>",
                parse_mode="HTML",
                supports_streaming=True,
                duration=duration if isinstance(duration, int) else None,
            )
    except Exception as e:
        await chat.send_message(f"Could not send video: {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Please set BOT_TOKEN env var.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("audio", audio_cmd))

    # Callback handlers
    app.add_handler(CallbackQueryHandler(settings_cb, pattern=r"^set_(h|a)_"))
    app.add_handler(CallbackQueryHandler(show_lists_cb, pattern=r"^(show_audio|show_video)\|"))
    app.add_handler(CallbackQueryHandler(pick_cb, pattern=r"^(pick_a|pick_v)\|"))
    app.add_handler(CallbackQueryHandler(cancel_cb, pattern=r"^cancel$"))

    # URL messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_url_message))

    app.add_error_handler(error_handler)

    logger.info("Bot started.")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
