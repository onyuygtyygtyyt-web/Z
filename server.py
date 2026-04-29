"""
Flask HTTP server that exposes the Gemini scraper to the WhatsApp bot.

Endpoints
---------
GET  /             -> Simple status page
GET  /health       -> JSON health check
POST /ask          -> { "user": "<id>", "text": "<prompt>" }
                      multipart with field "file" optional (image/audio/pdf...)
                      -> { "text": "...", "images": ["data:image/png;base64,..."] }
POST /image        -> { "user": "<id>", "prompt": "..." }
                      optional multipart "file" for editing
                      -> { "text": "...", "images": [base64...] }
POST /reset        -> { "user": "<id>" }
"""
from __future__ import annotations

import base64
import os
import sys
import threading
from typing import List, Optional, Tuple

from flask import Flask, jsonify, request

# Allow `from gemini.gemini_scraper import GeminiBrain`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gemini.gemini_scraper import GeminiBrain, AuthCookieError  # noqa: E402
from gemini.cookie_pool import CookiePool, MAX_SLOTS  # noqa: E402
from downloader import (  # noqa: E402
    download as yt_download,
    get_info as yt_info,
    resolve as yt_resolve,
)
from apk_downloader import (  # noqa: E402
    download as apk_download,
    get_info as apk_get_info,
)
from web_tools import (  # noqa: E402
    web_search as wt_search,
    fetch_url as wt_fetch,
    find_pdf as wt_find_pdf,
)
from pdf_maker import make_pdf as pm_make_pdf  # noqa: E402
from cloud_downloader import resolve_cloud_url as cloud_resolve  # noqa: E402
from pinterest_downloader import (  # noqa: E402
    search_pinterest as pin_search,
    download_pinterest as pin_download,
    search_and_fetch_images as pin_fetch_images,
)
from extras import (  # noqa: E402
    get_weather as ex_weather,
    get_prayer_times as ex_prayer,
    convert_currency as ex_currency,
    translate_text as ex_translate,
    wiki_summary as ex_wiki,
    make_qr as ex_qr,
    text_to_speech as ex_tts,
    get_time as ex_time,
)
from extras2 import (  # noqa: E402
    get_lyrics as ex_lyrics,
    get_quran as ex_quran,
    get_hadith as ex_hadith,
    get_crypto as ex_crypto,
    get_football as ex_football,
    get_joke as ex_joke,
    get_country as ex_country,
    get_dictionary as ex_dictionary,
    get_horoscope as ex_horoscope,
    shorten_url as ex_shorten,
    make_sticker as ex_sticker,
    get_youtube_transcript as ex_transcript,
)

# Legacy single-cookie path (auto-imported into the pool on first run).
COOKIES_PATH = os.path.join(os.path.dirname(__file__), "gemini", "cookies.txt")

# Where pool slots live.  Override with COOKIES_DIR (e.g. /data/cookies on
# Hugging Face Spaces with persistent storage).
COOKIES_DIR = os.environ.get(
    "COOKIES_DIR",
    os.path.join(os.path.dirname(__file__), "gemini", "cookies"),
)

# Token used by the WhatsApp bot to call /admin/* endpoints. When unset,
# admin endpoints only accept loopback requests (the in-container case).
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

app = Flask(__name__)

_pool = CookiePool(COOKIES_DIR)
_pool.import_legacy_file(COOKIES_PATH)

_brain_lock = threading.Lock()
_brain: Optional[GeminiBrain] = None


def get_brain() -> GeminiBrain:
    """Return the shared GeminiBrain. The cookie is set per-request by
    ``with_cookie_pool`` — we never pin the brain to a single account."""
    global _brain
    if _brain is not None:
        return _brain
    with _brain_lock:
        if _brain is not None:
            return _brain
        _brain = GeminiBrain(cookie_string="", model="flash")
        return _brain


def with_cookie_pool(fn):
    """Run ``fn(brain)`` rotating cookies on auth failure.

    On every call we pick the next healthy slot, hot-swap it into the
    scraper, and run ``fn``. If Gemini rejects the cookie we mark the
    slot sick and try the next one. Returns whatever ``fn`` returned."""
    brain = get_brain()
    if not _pool.has_any():
        raise AuthCookieError(
            "No Gemini cookies configured. Send `/cookie add` from the "
            "developer phone with a cookies.txt attachment."
        )
    last_err: Optional[Exception] = None
    attempts = max(1, _pool.count())
    for _ in range(attempts):
        picked = _pool.pick()
        if picked is None:
            break
        slot, header = picked
        brain.scraper.set_cookie(header)
        try:
            res = fn(brain)
            _pool.mark_good(slot)
            return res
        except AuthCookieError as e:
            print(f"[cookies] slot {slot} failed ({e}); rotating", flush=True)
            _pool.mark_bad(slot)
            last_err = e
            continue
    raise last_err or AuthCookieError("all cookie slots failed")


def _prewarm() -> None:
    """Best-effort token pre-fetch at startup. Silent when no cookies are
    configured yet (developer adds them via /cookie add)."""
    if not _pool.has_any():
        print("[server] No cookies in pool — waiting for /cookie add.", flush=True)
        return
    try:
        with_cookie_pool(lambda b: b.scraper._refresh_tokens())
        print("[server] Gemini tokens pre-warmed.", flush=True)
    except Exception as e:
        print(f"[server] Pre-warm failed: {e}", flush=True)


threading.Thread(target=_prewarm, daemon=True).start()


# ----------------------------------------------------------------------
# Admin authentication
# ----------------------------------------------------------------------
def _admin_authorized() -> bool:
    """Allow if ADMIN_TOKEN matches OR the request comes from loopback."""
    if ADMIN_TOKEN:
        token = (
            request.headers.get("X-Admin-Token")
            or request.args.get("admin_token")
            or ""
        )
        if token and token == ADMIN_TOKEN:
            return True
    if request.remote_addr in ("127.0.0.1", "::1", "localhost"):
        return True
    return False


def _collect_files() -> List[Tuple[str, bytes, Optional[str]]]:
    files: List[Tuple[str, bytes, Optional[str]]] = []
    for key in request.files:
        for f in request.files.getlist(key):
            files.append((f.filename or "file.bin", f.read(), f.mimetype))
    return files


def _b64_images(image_bytes_list: List[bytes]) -> List[str]:
    return [base64.b64encode(b).decode("ascii") for b in image_bytes_list]


@app.route("/")
def index():
    status = "ready" if _brain is not None else "lazy (will init on first call)"
    return (
        f"<html><body style='font-family:sans-serif;padding:20px'>"
        f"<h2>Gemini ↔ WhatsApp bridge</h2>"
        f"<p>Status: <b>{status}</b></p>"
        f"<p>Cookies dir: <code>{COOKIES_DIR}</code> — "
        f"{_pool.count()}/{MAX_SLOTS} slots in use</p>"
        f"<p>Endpoints:</p>"
        f"<ul>"
        f"<li><code>POST /ask</code> — chat (text + optional file)</li>"
        f"<li><code>POST /image</code> — generate / edit an image</li>"
        f"<li><code>POST /reset</code> — clear a user's memory</li>"
        f"<li><code>GET /admin/cookies</code> — list cookie slots</li>"
        f"<li><code>POST /admin/cookies</code> — add/replace a cookie</li>"
        f"<li><code>DELETE /admin/cookies/&lt;slot&gt;</code> — remove a slot</li>"
        f"<li><code>GET /health</code> — health check</li>"
        f"</ul>"
        f"</body></html>"
    )


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "brain_loaded": _brain is not None,
        "cookie_slots_used": _pool.count(),
        "cookie_slots_max": MAX_SLOTS,
        "cookies_dir": COOKIES_DIR,
    })


# ----------------------------------------------------------------------
# Cookie pool admin (developer-only — see _admin_authorized)
# ----------------------------------------------------------------------
@app.route("/admin/cookies", methods=["GET"])
def admin_cookies_list():
    if not _admin_authorized():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({
        "max_slots": MAX_SLOTS,
        "used": _pool.count(),
        "slots": _pool.list(),
    })


@app.route("/admin/cookies", methods=["POST"])
def admin_cookies_add():
    """Add or replace a cookie. Body (multipart or JSON):
        slot:    optional 1..10 (auto-assigned if absent)
        cookie:  raw cookie header OR JSON cookie-export string
        OR file upload field "file" containing the cookies.txt
    """
    if not _admin_authorized():
        return jsonify({"error": "unauthorized"}), 401

    raw = ""
    slot_raw = None
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        raw = payload.get("cookie") or payload.get("data") or ""
        slot_raw = payload.get("slot")
    else:
        slot_raw = request.form.get("slot")
        raw = request.form.get("cookie") or request.form.get("data") or ""
        if not raw and request.files:
            f = next(iter(request.files.values()))
            raw = f.read().decode("utf-8", errors="ignore")

    slot: Optional[int] = None
    if slot_raw not in (None, "", "auto"):
        try:
            slot = int(slot_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "slot must be an integer"}), 400

    try:
        assigned = _pool.add(raw, slot=slot)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # Try the new cookie immediately so we surface bad uploads early.
    test_ok = True
    test_err: Optional[str] = None
    try:
        brain = get_brain()
        brain.scraper.set_cookie(_pool.get(assigned) or "")
        brain.scraper._refresh_tokens()
        _pool.mark_good(assigned)
    except Exception as e:
        test_ok = False
        test_err = str(e)
        _pool.mark_bad(assigned)

    return jsonify({
        "ok": True,
        "slot": assigned,
        "test_ok": test_ok,
        "test_error": test_err,
        "used": _pool.count(),
    })


@app.route("/admin/cookies/<int:slot>", methods=["DELETE"])
def admin_cookies_remove(slot: int):
    if not _admin_authorized():
        return jsonify({"error": "unauthorized"}), 401
    removed = _pool.remove(slot)
    if not removed:
        return jsonify({"error": f"slot {slot} not found"}), 404
    return jsonify({"ok": True, "slot": slot, "used": _pool.count()})


@app.route("/admin/cookies/test", methods=["POST"])
def admin_cookies_test():
    """Test a specific slot (or all slots if none given) against Gemini."""
    if not _admin_authorized():
        return jsonify({"error": "unauthorized"}), 401
    payload = request.get_json(silent=True) or request.form
    slot_raw = payload.get("slot") if payload else None
    targets: List[int] = []
    if slot_raw:
        try:
            targets = [int(slot_raw)]
        except (TypeError, ValueError):
            return jsonify({"error": "slot must be an integer"}), 400
    else:
        targets = [s["slot"] for s in _pool.list()]

    results = []
    brain = get_brain()
    for s in targets:
        header = _pool.get(s)
        if header is None:
            results.append({"slot": s, "ok": False, "error": "slot empty"})
            continue
        brain.scraper.set_cookie(header)
        try:
            brain.scraper._refresh_tokens()
            _pool.mark_good(s)
            results.append({"slot": s, "ok": True})
        except Exception as e:
            _pool.mark_bad(s)
            results.append({"slot": s, "ok": False, "error": str(e)})
    return jsonify({"results": results})


@app.route("/ask", methods=["POST"])
def ask():
    user = request.form.get("user") or (request.json or {}).get("user") if request.is_json else request.form.get("user")
    text = request.form.get("text") or ""
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        user = user or payload.get("user")
        text = text or payload.get("text", "")

    if not user:
        return jsonify({"error": "missing 'user'"}), 400
    if not text and not request.files:
        return jsonify({"error": "missing 'text' or file"}), 400

    files = _collect_files()
    try:
        result = with_cookie_pool(
            lambda b: b.ask_full(user, text, files=files or None)
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "text": result.get("text", ""),
        "images": _b64_images(result.get("image_bytes", []) or []),
    })


@app.route("/image", methods=["POST"])
def image():
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        user = payload.get("user")
        prompt = payload.get("prompt", "")
    else:
        user = request.form.get("user")
        prompt = request.form.get("prompt", "")

    if not user or not prompt:
        return jsonify({"error": "missing 'user' or 'prompt'"}), 400

    reference = None
    files = _collect_files()
    if files:
        reference = files[0]

    try:
        imgs, text = with_cookie_pool(
            lambda b: b.generate_image(user, prompt, reference=reference)
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"text": text or "", "images": _b64_images(imgs or [])})


@app.route("/download", methods=["POST"])
def download_route():
    """Download a video/audio via yt-dlp and return it as base64.

    Body (form or JSON):
        query: URL or free-text search query  (required)
        mode:  "video" (default) or "audio"
        quality: "360" / "480" / "720" (default) / "1080"
        max_size_mb: int, default 1500 (1.5 GB)

    Response:
        { title, uploader, duration, ext, mime, size_bytes, data_b64 }
    """
    if request.is_json:
        payload = request.get_json(silent=True) or {}
    else:
        payload = request.form

    query = (payload.get("query") or "").strip()
    mode = (payload.get("mode") or "video").lower()
    quality = str(payload.get("quality") or "720")
    try:
        max_size_mb = int(payload.get("max_size_mb") or 1500)
    except (TypeError, ValueError):
        max_size_mb = 1500
    # Default behaviour: return a direct streaming URL so the WhatsApp
    # bot can pipe it straight to Baileys without buffering the file in
    # Python or Node memory.  Pass `stream=false` to force a base64
    # download (rare — only useful if the caller really needs bytes).
    stream = str(payload.get("stream", "true")).lower() not in ("0", "false", "no")

    if not query:
        return jsonify({"error": "missing 'query'"}), 400
    if mode not in ("video", "audio"):
        return jsonify({"error": "mode must be 'video' or 'audio'"}), 400

    if stream:
        try:
            result = yt_resolve(query, mode=mode, quality=quality)
        except Exception as e:
            import re
            raw = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip()
            return jsonify({"error": raw}), 500
        return jsonify({
            "title": result.get("title"),
            "uploader": result.get("uploader"),
            "duration": result.get("duration"),
            "webpage_url": result.get("webpage_url"),
            "ext": result.get("ext"),
            "mime": result.get("mime"),
            "platform": result.get("platform"),
            "thumbnail": result.get("thumbnail"),
            "download_url": result["download_url"],
            "stream_headers": result.get("stream_headers", {}),
        })

    try:
        result = yt_download(query, mode=mode, quality=quality, max_size_mb=max_size_mb)
    except Exception as e:
        import re
        raw = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip()
        lowered = raw.lower()
        if (
            "rate-limit" in lowered
            or "login required" in lowered
            or "cookies" in lowered
            or "this video is only available for registered users" in lowered
        ):
            friendly = (
                "هاد الموقع كيطلب تسجيل الدخول باش ينزل المحتوى "
                "(انستغرام، فيسبوك، تويتر الخاص...). صاحب البوت "
                "خاصو يضيف ملف cookies ديال حسابو فـ cookies/cookies.txt "
                "باش هاد النوع ديال الروابط يخدم."
            )
            return jsonify({"error": friendly, "raw": raw}), 502
        return jsonify({"error": raw}), 500

    return jsonify({
        "title": result.get("title"),
        "uploader": result.get("uploader"),
        "duration": result.get("duration"),
        "webpage_url": result.get("webpage_url"),
        "ext": result.get("ext"),
        "mime": result.get("mime"),
        "size_bytes": result.get("size_bytes"),
        "data_b64": base64.b64encode(result["data"]).decode("ascii"),
    })


@app.route("/info", methods=["POST"])
def info_route():
    """Return metadata only (no download)."""
    if request.is_json:
        payload = request.get_json(silent=True) or {}
    else:
        payload = request.form
    query = (payload.get("query") or "").strip()
    if not query:
        return jsonify({"error": "missing 'query'"}), 400
    try:
        return jsonify(yt_info(query))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/apk", methods=["POST"])
def apk_route():
    """Download an APK from APKPure.

    Body (form or JSON):
        query: app name or package id (required)
        max_size_mb: int, default 500

    Response:
        { package, title, developer, version_name, size_bytes, size_mb,
          min_android, target_android, ext, filename, mime, data_b64 }
    """
    if request.is_json:
        payload = request.get_json(silent=True) or {}
    else:
        payload = request.form

    query = (payload.get("query") or "").strip()
    try:
        max_size_mb = int(payload.get("max_size_mb") or 500)
    except (TypeError, ValueError):
        max_size_mb = 500

    if not query:
        return jsonify({"error": "missing 'query'"}), 400

    try:
        result = apk_download(query, max_size_mb=max_size_mb)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "package": result.get("package"),
        "title": result.get("title"),
        "developer": result.get("developer"),
        "version_name": result.get("version_name"),
        "size_bytes": result.get("size_bytes"),
        "size_mb": result.get("size_mb"),
        "min_android": result.get("min_android"),
        "target_android": result.get("target_android"),
        "ext": result.get("ext"),
        "filename": result.get("filename"),
        "mime": result.get("mime"),
        "data_b64": base64.b64encode(result["data"]).decode("ascii"),
    })


@app.route("/apk_info", methods=["POST"])
def apk_info_route():
    """Return APK metadata only (no download)."""
    if request.is_json:
        payload = request.get_json(silent=True) or {}
    else:
        payload = request.form
    query = (payload.get("query") or "").strip()
    if not query:
        return jsonify({"error": "missing 'query'"}), 400
    try:
        return jsonify(apk_get_info(query))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/websearch", methods=["POST"])
def websearch_route():
    """Search the web and return top results.

    Body: { query: str, max_results?: int }
    Response: { query, results: [{title, url, snippet}, ...] }
    """
    if request.is_json:
        payload = request.get_json(silent=True) or {}
    else:
        payload = request.form
    query = (payload.get("query") or "").strip()
    try:
        max_results = int(payload.get("max_results") or 5)
    except (TypeError, ValueError):
        max_results = 5
    if not query:
        return jsonify({"error": "missing 'query'"}), 400
    try:
        results = wt_search(query, max_results=max_results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"query": query, "results": results})


@app.route("/fetch", methods=["POST"])
def fetch_route():
    """Fetch a URL and return its main readable text.

    Body: { url: str, max_chars?: int }
    Response: { url, title, text, content_type }
    """
    if request.is_json:
        payload = request.get_json(silent=True) or {}
    else:
        payload = request.form
    url = (payload.get("url") or "").strip()
    try:
        max_chars = int(payload.get("max_chars") or 8000)
    except (TypeError, ValueError):
        max_chars = 8000
    if not url:
        return jsonify({"error": "missing 'url'"}), 400
    try:
        return jsonify(wt_fetch(url, max_chars=max_chars))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/screenshot", methods=["POST"])
def screenshot_route():
    """Take a screenshot of any public webpage.

    Body: { url: str, width?: int }
    Response: { url, mime, data_b64 } | { error }
    """
    import base64
    import requests as _rq
    payload = request.get_json(silent=True) if request.is_json else request.form
    payload = payload or {}
    url = (payload.get("url") or "").strip()
    if not url:
        return jsonify({"error": "missing 'url'"}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        width = int(payload.get("width") or 1280)
    except (TypeError, ValueError):
        width = 1280

    last_err = ""
    candidates = [
        f"https://image.thum.io/get/maxAge/12/png/width/{width}/{url}",
        f"https://s0.wp.com/mshots/v1/{url}?w={width}",
    ]
    for api_url in candidates:
        try:
            r = _rq.get(api_url, timeout=45,
                        headers={"User-Agent": "Mozilla/5.0 (Linux) ScreenshotBot"})
            if r.status_code == 200 and r.content and len(r.content) > 2000:
                mime = r.headers.get("Content-Type", "image/png").split(";")[0].strip()
                if not mime.startswith("image/"):
                    mime = "image/png"
                return jsonify({
                    "url": url,
                    "mime": mime,
                    "data_b64": base64.b64encode(r.content).decode("ascii"),
                })
            last_err = f"HTTP {r.status_code} (size={len(r.content) if r.content else 0})"
        except Exception as e:
            last_err = str(e)
            continue
    return jsonify({"error": f"screenshot service failed: {last_err}"}), 502


@app.route("/deepsearch", methods=["POST"])
def deepsearch_route():
    """Multi-source deep web search: search + fetch top results.

    Body: { query: str, num_pages?: int }
    Response: { query, results: [...], pages: [{url, title, text}, ...] }
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    payload = request.get_json(silent=True) if request.is_json else request.form
    payload = payload or {}
    query = (payload.get("query") or "").strip()
    if not query:
        return jsonify({"error": "missing 'query'"}), 400
    try:
        num_pages = max(1, min(5, int(payload.get("num_pages") or 3)))
    except (TypeError, ValueError):
        num_pages = 3
    try:
        results = wt_search(query, max_results=8) or []
    except Exception as e:
        return jsonify({"error": f"search failed: {e}"}), 500

    pages = []
    top = [r for r in results if r.get("url")][:num_pages]

    def _fetch_one(item):
        try:
            data = wt_fetch(item["url"], max_chars=3500)
            return {
                "url": item["url"],
                "title": item.get("title") or data.get("title", ""),
                "snippet": item.get("snippet", ""),
                "text": data.get("text", ""),
            }
        except Exception as e:
            return {
                "url": item["url"],
                "title": item.get("title", ""),
                "snippet": item.get("snippet", ""),
                "text": "",
                "fetch_error": str(e),
            }

    if top:
        with ThreadPoolExecutor(max_workers=min(3, len(top))) as ex:
            futures = [ex.submit(_fetch_one, it) for it in top]
            for f in as_completed(futures):
                pages.append(f.result())

    return jsonify({"query": query, "results": results, "pages": pages})


@app.route("/pdfsearch", methods=["POST"])
def pdfsearch_route():
    """Search the web for a PDF on `query`, return file + other sources.

    Body: { query: str }
    Response: {
      query, sources:[{title,url,snippet}],
      pdf: { url, filename, size_bytes, data_b64 } | null,
      pdf_title
    }
    """
    if request.is_json:
        payload = request.get_json(silent=True) or {}
    else:
        payload = request.form
    query = (payload.get("query") or "").strip()
    if not query:
        return jsonify({"error": "missing 'query'"}), 400
    try:
        out = wt_find_pdf(query)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    pdf = out.get("pdf")
    pdf_payload = None
    if pdf:
        pdf_payload = {
            "url": pdf["url"],
            "filename": pdf["filename"],
            "size_bytes": pdf["size_bytes"],
            "data_b64": base64.b64encode(pdf["data"]).decode("ascii"),
        }
    return jsonify({
        "query": out["query"],
        "pdf": pdf_payload,
        "pdf_title": out.get("pdf_title"),
        "sources": out.get("sources", []),
    })


@app.route("/makepdf", methods=["POST"])
def makepdf_route():
    """Generate a PDF from a title + body text and return it as base64.

    Body (JSON or form):
        title:   document title (optional)
        content: body text (required). Supports paragraphs separated by blank
                 lines and markdown-style headings (#, ##, ###) and bullets
                 (- item). Arabic and Latin text are both rendered correctly.
        filename: optional output filename (defaults to a slug of the title).

    Response:
        { filename, mime, size_bytes, data_b64 }
    """
    if request.is_json:
        payload = request.get_json(silent=True) or {}
    else:
        payload = request.form

    title = (payload.get("title") or "").strip()
    content = (payload.get("content") or payload.get("body") or "").strip()
    filename = (payload.get("filename") or "").strip()

    if not content:
        return jsonify({"error": "missing 'content'"}), 400

    if not filename:
        slug = "".join(c if c.isalnum() else "_" for c in (title or "document"))
        filename = (slug.strip("_") or "document")[:60] + ".pdf"
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"

    try:
        pdf_bytes = pm_make_pdf(title, content)
    except Exception as e:
        return jsonify({"error": f"pdf generation failed: {e}"}), 500

    return jsonify({
        "filename": filename,
        "mime": "application/pdf",
        "size_bytes": len(pdf_bytes),
        "data_b64": base64.b64encode(pdf_bytes).decode("ascii"),
    })


@app.route("/weather", methods=["POST"])
def weather_route():
    """Body: { place: str }  ->  weather summary dict."""
    payload = request.get_json(silent=True) if request.is_json else request.form
    place = (payload.get("place") or payload.get("query") or "").strip()
    if not place:
        return jsonify({"error": "missing 'place'"}), 400
    return jsonify(ex_weather(place))


@app.route("/prayer", methods=["POST"])
def prayer_route():
    """Body: { place: str, method?: str, date?: 'DD-MM-YYYY' }"""
    payload = request.get_json(silent=True) if request.is_json else request.form
    place = (payload.get("place") or payload.get("query") or "").strip()
    method = (payload.get("method") or "MOROCCO").strip()
    date = (payload.get("date") or "").strip() or None
    if not place:
        return jsonify({"error": "missing 'place'"}), 400
    return jsonify(ex_prayer(place, method=method, date=date))


@app.route("/currency", methods=["POST"])
def currency_route():
    """Body: { amount: float, from: 'USD', to: 'MAD' }"""
    payload = request.get_json(silent=True) if request.is_json else request.form
    amount = payload.get("amount", 1)
    src = (payload.get("from") or payload.get("src") or "").strip()
    dst = (payload.get("to") or payload.get("dst") or "").strip()
    if not src or not dst:
        return jsonify({"error": "missing 'from' / 'to'"}), 400
    return jsonify(ex_currency(amount, src, dst))


@app.route("/translate", methods=["POST"])
def translate_route():
    """Body: { text: str, target: 'en', source?: 'auto' }"""
    payload = request.get_json(silent=True) if request.is_json else request.form
    text = payload.get("text") or ""
    target = (payload.get("target") or "en").strip()
    source = (payload.get("source") or "auto").strip()
    if not (text or "").strip():
        return jsonify({"error": "missing 'text'"}), 400
    return jsonify(ex_translate(text, target=target, source=source))


@app.route("/wiki", methods=["POST"])
def wiki_route():
    """Body: { query: str, lang?: 'ar'|'en'|'fr'... }"""
    payload = request.get_json(silent=True) if request.is_json else request.form
    query = (payload.get("query") or "").strip()
    lang = (payload.get("lang") or "ar").strip()
    if not query:
        return jsonify({"error": "missing 'query'"}), 400
    return jsonify(ex_wiki(query, lang=lang))


@app.route("/qr", methods=["POST"])
def qr_route():
    """Body: { data: str }  ->  PNG image as base64."""
    payload = request.get_json(silent=True) if request.is_json else request.form
    data = (payload.get("data") or payload.get("text") or "").strip()
    if not data:
        return jsonify({"error": "missing 'data'"}), 400
    out = ex_qr(data)
    if not out.get("ok"):
        return jsonify({"error": out.get("error", "qr failed")}), 500
    return jsonify({
        "data": out["data"],
        "filename": out["filename"],
        "mime": out["mime"],
        "size_bytes": out["size_bytes"],
        "data_b64": base64.b64encode(out["png_bytes"]).decode("ascii"),
    })


@app.route("/tts", methods=["POST"])
def tts_route():
    """Body: { text: str, lang?: 'ar'|'en'|'fr'... }  ->  MP3 base64."""
    payload = request.get_json(silent=True) if request.is_json else request.form
    text = (payload.get("text") or "").strip()
    lang = (payload.get("lang") or "ar").strip()
    if not text:
        return jsonify({"error": "missing 'text'"}), 400
    out = ex_tts(text, lang=lang)
    if not out.get("ok"):
        return jsonify({"error": out.get("error", "tts failed")}), 500
    return jsonify({
        "text": out["text"],
        "lang": out["lang"],
        "filename": out["filename"],
        "mime": out["mime"],
        "size_bytes": out["size_bytes"],
        "data_b64": base64.b64encode(out["mp3_bytes"]).decode("ascii"),
    })


@app.route("/time", methods=["POST"])
def time_route():
    """Body: { tz?: 'Africa/Casablanca' }"""
    payload = request.get_json(silent=True) if request.is_json else request.form
    tz = (payload.get("tz") or payload.get("timezone") or "Africa/Casablanca").strip()
    return jsonify(ex_time(tz))


@app.route("/cloudfile", methods=["POST"])
def cloudfile_route():
    """Resolve and return a cloud storage file (MediaFire, Google Drive, Mega.nz).

    Body: { url: str, max_size_mb?: int }

    Response — if platform returns a direct URL (MediaFire / Google Drive):
        { ok, platform, filename, mime, size_bytes, size_readable, download_url }
    Response — if platform requires download first (Mega.nz):
        { ok, platform, filename, mime, size_bytes, size_readable, data_b64 }
    """
    payload = request.get_json(silent=True) if request.is_json else request.form
    url = (payload.get("url") or payload.get("query") or "").strip()
    try:
        max_size_mb = int(payload.get("max_size_mb") or 1500)
    except (TypeError, ValueError):
        max_size_mb = 1500
    if not url:
        return jsonify({"error": "missing 'url'"}), 400

    try:
        result = cloud_resolve(url, max_size_mb=max_size_mb)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not result.get("ok"):
        return jsonify({"error": result.get("error", "cloud resolve failed")}), 500

    # Mega returns raw bytes — encode to base64
    if "data" in result:
        raw = result.pop("data")
        result["data_b64"] = base64.b64encode(raw).decode("ascii")

    return jsonify(result)


@app.route("/pinterest/search", methods=["POST"])
def pinterest_search_route():
    """Search Pinterest for images/videos matching a query.

    Body: { query: str, num_results?: int }
    Response: {
      query, results: [{ pin_url, image_url, thumbnail, title }, ...]
    }
    """
    payload = request.get_json(silent=True) if request.is_json else request.form
    query = (payload.get("query") or "").strip()
    try:
        num_results = int(payload.get("num_results") or 6)
    except (TypeError, ValueError):
        num_results = 6
    if not query:
        return jsonify({"error": "missing 'query'"}), 400
    try:
        return jsonify(pin_search(query, num_results=num_results))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/pinterest/download", methods=["POST"])
def pinterest_download_route():
    """Download a Pinterest pin (image or video).

    Body: { query: str }  — can be a URL or a free-text search query
    Response (image):
      { media_type:"image", mime, ext, title, size_bytes, source_url, data_b64 }
    Response (video):
      { media_type:"video", mime, ext, title, size_bytes, source_url, data_b64 }
    """
    payload = request.get_json(silent=True) if request.is_json else request.form
    query = (payload.get("query") or payload.get("url") or "").strip()
    try:
        max_size_mb = int(payload.get("max_size_mb") or 100)
    except (TypeError, ValueError):
        max_size_mb = 100
    if not query:
        return jsonify({"error": "missing 'query'"}), 400
    try:
        result = pin_download(query, max_size_mb=max_size_mb)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if not result.get("ok"):
        return jsonify({"error": result.get("error", "pinterest download failed")}), 500
    data = result.pop("data")
    result["data_b64"] = base64.b64encode(data).decode("ascii")
    return jsonify(result)


@app.route("/pinterest/images", methods=["POST"])
def pinterest_images_route():
    """Search Pinterest and download the top N images.

    Body: { query: str, num_images?: int }
    Response: {
      query, images: [{ mime, ext, title, size_bytes, data_b64 }, ...]
    }
    """
    payload = request.get_json(silent=True) if request.is_json else request.form
    query = (payload.get("query") or "").strip()
    try:
        num_images = int(payload.get("num_images") or 4)
    except (TypeError, ValueError):
        num_images = 4
    if not query:
        return jsonify({"error": "missing 'query'"}), 400
    try:
        result = pin_fetch_images(query, num_images=num_images)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if not result.get("ok"):
        return jsonify({"error": result.get("error", "pinterest images failed")}), 500
    images_out = []
    for img in result.get("images") or []:
        raw = img.pop("data", b"")
        img["data_b64"] = base64.b64encode(raw).decode("ascii")
        images_out.append(img)
    return jsonify({"query": result["query"], "images": images_out})


# =====================================================================
# Extras2 routes: lyrics, quran, hadith, crypto, football, joke,
# country, dictionary, horoscope, shorten, sticker, transcript
# =====================================================================
@app.route("/lyrics", methods=["POST"])
def lyrics_route():
    payload = request.get_json(silent=True) if request.is_json else request.form
    query = (payload.get("query") or payload.get("q") or "").strip()
    if not query:
        return jsonify({"error": "missing 'query'"}), 400
    return jsonify(ex_lyrics(query))


@app.route("/quran", methods=["POST"])
def quran_route():
    payload = request.get_json(silent=True) if request.is_json else request.form
    surah = payload.get("surah")
    ayah = payload.get("ayah") or payload.get("verse")
    query = (payload.get("query") or "").strip() or None
    if not surah and not query:
        return jsonify({"error": "need 'surah' or 'query'"}), 400
    return jsonify(ex_quran(surah=surah, ayah=ayah, query=query))


@app.route("/hadith", methods=["POST"])
def hadith_route():
    payload = request.get_json(silent=True) if request.is_json else request.form
    query = (payload.get("query") or "").strip() or None
    return jsonify(ex_hadith(query=query))


@app.route("/crypto", methods=["POST"])
def crypto_route():
    payload = request.get_json(silent=True) if request.is_json else request.form
    coin = (payload.get("coin") or payload.get("query") or "").strip()
    vs = (payload.get("vs") or "usd").strip()
    if not coin:
        return jsonify({"error": "missing 'coin'"}), 400
    return jsonify(ex_crypto(coin, vs=vs))


@app.route("/football", methods=["POST"])
def football_route():
    payload = request.get_json(silent=True) if request.is_json else request.form
    team = (payload.get("team") or payload.get("query") or "").strip()
    if not team:
        return jsonify({"error": "missing 'team'"}), 400
    return jsonify(ex_football(team))


@app.route("/joke", methods=["POST"])
def joke_route():
    payload = request.get_json(silent=True) if request.is_json else request.form
    lang = (payload.get("lang") or "en").strip()
    return jsonify(ex_joke(lang=lang))


@app.route("/country", methods=["POST"])
def country_route():
    payload = request.get_json(silent=True) if request.is_json else request.form
    name = (payload.get("name") or payload.get("query") or "").strip()
    if not name:
        return jsonify({"error": "missing 'name'"}), 400
    return jsonify(ex_country(name))


@app.route("/dictionary", methods=["POST"])
def dictionary_route():
    payload = request.get_json(silent=True) if request.is_json else request.form
    word = (payload.get("word") or payload.get("query") or "").strip()
    if not word:
        return jsonify({"error": "missing 'word'"}), 400
    return jsonify(ex_dictionary(word))


@app.route("/horoscope", methods=["POST"])
def horoscope_route():
    payload = request.get_json(silent=True) if request.is_json else request.form
    sign = (payload.get("sign") or payload.get("query") or "").strip()
    if not sign:
        return jsonify({"error": "missing 'sign'"}), 400
    return jsonify(ex_horoscope(sign))


@app.route("/shorten", methods=["POST"])
def shorten_route():
    payload = request.get_json(silent=True) if request.is_json else request.form
    url = (payload.get("url") or payload.get("query") or "").strip()
    if not url:
        return jsonify({"error": "missing 'url'"}), 400
    return jsonify(ex_shorten(url))


@app.route("/sticker", methods=["POST"])
def sticker_route():
    """Body (any of): { url, text }  OR  multipart 'file' upload.
    Returns { filename, mime, size_bytes, data_b64 } on success."""
    image_bytes = None
    url = ""
    text = ""

    if request.files and "file" in request.files:
        try:
            image_bytes = request.files["file"].read()
        except Exception:
            image_bytes = None
        # form payload alongside file
        url = (request.form.get("url") or "").strip()
        text = (request.form.get("text") or "").strip()
    else:
        payload = request.get_json(silent=True) if request.is_json else request.form
        url = (payload.get("url") or "").strip()
        text = (payload.get("text") or "").strip()

    if not (image_bytes or url or text):
        return jsonify({"error": "need file, url, or text"}), 400

    out = ex_sticker(image_bytes=image_bytes, url=url or None, text=text or None)
    if not out.get("ok"):
        return jsonify({"error": out.get("error", "sticker failed")}), 500
    return jsonify({
        "filename": out["filename"],
        "mime": out["mime"],
        "size_bytes": out["size_bytes"],
        "data_b64": base64.b64encode(out["webp_bytes"]).decode("ascii"),
    })


@app.route("/transcript", methods=["POST"])
def transcript_route():
    payload = request.get_json(silent=True) if request.is_json else request.form
    url = (payload.get("url") or payload.get("query") or "").strip()
    lang = (payload.get("lang") or "").strip() or None
    if not url:
        return jsonify({"error": "missing 'url'"}), 400
    return jsonify(ex_transcript(url, lang=lang))


@app.route("/reset", methods=["POST"])
def reset():
    if request.is_json:
        user = (request.get_json(silent=True) or {}).get("user")
    else:
        user = request.form.get("user")
    if not user:
        return jsonify({"error": "missing 'user'"}), 400
    try:
        get_brain().reset(user)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
