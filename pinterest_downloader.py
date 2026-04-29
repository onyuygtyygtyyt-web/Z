"""Pinterest search and download helper for Omar bot.

Uses Pinterest's own BaseSearchResource JSON API for search (same approach
as the silana plugin) and direct CDN scraping for individual pin pages —
no yt-dlp dependency.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_BASE = "https://www.pinterest.com"
_SEARCH = "/resource/BaseSearchResource/get/"

_SEARCH_HEADERS = {
    "Accept": "application/json, text/javascript, */*, q=0.01",
    "Referer": "https://www.pinterest.com/",
    "User-Agent": UA,
    "X-App-Version": "a9522f",
    "X-Pinterest-AppState": "active",
    "X-Pinterest-PWS-Handler": "www/[username]/[slug].js",
    "X-Requested-With": "XMLHttpRequest",
}

_HTML_HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en,ar;q=0.8,fr;q=0.6",
}

TIMEOUT = 30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_pin_url(url: str) -> Optional[str]:
    m = re.search(r"pinterest\.[^/]+/pin/(\d+)", url or "")
    if m:
        return f"https://www.pinterest.com/pin/{m.group(1)}/"
    return None


def _is_cdn_image(url: str) -> bool:
    return bool(re.search(r"i\.pinimg\.com|pinimg\.com/(?:\d+x|originals)", url or ""))


def _ext_from_url(url: str, fallback: str = "jpg") -> str:
    m = re.search(r"\.(jpg|jpeg|png|gif|webp|mp4|mov)(\?|$)", url, re.IGNORECASE)
    if m:
        ext = m.group(1).lower()
        return "jpg" if ext == "jpeg" else ext
    return fallback


def _mime_for_ext(ext: str) -> str:
    return {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "gif": "image/gif",
        "webp": "image/webp",
        "mp4": "video/mp4", "mov": "video/mp4",
    }.get(ext, "image/jpeg")


def _download_image_url(url: str, *, max_mb: int = 50) -> Optional[bytes]:
    try:
        r = requests.get(url, headers=_HTML_HEADERS, timeout=TIMEOUT, stream=True)
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        if "image" not in ct and "video" not in ct and "octet" not in ct:
            return None
        chunks: List[bytes] = []
        size = 0
        for chunk in r.iter_content(8192):
            if not chunk:
                continue
            chunks.append(chunk)
            size += len(chunk)
            if size > max_mb * 1024 * 1024:
                return None
        return b"".join(chunks)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Cookie + search
# ---------------------------------------------------------------------------

def _get_cookies() -> str:
    try:
        r = requests.get(_BASE, headers=_HTML_HEADERS, timeout=TIMEOUT)
        cookies = r.cookies.get_dict()
        return "; ".join(f"{k}={v}" for k, v in cookies.items())
    except Exception:
        return ""


def search_pinterest(query: str, num_results: int = 6) -> Dict[str, Any]:
    """Search Pinterest pins matching *query* via the BaseSearchResource API."""
    if not query:
        return {"ok": False, "error": "empty query", "results": []}

    cookies = _get_cookies()
    headers = dict(_SEARCH_HEADERS)
    if cookies:
        headers["Cookie"] = cookies

    params = {
        "source_url": f"/search/pins/?q={query}",
        "data": json.dumps({
            "options": {
                "isPrefetch": False,
                "query": query,
                "scope": "pins",
                "bookmarks": [""],
                "page_size": max(num_results, 10),
            },
            "context": {},
        }),
    }

    try:
        r = requests.get(_BASE + _SEARCH, headers=headers, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        payload = r.json() or {}
    except Exception as e:
        return {"ok": False, "error": f"pinterest API failed: {e}", "results": []}

    raw = (
        payload.get("resource_response", {}).get("data", {}).get("results")
        or []
    )
    results: List[Dict[str, Any]] = []
    for item in raw:
        images = item.get("images") or {}
        orig = images.get("orig") or images.get("736x") or {}
        image_url = orig.get("url")
        if not image_url:
            continue
        pinner = item.get("pinner") or {}
        results.append({
            "id": item.get("id"),
            "pin_url": f"https://www.pinterest.com/pin/{item.get('id')}/" if item.get("id") else None,
            "image_url": image_url,
            "thumbnail": (images.get("236x") or orig).get("url"),
            "title": item.get("title") or item.get("description") or query,
            "description": item.get("description") or "",
            "uploader": pinner.get("full_name") or pinner.get("username"),
            "uploader_username": pinner.get("username"),
        })
        if len(results) >= num_results:
            break

    return {"ok": True, "query": query, "results": results}


# ---------------------------------------------------------------------------
# Pin page scrape (fallback for individual URLs)
# ---------------------------------------------------------------------------

def _scrape_pin_page(pin_url: str) -> Dict[str, Optional[str]]:
    """Pull the largest media URL out of a pinterest.com/pin/<id>/ page."""
    try:
        r = requests.get(pin_url, headers=_HTML_HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        html = r.text
    except Exception:
        return {"video": None, "image": None, "title": None}

    video = None
    image = None

    m = re.search(r'"video_url":"([^"]+)"', html)
    if m:
        video = m.group(1).replace("\\u002F", "/").replace("\\/", "/")

    m = re.search(r'"orig_url":"([^"]+)"', html) or \
        re.search(r'property="og:image"\s+content="([^"]+)"', html)
    if m:
        image = m.group(1).replace("\\u002F", "/").replace("\\/", "/")

    if not image:
        m = re.search(r'(https://i\.pinimg\.com/originals/[^"\'\\]+)', html)
        if m:
            image = m.group(1)
    if not image:
        m = re.search(r'(https://i\.pinimg\.com/\d+x/[^"\'\\]+)', html)
        if m:
            image = m.group(1)

    title = None
    m = re.search(r'<title>([^<]+)</title>', html)
    if m:
        title = m.group(1).strip()

    return {"video": video, "image": image, "title": title}


# ---------------------------------------------------------------------------
# Public download
# ---------------------------------------------------------------------------

def download_pinterest(url_or_query: str, max_size_mb: int = 100) -> Dict[str, Any]:
    """Download a Pinterest pin (image or video).

    *url_or_query* can be:
      - A full pinterest.com/pin/... URL  → download that specific pin
      - A direct i.pinimg.com CDN URL    → download bytes
      - A plain search query             → search and download first result
    """
    source = (url_or_query or "").strip()

    # If not a URL → search first
    if not source.lower().startswith(("http://", "https://")):
        res = search_pinterest(source, num_results=5)
        hits = res.get("results") or []
        chosen = next((h for h in hits if h.get("image_url") or h.get("pin_url")), None)
        if not chosen:
            return {"ok": False, "error": f"ما لقيت تا صورة على Pinterest: {url_or_query}"}
        source = chosen.get("pin_url") or chosen.get("image_url") or ""

    # Direct CDN URL → just fetch
    if _is_cdn_image(source) or any(
        source.lower().split("?")[0].endswith(ext)
        for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4")
    ):
        data = _download_image_url(source, max_mb=max_size_mb)
        if not data:
            return {"ok": False, "error": "فشل تحميل ملف Pinterest"}
        ext = _ext_from_url(source)
        is_video = ext == "mp4"
        return {
            "ok": True,
            "data": data,
            "mime": _mime_for_ext(ext),
            "ext": ext,
            "title": "pinterest",
            "size_bytes": len(data),
            "source_url": source,
            "media_type": "video" if is_video else "image",
        }

    # Pin page
    pin_url = _clean_pin_url(source) or source
    scraped = _scrape_pin_page(pin_url)

    # Prefer video if present
    target = scraped.get("video") or scraped.get("image")
    if not target:
        return {"ok": False, "error": "ما قدرتش نطلع رابط من صفحة Pinterest"}

    data = _download_image_url(target, max_mb=max_size_mb)
    if not data:
        return {"ok": False, "error": "فشل تحميل ملف Pinterest"}

    ext = _ext_from_url(target, fallback="jpg" if not scraped.get("video") else "mp4")
    is_video = ext == "mp4"
    return {
        "ok": True,
        "data": data,
        "mime": _mime_for_ext(ext),
        "ext": ext,
        "title": scraped.get("title") or "pinterest",
        "size_bytes": len(data),
        "source_url": pin_url,
        "media_type": "video" if is_video else "image",
    }


# ---------------------------------------------------------------------------
# Batch search → fetch top N images
# ---------------------------------------------------------------------------

def search_and_fetch_images(query: str, num_images: int = 4) -> Dict[str, Any]:
    """Search Pinterest and download the first *num_images* image bytes."""
    res = search_pinterest(query, num_results=max(num_images * 2, 6))
    if not res.get("ok") or not res.get("results"):
        return {"ok": False, "error": res.get("error", "ما لقيت نتائج")}

    images: List[Dict[str, Any]] = []
    for hit in res["results"]:
        if len(images) >= num_images:
            break
        url = hit.get("image_url") or ""
        if not url:
            continue
        data = _download_image_url(url, max_mb=20)
        if not data:
            continue
        ext = _ext_from_url(url)
        images.append({
            "data": data,
            "mime": _mime_for_ext(ext),
            "ext": ext,
            "title": hit.get("title") or query,
        })

    if not images:
        return {"ok": False, "error": "ما قدرتش ننزل تا صورة من Pinterest"}

    return {"ok": True, "query": query, "images": images}
