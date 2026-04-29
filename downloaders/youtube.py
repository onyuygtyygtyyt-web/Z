"""YouTube downloader — replaces yt-dlp with public scraper APIs.

Two providers are tried in order so a single outage on either side does
not break the bot:

  1. SaveTube  (yt.savetube.me)         — AES-128-CBC payload, primary
  2. ytdown.to (app.ytdown.to/proxy.php) — HTML-form proxy, fallback

For audio we always prefer the SaveTube MP3 endpoint because it is the
fastest and returns a true MP3 (no remux needed).
"""
from __future__ import annotations

import base64
import re
import time
from typing import Any, Dict, Optional

import requests

from .common import (
    DEFAULT_HEADERS,
    MOBILE_UA,
    TIMEOUT,
    download_to_bytes,
    ext_from_url,
    mime_for_ext,
)

YT_URL_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|shorts/|embed/|v/)|youtu\.be/)"
    r"([a-zA-Z0-9_-]{11})"
)


def is_youtube(url: str) -> bool:
    return bool(YT_URL_RE.search(url or ""))


def video_id(url: str) -> Optional[str]:
    m = YT_URL_RE.search(url or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# SaveTube provider
# ---------------------------------------------------------------------------

_SAVETUBE_KEY_HEX = "C5D58EF67A7584E4A29F6C35BBC4EB12"
_SAVETUBE_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://yt.savetube.me",
    "Referer": "https://yt.savetube.me/",
    "User-Agent": MOBILE_UA,
}


def _savetube_decrypt(enc_b64: str) -> Dict[str, Any]:
    """Decrypt SaveTube's AES-128-CBC base64 payload → JSON dict."""
    from Cryptodome.Cipher import AES  # type: ignore

    raw = base64.b64decode(enc_b64)
    iv, ct = raw[:16], raw[16:]
    cipher = AES.new(bytes.fromhex(_SAVETUBE_KEY_HEX), AES.MODE_CBC, iv)
    pt = cipher.decrypt(ct)
    # PKCS#7 unpad
    pad = pt[-1]
    if 1 <= pad <= 16:
        pt = pt[:-pad]
    import json
    return json.loads(pt.decode("utf-8", errors="replace"))


def _savetube_cdn() -> str:
    r = requests.get(
        "https://media.savetube.vip/api/random-cdn",
        headers=_SAVETUBE_HEADERS,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["cdn"]


def _savetube(url: str, *, mode: str, quality: str) -> Dict[str, Any]:
    vid = video_id(url)
    if not vid:
        raise ValueError("Not a YouTube URL")
    cdn = _savetube_cdn()

    info = requests.post(
        f"https://{cdn}/v2/info",
        json={"url": f"https://www.youtube.com/watch?v={vid}"},
        headers=_SAVETUBE_HEADERS,
        timeout=TIMEOUT,
    )
    info.raise_for_status()
    payload = info.json()
    decrypted = _savetube_decrypt(payload["data"])

    if mode == "audio":
        download_type = "audio"
        # SaveTube supports 64/128/192/256/320 — clamp to a known value.
        try:
            q = int(re.sub(r"\D", "", quality or "128") or 128)
        except ValueError:
            q = 128
        q_str = str(min([64, 128, 192, 256, 320], key=lambda x: abs(x - q)))
    else:
        download_type = "video"
        try:
            q = int(re.sub(r"\D", "", quality or "720") or 720)
        except ValueError:
            q = 720
        q_str = str(min([144, 240, 360, 480, 720, 1080], key=lambda x: abs(x - q)))

    dl = requests.post(
        f"https://{cdn}/download",
        json={
            "id": vid,
            "downloadType": download_type,
            "quality": q_str,
            "key": decrypted["key"],
        },
        headers=_SAVETUBE_HEADERS,
        timeout=TIMEOUT,
    )
    dl.raise_for_status()
    durl = dl.json().get("data", {}).get("downloadUrl")
    if not durl:
        raise RuntimeError("SaveTube returned no download URL")

    ext = "mp3" if mode == "audio" else "mp4"
    mime = "audio/mpeg" if mode == "audio" else "video/mp4"
    return {
        "ok": True,
        "title": decrypted.get("title"),
        "uploader": decrypted.get("uploader") or decrypted.get("channel"),
        "duration": decrypted.get("duration"),
        "thumbnail": decrypted.get("thumbnail"),
        "webpage_url": f"https://www.youtube.com/watch?v={vid}",
        "ext": ext,
        "mime": mime,
        "download_url": durl,
        "quality": q_str,
        "provider": "savetube",
    }


# ---------------------------------------------------------------------------
# ytdown.to provider (fallback)
# ---------------------------------------------------------------------------

_YTD_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://app.ytdown.to/id21/",
    "Origin": "https://app.ytdown.to",
    "User-Agent": MOBILE_UA,
}


def _normalize_q(q: str) -> str:
    q = (q or "").lower()
    for marker in ("1080", "720", "480", "360", "240", "144"):
        if marker in q:
            return marker
    if q in ("fhd",):
        return "1080"
    if q in ("hd",):
        return "720"
    if q in ("sd",):
        return "360"
    return "720"


def _ytdownto_convert(media_url: str) -> Optional[Dict[str, Any]]:
    try:
        r = requests.post(
            "https://app.ytdown.to/proxy.php",
            data={"url": media_url},
            headers=_YTD_HEADERS,
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        api = r.json().get("api") or {}
        return api if api.get("status") == "completed" else None
    except Exception:
        return None


def _ytdownto(url: str, *, mode: str, quality: str) -> Dict[str, Any]:
    r = requests.post(
        "https://app.ytdown.to/proxy.php",
        data={"url": url},
        headers=_YTD_HEADERS,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    api = r.json().get("api") or {}
    if api.get("status") != "ok":
        raise RuntimeError(f"ytdown.to status={api.get('status')}")

    target_q = _normalize_q(quality)

    selected_video = None
    fallback_video = None
    best_audio = None
    fallback_audio = None

    for item in api.get("mediaItems") or []:
        res = _ytdownto_convert(item.get("mediaUrl"))
        if not res:
            continue
        ext = (res.get("fileName") or "").rsplit(".", 1)[-1].lower() or "bin"
        obj = {
            "quality": item.get("mediaQuality"),
            "url": res.get("fileUrl"),
            "size": res.get("fileSize"),
            "ext": ext,
            "mime": ("video/" + ext) if item.get("type") == "Video" else ("audio/" + ext),
        }
        if item.get("type") == "Video":
            qn = _normalize_q(item.get("mediaQuality") or "")
            if qn == target_q and not selected_video:
                selected_video = obj
            if not fallback_video or int(qn or 0) > int(_normalize_q(fallback_video["quality"]) or 0):
                fallback_video = obj
        elif item.get("type") == "Audio":
            if ext == "mp3" and not best_audio:
                best_audio = obj
            if not fallback_audio:
                fallback_audio = obj

    if mode == "audio":
        chosen = best_audio or fallback_audio
    else:
        chosen = selected_video or fallback_video

    if not chosen or not chosen.get("url"):
        raise RuntimeError("ytdown.to returned no media URL")

    return {
        "ok": True,
        "title": api.get("title"),
        "uploader": (api.get("userInfo") or {}).get("name"),
        "duration": (api.get("mediaItems") or [{}])[0].get("mediaDuration"),
        "thumbnail": api.get("imagePreviewUrl"),
        "webpage_url": url,
        "ext": chosen["ext"],
        "mime": chosen["mime"],
        "download_url": chosen["url"],
        "quality": chosen.get("quality"),
        "provider": "ytdown.to",
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def resolve(url: str, *, mode: str = "video", quality: str = "720") -> Dict[str, Any]:
    """Return metadata + a direct download URL for a YouTube video."""
    last_err: Optional[Exception] = None
    for fn in (_savetube, _ytdownto):
        try:
            return fn(url, mode=mode, quality=quality)
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(
        f"All YouTube providers failed: {last_err}"
    )


def fetch_bytes(url: str, *, mode: str = "video", quality: str = "720",
                max_size_mb: int = 1500) -> Dict[str, Any]:
    """Resolve + download in one call; returns dict with `data` bytes."""
    info = resolve(url, mode=mode, quality=quality)
    data = download_to_bytes(
        info["download_url"], max_size_mb=max_size_mb,
        referer="https://www.youtube.com/",
    )
    info["data"] = data
    info["size_bytes"] = len(data)
    return info
