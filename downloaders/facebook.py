"""Facebook downloader via fdownloader.net (HD)."""
from __future__ import annotations

import re
from typing import Any, Dict, List

import requests

from .common import MOBILE_UA, TIMEOUT, download_to_bytes


FB_URL_RE = re.compile(
    r"(?:facebook\.com|fb\.watch|fb\.com|m\.facebook\.com)/", re.I
)

_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "https://fdownloader.net",
    "Referer": "https://fdownloader.net/",
    "User-Agent": MOBILE_UA,
}


def is_facebook(url: str) -> bool:
    return bool(FB_URL_RE.search(url or ""))


def _scrape_links(html: str) -> List[Dict[str, str]]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    out = []
    for a in soup.select("a.download-link-fb"):
        href = a.get("href")
        if not href:
            continue
        title = (a.get("title") or "").replace("Download ", "").strip()
        out.append({"quality": title or "auto", "url": href})
    if not out:
        # Fallback: any anchor pointing to a video-looking URL
        for a in soup.find_all("a"):
            href = a.get("href") or ""
            if re.search(r"\.mp4(\?|$)", href, re.I):
                out.append({"quality": "auto", "url": href})
    return out


def _quality_score(q: str) -> int:
    if not q:
        return 0
    m = re.search(r"(\d{3,4})", q)
    if m:
        return int(m.group(1))
    if "hd" in q.lower():
        return 720
    return 360


def resolve(url: str, *, mode: str = "video", quality: str = "720") -> Dict[str, Any]:
    r = requests.post(
        "https://v3.fdownloader.net/api/ajaxSearch",
        data={
            "q": url,
            "lang": "en",
            "web": "fdownloader.net",
            "v": "v2",
            "w": "",
        },
        headers=_HEADERS,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    payload = r.json() or {}
    html = payload.get("data") or ""
    links = _scrape_links(html)
    if not links:
        raise RuntimeError("fdownloader returned no links")

    target = 720
    try:
        target = int(re.sub(r"\D", "", quality or "720") or 720)
    except ValueError:
        pass
    # Pick the link whose numeric quality is closest to target, ties → higher
    chosen = max(links, key=lambda l: (_quality_score(l["quality"]) <= target,
                                       _quality_score(l["quality"])))

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    thumb = (soup.select_one(".thumbnail img") or {}).get("src") if soup.select_one(".thumbnail img") else None
    duration = (soup.select_one(".content p").get_text(strip=True)
                if soup.select_one(".content p") else None)

    return {
        "ok": True,
        "title": "facebook-video",
        "uploader": None,
        "duration": duration,
        "thumbnail": thumb,
        "webpage_url": url,
        "ext": "mp4",
        "mime": "video/mp4",
        "download_url": chosen["url"],
        "quality": chosen["quality"],
        "provider": "fdownloader",
    }


def fetch_bytes(url: str, *, mode: str = "video", quality: str = "720",
                max_size_mb: int = 1500) -> Dict[str, Any]:
    info = resolve(url, mode=mode, quality=quality)
    info["data"] = download_to_bytes(
        info["download_url"], max_size_mb=max_size_mb,
        referer="https://www.facebook.com/",
    )
    info["size_bytes"] = len(info["data"])
    return info
