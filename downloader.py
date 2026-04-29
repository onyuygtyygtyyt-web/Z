"""Compatibility facade — replaces the old yt-dlp-based downloader.

Internally we now dispatch to the multi-platform `downloaders` package,
which uses public scraper APIs (savetube, ytdown.to, tikwm, fdownloader,
downloadgram, x2twitter…) instead of yt-dlp.  The public surface stays
identical so server.py keeps working unchanged.

Two modes:

* ``resolve(...)`` — return metadata + a direct ``download_url`` (no bytes).
  Preferred path: the WhatsApp bot streams the URL straight to Baileys
  without the file ever touching Python RAM or local disk.

* ``download(...)`` — fetch the bytes into memory.  Only used as a
  fallback when the caller really needs the raw content (e.g. Mega.nz,
  which has no public CDN URL).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from downloaders import download as _download, get_info as _get_info

# Per-platform Referer headers Baileys must replay when streaming the
# direct URL.  Without them most CDNs (TikTok in particular) reject the
# request with 403.
REFERERS = {
    "youtube":   "https://www.youtube.com/",
    "tiktok":    "https://www.tiktok.com/",
    "facebook":  "https://www.facebook.com/",
    "instagram": "https://www.instagram.com/",
    "twitter":   "https://x.com/",
}


def _stream_headers(platform: Optional[str]) -> Dict[str, str]:
    ref = REFERERS.get((platform or "").lower())
    return {"Referer": ref} if ref else {}


def get_info(query: str) -> Dict[str, Any]:
    """Return metadata only — no file."""
    info = _get_info(query, mode="video", quality="720")
    return {
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "duration": info.get("duration"),
        "webpage_url": info.get("webpage_url"),
        "thumbnail": info.get("thumbnail"),
        "ext": info.get("ext"),
        "platform": info.get("platform"),
    }


def resolve(
    query: str,
    mode: str = "video",
    quality: str = "720",
) -> Dict[str, Any]:
    """Return metadata + a direct download URL with the headers needed
    to stream it (no bytes downloaded)."""
    info = _get_info(query, mode=mode, quality=quality)
    durl = info.get("download_url")
    if not durl:
        raise RuntimeError("provider returned no direct download URL")
    return {
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "duration": info.get("duration"),
        "webpage_url": info.get("webpage_url"),
        "thumbnail": info.get("thumbnail"),
        "ext": info.get("ext"),
        "mime": info.get("mime"),
        "platform": info.get("platform"),
        "download_url": durl,
        "stream_headers": _stream_headers(info.get("platform")),
    }


def download(
    query: str,
    mode: str = "video",
    quality: str = "720",
    max_size_mb: int = 1500,
) -> Dict[str, Any]:
    """Download a video or audio file — returns metadata + raw bytes."""
    info = _download(
        query,
        mode=mode,
        quality=quality,
        max_size_mb=max_size_mb,
    )
    return {
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "duration": info.get("duration"),
        "webpage_url": info.get("webpage_url"),
        "ext": info.get("ext"),
        "mime": info.get("mime"),
        "size_bytes": info.get("size_bytes"),
        "data": info["data"],
        "platform": info.get("platform"),
    }
