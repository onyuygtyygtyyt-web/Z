"""TikTok downloader via tikwm.com (no watermark)."""
from __future__ import annotations

import re
from typing import Any, Dict

import requests

from .common import MOBILE_UA, TIMEOUT, download_to_bytes


TIKTOK_URL_RE = re.compile(
    r"(?:tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)/", re.I
)

_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Cookie": "current_language=en",
    "User-Agent": MOBILE_UA,
}


def is_tiktok(url: str) -> bool:
    return bool(TIKTOK_URL_RE.search(url or ""))


def resolve(url: str, *, mode: str = "video") -> Dict[str, Any]:
    r = requests.post(
        "https://tikwm.com/api/",
        data={"url": url, "hd": "1"},
        headers=_HEADERS,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    payload = r.json() or {}
    if payload.get("code") not in (0, None):
        raise RuntimeError(f"tikwm error: {payload.get('msg') or payload}")

    data = payload.get("data") or {}
    if not data:
        raise RuntimeError("tikwm returned no data")

    if mode == "audio":
        durl = (data.get("music_info") or {}).get("play") or data.get("music")
        if not durl:
            raise RuntimeError("No audio track returned")
        return {
            "ok": True,
            "title": data.get("title") or "tiktok-audio",
            "uploader": (data.get("author") or {}).get("nickname"),
            "duration": data.get("duration"),
            "thumbnail": data.get("cover"),
            "webpage_url": url,
            "ext": "mp3",
            "mime": "audio/mpeg",
            "download_url": durl,
            "provider": "tikwm",
        }

    durl = data.get("hdplay") or data.get("play") or data.get("wmplay")
    if not durl:
        raise RuntimeError("No video URL returned")
    return {
        "ok": True,
        "title": data.get("title") or "tiktok",
        "uploader": (data.get("author") or {}).get("nickname"),
        "duration": data.get("duration"),
        "thumbnail": data.get("cover"),
        "webpage_url": url,
        "ext": "mp4",
        "mime": "video/mp4",
        "download_url": durl,
        "provider": "tikwm",
    }


def fetch_bytes(url: str, *, mode: str = "video",
                max_size_mb: int = 1500) -> Dict[str, Any]:
    info = resolve(url, mode=mode)
    info["data"] = download_to_bytes(
        info["download_url"], max_size_mb=max_size_mb,
        referer="https://www.tiktok.com/",
    )
    info["size_bytes"] = len(info["data"])
    return info
