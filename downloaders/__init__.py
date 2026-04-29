"""Multi-platform media downloader (no yt-dlp).

Each platform has its own scraper module wrapping a public web service.
The :func:`download` and :func:`get_info` facades auto-detect the platform
from the URL (or fall back to a YouTube search for free-text queries) and
return a uniform dict.

Supported platforms (auto-detected):
  • YouTube         — savetube.me + ytdown.to (audio/video)
  • TikTok          — tikwm.com (no watermark)
  • Facebook        — fdownloader.net (HD)
  • Instagram       — downloadgram.org → snapinsta.app fallback
  • Twitter / X     — x2twitter.com
  • Pinterest       — handled by pinterest_downloader.py
  • MediaFire / GDrive / Mega — handled by cloud_downloader.py
"""
from __future__ import annotations

from typing import Any, Dict

from . import facebook, instagram, tiktok, twitter, youtube
from .common import download_to_bytes, fmt_size, is_url
from .yt_search import first_url as yt_first_url
from .yt_search import search as yt_search


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def detect_platform(url: str) -> str:
    if youtube.is_youtube(url):
        return "youtube"
    if tiktok.is_tiktok(url):
        return "tiktok"
    if facebook.is_facebook(url):
        return "facebook"
    if instagram.is_instagram(url):
        return "instagram"
    if twitter.is_twitter(url):
        return "twitter"
    return "unknown"


_RESOLVERS = {
    "youtube": youtube.resolve,
    "tiktok": tiktok.resolve,
    "facebook": facebook.resolve,
    "instagram": instagram.resolve,
    "twitter": twitter.resolve,
}

_FETCHERS = {
    "youtube": youtube.fetch_bytes,
    "tiktok": tiktok.fetch_bytes,
    "facebook": facebook.fetch_bytes,
    "instagram": instagram.fetch_bytes,
    "twitter": twitter.fetch_bytes,
}


# ---------------------------------------------------------------------------
# Public API (mirrors the old downloader.py signatures)
# ---------------------------------------------------------------------------

def get_info(query: str, *, mode: str = "video", quality: str = "720") -> Dict[str, Any]:
    """Return metadata + a direct download URL (no bytes)."""
    url = query if is_url(query) else (yt_first_url(query) or "")
    if not url:
        raise RuntimeError(f"No YouTube result for: {query}")
    platform = detect_platform(url)
    resolver = _RESOLVERS.get(platform)
    if not resolver:
        raise RuntimeError(f"Unsupported platform: {url}")
    info = resolver(url, mode=mode, quality=quality) if platform != "tiktok" else resolver(url, mode=mode)
    info["platform"] = platform
    return info


def download(
    query: str,
    *,
    mode: str = "video",
    quality: str = "720",
    max_size_mb: int = 1500,
) -> Dict[str, Any]:
    """Download a video/audio file. Returns metadata + raw bytes in `data`."""
    url = query if is_url(query) else (yt_first_url(query) or "")
    if not url:
        raise RuntimeError(f"No YouTube result for: {query}")
    platform = detect_platform(url)
    fetcher = _FETCHERS.get(platform)
    if not fetcher:
        raise RuntimeError(f"Unsupported platform: {url}")
    if platform == "tiktok":
        info = fetcher(url, mode=mode, max_size_mb=max_size_mb)
    else:
        info = fetcher(url, mode=mode, quality=quality, max_size_mb=max_size_mb)
    info["platform"] = platform
    return info


__all__ = [
    "download",
    "get_info",
    "detect_platform",
    "yt_search",
    "yt_first_url",
    "youtube",
    "tiktok",
    "facebook",
    "instagram",
    "twitter",
    "is_url",
    "fmt_size",
    "download_to_bytes",
]
