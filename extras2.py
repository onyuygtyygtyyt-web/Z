"""Second batch of tool helpers for Omar.

Adds: lyrics, Quran, hadith, crypto prices, football scores, jokes,
country info, dictionary, horoscope, URL shortener, sticker maker, and
YouTube transcript. All use free, key-less APIs (or local conversion).
"""
from __future__ import annotations

import io
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse, parse_qs

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en,ar;q=0.8,fr;q=0.6"}
TIMEOUT = 25


# =====================================================================
# 1) Lyrics (lyrics.ovh, no key)
# =====================================================================
def get_lyrics(query: str) -> Dict[str, Any]:
    """`query` is "artist - title" or just a free text we try to split."""
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "empty query"}

    artist, title = "", ""
    for sep in (" - ", " – ", " — ", " by ", "|"):
        if sep in q:
            parts = q.split(sep, 1)
            artist, title = parts[0].strip(), parts[1].strip()
            break
    if not artist or not title:
        # Fallback: assume "title artist" or single string -> let API try
        words = q.split()
        if len(words) >= 2:
            artist, title = words[0], " ".join(words[1:])
        else:
            artist, title = q, q

    # Try both orderings
    for a, t in [(artist, title), (title, artist)]:
        try:
            r = requests.get(
                f"https://api.lyrics.ovh/v1/{quote(a)}/{quote(t)}",
                headers=HEADERS, timeout=TIMEOUT,
            )
            if r.status_code == 200:
                data = r.json() or {}
                lyr = (data.get("lyrics") or "").strip()
                if lyr:
                    return {
                        "ok": True, "artist": a, "title": t,
                        "lyrics": lyr,
                    }
        except Exception:
            continue

    return {"ok": False, "error": f"ما لقيتش كلمات الأغنية: {query}"}


# =====================================================================
# 2) Quran (alquran.cloud, no key)
# =====================================================================
_SURAH_NAMES_AR = {
    "الفاتحة": 1, "البقرة": 2, "آل عمران": 3, "النساء": 4, "المائدة": 5,
    "الأنعام": 6, "الأعراف": 7, "الأنفال": 8, "التوبة": 9, "يونس": 10,
    "هود": 11, "يوسف": 12, "الرعد": 13, "إبراهيم": 14, "الحجر": 15,
    "النحل": 16, "الإسراء": 17, "الكهف": 18, "مريم": 19, "طه": 20,
    "الأنبياء": 21, "الحج": 22, "المؤمنون": 23, "النور": 24, "الفرقان": 25,
    "الشعراء": 26, "النمل": 27, "القصص": 28, "العنكبوت": 29, "الروم": 30,
    "لقمان": 31, "السجدة": 32, "الأحزاب": 33, "سبأ": 34, "فاطر": 35,
    "يس": 36, "يٰس": 36, "الصافات": 37, "ص": 38, "الزمر": 39, "غافر": 40,
    "فصلت": 41, "الشورى": 42, "الزخرف": 43, "الدخان": 44, "الجاثية": 45,
    "الأحقاف": 46, "محمد": 47, "الفتح": 48, "الحجرات": 49, "ق": 50,
    "الذاريات": 51, "الطور": 52, "النجم": 53, "القمر": 54, "الرحمن": 55,
    "الواقعة": 56, "الحديد": 57, "المجادلة": 58, "الحشر": 59, "الممتحنة": 60,
    "الصف": 61, "الجمعة": 62, "المنافقون": 63, "التغابن": 64, "الطلاق": 65,
    "التحريم": 66, "الملك": 67, "القلم": 68, "الحاقة": 69, "المعارج": 70,
    "نوح": 71, "الجن": 72, "المزمل": 73, "المدثر": 74, "القيامة": 75,
    "الإنسان": 76, "المرسلات": 77, "النبأ": 78, "النازعات": 79, "عبس": 80,
    "التكوير": 81, "الانفطار": 82, "المطففين": 83, "الانشقاق": 84,
    "البروج": 85, "الطارق": 86, "الأعلى": 87, "الغاشية": 88, "الفجر": 89,
    "البلد": 90, "الشمس": 91, "الليل": 92, "الضحى": 93, "الشرح": 94,
    "التين": 95, "العلق": 96, "القدر": 97, "البينة": 98, "الزلزلة": 99,
    "العاديات": 100, "القارعة": 101, "التكاثر": 102, "العصر": 103,
    "الهمزة": 104, "الفيل": 105, "قريش": 106, "الماعون": 107, "الكوثر": 108,
    "الكافرون": 109, "النصر": 110, "المسد": 111, "الإخلاص": 112,
    "الفلق": 113, "الناس": 114,
}


def _resolve_surah(name_or_num) -> Optional[int]:
    if name_or_num is None:
        return None
    s = str(name_or_num).strip()
    if s.isdigit():
        n = int(s)
        return n if 1 <= n <= 114 else None
    s = s.replace("سورة ", "").strip()
    if s in _SURAH_NAMES_AR:
        return _SURAH_NAMES_AR[s]
    # Loose match (remove diacritics + try contains)
    no_diac = re.sub(r"[\u064B-\u0652\u0670]", "", s)
    for k, v in _SURAH_NAMES_AR.items():
        kk = re.sub(r"[\u064B-\u0652\u0670]", "", k)
        if kk == no_diac or no_diac in kk or kk in no_diac:
            return v
    return None


def get_quran(
    surah: Optional[Any] = None,
    ayah: Optional[Any] = None,
    query: Optional[str] = None,
) -> Dict[str, Any]:
    """Get a verse, a range, or a whole short surah.

    - surah + ayah -> single ayah
    - surah only -> whole surah (capped at 30 ayat)
    - query -> text search (Arabic)
    """
    try:
        # Text search
        if query and not surah:
            r = requests.get(
                f"https://api.alquran.cloud/v1/search/{quote(query)}/all/ar",
                headers=HEADERS, timeout=TIMEOUT,
            )
            r.raise_for_status()
            data = r.json() or {}
            matches = ((data.get("data") or {}).get("matches") or [])[:5]
            if not matches:
                return {"ok": False, "error": f"ما لقيتش آيات على: {query}"}
            return {
                "ok": True,
                "mode": "search",
                "query": query,
                "matches": [
                    {
                        "surah": m.get("surah", {}).get("englishName"),
                        "surah_ar": m.get("surah", {}).get("name"),
                        "number": m.get("numberInSurah"),
                        "text": m.get("text"),
                    }
                    for m in matches
                ],
            }

        sn = _resolve_surah(surah)
        if not sn:
            return {"ok": False, "error": f"ما عرفتش السورة: {surah}"}

        # Single ayah
        if ayah:
            try:
                an = int(str(ayah).strip())
            except Exception:
                return {"ok": False, "error": f"رقم آية غير صحيح: {ayah}"}
            r = requests.get(
                f"https://api.alquran.cloud/v1/ayah/{sn}:{an}/quran-uthmani",
                headers=HEADERS, timeout=TIMEOUT,
            )
            r.raise_for_status()
            d = (r.json() or {}).get("data") or {}
            if not d:
                return {"ok": False, "error": "ما لقيت الآية"}
            return {
                "ok": True,
                "mode": "ayah",
                "surah": (d.get("surah") or {}).get("name"),
                "surah_en": (d.get("surah") or {}).get("englishName"),
                "number": d.get("numberInSurah"),
                "text": d.get("text"),
                "audio": f"https://cdn.islamic.network/quran/audio/128/ar.alafasy/{d.get('number')}.mp3",
            }

        # Whole surah (cap to 30 ayat to keep response small)
        r = requests.get(
            f"https://api.alquran.cloud/v1/surah/{sn}/quran-uthmani",
            headers=HEADERS, timeout=TIMEOUT,
        )
        r.raise_for_status()
        d = (r.json() or {}).get("data") or {}
        ayat = (d.get("ayahs") or [])[:30]
        return {
            "ok": True,
            "mode": "surah",
            "surah": d.get("name"),
            "surah_en": d.get("englishName"),
            "number_of_ayahs": d.get("numberOfAyahs"),
            "ayat": [
                {"n": a.get("numberInSurah"), "text": a.get("text")}
                for a in ayat
            ],
            "truncated": (d.get("numberOfAyahs") or 0) > 30,
        }
    except Exception as e:
        return {"ok": False, "error": f"quran api failed: {e}"}


# =====================================================================
# 3) Hadith (hadeethenc.com - Arabic hadith API, no key)
# =====================================================================
def get_hadith(query: Optional[str] = None) -> Dict[str, Any]:
    """Random Arabic hadith with explanation, or search by keyword."""
    try:
        if query and query.strip():
            r = requests.get(
                "https://hadeethenc.com/api/v1/hadeeths/list/",
                params={"language": "ar", "page": 1, "per_page": 5},
                headers=HEADERS, timeout=TIMEOUT,
            )
            r.raise_for_status()
            data = (r.json() or {}).get("data") or []
            # Filter by query in titles
            ql = query.strip()
            matches = [h for h in data if ql in (h.get("title") or "")][:3]
            if not matches:
                matches = data[:3]
            results = []
            for h in matches:
                rr = requests.get(
                    f"https://hadeethenc.com/api/v1/hadeeths/one/",
                    params={"language": "ar", "id": h.get("id")},
                    headers=HEADERS, timeout=TIMEOUT,
                )
                if rr.ok:
                    one = rr.json() or {}
                    results.append({
                        "title": one.get("title"),
                        "hadeeth": one.get("hadeeth"),
                        "attribution": one.get("attribution"),
                        "grade": one.get("grade"),
                        "explanation": (one.get("explanation") or "")[:600],
                    })
            return {"ok": True, "mode": "search", "query": query, "results": results}

        # Random: get list and pick first
        import random as _rnd
        r = requests.get(
            "https://hadeethenc.com/api/v1/hadeeths/list/",
            params={"language": "ar", "page": _rnd.randint(1, 50), "per_page": 10},
            headers=HEADERS, timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = (r.json() or {}).get("data") or []
        if not data:
            return {"ok": False, "error": "ما لقيتش حديث"}
        pick = _rnd.choice(data)
        rr = requests.get(
            "https://hadeethenc.com/api/v1/hadeeths/one/",
            params={"language": "ar", "id": pick.get("id")},
            headers=HEADERS, timeout=TIMEOUT,
        )
        rr.raise_for_status()
        one = rr.json() or {}
        return {
            "ok": True,
            "mode": "random",
            "title": one.get("title"),
            "hadeeth": one.get("hadeeth"),
            "attribution": one.get("attribution"),
            "grade": one.get("grade"),
            "explanation": (one.get("explanation") or "")[:600],
        }
    except Exception as e:
        return {"ok": False, "error": f"hadith api failed: {e}"}


# =====================================================================
# 4) Crypto prices (CoinGecko, no key)
# =====================================================================
_CRYPTO_ALIASES = {
    "btc": "bitcoin", "bitcoin": "bitcoin",
    "eth": "ethereum", "ethereum": "ethereum",
    "bnb": "binancecoin", "sol": "solana", "solana": "solana",
    "xrp": "ripple", "ripple": "ripple",
    "ada": "cardano", "cardano": "cardano",
    "doge": "dogecoin", "dogecoin": "dogecoin",
    "ton": "the-open-network", "trx": "tron", "tron": "tron",
    "matic": "matic-network", "polygon": "matic-network",
    "ltc": "litecoin", "litecoin": "litecoin",
    "shib": "shiba-inu", "avax": "avalanche-2",
    "dot": "polkadot", "link": "chainlink",
    "bch": "bitcoin-cash", "uni": "uniswap",
    "atom": "cosmos", "near": "near", "apt": "aptos",
    "usdt": "tether", "usdc": "usd-coin",
}


def get_crypto(coin: str, vs: str = "usd") -> Dict[str, Any]:
    if not (coin or "").strip():
        return {"ok": False, "error": "empty coin"}
    coin_id = _CRYPTO_ALIASES.get(coin.strip().lower(), coin.strip().lower())
    vs_list = "usd,mad,eur,gbp,sar,aed"
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": coin_id, "vs_currencies": vs_list,
                "include_24hr_change": "true",
                "include_market_cap": "true",
            },
            headers=HEADERS, timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json() or {}
        if coin_id not in data:
            # Try search
            s = requests.get(
                "https://api.coingecko.com/api/v3/search",
                params={"query": coin}, headers=HEADERS, timeout=TIMEOUT,
            )
            if s.ok:
                hits = (s.json() or {}).get("coins") or []
                if hits:
                    coin_id = hits[0].get("id")
                    r = requests.get(
                        "https://api.coingecko.com/api/v3/simple/price",
                        params={"ids": coin_id, "vs_currencies": vs_list,
                                "include_24hr_change": "true"},
                        headers=HEADERS, timeout=TIMEOUT,
                    )
                    r.raise_for_status()
                    data = r.json() or {}
        if coin_id not in data:
            return {"ok": False, "error": f"ما لقيتش العملة: {coin}"}
        prices = data[coin_id]
        return {
            "ok": True,
            "coin": coin_id,
            "prices": {
                k: prices.get(k) for k in
                ("usd", "eur", "mad", "gbp", "sar", "aed") if k in prices
            },
            "change_24h_pct": prices.get("usd_24h_change"),
            "market_cap_usd": prices.get("usd_market_cap"),
        }
    except Exception as e:
        return {"ok": False, "error": f"crypto api failed: {e}"}


# =====================================================================
# 5) Football (TheSportsDB, no key)
# =====================================================================
def get_football(team: str) -> Dict[str, Any]:
    if not (team or "").strip():
        return {"ok": False, "error": "empty team"}
    name = team.strip().replace(" ", "_")
    try:
        r = requests.get(
            f"https://www.thesportsdb.com/api/v1/json/3/searchteams.php",
            params={"t": name}, headers=HEADERS, timeout=TIMEOUT,
        )
        r.raise_for_status()
        teams = (r.json() or {}).get("teams") or []
        if not teams:
            return {"ok": False, "error": f"ما لقيتش الفريق: {team}"}
        t = teams[0]
        team_id = t.get("idTeam")

        # Last 5 matches
        last = []
        try:
            lr = requests.get(
                "https://www.thesportsdb.com/api/v1/json/3/eventslast.php",
                params={"id": team_id}, headers=HEADERS, timeout=TIMEOUT,
            )
            if lr.ok:
                for m in (lr.json() or {}).get("results") or []:
                    last.append({
                        "date": m.get("dateEvent"),
                        "home": m.get("strHomeTeam"),
                        "away": m.get("strAwayTeam"),
                        "score": f"{m.get('intHomeScore') or '-'} - {m.get('intAwayScore') or '-'}",
                        "league": m.get("strLeague"),
                    })
        except Exception:
            pass

        # Next 5 matches
        nxt = []
        try:
            nr = requests.get(
                "https://www.thesportsdb.com/api/v1/json/3/eventsnext.php",
                params={"id": team_id}, headers=HEADERS, timeout=TIMEOUT,
            )
            if nr.ok:
                for m in (nr.json() or {}).get("events") or []:
                    nxt.append({
                        "date": m.get("dateEvent"),
                        "time": m.get("strTime"),
                        "home": m.get("strHomeTeam"),
                        "away": m.get("strAwayTeam"),
                        "league": m.get("strLeague"),
                    })
        except Exception:
            pass

        return {
            "ok": True,
            "team": t.get("strTeam"),
            "country": t.get("strCountry"),
            "league": t.get("strLeague"),
            "stadium": t.get("strStadium"),
            "founded": t.get("intFormedYear"),
            "badge": t.get("strBadge") or t.get("strTeamBadge"),
            "last_matches": last[:5],
            "next_matches": nxt[:5],
        }
    except Exception as e:
        return {"ok": False, "error": f"football api failed: {e}"}


# =====================================================================
# 6) Joke (jokeapi.dev, no key)
# =====================================================================
def get_joke(lang: str = "en") -> Dict[str, Any]:
    lang = (lang or "en").lower()
    if lang not in ("en", "de", "es", "fr", "pt", "cs"):
        lang = "en"
    try:
        r = requests.get(
            "https://v2.jokeapi.dev/joke/Any",
            params={
                "lang": lang, "safe-mode": "true",
                "blacklistFlags": "religious,political,racist,sexist,explicit",
            },
            headers=HEADERS, timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json() or {}
        if data.get("error"):
            return {"ok": False, "error": data.get("message", "joke api error")}
        if data.get("type") == "single":
            joke_text = data.get("joke") or ""
        else:
            joke_text = f"{data.get('setup', '')}\n\n{data.get('delivery', '')}"
        return {
            "ok": True,
            "lang": lang,
            "category": data.get("category"),
            "joke": joke_text.strip(),
        }
    except Exception as e:
        return {"ok": False, "error": f"joke api failed: {e}"}


# =====================================================================
# 7) Country info (restcountries.com, no key)
# =====================================================================
def get_country(name: str) -> Dict[str, Any]:
    if not (name or "").strip():
        return {"ok": False, "error": "empty name"}
    try:
        r = requests.get(
            f"https://restcountries.com/v3.1/name/{quote(name.strip())}",
            params={"fullText": "false"},
            headers=HEADERS, timeout=TIMEOUT,
        )
        if r.status_code == 404:
            return {"ok": False, "error": f"ما لقيتش الدولة: {name}"}
        r.raise_for_status()
        arr = r.json() or []
        if not arr:
            return {"ok": False, "error": f"ما لقيتش الدولة: {name}"}
        c = arr[0]
        names = c.get("name") or {}
        translations = (c.get("translations") or {}).get("ara") or {}
        currencies = c.get("currencies") or {}
        cur = next(iter(currencies.values()), {}) if currencies else {}
        languages = c.get("languages") or {}
        return {
            "ok": True,
            "name_en": names.get("common"),
            "name_official": names.get("official"),
            "name_ar": translations.get("common"),
            "capital": (c.get("capital") or [None])[0],
            "region": c.get("region"),
            "subregion": c.get("subregion"),
            "population": c.get("population"),
            "area_km2": c.get("area"),
            "currency": f"{cur.get('name', '')} ({cur.get('symbol', '')})".strip(),
            "languages": ", ".join(languages.values()) if languages else None,
            "flag": (c.get("flags") or {}).get("png"),
            "flag_emoji": c.get("flag"),
            "maps": (c.get("maps") or {}).get("googleMaps"),
            "tld": ", ".join(c.get("tld") or []),
            "calling_code": (
                ((c.get("idd") or {}).get("root") or "")
                + ((c.get("idd") or {}).get("suffixes") or [""])[0]
            ),
        }
    except Exception as e:
        return {"ok": False, "error": f"country api failed: {e}"}


# =====================================================================
# 8) Dictionary (dictionaryapi.dev, English, no key)
# =====================================================================
def get_dictionary(word: str) -> Dict[str, Any]:
    w = (word or "").strip()
    if not w:
        return {"ok": False, "error": "empty word"}
    try:
        r = requests.get(
            f"https://api.dictionaryapi.dev/api/v2/entries/en/{quote(w)}",
            headers=HEADERS, timeout=TIMEOUT,
        )
        if r.status_code == 404:
            return {"ok": False, "error": f"word not found: {w}"}
        r.raise_for_status()
        arr = r.json() or []
        if not arr:
            return {"ok": False, "error": f"word not found: {w}"}
        entry = arr[0]
        meanings: List[Dict[str, Any]] = []
        for m in (entry.get("meanings") or [])[:4]:
            defs = []
            for d in (m.get("definitions") or [])[:3]:
                defs.append({
                    "definition": d.get("definition"),
                    "example": d.get("example"),
                })
            meanings.append({
                "part_of_speech": m.get("partOfSpeech"),
                "definitions": defs,
                "synonyms": (m.get("synonyms") or [])[:5],
            })
        phon = ""
        for p in entry.get("phonetics") or []:
            if p.get("text"):
                phon = p["text"]
                break
        audio = ""
        for p in entry.get("phonetics") or []:
            if p.get("audio"):
                audio = p["audio"]
                break
        return {
            "ok": True,
            "word": entry.get("word"),
            "phonetic": phon or entry.get("phonetic"),
            "audio": audio,
            "meanings": meanings,
        }
    except Exception as e:
        return {"ok": False, "error": f"dictionary api failed: {e}"}


# =====================================================================
# 9) Horoscope (ohmanda, no key)
# =====================================================================
_SIGN_AR = {
    "الحمل": "aries", "aries": "aries",
    "الثور": "taurus", "taurus": "taurus",
    "الجوزاء": "gemini", "gemini": "gemini",
    "السرطان": "cancer", "cancer": "cancer",
    "الأسد": "leo", "leo": "leo",
    "العذراء": "virgo", "virgo": "virgo",
    "الميزان": "libra", "libra": "libra",
    "العقرب": "scorpio", "scorpio": "scorpio",
    "القوس": "sagittarius", "sagittarius": "sagittarius",
    "الجدي": "capricorn", "capricorn": "capricorn",
    "الدلو": "aquarius", "aquarius": "aquarius",
    "الحوت": "pisces", "pisces": "pisces",
}


def get_horoscope(sign: str) -> Dict[str, Any]:
    s = (sign or "").strip().lower()
    sign_en = _SIGN_AR.get(s) or _SIGN_AR.get((sign or "").strip()) or s
    if sign_en not in set(_SIGN_AR.values()):
        return {"ok": False, "error": f"unknown sign: {sign}"}
    try:
        r = requests.get(
            f"https://ohmanda.com/api/horoscope/{sign_en}",
            headers=HEADERS, timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json() or {}
        return {
            "ok": True,
            "sign": sign_en,
            "date": data.get("date"),
            "horoscope": data.get("horoscope"),
        }
    except Exception as e:
        return {"ok": False, "error": f"horoscope api failed: {e}"}


# =====================================================================
# 10) URL shortener (is.gd, no key)
# =====================================================================
def shorten_url(url: str) -> Dict[str, Any]:
    u = (url or "").strip()
    if not u:
        return {"ok": False, "error": "empty url"}
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    try:
        r = requests.get(
            "https://is.gd/create.php",
            params={"format": "simple", "url": u},
            headers=HEADERS, timeout=TIMEOUT,
        )
        r.raise_for_status()
        short = (r.text or "").strip()
        if not short.startswith("http"):
            return {"ok": False, "error": short or "shorten failed"}
        return {"ok": True, "url": u, "short": short}
    except Exception as e:
        return {"ok": False, "error": f"shorten failed: {e}"}


# =====================================================================
# 11) Sticker maker (PNG/JPG bytes -> WhatsApp .webp)
# =====================================================================
def make_sticker(
    image_bytes: Optional[bytes] = None,
    url: Optional[str] = None,
    text: Optional[str] = None,
) -> Dict[str, Any]:
    """Convert image (bytes or URL) into a 512x512 WebP sticker.

    If `text` is given (and no image), generate a text-on-transparent sticker.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as e:
        return {"ok": False, "error": f"Pillow not available: {e}"}

    img = None
    try:
        if image_bytes:
            img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        elif url:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True)
            r.raise_for_status()
            img = Image.open(io.BytesIO(r.content)).convert("RGBA")
        elif text:
            img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            font = None
            for path in (
                "/nix/store/*/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            ):
                import glob as _g
                hits = _g.glob(path)
                if hits:
                    try:
                        font = ImageFont.truetype(hits[0], 56)
                        break
                    except Exception:
                        pass
            if font is None:
                font = ImageFont.load_default()
            # Wrap text manually
            words = text.split()
            lines: List[str] = []
            cur = ""
            for w in words:
                trial = (cur + " " + w).strip()
                bbox = draw.textbbox((0, 0), trial, font=font)
                if bbox[2] - bbox[0] > 460 and cur:
                    lines.append(cur)
                    cur = w
                else:
                    cur = trial
            if cur:
                lines.append(cur)
            line_h = 70
            total_h = line_h * len(lines)
            y = max(20, (512 - total_h) // 2)
            for ln in lines:
                bbox = draw.textbbox((0, 0), ln, font=font)
                w = bbox[2] - bbox[0]
                x = (512 - w) // 2
                # Outline + fill for readability
                for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
                    draw.text((x + dx, y + dy), ln, font=font, fill=(0, 0, 0, 255))
                draw.text((x, y), ln, font=font, fill=(255, 255, 255, 255))
                y += line_h
        else:
            return {"ok": False, "error": "need image_bytes, url, or text"}

        # Resize keeping aspect ratio, pad transparent to 512x512
        img.thumbnail((512, 512), Image.LANCZOS)
        canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        ox = (512 - img.width) // 2
        oy = (512 - img.height) // 2
        canvas.paste(img, (ox, oy), img if img.mode == "RGBA" else None)

        buf = io.BytesIO()
        canvas.save(buf, format="WebP", quality=90, method=6)
        webp = buf.getvalue()
        return {
            "ok": True,
            "webp_bytes": webp,
            "size_bytes": len(webp),
            "filename": "sticker.webp",
            "mime": "image/webp",
        }
    except Exception as e:
        return {"ok": False, "error": f"sticker failed: {e}"}


# =====================================================================
# 12) YouTube transcript
# =====================================================================
def _yt_video_id(url_or_id: str) -> Optional[str]:
    s = (url_or_id or "").strip()
    if not s:
        return None
    # Already an ID?
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    try:
        u = urlparse(s if "://" in s else "https://" + s)
        if "youtu.be" in (u.netloc or ""):
            vid = (u.path or "/").strip("/").split("/")[0]
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
                return vid
        if "youtube.com" in (u.netloc or ""):
            qs = parse_qs(u.query or "")
            if "v" in qs and qs["v"]:
                vid = qs["v"][0]
                if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
                    return vid
            # /shorts/ID or /embed/ID
            m = re.search(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]{11})", u.path or "")
            if m:
                return m.group(1)
    except Exception:
        pass
    m = re.search(r"([A-Za-z0-9_-]{11})", s)
    return m.group(1) if m else None


def get_youtube_transcript(url: str, lang: Optional[str] = None) -> Dict[str, Any]:
    vid = _yt_video_id(url)
    if not vid:
        return {"ok": False, "error": "ما عرفتش رابط/ID اليوتيوب"}
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        prefs = []
        if lang:
            prefs.append(lang)
        prefs.extend(["ar", "en", "fr", "es", "de"])
        try:
            transcripts = YouTubeTranscriptApi.list_transcripts(vid)
            chosen = None
            try:
                chosen = transcripts.find_transcript(prefs)
            except Exception:
                # Try translatable -> translate to first preference
                for tr in transcripts:
                    if tr.is_translatable:
                        try:
                            chosen = tr.translate(prefs[0])
                            break
                        except Exception:
                            chosen = tr
                            break
                if not chosen:
                    chosen = next(iter(transcripts), None)
            if not chosen:
                return {"ok": False, "error": "ما كاينش transcript لهاد الفيديو"}
            data = chosen.fetch()
            lang_code = chosen.language_code
        except Exception:
            data = YouTubeTranscriptApi.get_transcript(vid, languages=prefs)
            lang_code = lang or "auto"

        # Normalize: items can be dicts or FetchedTranscriptSnippet objects
        def _txt(it):
            if isinstance(it, dict):
                return (it.get("text") or "").strip()
            return (getattr(it, "text", "") or "").strip()

        text = " ".join(_txt(it) for it in data if _txt(it))
        # Cap to keep response sane
        if len(text) > 12000:
            text = text[:12000] + " ...[تم الاقتطاع]"
        return {
            "ok": True,
            "video_id": vid,
            "lang": lang_code,
            "text": text,
            "char_count": len(text),
        }
    except Exception as e:
        return {"ok": False, "error": f"transcript failed: {e}"}
