"""
Cloud storage file resolver for Omar bot.

Supported platforms:
  - MediaFire  (mediafire.com)  → scrapes download page → direct URL
  - Google Drive (drive.google.com) → resolves usercontent download URL
  - Mega.nz    (mega.nz / mega.co.nz) → downloads via mega.py (crypto)

For MediaFire and Google Drive we return a `download_url` that Baileys can
stream directly without buffering the whole file in Python.
For Mega we must download the encrypted file and return raw bytes.
"""
from __future__ import annotations

import os
import re
import tempfile
from typing import Any, Dict, Optional

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en,ar;q=0.8,fr;q=0.6",
}
TIMEOUT = 30
MAX_SIZE_MB = 500


# ---------------------------------------------------------------------------
# MIME helpers
# ---------------------------------------------------------------------------
_MIME_MAP = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "zip": "application/zip",
    "rar": "application/x-rar-compressed",
    "7z": "application/x-7z-compressed",
    "tar": "application/x-tar",
    "gz": "application/gzip",
    "mp3": "audio/mpeg",
    "mp4": "video/mp4",
    "mkv": "video/x-matroska",
    "avi": "video/x-msvideo",
    "mov": "video/quicktime",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "apk": "application/vnd.android.package-archive",
    "exe": "application/x-msdownload",
    "txt": "text/plain",
    "csv": "text/csv",
    "json": "application/json",
    "xml": "application/xml",
    "py": "text/x-python",
    "js": "application/javascript",
}


def _mime_for_filename(filename: str) -> str:
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    return _MIME_MAP.get(ext, "application/octet-stream")


def _fmt_size(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / (1024**3):.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / (1024**2):.2f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


# ---------------------------------------------------------------------------
# MediaFire
# ---------------------------------------------------------------------------
_MF_PATTERN = re.compile(
    r"mediafire\.com/(file|folder|file_premium)/(\w+)", re.I
)


def resolve_mediafire(url: str) -> Dict[str, Any]:
    """Resolve a MediaFire share URL → direct download URL + metadata."""
    m = _MF_PATTERN.search(url)
    if not m:
        return {"ok": False, "error": "رابط MediaFire غير صالح"}

    quick_key = m.group(2)

    # 1) Get metadata from the public API (no auth required for public files)
    filename = f"mediafire_{quick_key}.bin"
    size = 0
    mimetype = "application/octet-stream"
    try:
        api = (
            f"https://www.mediafire.com/api/1.5/file/get_info.php"
            f"?quick_key={quick_key}&response_format=json"
        )
        r = requests.get(api, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        resp = r.json().get("response", {})
        if resp.get("result") == "Success":
            info = resp.get("file_info") or {}
            filename  = info.get("filename") or filename
            size      = int(info.get("size") or 0)
            mimetype  = info.get("mimetype") or _mime_for_filename(filename)
    except Exception:
        pass

    if size and size > MAX_SIZE_MB * 1024 * 1024:
        return {
            "ok": False,
            "error": f"الملف كبير جداً ({_fmt_size(size)}) — الحد الأقصى {MAX_SIZE_MB}MB",
        }

    # 2) Scrape the download page for the direct link.
    #    MediaFire serves the URL via #downloadButton OR via a JS-injected
    #    var; we try several patterns before giving up.
    download_url = None
    try:
        from bs4 import BeautifulSoup
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        html = r.text

        soup = BeautifulSoup(html, "html.parser")
        btn = soup.find(id="downloadButton")
        if btn and btn.get("href"):
            download_url = btn["href"]

        if not download_url:
            dl = soup.find("a", href=re.compile(r"download\d*\.mediafire\.com", re.I))
            if dl:
                download_url = dl["href"]

        if not download_url:
            # JS-rendered downloads: kArSjMrJg = 'https://download...'
            m = re.search(
                r"https?://download\d*\.mediafire\.com/[A-Za-z0-9./_+\-?=%&]+",
                html,
            )
            if m:
                download_url = m.group(0)

        if not download_url:
            return {
                "ok": False,
                "error": "ما لقيتش رابط التحميل فصفحة MediaFire — قد يكون الملف محذوفاً أو خاصاً",
            }
    except Exception as exc:
        return {"ok": False, "error": f"خطأ في قراءة صفحة MediaFire: {exc}"}

    return {
        "ok": True,
        "platform": "mediafire",
        "filename": filename,
        "mime": mimetype,
        "size_bytes": size,
        "size_readable": _fmt_size(size) if size else "?",
        "download_url": download_url,
    }


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------
_GD_PATTERNS = [
    re.compile(r"/d/([a-zA-Z0-9_-]+)"),
    re.compile(r"[?&]id=([a-zA-Z0-9_-]+)"),
    re.compile(r"/folders/([a-zA-Z0-9_-]+)"),
    re.compile(r"/open\?id=([a-zA-Z0-9_-]+)"),
]


def _gdrive_file_id(url: str) -> Optional[str]:
    for pat in _GD_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


def _gdrive_filename(file_id: str) -> str:
    """Try to extract the filename from the Google Drive view page."""
    try:
        r = requests.get(
            f"https://drive.google.com/file/d/{file_id}/view",
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        for pat in [
            r'property="og:title"\s+content="([^"]+)"',
            r'"title":"([^"]+)"',
            r"<title>([^<]+)</title>",
        ]:
            m = re.search(pat, r.text)
            if m:
                name = m.group(1).replace(" - Google Drive", "").strip()
                # decode HTML entities
                name = re.sub(r"&#(\d+);", lambda x: chr(int(x.group(1))), name)
                if name:
                    return name
    except Exception:
        pass
    return f"gdrive_{file_id}"


def resolve_gdrive(url: str) -> Dict[str, Any]:
    """Resolve a Google Drive share URL → direct download URL."""
    file_id = _gdrive_file_id(url.replace("&amp;", "&"))
    if not file_id:
        return {"ok": False, "error": "ما لقيتش ID الملف فـ Google Drive URL"}

    filename = _gdrive_filename(file_id)

    # Try drive.usercontent.google.com — handles both small and large files
    base = f"https://drive.usercontent.google.com/download?id={file_id}&export=download"

    try:
        r = requests.get(base, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        ct = r.headers.get("content-type", "")

        # Small file / direct download
        if "text/html" not in ct:
            size = int(r.headers.get("content-length", 0))
            return {
                "ok": True,
                "platform": "gdrive",
                "filename": filename,
                "mime": ct.split(";")[0].strip() or _mime_for_filename(filename),
                "size_bytes": size,
                "size_readable": _fmt_size(size) if size else "?",
                "download_url": base,
            }

        # Large file — extract uuid + confirm token from the warning page
        html = r.text
        uuid_m    = re.search(r'name="uuid"\s+value="([^"]+)"', html)
        confirm_m = re.search(r'name="confirm"\s+value="([^"]+)"', html)
        uuid_val    = uuid_m.group(1)    if uuid_m    else None
        confirm_val = confirm_m.group(1) if confirm_m else "t"

        final = f"{base}&confirm={confirm_val}"
        if uuid_val:
            final += f"&uuid={uuid_val}"

        # Verify the final URL actually returns a file (not HTML)
        r2 = requests.head(final, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        ct2 = r2.headers.get("content-type", "")
        if "text/html" in ct2:
            # Still html — fall back to plain confirm=t
            final = f"{base}&confirm=t"
            r2 = requests.head(final, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
            ct2 = r2.headers.get("content-type", "")

        size = int(r2.headers.get("content-length", 0))
        if size and size > MAX_SIZE_MB * 1024 * 1024:
            return {
                "ok": False,
                "error": f"الملف كبير جداً ({_fmt_size(size)}) — الحد الأقصى {MAX_SIZE_MB}MB",
            }

        return {
            "ok": True,
            "platform": "gdrive",
            "filename": filename,
            "mime": ct2.split(";")[0].strip() or _mime_for_filename(filename),
            "size_bytes": size,
            "size_readable": _fmt_size(size) if size else "?",
            "download_url": final,
        }

    except Exception as exc:
        return {"ok": False, "error": f"خطأ في Google Drive: {exc}"}


# ---------------------------------------------------------------------------
# Mega.nz
# ---------------------------------------------------------------------------

def download_mega(url: str, max_size_mb: int = MAX_SIZE_MB) -> Dict[str, Any]:
    """Download a Mega.nz file using mega.py (handles client-side decryption)."""
    try:
        from mega import Mega  # type: ignore
    except ImportError:
        return {"ok": False, "error": "mega.py غير مثبّت — شغّل: uv add mega.py"}

    try:
        m = Mega()
        m.login_anonymous()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = m.download_url(url, dest_path=tmpdir)
            if not path or not os.path.exists(str(path)):
                return {"ok": False, "error": "mega.py ما رجعت ملف — تحقق من الرابط"}

            path = str(path)
            size = os.path.getsize(path)

            if size > max_size_mb * 1024 * 1024:
                return {
                    "ok": False,
                    "error": f"الملف كبير جداً ({_fmt_size(size)}) — الحد الأقصى {max_size_mb}MB",
                }

            filename = os.path.basename(path)
            with open(path, "rb") as f:
                data = f.read()

        return {
            "ok": True,
            "platform": "mega",
            "filename": filename,
            "mime": _mime_for_filename(filename),
            "size_bytes": size,
            "size_readable": _fmt_size(size),
            "data": data,          # bytes — no public URL available for Mega
        }

    except Exception as exc:
        return {"ok": False, "error": f"خطأ في Mega: {exc}"}


# ---------------------------------------------------------------------------
# Universal resolver
# ---------------------------------------------------------------------------

def resolve_cloud_url(url: str, max_size_mb: int = MAX_SIZE_MB) -> Dict[str, Any]:
    """Auto-detect platform and resolve / download the cloud file."""
    url = url.strip()
    low = url.lower()

    if "mediafire.com" in low:
        return resolve_mediafire(url)

    if "drive.google.com" in low or "drive.usercontent.google.com" in low:
        return resolve_gdrive(url)

    if "mega.nz" in low or "mega.co.nz" in low:
        return download_mega(url, max_size_mb=max_size_mb)

    return {
        "ok": False,
        "error": f"ما عرفتش هاد الـ platform — كنعرف MediaFire، Google Drive، وMega.nz فقط",
    }
