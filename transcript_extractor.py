"""Transcript extraction utilities.

This module exposes separate low-risk and hard-fallback functions.

Low-risk:
- read local cache
- get YouTube public captions

Hard fallback:
- yt-dlp + optional cookiefile + proxy + Whisper

The category digest flow should call content_resolver.resolve_video_text(),
not get_transcript() directly, so it can try public/web/RSS sources before
using YouTube cookie fallback.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values
from youtube_transcript_api import YouTubeTranscriptApi

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
AUDIO_TMP_DIR = BASE_DIR / "audio_tmp"

_ENV = dotenv_values(BASE_DIR / ".env")

TRANSCRIPT_CACHE_DIR = Path(
    _ENV.get("TRANSCRIPT_CACHE_DIR")
    or os.environ.get("TRANSCRIPT_CACHE_DIR")
    or str(BASE_DIR / "transcript_cache")
)

WHISPER_MODEL = (
    _ENV.get("WHISPER_MODEL")
    or os.environ.get("WHISPER_MODEL")
    or "base"
)

_PROXY_URL = (
    _ENV.get("HTTPS_PROXY")
    or _ENV.get("https_proxy")
    or _ENV.get("HTTP_PROXY")
    or _ENV.get("http_proxy")
    or os.environ.get("HTTPS_PROXY")
    or os.environ.get("https_proxy")
    or os.environ.get("HTTP_PROXY")
    or os.environ.get("http_proxy")
)

_YOUTUBE_COOKIE_FILE = (
    _ENV.get("YOUTUBE_COOKIE_FILE")
    or os.environ.get("YOUTUBE_COOKIE_FILE")
    or ""
)

if _PROXY_URL:
    os.environ["HTTP_PROXY"] = _PROXY_URL
    os.environ["HTTPS_PROXY"] = _PROXY_URL
    os.environ["http_proxy"] = _PROXY_URL
    os.environ["https_proxy"] = _PROXY_URL
    os.environ["NO_PROXY"] = "openrouter.ai,api.openrouter.ai,localhost,127.0.0.1"
    os.environ["no_proxy"] = "openrouter.ai,api.openrouter.ai,localhost,127.0.0.1"

# Make Deno visible to yt-dlp when running from cron.
_DENO_BIN_DIR = str(Path.home() / ".deno" / "bin")
if Path(_DENO_BIN_DIR).exists() and _DENO_BIN_DIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] = f"{_DENO_BIN_DIR}:{os.environ.get('PATH', '')}"


def _safe_id(value: str) -> str:
    return (
        str(value)
        .replace(":", "_")
        .replace("/", "_")
        .replace("\\", "_")
        .replace("?", "_")
        .replace("&", "_")
        .replace("=", "_")
    )


def transcript_cache_path(platform: str, source_id: str) -> Path:
    TRANSCRIPT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return TRANSCRIPT_CACHE_DIR / f"{platform}_{_safe_id(source_id)}.txt"


def read_transcript_cache(platform: str, source_id: str, min_chars: int = 100) -> Optional[str]:
    path = transcript_cache_path(platform, source_id)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="ignore").strip()
    if len(text) >= min_chars:
        logger.info("Using transcript cache: %s (%d chars)", path, len(text))
        return text
    return None


def write_transcript_cache(platform: str, source_id: str, text: str, min_chars: int = 100) -> None:
    text = (text or "").strip()
    if len(text) < min_chars:
        return
    path = transcript_cache_path(platform, source_id)
    path.write_text(text, encoding="utf-8")
    logger.info("Saved transcript cache: %s (%d chars)", path, len(text))


def get_youtube_captions_only(video_id: str) -> Optional[str]:
    """Try YouTube public captions only. Does not use yt-dlp."""
    cached = read_transcript_cache("youtube", video_id)
    if cached:
        return cached

    try:
        ytt = YouTubeTranscriptApi()
        transcript_list = ytt.list(video_id)

        try:
            transcript = transcript_list.find_transcript(
                ["zh", "zh-Hans", "zh-CN", "zh-TW", "zh-Hant", "en"]
            )
        except Exception:
            transcript = transcript_list.find_generated_transcript(
                ["zh", "zh-Hans", "zh-CN", "zh-TW", "zh-Hant", "en"]
            )

        entries = transcript.fetch()
        parts = []
        for entry in entries:
            if hasattr(entry, "text"):
                parts.append(entry.text)
            elif isinstance(entry, dict):
                parts.append(entry.get("text", ""))

        text = " ".join(parts).strip()
        if text:
            logger.info("Got YouTube public captions for %s (%d chars)", video_id, len(text))
            write_transcript_cache("youtube", video_id, text)
            return text

    except Exception as exc:
        logger.warning("No YouTube public captions for %s: %s", video_id, exc)

    return None


def _build_ydl_opts(outtmpl: str, use_youtube_cookies: bool = False) -> dict:
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "64",
            }
        ],
        "quiet": True,
        "no_warnings": False,
        "noplaylist": True,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 30,
    }

    if _PROXY_URL:
        ydl_opts["proxy"] = _PROXY_URL

    if use_youtube_cookies and _YOUTUBE_COOKIE_FILE:
        cookie_path = Path(_YOUTUBE_COOKIE_FILE).expanduser()
        if cookie_path.exists():
            ydl_opts["cookiefile"] = str(cookie_path)
            logger.info("yt-dlp will use cookie file: %s", cookie_path)
        else:
            logger.warning("YOUTUBE_COOKIE_FILE is set but missing: %s", cookie_path)

    return ydl_opts


def transcribe_url_with_whisper(
    source_id: str,
    url: str,
    platform: str = "external_audio",
    use_youtube_cookies: bool = False,
) -> str:
    """Download an audio/video URL and transcribe with Whisper."""
    cached = read_transcript_cache(platform, source_id)
    if cached:
        return cached

    import whisper
    import yt_dlp

    AUDIO_TMP_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = _safe_id(source_id)
    audio_path = AUDIO_TMP_DIR / f"{safe_id}.mp3"

    try:
        logger.info("Downloading audio for %s from %s", source_id, url)
        ydl_opts = _build_ydl_opts(
            str(AUDIO_TMP_DIR / f"{safe_id}.%(ext)s"),
            use_youtube_cookies=use_youtube_cookies,
        )

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if not audio_path.exists():
            raise RuntimeError(f"Audio download failed for {source_id}")

        logger.info("Transcribing with Whisper model=%s ...", WHISPER_MODEL)
        model = whisper.load_model(WHISPER_MODEL)
        result = model.transcribe(str(audio_path))
        text = (result.get("text") or "").strip()

        if not text:
            raise RuntimeError(f"Whisper returned empty text for {source_id}")

        write_transcript_cache(platform, source_id, text)
        return text

    finally:
        if audio_path.exists():
            audio_path.unlink()


def transcribe_youtube_hard_fallback(video_id: str) -> str:
    """Last resort: YouTube cookie/proxy + yt-dlp + Whisper."""
    cached = read_transcript_cache("youtube", video_id)
    if cached:
        return cached

    url = f"https://www.youtube.com/watch?v={video_id}"
    return transcribe_url_with_whisper(
        source_id=video_id,
        url=url,
        platform="youtube",
        use_youtube_cookies=True,
    )


def get_transcript(video_id: str) -> str:
    """Backward-compatible function.

    Old behavior is preserved:
    YouTube public captions first, then hard fallback.
    New digest flow should prefer content_resolver.resolve_video_text().
    """
    text = get_youtube_captions_only(video_id)
    if text:
        return text

    logger.info("Falling back to YouTube hard fallback for %s", video_id)
    try:
        return transcribe_youtube_hard_fallback(video_id)
    except Exception as exc:
        logger.error("YouTube hard fallback failed for %s: %s", video_id, exc)
        raise RuntimeError(
            f"Could not extract transcript for video {video_id}. "
            "Neither YouTube captions nor hard fallback succeeded."
        ) from exc


# ── Bilibili transcript extraction ──────────────────────────────────────────

async def _get_bilibili_subtitles(bvid: str) -> Optional[str]:
    try:
        import config
        from bilibili_api import Credential, video

        credential = None
        if config.BILIBILI_SESSDATA and config.BILIBILI_BILI_JCT and config.BILIBILI_BUVID3:
            credential = Credential(
                sessdata=config.BILIBILI_SESSDATA,
                bili_jct=config.BILIBILI_BILI_JCT,
                buvid3=config.BILIBILI_BUVID3,
            )

        v = video.Video(bvid=bvid, credential=credential)
        info = await v.get_info()

        subtitle_info = info.get("subtitle", {})
        subtitle_list = subtitle_info.get("list", [])
        if not subtitle_list:
            logger.info("No subtitles available for Bilibili video %s", bvid)
            return None

        selected = None
        for sub in subtitle_list:
            lang = sub.get("lan", "")
            if "zh" in lang or "cn" in lang:
                selected = sub
                break
        if selected is None:
            selected = subtitle_list[0]

        subtitle_url = selected.get("subtitle_url", "")
        if not subtitle_url:
            return None
        if subtitle_url.startswith("//"):
            subtitle_url = "https:" + subtitle_url

        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(subtitle_url)
            resp.raise_for_status()
            subtitle_data = resp.json()

        body = subtitle_data.get("body", [])
        text = " ".join(entry.get("content", "") for entry in body).strip()

        if text:
            logger.info("Got Bilibili subtitles for %s (%d chars)", bvid, len(text))
            write_transcript_cache("bilibili", bvid, text)
            return text

    except Exception as exc:
        logger.warning("Failed to get Bilibili subtitles for %s: %s", bvid, exc)

    return None


def get_bilibili_transcript(bvid: str) -> str:
    cached = read_transcript_cache("bilibili", bvid)
    if cached:
        return cached

    text = asyncio.run(_get_bilibili_subtitles(bvid))
    if text:
        return text

    bilibili_url = f"https://www.bilibili.com/video/{bvid}"
    source_id = f"bilibili_{bvid}"

    logger.info("Falling back to Bilibili audio transcription for %s", bvid)
    try:
        text = transcribe_url_with_whisper(
            source_id=source_id,
            url=bilibili_url,
            platform="bilibili",
            use_youtube_cookies=False,
        )
        write_transcript_cache("bilibili", bvid, text)
        return text
    except Exception as exc:
        logger.error("Bilibili fallback failed for %s: %s", bvid, exc)
        raise RuntimeError(
            f"Could not extract transcript for Bilibili video {bvid}. "
            "Neither subtitles nor Whisper transcription succeeded."
        ) from exc
