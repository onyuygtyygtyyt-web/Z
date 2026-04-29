"""
All tools Omar can call.

Each tool returns a dict with at least {"ok": bool}.
File-producing tools also return {"file": {"path","name","mime","size","type"}}
where `type` is one of: image | audio | video | document.
"""
from __future__ import annotations

import inspect
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests


DOWNLOADS_ROOT = Path(__file__).parent / "downloads"
DOWNLOADS_ROOT.mkdir(parents=True, exist_ok=True)

DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# WhatsApp media size cap. WhatsApp documents go up to 2 GB; we cap at
# 1.5 GB so the file always fits with overhead.
MAX_FILE_MB = 1500


def _user_dir(user_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", user_id)[:80] or "anon"
    p = DOWNLOADS_ROOT / safe
    p.mkdir(parents=True, exist_ok=True)
    return p


def _file_meta(path: Path, type_hint: str = "document") -> Dict[str, Any]:
    return {
        "path": str(path),
        "name": path.name,
        "mime": mimetypes.guess_type(str(path))[0] or "application/octet-stream",
        "size": path.stat().st_size,
        "type": type_hint,
    }


def _check_size(path: Path) -> Optional[str]:
    mb = path.stat().st_size / (1024 * 1024)
    if mb > MAX_FILE_MB:
        return f"file too large ({mb:.1f} MB > {MAX_FILE_MB} MB)"
    return None


# =====================================================================
# 1) Media download — YouTube, Facebook, IG, TikTok, Twitter, SoundCloud…
# =====================================================================
def download_media(
    user_id: str,
    url: str,
    format: str = "video",
    quality: str = "best",
    **_,
) -> Dict[str, Any]:
    """Multi-platform downloader (no yt-dlp).

    Uses the `downloaders` package which dispatches to public scraper APIs:
    YouTube → savetube/ytdown.to, TikTok → tikwm, Facebook → fdownloader,
    Instagram → downloadgram, Twitter/X → x2twitter.

    format: "video" or "audio"
    quality (video):  "best" | "1080" | "720" | "480" | "360" | "240"
    quality (audio):  "best" | "320k" | "192k" | "128k" | "64k"
    """
    import re as _re
    from downloaders import download as _multi_download

    mode = "audio" if format == "audio" else "video"
    if mode == "audio":
        q = "192" if quality in ("best", "", None) else _re.sub(r"\D", "", quality) or "192"
    else:
        q = "1080" if quality == "best" else (quality if quality.isdigit() else "720")

    try:
        info = _multi_download(
            url,
            mode=mode,
            quality=q,
            max_size_mb=MAX_FILE_MB,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}

    out_dir = _user_dir(user_id)
    safe_title = _re.sub(r"[^\w\s.\-]", "_", (info.get("title") or "media"))[:80].strip() or "media"
    ext = info.get("ext") or ("mp3" if mode == "audio" else "mp4")
    path = out_dir / f"{safe_title}.{ext}"
    # Avoid clashes
    i = 1
    while path.exists():
        path = out_dir / f"{safe_title}_{i}.{ext}"
        i += 1
    path.write_bytes(info["data"])

    err = _check_size(path)
    if err:
        try: path.unlink()
        except Exception: pass
        return {"ok": False, "error": err + " — try a lower quality"}

    return {
        "ok": True,
        "title": info.get("title"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "platform": info.get("platform"),
        "file": _file_meta(path, "audio" if mode == "audio" else "video"),
    }


# =====================================================================
# 2) APK download — APKPure mobile API (apkeep-compatible endpoints)
# =====================================================================
APKPURE_VERSIONS_URL = (
    "https://api.pureapk.com/m/v3/cms/app_version?hl=en-US&package_name="
)
APKPURE_HEADERS = {
    "x-cv": "3172501",
    "x-sv": "29",
    "x-gp": "1",
    "x-abis": "arm64-v8a,armeabi-v7a,armeabi,x86,x86_64",
    "user-agent": "okhttp/4.12.0",
}
_APK_DL_RE = re.compile(
    rb"(X?APKJ).{0,30}?(https?://[A-Za-z0-9._\-/?#&=%~:]+)"
)


def download_apk(
    user_id: str,
    package_name: str,
    version: Optional[str] = None,
    **_,
) -> Dict[str, Any]:
    """Download APK / XAPK from APKPure using their open mobile API."""
    try:
        r = requests.get(
            APKPURE_VERSIONS_URL + package_name,
            headers=APKPURE_HEADERS, timeout=60,
        )
    except Exception as e:
        return {"ok": False, "error": f"apkpure request failed: {e}"}

    if not r.ok:
        return {"ok": False, "error": f"apkpure responded {r.status_code}"}

    body = r.content
    matches = list(_APK_DL_RE.finditer(body))
    if not matches:
        return {"ok": False, "error": "no APK download URL returned for this package"}

    # If a version was requested, prefer a match near that version string.
    chosen = matches[0]
    if version:
        v_bytes = version.encode()
        for m in matches:
            window = body[max(0, m.start() - 200): m.start()]
            if v_bytes in window:
                chosen = m
                break

    kind = chosen.group(1).decode()
    url = chosen.group(2).decode()
    ext = "xapk" if kind == "XAPKJ" else "apk"

    out_dir = _user_dir(user_id)
    fname = f"{package_name}{('-' + version) if version else ''}.{ext}"
    out = out_dir / fname

    try:
        with requests.get(url, headers=APKPURE_HEADERS, stream=True, timeout=600) as dl:
            if not dl.ok:
                return {"ok": False, "error": f"download failed {dl.status_code}"}
            with open(out, "wb") as f:
                for chunk in dl.iter_content(8192):
                    if chunk:
                        f.write(chunk)
    except Exception as e:
        return {"ok": False, "error": f"download error: {e}"}

    err = _check_size(out)
    if err:
        try: out.unlink()
        except Exception: pass
        return {"ok": False, "error": err}

    return {
        "ok": True,
        "package": package_name,
        "version": version,
        "kind": ext,
        "file": _file_meta(out, "document"),
    }


# =====================================================================
# 3) Google-style web search (DuckDuckGo, no API key)
# =====================================================================
def web_search(query: str, count: int = 5, **_) -> Dict[str, Any]:
    from duckduckgo_search import DDGS
    try:
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, max_results=max(1, min(int(count), 10))))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    results = [
        {"title": r.get("title"), "url": r.get("href"), "snippet": r.get("body")}
        for r in raw
    ]
    return {"ok": True, "query": query, "results": results}


# =====================================================================
# 4) Fetch URL — return clean readable text (for summarisation)
# =====================================================================
def fetch_url(url: str, max_chars: int = 6000, **_) -> Dict[str, Any]:
    from bs4 import BeautifulSoup
    from readability import Document
    try:
        r = requests.get(url, headers={"user-agent": "Mozilla/5.0"}, timeout=30)
        r.raise_for_status()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    ctype = r.headers.get("content-type", "")
    if "text/html" not in ctype and "xml" not in ctype:
        # Non-HTML resource — just return the first chunk as preview
        return {"ok": True, "url": url, "content_type": ctype,
                "preview": r.text[:max_chars]}

    try:
        doc = Document(r.text)
        title = doc.title()
        cleaned = doc.summary()
        text = BeautifulSoup(cleaned, "lxml").get_text("\n", strip=True)
    except Exception:
        text = BeautifulSoup(r.text, "lxml").get_text("\n", strip=True)
        title = (BeautifulSoup(r.text, "lxml").title or {}).get_text() if False else ""

    if len(text) > max_chars:
        text = text[:max_chars] + "\n…[truncated]"
    return {"ok": True, "url": url, "title": title, "text": text}


# =====================================================================
# 5) URL → PDF (extracted readable content + source line)
# =====================================================================
def url_to_pdf(
    user_id: str,
    url: str,
    filename: Optional[str] = None,
    **_,
) -> Dict[str, Any]:
    from fpdf import FPDF

    fetched = fetch_url(url, max_chars=200_000)
    if not fetched.get("ok"):
        return fetched

    title = fetched.get("title") or "Page"
    text = fetched.get("text") or fetched.get("preview", "")

    out_dir = _user_dir(user_id)
    safe_title = re.sub(r"[^A-Za-z0-9_.\- ]", "", title)[:60].strip() or "page"
    fname = filename or f"{safe_title}.pdf"
    if not fname.lower().endswith(".pdf"):
        fname += ".pdf"
    out = out_dir / fname

    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(True, margin=15)
    if Path(DEJAVU).exists():
        pdf.add_font("DejaVu", "", DEJAVU, uni=True)
        title_font = body_font = "DejaVu"
    else:
        title_font = body_font = "Helvetica"

    pdf.set_font(title_font, size=14)
    pdf.multi_cell(0, 10, title)
    pdf.set_font(body_font, size=10)
    pdf.set_text_color(80, 80, 80)
    pdf.multi_cell(0, 6, f"Source: {url}")
    pdf.ln(2)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font(body_font, size=11)
    pdf.multi_cell(0, 6, text)
    pdf.output(str(out))

    return {"ok": True, "title": title, "source": url, "file": _file_meta(out, "document")}


# =====================================================================
# 6) Find a free PDF on the web (e.g. a book by name)
# =====================================================================
def find_book_pdf(user_id: str, query: str, **_) -> Dict[str, Any]:
    from duckduckgo_search import DDGS

    pdf_url = None
    pdf_meta = None
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(f"{query} filetype:pdf", max_results=20):
                href = r.get("href") or ""
                if href.lower().endswith(".pdf"):
                    pdf_url = href
                    pdf_meta = r
                    break
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if not pdf_url:
        return {"ok": False, "error": "no free PDF found for this query"}

    safe = re.sub(r"[^A-Za-z0-9_.\- ]", "", query)[:60].strip().replace(" ", "_") or "book"
    out = _user_dir(user_id) / f"{safe}.pdf"

    try:
        with requests.get(pdf_url, headers={"user-agent": "Mozilla/5.0"},
                          stream=True, timeout=180) as dl:
            dl.raise_for_status()
            with open(out, "wb") as f:
                for chunk in dl.iter_content(8192):
                    if chunk:
                        f.write(chunk)
    except Exception as e:
        return {"ok": False, "error": str(e), "source_url": pdf_url}

    err = _check_size(out)
    if err:
        try: out.unlink()
        except Exception: pass
        return {"ok": False, "error": err, "source_url": pdf_url}

    return {
        "ok": True,
        "title": (pdf_meta or {}).get("title"),
        "source_url": pdf_url,
        "file": _file_meta(out, "document"),
    }


# =====================================================================
# 7) Make a code file (extension chosen by language)
# =====================================================================
LANG_EXT = {
    "python": "py", "py": "py",
    "javascript": "js", "js": "js", "node": "js", "nodejs": "js",
    "typescript": "ts", "ts": "ts",
    "html": "html", "css": "css", "scss": "scss",
    "java": "java", "kotlin": "kt", "swift": "swift",
    "c": "c", "cpp": "cpp", "c++": "cpp", "h": "h",
    "csharp": "cs", "c#": "cs",
    "go": "go", "rust": "rs", "ruby": "rb", "php": "php",
    "bash": "sh", "shell": "sh", "sh": "sh", "zsh": "sh",
    "sql": "sql", "json": "json", "yaml": "yml", "yml": "yml",
    "xml": "xml", "markdown": "md", "md": "md",
    "dart": "dart", "lua": "lua", "perl": "pl", "r": "r",
    "vue": "vue", "svelte": "svelte", "tsx": "tsx", "jsx": "jsx",
    "powershell": "ps1", "ini": "ini", "toml": "toml", "env": "env",
}


def make_code_file(
    user_id: str,
    language: str,
    content: str,
    filename: Optional[str] = None,
    **_,
) -> Dict[str, Any]:
    ext = LANG_EXT.get((language or "").lower().strip(), "txt")
    out_dir = _user_dir(user_id)
    if not filename:
        filename = f"snippet_{int(time.time())}.{ext}"
    elif "." not in filename:
        filename = f"{filename}.{ext}"
    out = out_dir / filename
    out.write_text(content, encoding="utf-8")
    return {"ok": True, "language": language,
            "file": _file_meta(out, "document")}


# =====================================================================
# 8) Make any text/PDF/CSV/JSON/etc file
# =====================================================================
def make_file(
    user_id: str,
    format: str,
    content: str,
    filename: Optional[str] = None,
    **_,
) -> Dict[str, Any]:
    fmt = (format or "txt").lower().strip().lstrip(".")
    out_dir = _user_dir(user_id)
    if not filename:
        filename = f"file_{int(time.time())}.{fmt}"
    elif not filename.lower().endswith("." + fmt):
        filename = f"{filename}.{fmt}"
    out = out_dir / filename

    if fmt == "pdf":
        from fpdf import FPDF
        pdf = FPDF()
        pdf.add_page()
        pdf.set_auto_page_break(True, margin=15)
        if Path(DEJAVU).exists():
            pdf.add_font("DejaVu", "", DEJAVU, uni=True)
            pdf.set_font("DejaVu", size=11)
        else:
            pdf.set_font("Helvetica", size=11)
        pdf.multi_cell(0, 6, content)
        pdf.output(str(out))
    else:
        out.write_text(content, encoding="utf-8")

    return {"ok": True, "format": fmt, "file": _file_meta(out, "document")}


# =====================================================================
# 9) Current time (Morocco)
# =====================================================================
def get_time(**_) -> Dict[str, Any]:
    from datetime import datetime, timezone, timedelta
    morocco = timezone(timedelta(hours=1))  # Morocco UTC+1 year-round since 2018
    now = datetime.now(morocco)
    return {
        "ok": True,
        "iso": now.isoformat(),
        "human": now.strftime("%A %d %B %Y, %H:%M"),
        "tz": "Africa/Casablanca (UTC+1)",
    }


# =====================================================================
# Tool registry & dispatcher
# =====================================================================
TOOLS = {
    "download_media":  download_media,
    "download_apk":    download_apk,
    "web_search":      web_search,
    "fetch_url":       fetch_url,
    "url_to_pdf":      url_to_pdf,
    "find_book_pdf":   find_book_pdf,
    "make_code_file":  make_code_file,
    "make_file":       make_file,
    "get_time":        get_time,
}


def run_tool(call: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    name = call.get("name", "")
    args = call.get("args") or {}
    fn = TOOLS.get(name)
    if not fn:
        return {"name": name, "ok": False, "error": f"unknown tool '{name}'"}

    try:
        if "user_id" in inspect.signature(fn).parameters:
            args = {**args, "user_id": user_id}
        out = fn(**args)
        if not isinstance(out, dict):
            out = {"ok": True, "result": out}
        out["name"] = name
        return out
    except TypeError as e:
        return {"name": name, "ok": False, "error": f"bad arguments: {e}"}
    except Exception as e:
        return {"name": name, "ok": False, "error": str(e)}
