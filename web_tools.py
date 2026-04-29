"""Web search, page fetch, and PDF discovery utilities.

Pure open-source: uses `ddgs` for search and `readability-lxml` to extract
the main article text from HTML pages. No paid APIs, no headless browser.

Functions
---------
web_search(query, max_results=5) -> list[dict]
    Returns [{"title", "url", "snippet"}, ...] from DuckDuckGo.

fetch_url(url, max_chars=8000) -> dict
    Downloads a page and extracts the main readable text plus title.

find_pdf(query, max_results=8) -> dict
    Searches the web for the query, picks the first result that is a PDF
    (by URL extension or Content-Type), downloads it, and also returns
    a list of other relevant non-PDF sources.
"""
from __future__ import annotations

import io
import re
import urllib.parse as urlparse
from typing import Dict, List, Optional

import requests
from ddgs import DDGS
from readability import Document

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en,ar;q=0.8"}
TIMEOUT = 25
MAX_PDF_BYTES = 100 * 1024 * 1024  # 100 MB safety cap


# ---------------------------------------------------------------- search


def web_search(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """Return top web results as a list of {title, url, snippet}."""
    if not query or not query.strip():
        return []
    out: List[Dict[str, str]] = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results) or []:
            url = r.get("href") or r.get("url") or ""
            if not url:
                continue
            out.append({
                "title": (r.get("title") or "").strip(),
                "url": url,
                "snippet": (r.get("body") or "").strip(),
            })
    return out


# ---------------------------------------------------------------- fetch


def _strip_html(html: str) -> str:
    """Drop tags and collapse whitespace."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#39;", "'", text)
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_url(url: str, max_chars: int = 8000) -> Dict[str, Optional[str]]:
    """Fetch `url` and return {title, text, url, content_type}.

    For HTML pages the readable main body is extracted via readability-lxml.
    For non-HTML responses (e.g. PDF) only the metadata is returned and
    the caller is expected to handle bytes via a different path.
    """
    if not url:
        raise ValueError("url is required")
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} fetching {url}")

    if "html" not in ctype and "xml" not in ctype:
        return {
            "url": r.url,
            "title": None,
            "text": None,
            "content_type": ctype,
        }

    try:
        doc = Document(r.text)
        title = (doc.short_title() or "").strip() or None
        body_html = doc.summary(html_partial=True)
        body = _strip_html(body_html)
    except Exception:
        title = None
        body = _strip_html(r.text)

    if max_chars and len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n[...]"

    return {
        "url": r.url,
        "title": title,
        "text": body,
        "content_type": ctype,
    }


# ---------------------------------------------------------------- pdf


def _looks_like_pdf_url(url: str) -> bool:
    path = urlparse.urlparse(url).path.lower()
    return path.endswith(".pdf")


# Hints in anchor text that usually mean "click here to download the PDF".
_PDF_LINK_HINTS_AR = ("تحميل", "تنزيل", "حمل", "اقرأ", "قراءة", "PDF", "pdf")


def _extract_pdf_links_from_page(url: str) -> List[str]:
    """Visit `url` and return candidate PDF links found on the page.

    Looks for anchors whose href ends in .pdf, OR whose visible text
    contains common Arabic/English download hints. Resolves relative
    URLs against the page's base URL. Returns links de-duplicated and
    ordered by likelihood (direct .pdf hrefs first).
    """
    try:
        r = requests.get(
            url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True
        )
    except Exception:
        return []
    if r.status_code >= 400:
        return []
    ctype = (r.headers.get("Content-Type") or "").lower()
    if "html" not in ctype and "xml" not in ctype:
        return []

    base = r.url
    html = r.text
    direct: List[str] = []
    hinted: List[str] = []
    seen: set = set()

    # Match every <a ...href="..."...>...</a> tag (greedy enough to capture
    # the visible label between the tags).
    for m in re.finditer(
        r'<a\b[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        html,
        flags=re.I | re.S,
    ):
        href, label = m.group(1), m.group(2)
        if href.startswith(("javascript:", "mailto:", "#")):
            continue
        full = urlparse.urljoin(base, href)
        if full in seen:
            continue
        seen.add(full)
        label_text = re.sub(r"<[^>]+>", " ", label)
        label_text = re.sub(r"\s+", " ", label_text).strip().lower()

        if _looks_like_pdf_url(full):
            direct.append(full)
        elif any(h.lower() in label_text for h in _PDF_LINK_HINTS_AR) or \
             any(h.lower() in full.lower() for h in ("download", "pdf")):
            hinted.append(full)

    # Cap candidates so we don't fan out too far.
    return direct[:5] + hinted[:5]


def _pdf_filename_from_url(url: str, fallback: str = "document.pdf") -> str:
    path = urlparse.urlparse(url).path
    name = urlparse.unquote(path.rsplit("/", 1)[-1] or "")
    if not name or "." not in name:
        return fallback
    if not name.lower().endswith(".pdf"):
        name = name.rsplit(".", 1)[0] + ".pdf"
    return name


def _try_download_pdf(url: str) -> Optional[Dict]:
    """Try to download a single URL as a PDF. Returns None if it isn't one."""
    try:
        r = requests.get(
            url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True, stream=True
        )
    except Exception:
        return None
    if r.status_code >= 400:
        return None
    ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    is_pdf = ctype == "application/pdf" or _looks_like_pdf_url(r.url)
    if not is_pdf:
        # Sniff first bytes to be safe (some servers return octet-stream).
        first = r.raw.read(5) if r.raw else b""
        if first != b"%PDF-":
            return None
        buf = io.BytesIO(first)
    else:
        buf = io.BytesIO()

    total = buf.getbuffer().nbytes
    for chunk in r.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        buf.write(chunk)
        total += len(chunk)
        if total > MAX_PDF_BYTES:
            return None
    data = buf.getvalue()
    if not data.startswith(b"%PDF-"):
        return None
    return {
        "url": r.url,
        "filename": _pdf_filename_from_url(r.url),
        "data": data,
        "size_bytes": len(data),
        "content_type": "application/pdf",
    }


def find_pdf(query: str, max_results: int = 8) -> Dict:
    """Search the web for `query`, return the first PDF + other sources.

    Returns
    -------
    {
      "query": str,
      "pdf": {url, filename, data (bytes), size_bytes} | None,
      "pdf_title": str | None,    # best-guess title from the search hit
      "sources": [{title, url, snippet}, ...]   # up to 5 non-PDF results
    }
    """
    results = web_search(query, max_results=max_results)
    pdf_payload: Optional[Dict] = None
    pdf_title: Optional[str] = None
    other_sources: List[Dict[str, str]] = []

    # Pass 1: explicit .pdf URLs.
    for r in results:
        url = r["url"]
        if _looks_like_pdf_url(url) and pdf_payload is None:
            got = _try_download_pdf(url)
            if got:
                pdf_payload = got
                pdf_title = r.get("title") or None
                continue
        other_sources.append(r)

    # Pass 2: if no .pdf URL hit, try a focused "filetype:pdf" search.
    if pdf_payload is None:
        extra = web_search(f"{query} filetype:pdf", max_results=max_results)
        for r in extra:
            url = r["url"]
            if _looks_like_pdf_url(url):
                got = _try_download_pdf(url)
                if got:
                    pdf_payload = got
                    pdf_title = r.get("title") or None
                    break

    # Pass 3: many Arabic / book sites (foulabook, ekitabs, archive.org book
    # pages, university course pages...) link to a real .pdf from inside an
    # HTML "book" page — they don't expose the .pdf in the search snippet.
    # Visit the top non-PDF results and scrape candidate PDF links.
    if pdf_payload is None:
        candidates = (other_sources or [])[:6]
        for src in candidates:
            page_url = src["url"]
            links = _extract_pdf_links_from_page(page_url)
            for link in links:
                got = _try_download_pdf(link)
                if got:
                    pdf_payload = got
                    pdf_title = src.get("title") or None
                    break
            if pdf_payload is not None:
                break

    return {
        "query": query,
        "pdf": pdf_payload,
        "pdf_title": pdf_title,
        "sources": other_sources[:5],
    }
