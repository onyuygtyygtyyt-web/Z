"""YouTube search via scraping the public results page (no API key).

Used by the multi-platform `download()` facade when the caller passes a
free-text query instead of a URL.  We reach for the first video on
youtube.com/results and return its watch URL + basic metadata.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

import requests

from .common import DEFAULT_HEADERS, TIMEOUT


_INITIAL_DATA_RE = re.compile(
    r"var ytInitialData\s*=\s*(\{.*?\});\s*</script>", re.DOTALL
)


def _parse_results(html: str) -> List[Dict[str, Any]]:
    m = _INITIAL_DATA_RE.search(html)
    if not m:
        # Fallback: find any /watch?v=ID
        ids = re.findall(r"\"videoId\":\"([a-zA-Z0-9_-]{11})\"", html)
        return [
            {"id": vid, "url": f"https://www.youtube.com/watch?v={vid}"}
            for vid in dict.fromkeys(ids)
        ]

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []

    out: List[Dict[str, Any]] = []
    try:
        contents = (
            data["contents"]
            ["twoColumnSearchResultsRenderer"]
            ["primaryContents"]
            ["sectionListRenderer"]
            ["contents"]
        )
    except (KeyError, TypeError):
        return out

    for section in contents:
        items = (
            section.get("itemSectionRenderer", {}).get("contents")
            or []
        )
        for item in items:
            v = item.get("videoRenderer")
            if not v:
                continue
            vid = v.get("videoId")
            if not vid:
                continue
            title = "".join(
                run.get("text", "") for run in v.get("title", {}).get("runs", [])
            )
            length = (
                v.get("lengthText", {}).get("simpleText")
                or v.get("lengthText", {}).get("accessibility", {})
                .get("accessibilityData", {}).get("label")
            )
            channel = (
                v.get("ownerText", {}).get("runs", [{}])[0].get("text")
                or v.get("longBylineText", {}).get("runs", [{}])[0].get("text")
            )
            views = v.get("viewCountText", {}).get("simpleText")
            thumbs = v.get("thumbnail", {}).get("thumbnails") or []
            thumbnail = thumbs[-1]["url"] if thumbs else None

            out.append({
                "id": vid,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "title": title,
                "duration": length,
                "channel": channel,
                "views": views,
                "thumbnail": thumbnail,
            })
    return out


def search(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Return up to *limit* YouTube videos matching *query*."""
    if not query:
        return []
    r = requests.get(
        "https://www.youtube.com/results",
        params={"search_query": query, "hl": "en"},
        headers={**DEFAULT_HEADERS, "Accept-Language": "en"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return _parse_results(r.text)[:limit]


def first_url(query: str) -> Optional[str]:
    """Return the watch URL of the top YouTube hit for *query*, or None."""
    hits = search(query, limit=1)
    return hits[0]["url"] if hits else None
