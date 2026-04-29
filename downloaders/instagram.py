"""Instagram downloader — tries downloadgram.org first, then snapinsta as fallback."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import requests

from .common import MOBILE_UA, TIMEOUT, download_to_bytes, ext_from_url


IG_URL_RE = re.compile(
    r"(?:instagram\.com|instagr\.am)/(?:p|reel|tv|reels)/", re.I
)


def is_instagram(url: str) -> bool:
    return bool(IG_URL_RE.search(url or ""))


def _downloadgram(url: str) -> Optional[Dict[str, Any]]:
    try:
        r = requests.post(
            "https://api.downloadgram.org/media",
            data={"url": url},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": MOBILE_UA,
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
    except Exception:
        return None

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(r.text or "", "html.parser")
    a = soup.find("a", href=True)
    if not a:
        return None
    durl = a["href"].replace('\\"', "")
    ext = ext_from_url(durl, fallback="mp4")
    is_video = ext in ("mp4", "mov", "webm")
    return {
        "ok": True,
        "title": "instagram",
        "uploader": None,
        "duration": None,
        "thumbnail": None,
        "webpage_url": url,
        "ext": ext,
        "mime": ("video/mp4" if is_video else "image/jpeg"),
        "download_url": durl,
        "provider": "downloadgram",
        "media_type": "video" if is_video else "image",
    }


def _snapinsta(url: str) -> Optional[Dict[str, Any]]:
    try:
        r = requests.post(
            "https://snapinsta.app/api/ajaxSearch",
            data={"q": url, "t": "media", "lang": "en"},
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": "https://snapinsta.app",
                "Referer": "https://snapinsta.app/",
                "User-Agent": MOBILE_UA,
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
    except Exception:
        return None

    payload = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"data": r.text}
    html = payload.get("data") or ""

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    candidates: List[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"\.(mp4|jpg|jpeg|webp|png)(\?|$)", href, re.I):
            candidates.append(href)
    if not candidates:
        return None

    durl = candidates[0]
    ext = ext_from_url(durl, fallback="mp4")
    is_video = ext in ("mp4", "mov", "webm")
    return {
        "ok": True,
        "title": "instagram",
        "uploader": None,
        "duration": None,
        "thumbnail": None,
        "webpage_url": url,
        "ext": ext,
        "mime": ("video/mp4" if is_video else "image/jpeg"),
        "download_url": durl,
        "provider": "snapinsta",
        "media_type": "video" if is_video else "image",
    }


def resolve(url: str, *, mode: str = "video", quality: str = "720") -> Dict[str, Any]:
    for fn in (_downloadgram, _snapinsta):
        info = fn(url)
        if info and info.get("download_url"):
            return info
    raise RuntimeError("All Instagram providers failed")


def fetch_bytes(url: str, *, mode: str = "video", quality: str = "720",
                max_size_mb: int = 1500) -> Dict[str, Any]:
    info = resolve(url, mode=mode, quality=quality)
    info["data"] = download_to_bytes(
        info["download_url"], max_size_mb=max_size_mb,
        referer="https://www.instagram.com/",
    )
    info["size_bytes"] = len(info["data"])
    return info
