"""Extra tool helpers for Omar.

Pure-Python helpers used by the Flask server to back new bot tools:
weather, prayer times, currency conversion, translation, Wikipedia,
QR code generation, text-to-speech, and timezone-aware time.

All helpers either return a JSON-serialisable dict or, for the file
producing ones (qr, tts), bytes plus metadata. Network calls use only
free, key-less APIs:

  - Open-Meteo + their geocoder for weather
  - Aladhan for prayer times
  - exchangerate.host for currency
  - Wikipedia REST API for summaries
  - deep-translator (Google free endpoint) for translation
"""
from __future__ import annotations

import io
import re
from datetime import datetime, timedelta, timezone as _tz
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en,ar;q=0.8,fr;q=0.6"}
TIMEOUT = 25


# =====================================================================
# Geocoding (used by weather + prayer times)
# =====================================================================
def _geocode(place: str) -> Optional[Dict[str, Any]]:
    """Resolve a free-text place name -> lat/lon via Open-Meteo geocoder."""
    try:
        r = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": place, "count": 1, "language": "en", "format": "json"},
            headers=HEADERS, timeout=TIMEOUT,
        )
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
        if not results:
            return None
        g = results[0]
        return {
            "name": g.get("name"),
            "country": g.get("country"),
            "lat": g.get("latitude"),
            "lon": g.get("longitude"),
            "tz": g.get("timezone"),
        }
    except Exception:
        return None


# =====================================================================
# 1) Weather (Open-Meteo, no key)
# =====================================================================
_WEATHER_CODES = {
    0: "صافي", 1: "صافي معظم الوقت", 2: "غائم جزئياً", 3: "غائم",
    45: "ضباب", 48: "ضباب جليدي",
    51: "رذاذ خفيف", 53: "رذاذ", 55: "رذاذ كثيف",
    61: "مطر خفيف", 63: "مطر", 65: "مطر غزير",
    71: "ثلج خفيف", 73: "ثلج", 75: "ثلج كثيف",
    80: "زخات مطر", 81: "زخات مطر قوية", 82: "زخات مطر عنيفة",
    95: "عاصفة رعدية", 96: "عاصفة رعدية مع برَد", 99: "عاصفة شديدة مع برَد",
}


def get_weather(place: str) -> Dict[str, Any]:
    g = _geocode(place)
    if not g:
        return {"ok": False, "error": f"ما لقيتش بلاصة بهاد الاسم: {place}"}
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": g["lat"], "longitude": g["lon"],
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                           "is_day,precipitation,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,"
                         "precipitation_probability_max,weather_code",
                "timezone": "auto", "forecast_days": 3,
            },
            headers=HEADERS, timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": f"weather request failed: {e}"}

    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    code = cur.get("weather_code")
    forecast = []
    days = daily.get("time") or []
    for i in range(len(days)):
        forecast.append({
            "date": days[i],
            "min": (daily.get("temperature_2m_min") or [None])[i],
            "max": (daily.get("temperature_2m_max") or [None])[i],
            "rain_pct": (daily.get("precipitation_probability_max") or [None])[i],
            "desc": _WEATHER_CODES.get(
                (daily.get("weather_code") or [None])[i], "—"),
        })
    return {
        "ok": True,
        "place": f"{g['name']}, {g['country']}",
        "current": {
            "temp_c": cur.get("temperature_2m"),
            "feels_c": cur.get("apparent_temperature"),
            "humidity": cur.get("relative_humidity_2m"),
            "wind_kmh": cur.get("wind_speed_10m"),
            "is_day": cur.get("is_day"),
            "desc": _WEATHER_CODES.get(code, "—"),
        },
        "forecast": forecast,
    }


# =====================================================================
# 2) Prayer times (Aladhan, no key)
# =====================================================================
_METHODS = {
    "MWL": 3, "ISNA": 2, "EGY": 5, "MAKKAH": 4,
    "KARACHI": 1, "TEHRAN": 7, "JAFARI": 0, "MOROCCO": 21,
}


def get_prayer_times(
    place: str,
    method: str = "MOROCCO",
    date: Optional[str] = None,
) -> Dict[str, Any]:
    """Return today's (or requested date's) prayer times for a city."""
    g = _geocode(place)
    if not g:
        return {"ok": False, "error": f"ما لقيتش بلاصة: {place}"}

    method_id = _METHODS.get((method or "MOROCCO").upper(), 21)
    when = date or datetime.now().strftime("%d-%m-%Y")
    try:
        r = requests.get(
            f"https://api.aladhan.com/v1/timings/{when}",
            params={
                "latitude": g["lat"], "longitude": g["lon"],
                "method": method_id,
            },
            headers=HEADERS, timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": f"prayer api failed: {e}"}

    timings = (data.get("data") or {}).get("timings") or {}
    keys = ["Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Isha"]
    return {
        "ok": True,
        "place": f"{g['name']}, {g['country']}",
        "date": when,
        "method": method.upper(),
        "timings": {k: timings.get(k) for k in keys if k in timings},
    }


# =====================================================================
# 3) Currency conversion (exchangerate.host, no key)
# =====================================================================
def convert_currency(
    amount: float, src: str, dst: str,
) -> Dict[str, Any]:
    src = (src or "").upper().strip()
    dst = (dst or "").upper().strip()
    if not src or not dst:
        return {"ok": False, "error": "missing source or target currency"}
    try:
        amt = float(amount)
    except Exception:
        return {"ok": False, "error": "invalid amount"}

    # Primary: exchangerate.host
    try:
        r = requests.get(
            "https://api.exchangerate.host/convert",
            params={"from": src, "to": dst, "amount": amt},
            headers=HEADERS, timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        result = data.get("result")
        rate = (data.get("info") or {}).get("rate")
        if result is not None:
            return {
                "ok": True, "from": src, "to": dst,
                "amount": amt, "rate": rate, "result": result,
            }
    except Exception:
        pass

    # Fallback: open.er-api.com
    try:
        r = requests.get(
            f"https://open.er-api.com/v6/latest/{src}",
            headers=HEADERS, timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        rates = data.get("rates") or {}
        if dst not in rates:
            return {"ok": False, "error": f"currency {dst} not found"}
        rate = rates[dst]
        return {
            "ok": True, "from": src, "to": dst,
            "amount": amt, "rate": rate, "result": amt * rate,
        }
    except Exception as e:
        return {"ok": False, "error": f"currency request failed: {e}"}


# =====================================================================
# 4) Translation (deep-translator, Google free endpoint)
# =====================================================================
def translate_text(text: str, target: str = "en", source: str = "auto") -> Dict[str, Any]:
    if not (text or "").strip():
        return {"ok": False, "error": "empty text"}
    try:
        from deep_translator import GoogleTranslator
        translated = GoogleTranslator(
            source=(source or "auto"),
            target=(target or "en").lower(),
        ).translate(text)
        return {"ok": True, "source": source, "target": target,
                "text": text, "translated": translated}
    except Exception as e:
        return {"ok": False, "error": f"translation failed: {e}"}


# =====================================================================
# 5) Wikipedia summary (no key)
# =====================================================================
def wiki_summary(query: str, lang: str = "ar") -> Dict[str, Any]:
    if not (query or "").strip():
        return {"ok": False, "error": "empty query"}
    lang = (lang or "ar").lower()

    # Resolve the page title via the search API first to handle redirects
    # and approximate matches.
    try:
        s = requests.get(
            f"https://{lang}.wikipedia.org/w/api.php",
            params={
                "action": "query", "list": "search", "srsearch": query,
                "srlimit": 1, "format": "json",
            },
            headers=HEADERS, timeout=TIMEOUT,
        )
        s.raise_for_status()
        hits = (s.json().get("query") or {}).get("search") or []
        if not hits:
            return {"ok": False, "error": f"no Wikipedia article for '{query}'"}
        title = hits[0]["title"]
    except Exception as e:
        return {"ok": False, "error": f"wiki search failed: {e}"}

    try:
        r = requests.get(
            f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/"
            + requests.utils.quote(title, safe=""),
            headers=HEADERS, timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": f"wiki summary failed: {e}"}

    return {
        "ok": True,
        "title": data.get("title") or title,
        "description": data.get("description"),
        "extract": data.get("extract"),
        "url": ((data.get("content_urls") or {}).get("desktop") or {}).get("page"),
        "thumbnail": (data.get("thumbnail") or {}).get("source"),
    }


# =====================================================================
# 6) QR code generation (image bytes)
# =====================================================================
def make_qr(data: str) -> Dict[str, Any]:
    if not (data or "").strip():
        return {"ok": False, "error": "empty data"}
    try:
        import qrcode
        img = qrcode.make(data)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png = buf.getvalue()
        return {
            "ok": True,
            "data": data,
            "png_bytes": png,
            "size_bytes": len(png),
            "filename": "qr.png",
            "mime": "image/png",
        }
    except Exception as e:
        return {"ok": False, "error": f"qr generation failed: {e}"}


# =====================================================================
# 7) Text-to-speech (gTTS, audio bytes)
# =====================================================================
_LANG_ALIASES = {
    "arabic": "ar", "ara": "ar", "ar": "ar",
    "english": "en", "eng": "en", "en": "en",
    "french": "fr", "fra": "fr", "francais": "fr", "fr": "fr",
    "spanish": "es", "es": "es", "german": "de", "de": "de",
    "italian": "it", "it": "it", "portuguese": "pt", "pt": "pt",
    "turkish": "tr", "tr": "tr", "russian": "ru", "ru": "ru",
}


def text_to_speech(text: str, lang: str = "ar") -> Dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty text"}
    if len(text) > 5000:
        text = text[:5000]
    lang_code = _LANG_ALIASES.get((lang or "ar").lower(), (lang or "ar").lower())
    try:
        from gtts import gTTS
        tts = gTTS(text=text, lang=lang_code, slow=False)
        buf = io.BytesIO()
        tts.write_to_fp(buf)
        mp3 = buf.getvalue()
        return {
            "ok": True,
            "text": text,
            "lang": lang_code,
            "mp3_bytes": mp3,
            "size_bytes": len(mp3),
            "filename": "voice.mp3",
            "mime": "audio/mpeg",
        }
    except Exception as e:
        return {"ok": False, "error": f"tts failed: {e}"}


# =====================================================================
# 8) Timezone-aware current time
# =====================================================================
_TZ_ALIASES = {
    "morocco": "Africa/Casablanca", "casa": "Africa/Casablanca",
    "casablanca": "Africa/Casablanca", "rabat": "Africa/Casablanca",
    "algeria": "Africa/Algiers", "tunis": "Africa/Tunis",
    "egypt": "Africa/Cairo", "cairo": "Africa/Cairo",
    "saudi": "Asia/Riyadh", "riyadh": "Asia/Riyadh",
    "uae": "Asia/Dubai", "dubai": "Asia/Dubai",
    "paris": "Europe/Paris", "france": "Europe/Paris",
    "london": "Europe/London", "uk": "Europe/London",
    "newyork": "America/New_York", "ny": "America/New_York",
    "tokyo": "Asia/Tokyo", "japan": "Asia/Tokyo",
    "utc": "UTC", "gmt": "UTC",
}


def get_time(tz: str = "Africa/Casablanca") -> Dict[str, Any]:
    raw = (tz or "Africa/Casablanca").strip()
    key = re.sub(r"[\s_]+", "", raw).lower()
    name = _TZ_ALIASES.get(key, raw)
    try:
        zone = ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return {"ok": False, "error": f"unknown timezone '{tz}'"}
    now = datetime.now(zone)
    offset = now.utcoffset() or timedelta(0)
    hours = int(offset.total_seconds() // 3600)
    return {
        "ok": True,
        "tz": name,
        "iso": now.isoformat(timespec="seconds"),
        "human": now.strftime("%A %d %B %Y, %H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "offset": f"UTC{'+' if hours >= 0 else ''}{hours}",
    }
