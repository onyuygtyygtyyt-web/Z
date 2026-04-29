"""Twitter / X downloader via x2twitter.com (with cftoken handshake)."""
from __future__ import annotations

import re
from typing import Any, Dict, List

import requests

from .common import MOBILE_UA, TIMEOUT, download_to_bytes


TWITTER_URL_RE = re.compile(
    r"(?:twitter\.com|x\.com|t\.co|mobile\.twitter\.com)/[^/]+/status/", re.I
)

_BASE = "https://x2twitter.com"
_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-EN,en;q=0.9",
    "Cache-Control": "no-cache",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{_BASE}/en",
    "User-Agent": MOBILE_UA,
}


def is_twitter(url: str) -> bool:
    return bool(TWITTER_URL_RE.search(url or ""))


def _scrape_videos(html: str) -> List[Dict[str, Any]]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for p in soup.select(".dl-action p"):
        a = p.find("a")
        if not a or not a.get("href"):
            continue
        text = p.get_text(" ", strip=True)
        if "MP4" not in text.upper():
            continue
        m = re.search(r"(\d{3,4})p", text)
        out.append({
            "type": "mp4",
            "reso": (m.group(0) if m else None),
            "url": a["href"],
        })
    return out


def _scrape_images(html: str) -> List[str]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for li in soup.select("ul.download-box li"):
        a = li.find("a")
        if a and a.get("href"):
            out.append(a["href"])
    return out


def resolve(url: str, *, mode: str = "video", quality: str = "1024") -> Dict[str, Any]:
    # 1) Get cftoken
    try:
        tok_r = requests.post(
            f"{_BASE}/api/userverify",
            json={"url": url},
            headers=_HEADERS,
            timeout=TIMEOUT,
        )
        tok_r.raise_for_status()
        token = tok_r.json().get("token", "")
    except Exception:
        token = ""

    # 2) Scrape
    r = requests.post(
        f"{_BASE}/api/ajaxSearch",
        data={"q": url, "lang": "id", "cftoken": token},
        headers=_HEADERS,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    payload = r.json() or {}
    if payload.get("status") != "ok":
        raise RuntimeError(f"x2twitter status={payload.get('status')}")

    html = (payload.get("data") or "").replace('\\"', '"')

    videos = _scrape_videos(html)
    images = _scrape_images(html)

    if videos:
        target = quality if quality.endswith("p") else f"{quality}p"
        chosen = next((v for v in videos if v.get("reso") == target), None) or videos[0]
        return {
            "ok": True,
            "title": "twitter-video",
            "uploader": None,
            "duration": None,
            "thumbnail": None,
            "webpage_url": url,
            "ext": "mp4",
            "mime": "video/mp4",
            "download_url": chosen["url"],
            "quality": chosen.get("reso"),
            "provider": "x2twitter",
            "media_type": "video",
        }

    if images:
        return {
            "ok": True,
            "title": "twitter-image",
            "uploader": None,
            "duration": None,
            "thumbnail": images[0],
            "webpage_url": url,
            "ext": "jpg",
            "mime": "image/jpeg",
            "download_url": images[0],
            "provider": "x2twitter",
            "media_type": "image",
            "extra_images": images[1:],
        }

    raise RuntimeError("x2twitter returned no media")


def fetch_bytes(url: str, *, mode: str = "video", quality: str = "1024",
                max_size_mb: int = 1500) -> Dict[str, Any]:
    info = resolve(url, mode=mode, quality=quality)
    info["data"] = download_to_bytes(
        info["download_url"], max_size_mb=max_size_mb,
        referer="https://x.com/",
    )
    info["size_bytes"] = len(info["data"])
    return info
