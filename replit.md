# Omar — WhatsApp + Gemini Bot

A two-process Replit app:

- **Gemini Server** (`server.py`, gunicorn on port 5000) — Flask app that
  scrapes Google's Gemini web UI and exposes a tool API used by the bot.
- **WhatsApp Bot** (`index.js`, Node + Baileys) — connects to WhatsApp,
  forwards user messages to the Gemini bridge, parses `<tool>...</tool>`
  blocks in Omar's reply, and dispatches each one to a Python endpoint.

## Persona — Omar

- Defined in `gemini/gemini_scraper.py` (`DEFAULT_PERSONA`).
- Friendly Moroccan voice, Darija-first, mixes Arabic/French/English to
  match the user.
- Uses WhatsApp formatting only (`*bold*`, `_italic_`, `~strike~`,
  ```` ```code``` ````, `>quote`). Markdown like `**` / `__` / `##` /
  `[text](url)` is forbidden.
- Anti-hallucination: if Omar isn't sure, he says "ما عرفتش" or uses the
  `search` tool instead of inventing facts.
- **Strict makepdf rule** (fix for over-eager PDF generation):
  `makepdf` is only triggered when the user *explicitly* writes "PDF",
  "ملف", or "تقرير". Plain "ملخص" / "اشرح" / "حدثني" → text reply in
  the chat. "بدون PDF" / "ماشي PDF" must be obeyed.

## Tools (dispatched from `<tool>{...}</tool>` blocks)

| name        | endpoint        | output                                    |
|-------------|-----------------|-------------------------------------------|
| `download`  | `/download`     | audio (MP3) or video file                 |
| `image`     | `/image`        | generated image(s)                        |
| `apk`       | `/apk`          | APK file from OMARAI/aria2/apkeep         |
| `search`    | `/websearch`    | DuckDuckGo results → Omar summarizes      |
| `fetch`     | `/fetch`        | page text → Omar summarizes               |
| `pdf`       | `/pdfsearch`    | finds and sends an existing PDF           |
| `code`      | (inline)        | code/text file as document                |
| `makepdf`   | `/makepdf`      | generates a PDF from given content        |
| `weather`   | `/weather`      | Open-Meteo current + 3-day forecast       |
| `prayer`    | `/prayer`       | Aladhan prayer times (default MOROCCO)    |
| `currency`  | `/currency`     | exchangerate.host conversion              |
| `translate` | `/translate`    | deep-translator (auto-detect source)      |
| `wiki`      | `/wiki`         | Wikipedia REST summary                    |
| `qr`        | `/qr`           | PNG QR code                               |
| `tts`       | `/tts`          | gTTS voice note (MP3)                     |
| `time`      | `/time`         | current time in any IANA timezone         |
| `pinterest` | `/pinterest/*`  | search Pinterest images / download pin    |
| `cloudfile` | `/cloudfile`    | MediaFire / Google Drive / Mega.nz files  |

The 8 lightweight info tools are implemented in `extras.py` (free,
keyless APIs only) and wrapped by Flask routes in `server.py`. The
`apk_downloader.py` uses cloudscraper to scrape OMARAI with aria2 +
apkeep fallbacks. The `downloaders/` package replaces yt-dlp with
public scraper APIs — one module per platform (savetube + ytdown.to
for YouTube, tikwm for TikTok, fdownloader for Facebook, downloadgram
for Instagram, x2twitter for Twitter/X) plus a YouTube-search helper.
`downloader.py` is a thin compat facade that auto-detects the platform
from a URL (or falls back to a YouTube search for free-text queries).
`pinterest_downloader.py` uses Pinterest's BaseSearchResource JSON API
for search and direct CDN scraping for individual pins.
`cloud_downloader.py` resolves MediaFire (page scrape → direct link),
Google Drive (usercontent.google.com + confirm token), and Mega.nz
(mega.py anonymous login + decryption).

## Conventions

- Python 3.11, gunicorn `--reload`, listens on port 5000.
- Node 20+, Baileys `@whiskeysockets/baileys`.
- All Python deps managed via `uv` in `pyproject.toml`.
- All Node deps in `package.json`.
- `safeSend(...)` always quotes the original WhatsApp message.
- For attachments that support a caption (image/video/document/qr),
  Omar's short intro sentence is used as the caption so the user gets
  one combined message instead of "text" + "file".
- Audio messages (MP3 downloads, TTS) cannot carry a caption; Omar's
  intro is sent as the preceding text only.
- **Streaming downloads (no disk, no buffering).** `/download` returns
  `download_url` + `stream_headers` by default; the Node bot passes
  `{ video: { url, headers }, ... }` (or `audio:` / `document:`) to
  Baileys, which streams the bytes straight from the source CDN to
  WhatsApp. Files never land in Python or Node memory and are never
  written to disk. Cap is **1.5 GB** (server.py + downloader.py +
  omar_tools.py). Pass `stream:false` to force base64 bytes (only
  needed for providers without a public CDN URL).
- Image generation asks Gemini for *one* image per call (previously two
  variations, which caused the bot to send the picture twice).
- **Cookie pool (up to 10 slots).** `gemini/cookie_pool.py` round-robins
  between healthy cookies; `with_cookie_pool(...)` in `server.py` wraps
  every Gemini call, marks a slot sick for 1 h on auth failure, and
  retries the next slot. Slots live under `COOKIES_DIR`
  (default `gemini/cookies/`, set to `/data/cookies` on Hugging Face).
  Legacy `gemini/cookies.txt` is auto-imported into slot 1 on first run.
  Managed at runtime via `/admin/cookies` (loopback or `X-Admin-Token`)
  and from WhatsApp via `/cookie list|add [N]|del N|test [N]` —
  developer-only (`DEVELOPER_NUMBER` env var, default `212688898322`,
  matched against `msg.key.participant ?? msg.key.remoteJid`).
- **Hugging Face Spaces deployment.** `Dockerfile` (Node 20 + Python 3.11
  + ffmpeg, UID 1000), `start.sh` (gunicorn on `127.0.0.1:5000` + Node
  bot + keep-alive HTTP on `$PORT=7860`), `.dockerignore`, and
  `HUGGINGFACE.md` walkthrough. Persistent state lives at `/data/auth`
  and `/data/cookies` so a paid HF persistent-storage volume keeps the
  WhatsApp session and cookies across restarts.

## Workflows

- `Gemini Server`: `gunicorn --bind 0.0.0.0:5000 --workers 1 --threads 4 --timeout 180 --reuse-port --reload main:app`
- `WhatsApp Bot`: `node index.js`
