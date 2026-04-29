"""APK downloader using the OMARAI strategy (cloudscraper + aria2 + apkeep).

This file copies the APKPure-related scraping/download code from
https://github.com/omarxarafp/OMARAI verbatim, then adds a thin synchronous
adapter (`get_info` / `download`) that matches the response shape expected by
`server.py`'s /apk and /apk_info endpoints.

3-tier strategy:
    1. APKPure CDN direct download via aria2 (XAPK -> APK fallback)
    2. APKPure mobile-site search via cloudscraper to resolve free-text queries
    3. apkeep binary as final fallback when APKPure CDN refuses
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Begin verbatim copy of OMARAI/api_server.py helpers (APKPure block)
# ---------------------------------------------------------------------------
import asyncio
import time
import os
import subprocess
from typing import Optional, Dict, Any, List
import sys
import httpx
import cloudscraper
import re
from bs4 import BeautifulSoup
import requests
from urllib.parse import quote_plus

DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app_cache')
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

APKEEP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'apkeep')

scraper = cloudscraper.create_scraper(
 browser={
 'browser': 'chrome',
 'platform': 'android',
 'mobile': True
 }
)

httpx_client: Optional[httpx.AsyncClient] = None

async def get_httpx_client() -> httpx.AsyncClient:
    global httpx_client
    if httpx_client is None:
        httpx_client = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers={
                'User-Agent': 'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
                'Sec-CH-UA-Mobile': '?1',
            }
        )
    return httpx_client


async def fetch_with_protection(url: str, use_cloudscraper: bool = True) -> Optional[str]:
    """Fetch URL with anti-bot protection bypass using mobile headers"""
    mobile_headers = {
        'User-Agent': 'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Sec-CH-UA': '"Chromium";v="120", "Google Chrome";v="120", "Not_A Brand";v="99"',
        'Sec-CH-UA-Mobile': '?1',
        'Sec-CH-UA-Platform': '"Android"',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-User': '?1',
        'Sec-Fetch-Dest': 'document',
        'Upgrade-Insecure-Requests': '1',
    }

    if use_cloudscraper:
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: scraper.get(url, headers=mobile_headers, timeout=20)
            )
            if response.status_code == 200:
                print(f"[CloudScraper] Success", file=sys.stderr)
                return response.text
        except Exception as e:
            print(f"[CloudScraper] Failed: {e}", file=sys.stderr)

    try:
        client = await get_httpx_client()
        response = await client.get(url, headers=mobile_headers)
        if response.status_code == 200:
            print(f"[httpx] Success", file=sys.stderr)
            return response.text
    except Exception as e:
        print(f"[httpx] Failed: {e}", file=sys.stderr)

    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: requests.get(url, headers=mobile_headers, timeout=15)
        )
        if response.status_code == 200:
            print(f"[requests] Success", file=sys.stderr)
            return response.text
    except Exception as e:
        print(f"[requests] Failed: {e}", file=sys.stderr)

    return None


async def download_with_aria2(url: str, output_path: str, filename: str) -> Optional[str]:
    """Download file using aria2c with multiple connections for speed"""
    try:
        print(f"[aria2] Downloading with 16 connections...", file=sys.stderr)
        start_time = time.time()

        result = subprocess.run(
            [
                'aria2c',
                '-x', '16',
                '-s', '16',
                '-k', '1M',
                '--max-connection-per-server=16',
                '--min-split-size=1M',
                '--file-allocation=none',
                '--continue=true',
                '-d', output_path,
                '-o', filename,
                '--timeout=120',
                '--connect-timeout=30',
                url
            ],
            capture_output=True,
            text=True,
            timeout=300
        )

        elapsed = time.time() - start_time
        file_path = os.path.join(output_path, filename)

        if os.path.exists(file_path) and os.path.getsize(file_path) > 100000:
            size_mb = os.path.getsize(file_path) / (1024 * 1024)
            print(f"[aria2] Downloaded: {size_mb:.1f} MB in {elapsed:.1f}s", file=sys.stderr)
            return file_path

        print(f"[aria2] Failed: {result.stderr}", file=sys.stderr)
        return None

    except subprocess.TimeoutExpired:
        print(f"[aria2] Timeout", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[aria2] Error: {e}", file=sys.stderr)
        return None


import zipfile
import io


def detect_real_file_type(file_path: str) -> str:
    """Detect actual file type by inspecting ZIP contents"""
    try:
        with zipfile.ZipFile(file_path, 'r') as zf:
            names = zf.namelist()
            names_lower = [n.lower() for n in names]

            if 'manifest.json' in names_lower:
                print(f"[Type Detect] Found manifest.json - this is XAPK", file=sys.stderr)
                return 'xapk'

            has_apk = any(n.endswith('.apk') for n in names_lower)
            has_obb = any('.obb' in n for n in names_lower)

            if has_apk or has_obb:
                print(f"[Type Detect] Found APK/OBB inside - this is XAPK", file=sys.stderr)
                return 'xapk'

            if 'androidmanifest.xml' in names_lower:
                print(f"[Type Detect] Found AndroidManifest.xml at root - this is APK", file=sys.stderr)
                return 'apk'

            if 'classes.dex' in names_lower or 'resources.arsc' in names_lower:
                print(f"[Type Detect] Found APK structure - this is APK", file=sys.stderr)
                return 'apk'

            print(f"[Type Detect] Unknown structure, files: {names[:5]}", file=sys.stderr)
            return 'apk'

    except zipfile.BadZipFile:
        print(f"[Type Detect] Not a valid ZIP file", file=sys.stderr)
        return 'apk'
    except Exception as e:
        print(f"[Type Detect] Error: {e}", file=sys.stderr)
        return 'apk'


async def download_from_apkpure(package_name: str, output_dir: str) -> Optional[str]:
    """Download from APKPure and detect real file type from content"""
    try:
        temp_filename = f"{package_name}.tmp"
        download_url = f"https://d.apkpure.com/b/XAPK/{package_name}?version=latest"

        print(f"[APKPure] Downloading {package_name}...", file=sys.stderr)
        result = await download_with_aria2(download_url, output_dir, temp_filename)

        if not result or not os.path.exists(result) or os.path.getsize(result) < 100000:
            download_url = f"https://d.apkpure.com/b/APK/{package_name}?version=latest"
            print(f"[APKPure] XAPK failed, trying APK endpoint...", file=sys.stderr)
            result = await download_with_aria2(download_url, output_dir, temp_filename)

        if not result or not os.path.exists(result) or os.path.getsize(result) < 100000:
            print(f"[APKPure] Download failed for {package_name}", file=sys.stderr)
            return None

        real_type = detect_real_file_type(result)
        final_filename = f"{package_name}.{real_type}"
        final_path = os.path.join(output_dir, final_filename)

        if result != final_path:
            os.rename(result, final_path)
            print(f"[APKPure] Renamed to: {final_filename}", file=sys.stderr)

        return final_path

    except Exception as e:
        print(f"[APKPure] Error: {e}", file=sys.stderr)
        return None


async def search_apkpure(query: str, num_results: int = 10) -> List[Dict[str, Any]]:
    """Search APKPure for apps matching the query using mobile site"""
    try:
        search_url = f"https://m.apkpure.com/search?q={quote_plus(query)}"

        print(f"[APKPure Search] Searching (mobile): {query}", file=sys.stderr)

        html_content = await fetch_with_protection(search_url)

        if not html_content:
            print(f"[APKPure Search] Failed to fetch search page", file=sys.stderr)
            return []

        soup = BeautifulSoup(html_content, 'lxml')
        apps = []
        seen_ids = set()

        def extract_app_id(href):
            if href.startswith('https://apkpure.com/'):
                href = href.replace('https://apkpure.com', '')
            elif href.startswith('https://m.apkpure.com/'):
                href = href.replace('https://m.apkpure.com', '')
            if not href.startswith('/'):
                return None
            parts = [p for p in href.strip('/').split('/') if p]
            if len(parts) >= 2:
                potential_id = parts[-1]
                if 'download' in potential_id.lower():
                    return None
                if '.' in potential_id and potential_id.count('.') >= 1:
                    return potential_id
            return None

        first_app = soup.find('div', class_='first')
        if first_app:
            link = first_app.find('a', href=True)
            if link:
                href = link.get('href', '')
                app_id = extract_app_id(href)
                if app_id and app_id not in seen_ids:
                    seen_ids.add(app_id)
                    p1 = first_app.find('p', class_='p1')
                    p2 = first_app.find('p', class_='p2')
                    app_name = p1.get_text(strip=True) if p1 else None
                    developer = p2.get_text(strip=True) if p2 else ''

                    if not app_name:
                        parts = [p for p in href.strip('/').split('/') if p]
                        app_slug = parts[0] if parts else ''
                        app_name = app_slug.replace('-', ' ').title()

                    img = first_app.find('img')
                    icon = img.get('src') or img.get('data-src') if img else None

                    apps.append({
                        'title': app_name,
                        'appId': app_id,
                        'developer': developer,
                        'score': 0.0,
                        'icon': icon
                    })
                    print(f"[APKPure Search] Found (featured): {app_name} ({app_id})", file=sys.stderr)

        search_container = soup.find('ul', class_='search-res')

        if search_container:
            for li in search_container.find_all('li'):
                if len(apps) >= num_results:
                    break

                link = li.find('a', href=True)
                if not link:
                    continue

                href = link.get('href', '')
                app_id = extract_app_id(href)

                if not app_id or app_id in seen_ids:
                    continue
                seen_ids.add(app_id)

                p1 = li.find('p', class_='p1')
                p2 = li.find('p', class_='p2')

                app_name = p1.get_text(strip=True) if p1 else None
                developer = p2.get_text(strip=True) if p2 else ''

                if not app_name:
                    parts = [p for p in href.strip('/').split('/') if p]
                    app_slug = parts[0] if parts else ''
                    app_name = app_slug.replace('-', ' ').title()

                img = li.find('img')
                icon = None
                if img:
                    icon = img.get('src') or img.get('data-src') or img.get('data-original')

                score = 0.0
                score_elem = li.find(class_='star')
                if score_elem:
                    try:
                        score = float(score_elem.get_text(strip=True))
                    except:
                        pass

                apps.append({
                    'title': app_name,
                    'appId': app_id,
                    'developer': developer,
                    'score': score,
                    'icon': icon
                })
                print(f"[APKPure Search] Found: {app_name} ({app_id})", file=sys.stderr)

        print(f"[APKPure Search] Total found: {len(apps)} apps", file=sys.stderr)
        return apps[:num_results]

    except Exception as e:
        print(f"[APKPure Search] Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return []


def download_with_apkeep(package_name: str, output_dir: str) -> Optional[str]:
    try:
        print(f"[apkeep] Downloading {package_name}...", file=sys.stderr)
        start_time = time.time()

        result = subprocess.run(
            [APKEEP_PATH, "-a", package_name, output_dir],
            capture_output=True,
            text=True,
            timeout=300
        )

        elapsed = time.time() - start_time

        if "downloaded successfully" in result.stdout.lower():
            for ext in ['.xapk', '.apk', '.apks']:
                file_path = os.path.join(output_dir, f"{package_name}{ext}")
                if os.path.exists(file_path):
                    size_mb = os.path.getsize(file_path) / (1024 * 1024)
                    print(f"[apkeep] Downloaded {package_name}: {size_mb:.1f} MB in {elapsed:.1f}s", file=sys.stderr)
                    return file_path

        if "could not get download url" in result.stdout.lower() or "skipping" in result.stdout.lower():
            print(f"[apkeep] App not found: {package_name}", file=sys.stderr)
            return None

        print(f"[apkeep] Failed: {result.stdout} {result.stderr}", file=sys.stderr)
        return None

    except subprocess.TimeoutExpired:
        print(f"[apkeep] Timeout for {package_name}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[apkeep] Error: {e}", file=sys.stderr)
        return None

# ---------------------------------------------------------------------------
# End verbatim copy. Below is a thin sync adapter so server.py /apk routes
# keep their existing dict shape ({package, title, version_name, ext,
# filename, size_bytes, size_mb, mime, data, download_url}).
# ---------------------------------------------------------------------------

_PKG_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$")


def _looks_like_package(s: str) -> bool:
    return bool(_PKG_RE.match(s.strip()))


def _run(coro):
    """Run an async coroutine from a sync Flask request handler."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            raise RuntimeError("running")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def _resolve(query: str) -> Dict[str, Any]:
    """Turn a free-text query or package id into {appId, title, developer}."""
    q = query.strip()
    if _looks_like_package(q):
        return {"appId": q, "title": q, "developer": ""}
    hits = _run(search_apkpure(q, num_results=5))
    if not hits:
        raise RuntimeError(f"No APKPure result for: {query}")
    return hits[0]


def _mime_for(ext: str) -> str:
    ext = (ext or "apk").lower()
    if ext == "xapk":
        return "application/octet-stream"
    if ext == "apks":
        return "application/octet-stream"
    return "application/vnd.android.package-archive"


def get_info(package_or_query: str) -> Dict[str, Any]:
    """Resolve a query and return APK metadata without downloading the file."""
    meta = _resolve(package_or_query)
    pkg = meta["appId"]
    return {
        "package": pkg,
        "title": meta.get("title") or pkg,
        "developer": meta.get("developer") or "",
        "version_name": None,
        "ext": "apk",
        "filename": f"{pkg}.apk",
        "size_bytes": 0,
        "size_mb": 0,
        "mime": _mime_for("apk"),
        "download_url": f"https://d.apkpure.com/b/XAPK/{pkg}?version=latest",
    }


def download(package_or_query: str, max_size_mb: int = 500) -> Dict[str, Any]:
    """Download the latest APK/XAPK and return its bytes plus metadata."""
    meta = _resolve(package_or_query)
    pkg = meta["appId"]
    title = meta.get("title") or pkg

    file_path: Optional[str] = _run(download_from_apkpure(pkg, DOWNLOADS_DIR))

    if not file_path:
        print(f"[apk_downloader] APKPure failed, trying apkeep fallback...", file=sys.stderr)
        file_path = download_with_apkeep(pkg, DOWNLOADS_DIR)

    if not file_path or not os.path.exists(file_path):
        raise RuntimeError(f"All APK sources failed for {title}")

    size_bytes = os.path.getsize(file_path)
    size_mb = size_bytes / (1024 * 1024)

    if size_mb > max_size_mb:
        try:
            os.remove(file_path)
        except OSError:
            pass
        raise RuntimeError(
            f"{title} is {size_mb:.1f}MB which exceeds the {max_size_mb}MB limit."
        )

    ext = os.path.splitext(file_path)[1].lstrip(".") or "apk"
    safe_title = re.sub(r"[\\/]", "_", title).strip()[:80] or pkg
    filename = f"{safe_title}_{pkg}.{ext}"

    with open(file_path, "rb") as f:
        data = f.read()

    try:
        os.remove(file_path)
    except OSError:
        pass

    return {
        "package": pkg,
        "title": title,
        "developer": meta.get("developer") or "",
        "version_name": None,
        "ext": ext,
        "filename": filename,
        "size_bytes": size_bytes,
        "size_mb": round(size_mb, 2),
        "mime": _mime_for(ext),
        "download_url": f"https://d.apkpure.com/b/{ext.upper()}/{pkg}?version=latest",
        "data": data,
    }
