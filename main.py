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
