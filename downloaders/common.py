"""Shared helpers for the multi-platform downloader package.

All HTTP scrapers in this package use the same User-Agent, timeout and
size-cap helpers defined here so behaviour is consistent across platforms.
"""
from __future__ import annotations

import re
from typing import Optional

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Mobile) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Mobile Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en,ar;q=0.8,fr;q=0.6",
}

TIMEOUT = 30
DOWNLOAD_TIMEOUT = 120

URL_PREFIXES = ("http://", "https://")


def is_url(s: str) -> bool:
    return bool(s) and s.strip().lower().startswith(URL_PREFIXES)


def fmt_size(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / (1024**3):.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / (1024**2):.2f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


_MIME_MAP = {
    "mp4": "video/mp4",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "webm": "video/webm",
    "mov": "video/quicktime",
    "avi": "video/x-msvideo",
    "mkv": "video/x-matroska",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "pdf": "application/pdf",
    "zip": "application/zip",
    "rar": "application/x-rar-compressed",
    "7z": "application/x-7z-compressed",
    "apk": "application/vnd.android.package-archive",
    "txt": "text/plain",
}


def mime_for_ext(ext: str, default: str = "application/octet-stream") -> str:
    return _MIME_MAP.get((ext or "").lower().lstrip("."), default)


def ext_from_url(url: str, fallback: str = "bin") -> str:
    m = re.search(r"\.([a-zA-Z0-9]{2,5})(?:\?|#|$)", url or "")
    if m:
        ext = m.group(1).lower()
        if ext in _MIME_MAP:
            return ext
    return fallback


def download_to_bytes(
    url: str,
    *,
    max_size_mb: int = 1500,
    headers: Optional[dict] = None,
    referer: Optional[str] = None,
) -> bytes:
    """Stream a URL to memory, raising if it exceeds *max_size_mb*."""
    h = dict(DEFAULT_HEADERS)
    if headers:
        h.update(headers)
    if referer:
        h["Referer"] = referer

    cap = max_size_mb * 1024 * 1024
    chunks: list = []
    size = 0
    with requests.get(
        url,
        headers=h,
        timeout=DOWNLOAD_TIMEOUT,
        stream=True,
        allow_redirects=True,
    ) as r:
        r.raise_for_status()
        for chunk in r.iter_content(64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > cap:
                raise RuntimeError(
                    f"file exceeds {max_size_mb}MB cap (so far {fmt_size(size)})"
                )
            chunks.append(chunk)
    return b"".join(chunks)


def strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s or "").strip()
