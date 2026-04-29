const {
    default: makeWASocket,
    useMultiFileAuthState,
    DisconnectReason,
    fetchLatestBaileysVersion,
    makeCacheableSignalKeyStore,
    Browsers,
    downloadMediaMessage,
} = require("@whiskeysockets/baileys");
const pino = require("pino");
const readline = require("readline");
const fs = require("fs");
const path = require("path");

const logger = pino({ level: "silent" });

const AUTH_DIR = path.join(__dirname, "auth_info");
if (!fs.existsSync(AUTH_DIR)) {
    fs.mkdirSync(AUTH_DIR, { recursive: true });
}

// Load defaults from config.js; env vars (if set) take precedence.
let fileConfig = {};
try { fileConfig = require("./config.js"); }
catch (e) { console.warn("[config] config.js not found, using env vars only"); }

const GEMINI_SERVER = process.env.GEMINI_SERVER || fileConfig.GEMINI_SERVER || "http://127.0.0.1:5000";

// Developer phone number — only this user can run /cookie commands.
const DEVELOPER_NUMBER = (process.env.DEVELOPER_NUMBER || fileConfig.DEVELOPER_NUMBER || "212688898322").replace(/\D/g, "");
// WhatsApp's new "Linked Identity" format hides the real phone number
// behind a numeric LID (e.g. "187136791855332@lid"). Comma-separated digits.
const DEVELOPER_LIDS = (process.env.DEVELOPER_LID || fileConfig.DEVELOPER_LID || "")
    .split(",")
    .map((s) => s.replace(/\D/g, ""))
    .filter(Boolean);

// Optional shared secret for /admin/* endpoints. When set both the bot
// and the server must agree on the same value (server reads ADMIN_TOKEN).
const ADMIN_TOKEN = process.env.ADMIN_TOKEN || fileConfig.ADMIN_TOKEN || "";

function senderIds(msg) {
    if (!msg || !msg.key) return { phone: "", lid: "", senderPn: "" };
    if (msg.key.fromMe) return { phone: "", lid: "", senderPn: "" };
    const jid = msg.key.participant || msg.key.remoteJid || "";
    const ident = (jid.split("@")[0] || "").split(":")[0].replace(/\D/g, "");
    const isLid = jid.endsWith("@lid");
    // Baileys exposes the real phone for LID senders via msg.key.senderPn
    const senderPn = (msg.key.senderPn || "").split("@")[0].split(":")[0].replace(/\D/g, "");
    return {
        phone: isLid ? senderPn : ident,
        lid: isLid ? ident : "",
        senderPn,
    };
}

function senderPhone(msg) {
    const ids = senderIds(msg);
    return ids.phone || ids.senderPn || "";
}

function isDeveloper(msg) {
    const ids = senderIds(msg);
    if (ids.phone && ids.phone === DEVELOPER_NUMBER) return true;
    if (ids.senderPn && ids.senderPn === DEVELOPER_NUMBER) return true;
    if (ids.lid && DEVELOPER_LIDS.includes(ids.lid)) return true;
    return false;
}

// Appended to every outgoing caption / text message
const BOT_FOOTER = "\n\n📸 تابعني على انستجرام 👇\nhttps://instagram.com/omqrxarafb";

const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
});

const question = (text) => new Promise((resolve) => rl.question(text, resolve));

// ---------------------------------------------------------------------------
// Helpers — calling the Flask Gemini bridge
// ---------------------------------------------------------------------------
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Module-level reference to the active WhatsApp socket. Helpers like
// performDownload / performImageGen / executeToolCall need to send messages
// from outside the startBot() closure, so we expose the socket here.
let sockRef = null;

async function safeSend(jid, content, opts) {
    if (!sockRef) {
        console.error("safeSend called before socket was ready");
        return;
    }
    try {
        // Append Instagram footer to text or caption fields
        const withFooter = { ...content };
        if (typeof withFooter.text === "string") {
            withFooter.text = withFooter.text + BOT_FOOTER;
        } else if (typeof withFooter.caption === "string") {
            withFooter.caption = withFooter.caption + BOT_FOOTER;
        }
        await sockRef.sendMessage(jid, withFooter, opts);
    } catch (err) {
        console.error("sendMessage failed:", err?.message || err);
    }
}

// Call an /admin/* endpoint on the Gemini bridge. Adds the admin token
// header automatically when one is configured.
async function callAdmin(endpoint, { method = "GET", fields = null, file = null } = {}) {
    const url = `${GEMINI_SERVER}${endpoint}`;
    const headers = {};
    if (ADMIN_TOKEN) headers["X-Admin-Token"] = ADMIN_TOKEN;

    const init = { method, headers };
    if (fields || file) {
        const form = new FormData();
        if (fields) {
            for (const [k, v] of Object.entries(fields)) {
                if (v !== undefined && v !== null) form.append(k, String(v));
            }
        }
        if (file) {
            const blob = new Blob([file.bytes], {
                type: file.mime || "text/plain",
            });
            form.append("file", blob, file.name || "cookies.txt");
        }
        init.body = form;
    }

    const res = await fetch(url, init);
    const text = await res.text();
    let data;
    try { data = JSON.parse(text); } catch { data = { error: text.slice(0, 200) }; }
    if (!res.ok || data.error) {
        throw new Error(data.error || `HTTP ${res.status}`);
    }
    return data;
}

async function callGemini(endpoint, fields, file, { retries = 4, retryDelayMs = 1500 } = {}) {
    let lastErr;

    for (let attempt = 0; attempt <= retries; attempt++) {
        // Rebuild form each attempt because Blob streams can't be re-sent.
        const form = new FormData();
        for (const [k, v] of Object.entries(fields)) {
            if (v !== undefined && v !== null) form.append(k, String(v));
        }
        if (file) {
            const blob = new Blob([file.bytes], {
                type: file.mime || "application/octet-stream",
            });
            form.append("file", blob, file.name || "file.bin");
        }

        let res;
        try {
            res = await fetch(`${GEMINI_SERVER}${endpoint}`, {
                method: "POST",
                body: form,
            });
        } catch (err) {
            lastErr = new Error(
                `Cannot reach Gemini server at ${GEMINI_SERVER}: ${err.message}`
            );
            // Network-level failure (server restarting, not yet up) → retry.
            if (attempt < retries) {
                await sleep(retryDelayMs * (attempt + 1));
                continue;
            }
            throw lastErr;
        }

        const text = await res.text();
        let data;
        try {
            data = JSON.parse(text);
        } catch {
            lastErr = new Error(
                `Bad JSON from Gemini server (HTTP ${res.status}): ${text.slice(0, 200)}`
            );
            if (res.status >= 500 && attempt < retries) {
                await sleep(retryDelayMs * (attempt + 1));
                continue;
            }
            throw lastErr;
        }

        if (!res.ok || data.error) {
            lastErr = new Error(data.error || `HTTP ${res.status}`);
            // Retry on 5xx server errors only.
            if (res.status >= 500 && attempt < retries) {
                await sleep(retryDelayMs * (attempt + 1));
                continue;
            }
            throw lastErr;
        }

        return data;
    }

    throw lastErr || new Error("Unknown error");
}

// Match http/https URLs in a chunk of text. yt-dlp supports 1000+ sites,
// so we just trust any URL and let the server decide if it's downloadable.
const URL_RE = /\bhttps?:\/\/[^\s<>"]+/i;

function extractUrl(text) {
    if (!text) return null;
    const m = text.match(URL_RE);
    return m ? m[0] : null;
}

function fmtDuration(sec) {
    if (!sec || sec <= 0) return "";
    const m = Math.floor(sec / 60);
    const s = Math.floor(sec % 60);
    return `${m}:${String(s).padStart(2, "0")}`;
}

function fmtSize(bytes) {
    if (!bytes) return "";
    const mb = bytes / (1024 * 1024);
    return mb >= 1 ? `${mb.toFixed(1)}MB` : `${(bytes / 1024).toFixed(0)}KB`;
}

async function performDownload(sock, msg, from, query, mode, quality, userCaption = null) {
    try {
        // Show a typing/recording indicator instead of sending a status text
        // — the user already sees Omar's short intro line right above.
        await sock.sendPresenceUpdate(
            mode === "audio" ? "recording" : "composing",
            from
        ).catch(() => {});

        const data = await callGemini(
            "/download",
            { query, mode, quality, stream: true },
            null,
            { retries: 1, retryDelayMs: 1000 }
        );

        // Build the media payload. We strongly prefer streaming the source
        // URL straight to Baileys ({ url: ... }) so the file never lands in
        // Python or Node memory — that's what makes 1.5 GB downloads safe.
        // We only fall back to base64 bytes if the server returned them
        // (e.g. for the legacy stream:false code path or future providers
        // with no public CDN URL).
        const sourceUrl = data.download_url || null;
        const sourceHeaders =
            data.stream_headers && Object.keys(data.stream_headers).length
                ? data.stream_headers
                : undefined;
        const fallbackBuf =
            !sourceUrl && data.data_b64
                ? Buffer.from(data.data_b64, "base64")
                : null;
        if (!sourceUrl && !fallbackBuf) {
            throw new Error("server returned neither download_url nor data_b64");
        }
        const mediaSource = sourceUrl
            ? { url: sourceUrl, headers: sourceHeaders }
            : fallbackBuf;

        if (mode === "audio") {
            // WhatsApp audio messages can't carry a caption — send only the
            // file. Omar's own reply (sent before this) already mentions the
            // song title to the user.
            await safeSend(
                from,
                {
                    audio: mediaSource,
                    mimetype: "audio/mpeg",
                    fileName: `${(data.title || "audio").slice(0, 60)}.mp3`,
                },
                { quoted: msg }
            );
        } else {
            // Videos support captions. Prefer Omar's own intro sentence
            // (userCaption) over the auto-generated title+duration so the
            // user gets one combined message.
            const autoCaption = [
                data.title,
                fmtDuration(data.duration) && `(${fmtDuration(data.duration)})`,
            ].filter(Boolean).join(" ");
            const caption = (userCaption && userCaption.trim()) || autoCaption;
            await safeSend(
                from,
                {
                    video: mediaSource,
                    mimetype: data.mime || "video/mp4",
                    fileName: `${(data.title || "video").slice(0, 60)}.${data.ext || "mp4"}`,
                    caption: caption || undefined,
                },
                { quoted: msg }
            );
        }
    } catch (err) {
        console.error("Download failed:", err.message);
        await safeSend(
            from,
            { text: `ما قدرتش ننزل: ${err.message}` },
            { quoted: msg }
        );
    }
}

async function performImageGen(sock, msg, from, prompt, refFile = null, userCaption = null) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const data = await callGemini(
            "/image",
            { user: from, prompt },
            refFile
        );
        if (data.images && data.images.length) {
            const firstCaption = (userCaption && userCaption.trim()) || data.text || undefined;
            for (let i = 0; i < data.images.length; i++) {
                await safeSend(
                    from,
                    {
                        image: Buffer.from(data.images[i], "base64"),
                        caption: i === 0 ? firstCaption : undefined,
                    },
                    { quoted: msg }
                );
            }
        } else {
            await safeSend(
                from,
                { text: data.text || "ما قدرتش نولّد الصورة." },
                { quoted: msg }
            );
        }
    } catch (err) {
        console.error("Image generation failed:", err.message);
        await safeSend(
            from,
            { text: `ما قدرتش نولّد الصورة: ${err.message}` },
            { quoted: msg }
        );
    }
}

// Find every <tool>{...}</tool> block in Omar's reply, parse it as JSON,
// and dispatch each one to the right helper. Returns:
//   { cleanText: string,  // the reply with tool blocks stripped
//     calls: Array<object> }
function parseToolCalls(replyText) {
    const TOOL_RE = /<tool>([\s\S]+?)<\/tool>/gi;
    const calls = [];
    let m;
    while ((m = TOOL_RE.exec(replyText || "")) !== null) {
        const raw = m[1].trim();
        try {
            calls.push(JSON.parse(raw));
        } catch (e) {
            console.error("Bad tool JSON:", raw, "->", e.message);
        }
    }
    const cleanText = (replyText || "").replace(TOOL_RE, "").replace(/\n{3,}/g, "\n\n").trim();
    return { cleanText, calls };
}

// Tools whose output is a single attachment (image / video / document)
// that supports a WhatsApp caption. When Omar wrote a short sentence
// before such a <tool> block, we use that sentence as the file caption
// so the user gets ONE combined message instead of "text" + "file".
function callProducesCaptionableAttachment(call) {
    if (!call || typeof call !== "object") return false;
    switch (call.name) {
        case "image":
        case "pinterest":
        case "cloudfile":
        case "code":
        case "makepdf":
        case "apk":
        case "pdf":
        case "qr":
        case "screenshot":
        case "sticker":
            return true;
        case "download":
            // Audio messages on WhatsApp cannot carry a caption, so we
            // can only inline the caption for video downloads.
            return (call.mode || "video").toString().toLowerCase() !== "audio";
        default:
            return false;
    }
}

// ---------------------------------------------------------------------------
// Cloud storage file download (MediaFire, Google Drive, Mega.nz)
// ---------------------------------------------------------------------------
async function performCloudDownload(sock, msg, from, url, userCaption = null) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const data = await callGemini(
            "/cloudfile",
            { url },
            null,
            { retries: 1, retryDelayMs: 2000 }
        );

        const filename = data.filename || "file";
        const mime     = data.mime     || "application/octet-stream";
        const sizeStr  = data.size_readable ? ` (${data.size_readable})` : "";
        const caption  = (userCaption && userCaption.trim()) || `${filename}${sizeStr}`;

        if (data.download_url) {
            // MediaFire / Google Drive — stream directly from the resolved URL
            await safeSend(
                from,
                {
                    document: { url: data.download_url },
                    mimetype: mime,
                    fileName: filename,
                    caption,
                },
                { quoted: msg }
            );
        } else if (data.data_b64) {
            // Mega.nz — decrypted bytes returned as base64
            const buf = Buffer.from(data.data_b64, "base64");
            await safeSend(
                from,
                {
                    document: buf,
                    mimetype: mime,
                    fileName: filename,
                    caption,
                },
                { quoted: msg }
            );
        } else {
            await safeSend(from, { text: "ما قدرتش ننزل الملف." }, { quoted: msg });
        }
    } catch (err) {
        console.error("Cloud download failed:", err.message);
        await safeSend(
            from,
            { text: `ما قدرتش ننزل الملف: ${err.message}` },
            { quoted: msg }
        );
    }
}

// ---------------------------------------------------------------------------
// Pinterest search (sends multiple images) and pin download (image or video)
// ---------------------------------------------------------------------------
async function performPinterestImages(sock, msg, from, query, num_images, userCaption = null) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const data = await callGemini(
            "/pinterest/images",
            { query, num_images: num_images || 4 },
            null,
            { retries: 1, retryDelayMs: 1500 }
        );
        const images = data.images || [];
        if (!images.length) {
            await safeSend(from, { text: `ما لقيت تا صورة على Pinterest: ${query}` }, { quoted: msg });
            return;
        }
        for (let i = 0; i < images.length; i++) {
            const img = images[i];
            const buf = Buffer.from(img.data_b64, "base64");
            const caption = i === 0 ? ((userCaption && userCaption.trim()) || `Pinterest: ${query}`) : undefined;
            await safeSend(from, { image: buf, caption }, { quoted: msg });
        }
    } catch (err) {
        console.error("Pinterest images failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نجيب الصور من Pinterest: ${err.message}` }, { quoted: msg });
    }
}

async function performPinterestDownload(sock, msg, from, query, userCaption = null) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const data = await callGemini(
            "/pinterest/download",
            { query },
            null,
            { retries: 1, retryDelayMs: 1500 }
        );
        const buf = Buffer.from(data.data_b64, "base64");
        const caption = (userCaption && userCaption.trim()) || data.title || "Pinterest";
        if (data.media_type === "video") {
            await safeSend(
                from,
                {
                    video: buf,
                    mimetype: data.mime || "video/mp4",
                    fileName: `${(data.title || "video").slice(0, 60)}.${data.ext || "mp4"}`,
                    caption,
                },
                { quoted: msg }
            );
        } else {
            await safeSend(from, { image: buf, caption }, { quoted: msg });
        }
    } catch (err) {
        console.error("Pinterest download failed:", err.message);
        await safeSend(from, { text: `ما قدرتش ننزل من Pinterest: ${err.message}` }, { quoted: msg });
    }
}

async function executeToolCall(sock, msg, from, call, userCaption = null) {
    if (!call || typeof call !== "object") return;
    switch (call.name) {
        case "download": {
            const query = call.query || call.url || "";
            const mode = (call.mode || "video").toLowerCase() === "audio" ? "audio" : "video";
            const quality = String(call.quality || "720");
            if (!query) {
                await safeSend(from, { text: "ما عطيتش لي اسم أو رابط للتنزيل." }, { quoted: msg });
                return;
            }
            await performDownload(sock, msg, from, query, mode, quality, userCaption);
            return;
        }
        case "image": {
            const prompt = call.prompt || call.query || "";
            if (!prompt) {
                await safeSend(from, { text: "ما عطيتش لي وصف للصورة." }, { quoted: msg });
                return;
            }
            await performImageGen(sock, msg, from, prompt, null, userCaption);
            return;
        }
        case "apk": {
            const query = call.query || call.package || call.name_query || "";
            if (!query) {
                await safeSend(from, { text: "ما عطيتش لي اسم التطبيق." }, { quoted: msg });
                return;
            }
            await performApkDownload(sock, msg, from, query, userCaption);
            return;
        }
        case "search": {
            const query = call.query || call.q || "";
            if (!query) {
                await safeSend(from, { text: "ما عطيتش لي شي حاجة باش نقلب عليها." }, { quoted: msg });
                return;
            }
            await performWebSearch(sock, msg, from, query);
            return;
        }
        case "fetch": {
            const url = call.url || call.query || "";
            if (!url) {
                await safeSend(from, { text: "ما عطيتش لي رابط نقرأو." }, { quoted: msg });
                return;
            }
            await performFetch(sock, msg, from, url);
            return;
        }
        case "screenshot": {
            const url = call.url || call.query || "";
            if (!url) {
                await safeSend(from, { text: "ما عطيتش لي رابط نديرو سكرين." }, { quoted: msg });
                return;
            }
            const width = Number(call.width) || 1280;
            await performScreenshot(sock, msg, from, url, width, userCaption);
            return;
        }
        case "deepsearch": {
            const query = call.query || call.q || "";
            if (!query) {
                await safeSend(from, { text: "ما عطيتش لي شي موضوع نبحث عليه عميق." }, { quoted: msg });
                return;
            }
            await performDeepSearch(sock, msg, from, query);
            return;
        }
        case "pdf": {
            const query = call.query || call.q || "";
            if (!query) {
                await safeSend(from, { text: "ما عطيتش لي شي موضوع نقلب عليه." }, { quoted: msg });
                return;
            }
            await performPdfSearch(sock, msg, from, query, userCaption);
            return;
        }
        case "code": {
            const filename = (call.filename || call.name_file || "file.txt").toString();
            const content = call.content || call.code || call.text || "";
            if (!content) {
                await safeSend(from, { text: "ما عطيتش لي محتوى الملف." }, { quoted: msg });
                return;
            }
            await performCodeFile(sock, msg, from, filename, content, userCaption);
            return;
        }
        case "makepdf": {
            const title = (call.title || "").toString();
            const content = call.content || call.body || call.text || "";
            const filename = (call.filename || "").toString();
            if (!content) {
                await safeSend(from, { text: "ما عطيتش لي محتوى الـ PDF." }, { quoted: msg });
                return;
            }
            await performMakePdf(sock, msg, from, title, content, filename, userCaption);
            return;
        }
        case "weather": {
            const place = (call.place || call.city || call.query || "").toString().trim();
            if (!place) {
                await safeSend(from, { text: "ما عطيتيش لي اسم المدينة." }, { quoted: msg });
                return;
            }
            await performWeather(sock, msg, from, place);
            return;
        }
        case "prayer": {
            const place = (call.place || call.city || call.query || "").toString().trim();
            const method = (call.method || "").toString().trim() || null;
            const date = (call.date || "").toString().trim() || null;
            if (!place) {
                await safeSend(from, { text: "ما عطيتيش لي اسم المدينة." }, { quoted: msg });
                return;
            }
            await performPrayer(sock, msg, from, place, method, date);
            return;
        }
        case "currency": {
            const amount = Number(call.amount ?? call.value ?? 1);
            const src = (call.from || call.src || "").toString().trim().toUpperCase();
            const dst = (call.to || call.dst || "").toString().trim().toUpperCase();
            if (!src || !dst) {
                await safeSend(from, { text: "ما عرفتش العملة من ولا لاش." }, { quoted: msg });
                return;
            }
            await performCurrency(sock, msg, from, isNaN(amount) ? 1 : amount, src, dst);
            return;
        }
        case "translate": {
            const text = (call.text || call.query || "").toString();
            const target = (call.target || call.to || "en").toString().trim();
            const source = (call.source || call.from || "auto").toString().trim();
            if (!text.trim()) {
                await safeSend(from, { text: "ما عطيتيش لي نص نترجم." }, { quoted: msg });
                return;
            }
            await performTranslate(sock, msg, from, text, target, source);
            return;
        }
        case "wiki": {
            const query = (call.query || call.q || call.title || "").toString().trim();
            const lang = (call.lang || "ar").toString().trim();
            if (!query) {
                await safeSend(from, { text: "ما عطيتيش لي موضوع." }, { quoted: msg });
                return;
            }
            await performWiki(sock, msg, from, query, lang);
            return;
        }
        case "qr": {
            const data = (call.data || call.text || call.url || call.query || "").toString();
            if (!data) {
                await safeSend(from, { text: "ما عطيتيش لي محتوى للـ QR." }, { quoted: msg });
                return;
            }
            await performQr(sock, msg, from, data, userCaption);
            return;
        }
        case "tts": {
            const text = (call.text || call.query || "").toString();
            const lang = (call.lang || "ar").toString().trim();
            if (!text.trim()) {
                await safeSend(from, { text: "ما عطيتيش لي نص للصوت." }, { quoted: msg });
                return;
            }
            await performTts(sock, msg, from, text, lang);
            return;
        }
        case "time": {
            const tz = (call.tz || call.timezone || call.zone || "Africa/Casablanca").toString().trim();
            await performTime(sock, msg, from, tz);
            return;
        }
        case "pinterest": {
            const query = (call.query || call.q || call.url || "").toString().trim();
            if (!query) {
                await safeSend(from, { text: "ما عطيتيش لي شي موضوع أو رابط Pinterest." }, { quoted: msg });
                return;
            }
            if (/pinterest\.[^/]+\/pin\/\d+/i.test(query) || query.startsWith("http")) {
                await performPinterestDownload(sock, msg, from, query, userCaption);
            } else {
                const num = parseInt(call.num_images || call.count || 4, 10) || 4;
                await performPinterestImages(sock, msg, from, query, num, userCaption);
            }
            return;
        }
        case "cloudfile": {
            const url = (call.url || call.query || call.link || "").toString().trim();
            if (!url) {
                await safeSend(from, { text: "ما عطيتيش لي رابط الملف." }, { quoted: msg });
                return;
            }
            await performCloudDownload(sock, msg, from, url, userCaption);
            return;
        }
        case "lyrics": {
            const query = (call.query || call.q || call.title || "").toString().trim();
            if (!query) {
                await safeSend(from, { text: "ما عطيتيش لي اسم الأغنية." }, { quoted: msg });
                return;
            }
            await performLyrics(sock, msg, from, query);
            return;
        }
        case "quran": {
            const surah = call.surah ?? call.sura ?? null;
            const ayah = call.ayah ?? call.verse ?? null;
            const query = (call.query || "").toString().trim() || null;
            if (!surah && !query) {
                await safeSend(from, { text: "ما عطيتيش لي السورة ولا الكلمات." }, { quoted: msg });
                return;
            }
            await performQuran(sock, msg, from, surah, ayah, query);
            return;
        }
        case "hadith": {
            const query = (call.query || "").toString().trim() || null;
            await performHadith(sock, msg, from, query);
            return;
        }
        case "crypto": {
            const coin = (call.coin || call.query || "").toString().trim();
            if (!coin) {
                await safeSend(from, { text: "ما عطيتيش لي اسم العملة." }, { quoted: msg });
                return;
            }
            await performCrypto(sock, msg, from, coin);
            return;
        }
        case "football": {
            const team = (call.team || call.query || "").toString().trim();
            if (!team) {
                await safeSend(from, { text: "ما عطيتيش لي اسم الفريق." }, { quoted: msg });
                return;
            }
            await performFootball(sock, msg, from, team);
            return;
        }
        case "joke": {
            const lang = (call.lang || "en").toString().trim();
            await performJoke(sock, msg, from, lang);
            return;
        }
        case "country": {
            const name = (call.name || call.query || "").toString().trim();
            if (!name) {
                await safeSend(from, { text: "ما عطيتيش لي اسم الدولة." }, { quoted: msg });
                return;
            }
            await performCountry(sock, msg, from, name);
            return;
        }
        case "dictionary": {
            const word = (call.word || call.query || "").toString().trim();
            if (!word) {
                await safeSend(from, { text: "ما عطيتيش لي الكلمة." }, { quoted: msg });
                return;
            }
            await performDictionary(sock, msg, from, word);
            return;
        }
        case "horoscope": {
            const sign = (call.sign || call.query || "").toString().trim();
            if (!sign) {
                await safeSend(from, { text: "ما عطيتيش لي البرج." }, { quoted: msg });
                return;
            }
            await performHoroscope(sock, msg, from, sign);
            return;
        }
        case "shorten": {
            const url = (call.url || call.query || "").toString().trim();
            if (!url) {
                await safeSend(from, { text: "ما عطيتيش لي رابط نقصرو." }, { quoted: msg });
                return;
            }
            await performShorten(sock, msg, from, url);
            return;
        }
        case "sticker": {
            const url = (call.url || "").toString().trim();
            const text = (call.text || "").toString().trim();
            if (!url && !text) {
                await safeSend(from, { text: "ما عطيتيش لي صورة ولا نص للستيكر." }, { quoted: msg });
                return;
            }
            await performSticker(sock, msg, from, { url, text }, userCaption);
            return;
        }
        case "transcript": {
            const url = (call.url || call.query || "").toString().trim();
            const lang = (call.lang || "").toString().trim() || null;
            if (!url) {
                await safeSend(from, { text: "ما عطيتيش لي رابط الفيديو." }, { quoted: msg });
                return;
            }
            await performTranscript(sock, msg, from, url, lang);
            return;
        }
        default:
            console.error("Unknown tool call:", JSON.stringify(call));
    }
}

// ---------------------------------------------------------------------------
// Send a generated code/text file to the user as a WhatsApp document.
// ---------------------------------------------------------------------------
const CODE_MIME_BY_EXT = {
    py: "text/x-python",
    js: "application/javascript",
    mjs: "application/javascript",
    cjs: "application/javascript",
    ts: "application/typescript",
    tsx: "application/typescript",
    jsx: "text/jsx",
    html: "text/html",
    htm: "text/html",
    css: "text/css",
    scss: "text/x-scss",
    cpp: "text/x-c++src",
    cc: "text/x-c++src",
    cxx: "text/x-c++src",
    c: "text/x-csrc",
    h: "text/x-chdr",
    hpp: "text/x-c++hdr",
    java: "text/x-java-source",
    kt: "text/x-kotlin",
    swift: "text/x-swift",
    go: "text/x-go",
    rs: "text/rust",
    php: "application/x-php",
    rb: "application/x-ruby",
    sh: "application/x-sh",
    bash: "application/x-sh",
    zsh: "application/x-sh",
    sql: "application/sql",
    json: "application/json",
    yaml: "application/x-yaml",
    yml: "application/x-yaml",
    xml: "application/xml",
    toml: "application/toml",
    ini: "text/plain",
    env: "text/plain",
    md: "text/markdown",
    markdown: "text/markdown",
    txt: "text/plain",
    csv: "text/csv",
    tsv: "text/tab-separated-values",
    log: "text/plain",
    dockerfile: "text/plain",
    makefile: "text/plain",
    r: "text/x-r",
    lua: "text/x-lua",
    pl: "text/x-perl",
    dart: "application/dart",
    vue: "text/x-vue",
    svelte: "text/plain",
};

function sanitizeFilename(name) {
    let n = String(name || "").trim().replace(/[\\/]/g, "_");
    n = n.replace(/[^A-Za-z0-9._\-]/g, "_");
    if (!n) n = "file.txt";
    if (n.length > 80) n = n.slice(0, 80);
    if (!/\./.test(n)) n += ".txt";
    return n;
}

function mimeForFilename(name) {
    const ext = (name.split(".").pop() || "").toLowerCase();
    return CODE_MIME_BY_EXT[ext] || "text/plain";
}

async function performMakePdf(sock, msg, from, title, content, filename, userCaption = null) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const data = await callGemini(
            "/makepdf",
            { title, content, filename },
            null,
            { retries: 1, retryDelayMs: 1500 }
        );
        const buf = Buffer.from(data.data_b64, "base64");
        const sizeKb = (data.size_bytes / 1024).toFixed(1);
        const safeName = data.filename || "document.pdf";
        const autoCaption = `${(title || safeName).trim()} (${sizeKb}KB)`;
        const caption = (userCaption && userCaption.trim()) || autoCaption;
        await safeSend(
            from,
            {
                document: buf,
                mimetype: "application/pdf",
                fileName: safeName,
                caption,
            },
            { quoted: msg }
        );
    } catch (err) {
        console.error("makepdf failed:", err.message);
        await safeSend(
            from,
            { text: `ما قدرتش نولّد الـ PDF: ${err.message}` },
            { quoted: msg }
        );
    }
}

async function performCodeFile(sock, msg, from, filename, content, userCaption = null) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const safeName = sanitizeFilename(filename);
        const buf = Buffer.from(String(content), "utf-8");
        const sizeKb = (buf.length / 1024).toFixed(1);
        const mime = mimeForFilename(safeName);
        const autoCaption = `${safeName} (${sizeKb}KB)`;
        const caption = (userCaption && userCaption.trim()) || autoCaption;
        await safeSend(
            from,
            {
                document: buf,
                mimetype: mime,
                fileName: safeName,
                caption,
            },
            { quoted: msg }
        );
    } catch (err) {
        console.error("Code file send failed:", err.message);
        await safeSend(
            from,
            { text: `ما قدرتش نرسل الملف: ${err.message}` },
            { quoted: msg }
        );
    }
}

// Send a follow-up turn to Omar so he can produce a summary or wrap-up
// message based on data the backend just gathered (search results, page
// text, PDF metadata...). Returns the clean text reply with any nested
// tool blocks stripped out — we don't execute them to avoid loops.
async function summarizeViaOmar(from, instruction) {
    try {
        const data = await callGemini(
            "/ask",
            { user: from, text: instruction },
            null,
            { retries: 1, retryDelayMs: 1500 }
        );
        const reply = data.text || "";
        const { cleanText } = parseToolCalls(reply);
        return (cleanText || reply || "").trim();
    } catch (err) {
        console.error("Summarize follow-up failed:", err.message);
        return "";
    }
}

async function performWebSearch(sock, msg, from, query) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const data = await callGemini(
            "/websearch",
            { query, max_results: 5 },
            null,
            { retries: 1, retryDelayMs: 1500 }
        );
        const results = data.results || [];
        if (!results.length) {
            await safeSend(from, { text: "ما لقيت تا نتيجة على هاد البحث." }, { quoted: msg });
            return;
        }
        const lines = results.map((r, i) =>
            `${i + 1}. ${r.title}\n${r.url}\n${(r.snippet || "").slice(0, 300)}`
        ).join("\n\n");
        const prompt =
            `[نتائج بحث في الويب على "${query}"]\n\n${lines}\n\n` +
            `لخص هاد النتائج للمستخدم بشكل واضح ومفيد بالدارجة، ` +
            `وحط في الأخير قائمة المصادر بالروابط الخام (بدون Markdown). ` +
            `ما تستعملش أي أداة في الجواب.`;
        const summary = await summarizeViaOmar(from, prompt);
        if (summary) {
            await safeSend(from, { text: summary }, { quoted: msg });
        } else {
            await safeSend(from, { text: lines }, { quoted: msg });
        }
    } catch (err) {
        console.error("Web search failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نبحث: ${err.message}` }, { quoted: msg });
    }
}

async function performFetch(sock, msg, from, url) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const data = await callGemini(
            "/fetch",
            { url, max_chars: 8000 },
            null,
            { retries: 1, retryDelayMs: 1500 }
        );
        if (!data.text) {
            await safeSend(
                from,
                { text: `الرابط ماشي صفحة نص (${data.content_type || "?"})، ما قدرتش نلخصو.` },
                { quoted: msg }
            );
            return;
        }
        const prompt =
            `[محتوى الصفحة من ${data.url}]\n` +
            (data.title ? `العنوان: ${data.title}\n` : "") +
            `\n${data.text}\n\n` +
            `لخص هاد الصفحة للمستخدم بالدارجة بشكل مفيد ومركز. ` +
            `حط الرابط الأصلي في الأخير. بدون Markdown وبدون أي أداة.`;
        const summary = await summarizeViaOmar(from, prompt);
        if (summary) {
            await safeSend(from, { text: summary }, { quoted: msg });
        } else {
            await safeSend(
                from,
                { text: `${data.title || ""}\n${data.text.slice(0, 1500)}\n\n${data.url}`.trim() },
                { quoted: msg }
            );
        }
    } catch (err) {
        console.error("Fetch failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نقرا الرابط: ${err.message}` }, { quoted: msg });
    }
}

async function performScreenshot(sock, msg, from, url, width, userCaption = null) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const data = await callGemini(
            "/screenshot",
            { url, width },
            null,
            { retries: 1, retryDelayMs: 2000 }
        );
        if (!data.data_b64) {
            await safeSend(
                from,
                { text: `ما قدرتش ناخد سكرين شوت ديال هاد الموقع.` },
                { quoted: msg }
            );
            return;
        }
        const caption = (userCaption && userCaption.trim()) ||
            `📸 سكرين شوت ديال ${data.url || url}`;
        await safeSend(
            from,
            {
                image: Buffer.from(data.data_b64, "base64"),
                caption,
            },
            { quoted: msg }
        );
    } catch (err) {
        console.error("Screenshot failed:", err.message);
        await safeSend(
            from,
            { text: `ما قدرتش ناخد سكرين: ${err.message}` },
            { quoted: msg }
        );
    }
}

async function performDeepSearch(sock, msg, from, query) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const data = await callGemini(
            "/deepsearch",
            { query, num_pages: 3 },
            null,
            { retries: 1, retryDelayMs: 2000 }
        );
        const results = data.results || [];
        const pages = data.pages || [];
        if (!results.length && !pages.length) {
            await safeSend(from, { text: "ما لقيت تا نتيجة على هاد البحث العميق." }, { quoted: msg });
            return;
        }
        const pagesBlock = pages.map((p, i) =>
            `--- صفحة ${i + 1}: ${p.title || ""} (${p.url}) ---\n` +
            (p.text || p.snippet || "(ما قدرتش نقرأها)").slice(0, 2500)
        ).join("\n\n");
        const otherBlock = results.slice(pages.length).map((r, i) =>
            `${i + 1}. ${r.title}\n${r.url}\n${(r.snippet || "").slice(0, 200)}`
        ).join("\n\n");
        const prompt =
            `[بحث عميق على "${query}"]\n\n` +
            `قرأت لك ${pages.length} صفحات كاملة من أحسن النتائج. ها هي:\n\n` +
            `${pagesBlock}\n\n` +
            (otherBlock ? `[نتائج إضافية ما قراتهاش بالكامل]\n${otherBlock}\n\n` : "") +
            `لخص هاد البحث العميق للمستخدم بشكل شامل ومركز بالدارجة، ` +
            `جمع المعلومات من كل المصادر، ` +
            `وحط في الأخير قائمة المصادر بالروابط الخام (بدون Markdown). ` +
            `ما تستعملش أي أداة في الجواب.`;
        const summary = await summarizeViaOmar(from, prompt);
        if (summary) {
            await safeSend(from, { text: summary }, { quoted: msg });
        } else {
            const fallback = pages.map((p) => `• ${p.title}\n${p.url}`).join("\n\n");
            await safeSend(from, { text: fallback || "ما قدرتش نلخص النتائج." }, { quoted: msg });
        }
    } catch (err) {
        console.error("Deep search failed:", err.message);
        await safeSend(from, { text: `ما قدرتش ندير بحث عميق: ${err.message}` }, { quoted: msg });
    }
}

async function performPdfSearch(sock, msg, from, query, userCaption = null) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const data = await callGemini(
            "/pdfsearch",
            { query },
            null,
            { retries: 1, retryDelayMs: 1500 }
        );
        const sources = data.sources || [];
        const pdf = data.pdf;

        if (pdf) {
            const buf = Buffer.from(pdf.data_b64, "base64");
            const sizeMb = (pdf.size_bytes / (1024 * 1024)).toFixed(2);
            const autoCaption = `${data.pdf_title || pdf.filename} (${sizeMb}MB)\n${pdf.url}`;
            // If Omar wrote an intro sentence, prefer that as the caption
            // (and append the source URL on a new line for reference).
            const caption = (userCaption && userCaption.trim())
                ? `${userCaption.trim()}\n${pdf.url}`
                : autoCaption;
            await safeSend(
                from,
                {
                    document: buf,
                    mimetype: "application/pdf",
                    fileName: pdf.filename || "document.pdf",
                    caption,
                },
                { quoted: msg }
            );
        }

        // If we successfully delivered the PDF as a captioned document, do
        // NOT send a follow-up message with extra source links — the user
        // already has what they asked for and a second message feels like
        // spam. Only when no PDF was found do we apologize and offer the
        // alternative sources we found.
        if (!pdf) {
            const srcLines = sources.map((r, i) =>
                `${i + 1}. ${r.title}\n${r.url}`
            ).join("\n\n");
            const intro =
                `[ما لقيتش ملف PDF على "${query}". هاديك هي مصادر مفيدة:\n\n${srcLines}\n\n` +
                `اعتذر للمستخدم بأنك ما لقيتش PDF، وقدم له هاد المصادر بالروابط الخام، بدون Markdown وبدون أداة.]`;
            const summary = await summarizeViaOmar(from, intro);
            if (summary) {
                await safeSend(from, { text: summary }, { quoted: msg });
            } else {
                await safeSend(from, { text: srcLines || "ما لقيت والو." }, { quoted: msg });
            }
        }
    } catch (err) {
        console.error("PDF search failed:", err.message);
        await safeSend(from, { text: `ما قدرتش ندوز البحث: ${err.message}` }, { quoted: msg });
    }
}

async function performApkDownload(sock, msg, from, query, userCaption = null) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});

        const data = await callGemini(
            "/apk",
            { query },
            null,
            { retries: 1, retryDelayMs: 1500 }
        );

        const buf = Buffer.from(data.data_b64, "base64");
        const sizeMb = data.size_mb ?? (buf.length / (1024 * 1024)).toFixed(1);
        const minAndroid = data.min_android ? `، يشتغل من Android ${data.min_android}` : "";
        const autoCaption = `${data.title || query} نسخة ${data.version_name || "?"} (${sizeMb}MB)${minAndroid}`;
        const caption = (userCaption && userCaption.trim()) || autoCaption;

        await safeSend(
            from,
            {
                document: buf,
                mimetype: data.mime || "application/vnd.android.package-archive",
                fileName: data.filename || `${(data.title || query).slice(0, 60)}.${data.ext || "apk"}`,
                caption,
            },
            { quoted: msg }
        );
    } catch (err) {
        console.error("APK download failed:", err.message);
        await safeSend(
            from,
            { text: `ما قدرتش ننزل التطبيق: ${err.message}` },
            { quoted: msg }
        );
    }
}

// ---------------------------------------------------------------------------
// Lightweight info tools (weather, prayer, currency, translate, wiki,
// qr, tts, time). Each calls a Flask endpoint then either summarizes
// via Omar or replies directly with formatted text.
// ---------------------------------------------------------------------------
async function performWeather(sock, msg, from, place) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const data = await callGemini(
            "/weather",
            { place },
            null,
            { retries: 1, retryDelayMs: 1500 }
        );
        if (data.error) {
            await safeSend(from, { text: `ما لقيتش الجو ديال "${place}": ${data.error}` }, { quoted: msg });
            return;
        }
        const cur = data.current || {};
        const daily = data.forecast || data.daily || [];
        const lines = [
            `[الطقس فـ ${data.place || place}]`,
            `الحالي: ${cur.temp_c}°C، إحساس ${cur.feels_c}°C، ${cur.desc || ""}`,
            `الرطوبة ${cur.humidity}% — الرياح ${cur.wind_kmh} كم/س`,
            "",
            "توقعات الأيام الجاية:",
            ...daily.map(d =>
                `${d.date}: ${d.min}° → ${d.max}° (${d.desc || ""})` +
                (d.rain_pct != null ? ` — مطر ${d.rain_pct}%` : "")
            ),
        ].join("\n");
        const prompt =
            `${lines}\n\n` +
            `قدّم هاد المعلومات للمستخدم بشكل طبيعي وودود بالدارجة، ` +
            `بدون Markdown وبدون أي أداة. خليه قصير ومركز.`;
        const summary = await summarizeViaOmar(from, prompt);
        await safeSend(from, { text: summary || lines }, { quoted: msg });
    } catch (err) {
        console.error("Weather failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نجيب الجو: ${err.message}` }, { quoted: msg });
    }
}

async function performPrayer(sock, msg, from, place, method, date) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const body = { place };
        if (method) body.method = method;
        if (date) body.date = date;
        const data = await callGemini(
            "/prayer",
            body,
            null,
            { retries: 1, retryDelayMs: 1500 }
        );
        if (data.error) {
            await safeSend(from, { text: `ما لقيتش أوقات الصلاة: ${data.error}` }, { quoted: msg });
            return;
        }
        const t = data.timings || {};
        const lines = [
            `[أوقات الصلاة فـ ${data.place || place} — ${data.date || ""}]`,
            `الفجر: ${t.Fajr || "-"}`,
            `الشروق: ${t.Sunrise || "-"}`,
            `الظهر: ${t.Dhuhr || "-"}`,
            `العصر: ${t.Asr || "-"}`,
            `المغرب: ${t.Maghrib || "-"}`,
            `العشاء: ${t.Isha || "-"}`,
        ].join("\n");
        const prompt =
            `${lines}\n\n` +
            `قدّم أوقات الصلاة هادي للمستخدم بشكل واضح وودود، ` +
            `استعمل *bold* لاسم كل صلاة. بدون أي أداة.`;
        const summary = await summarizeViaOmar(from, prompt);
        await safeSend(from, { text: summary || lines }, { quoted: msg });
    } catch (err) {
        console.error("Prayer failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نجيب الأوقات: ${err.message}` }, { quoted: msg });
    }
}

async function performCurrency(sock, msg, from, amount, src, dst) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const data = await callGemini(
            "/currency",
            { amount, from: src, to: dst },
            null,
            { retries: 1, retryDelayMs: 1500 }
        );
        if (data.error) {
            await safeSend(from, { text: `ما قدرتش نحول العملة: ${data.error}` }, { quoted: msg });
            return;
        }
        const text =
            `*${data.amount} ${data.from}* = *${data.result} ${data.to}*\n` +
            `_السعر: 1 ${data.from} = ${data.rate} ${data.to}_`;
        await safeSend(from, { text }, { quoted: msg });
    } catch (err) {
        console.error("Currency failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نحول: ${err.message}` }, { quoted: msg });
    }
}

async function performTranslate(sock, msg, from, text, target, source) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const data = await callGemini(
            "/translate",
            { text, target, source: source || "auto" },
            null,
            { retries: 1, retryDelayMs: 1500 }
        );
        if (data.error) {
            await safeSend(from, { text: `ما قدرتش نترجم: ${data.error}` }, { quoted: msg });
            return;
        }
        await safeSend(from, { text: data.translated || "(ما عطاتش نتيجة)" }, { quoted: msg });
    } catch (err) {
        console.error("Translate failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نترجم: ${err.message}` }, { quoted: msg });
    }
}

async function performWiki(sock, msg, from, query, lang) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const data = await callGemini(
            "/wiki",
            { query, lang: lang || "ar" },
            null,
            { retries: 1, retryDelayMs: 1500 }
        );
        if (data.error) {
            await safeSend(from, { text: `ما لقيت والو على "${query}": ${data.error}` }, { quoted: msg });
            return;
        }
        const block =
            `[ويكيبيديا — ${data.title || query}]\n` +
            (data.description ? `${data.description}\n\n` : "") +
            `${data.extract || data.summary || ""}\n\n` +
            (data.url ? `المصدر: ${data.url}` : "");
        const prompt =
            `${block}\n\n` +
            `لخص هاد المعلومات للمستخدم بالدارجة بشكل طبيعي ومفيد، ` +
            `حط الرابط فالأخير خام، بدون Markdown وبدون أداة.`;
        const summary = await summarizeViaOmar(from, prompt);
        await safeSend(from, { text: summary || block }, { quoted: msg });
    } catch (err) {
        console.error("Wiki failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نقلب فويكيبيديا: ${err.message}` }, { quoted: msg });
    }
}

async function performQr(sock, msg, from, data, userCaption = null) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const resp = await callGemini(
            "/qr",
            { data },
            null,
            { retries: 1, retryDelayMs: 1500 }
        );
        if (resp.error) {
            await safeSend(from, { text: `ما قدرتش نولّد QR: ${resp.error}` }, { quoted: msg });
            return;
        }
        const buf = Buffer.from(resp.data_b64, "base64");
        const caption = (userCaption && userCaption.trim()) || undefined;
        await safeSend(from, { image: buf, caption }, { quoted: msg });
    } catch (err) {
        console.error("QR failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نولّد QR: ${err.message}` }, { quoted: msg });
    }
}

async function performTts(sock, msg, from, text, lang) {
    try {
        await sock.sendPresenceUpdate("recording", from).catch(() => {});
        const resp = await callGemini(
            "/tts",
            { text, lang: lang || "ar" },
            null,
            { retries: 1, retryDelayMs: 2000 }
        );
        if (resp.error) {
            await safeSend(from, { text: `ما قدرتش نسجل الصوت: ${resp.error}` }, { quoted: msg });
            return;
        }
        const buf = Buffer.from(resp.data_b64, "base64");
        await safeSend(
            from,
            {
                audio: buf,
                mimetype: "audio/mpeg",
                fileName: resp.filename || "tts.mp3",
                ptt: true,
            },
            { quoted: msg }
        );
    } catch (err) {
        console.error("TTS failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نسجل الصوت: ${err.message}` }, { quoted: msg });
    }
}

async function performTime(sock, msg, from, tz) {
    try {
        const data = await callGemini(
            "/time",
            { tz: tz || "Africa/Casablanca" },
            null,
            { retries: 1, retryDelayMs: 1000 }
        );
        if (data.error) {
            await safeSend(from, { text: `ما لقيتش الوقت: ${data.error}` }, { quoted: msg });
            return;
        }
        const text =
            `*${data.tz}*\n` +
            `${data.human || `${data.date || ""} ${data.time || ""}`.trim()}\n` +
            `_${data.offset || ""}_`;
        await safeSend(from, { text: text.trim() }, { quoted: msg });
    } catch (err) {
        console.error("Time failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نجيب الوقت: ${err.message}` }, { quoted: msg });
    }
}

// ---------------------------------------------------------------------------
// Extras2: lyrics, quran, hadith, crypto, football, joke, country,
// dictionary, horoscope, shorten, sticker, transcript.
// ---------------------------------------------------------------------------
function chunkText(s, maxLen = 3500) {
    const out = [];
    let buf = "";
    for (const line of String(s || "").split("\n")) {
        if ((buf + "\n" + line).length > maxLen && buf) {
            out.push(buf);
            buf = line;
        } else {
            buf = buf ? `${buf}\n${line}` : line;
        }
    }
    if (buf) out.push(buf);
    return out;
}

async function performLyrics(sock, msg, from, query) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const data = await callGemini("/lyrics", { query }, null, { retries: 1, retryDelayMs: 1500 });
        if (data.error || !data.ok) {
            await safeSend(from, { text: `ما لقيتش الكلمات: ${data.error || "غير متاح"}` }, { quoted: msg });
            return;
        }
        const header = `🎵 *${data.artist}* — _${data.title}_\n\n`;
        const parts = chunkText(data.lyrics, 3500);
        await safeSend(from, { text: header + parts[0] }, { quoted: msg });
        for (let i = 1; i < parts.length; i++) {
            await safeSend(from, { text: parts[i] });
        }
    } catch (err) {
        console.error("Lyrics failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نجيب الكلمات: ${err.message}` }, { quoted: msg });
    }
}

async function performQuran(sock, msg, from, surah, ayah, query) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const body = {};
        if (surah !== null && surah !== undefined && surah !== "") body.surah = surah;
        if (ayah !== null && ayah !== undefined && ayah !== "") body.ayah = ayah;
        if (query) body.query = query;
        const data = await callGemini("/quran", body, null, { retries: 1, retryDelayMs: 1500 });
        if (data.error || !data.ok) {
            await safeSend(from, { text: `ما قدرتش نجيب الآية: ${data.error || ""}` }, { quoted: msg });
            return;
        }
        if (data.mode === "ayah") {
            const text = `📖 *${data.surah}* — آية ${data.number}\n\n${data.text}`;
            await safeSend(from, { text }, { quoted: msg });
        } else if (data.mode === "surah") {
            const lines = (data.ayat || []).map(a => `﴿${a.text}﴾ (${a.n})`).join("\n");
            let body = `📖 *${data.surah}* (${data.number_of_ayahs} آية)\n\n${lines}`;
            if (data.truncated) body += "\n\n_...تم اقتطاع باقي السورة._";
            const parts = chunkText(body, 3500);
            await safeSend(from, { text: parts[0] }, { quoted: msg });
            for (let i = 1; i < parts.length; i++) await safeSend(from, { text: parts[i] });
        } else if (data.mode === "search") {
            const lines = (data.matches || []).map(m =>
                `🔹 *${m.surah_ar}* (${m.number}):\n${m.text}`
            ).join("\n\n");
            await safeSend(from, { text: `🔎 نتائج البحث على "${data.query}":\n\n${lines}` }, { quoted: msg });
        }
    } catch (err) {
        console.error("Quran failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نجيب القرآن: ${err.message}` }, { quoted: msg });
    }
}

async function performHadith(sock, msg, from, query) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const body = {};
        if (query) body.query = query;
        const data = await callGemini("/hadith", body, null, { retries: 1, retryDelayMs: 2000 });
        if (data.error || !data.ok) {
            await safeSend(from, { text: `ما قدرتش نجيب الحديث: ${data.error || ""}` }, { quoted: msg });
            return;
        }
        let text;
        if (data.mode === "search" && Array.isArray(data.results)) {
            text = data.results.map((h, i) =>
                `🔹 *${h.title || `حديث ${i + 1}`}*\n${h.hadeeth}\n` +
                (h.attribution ? `_${h.attribution}_\n` : "") +
                (h.grade ? `الدرجة: ${h.grade}` : "")
            ).join("\n\n");
        } else {
            text =
                `📜 *${data.title || "حديث شريف"}*\n\n` +
                `${data.hadeeth || ""}\n\n` +
                (data.attribution ? `_${data.attribution}_\n` : "") +
                (data.grade ? `*الدرجة:* ${data.grade}\n` : "") +
                (data.explanation ? `\n*الشرح:* ${data.explanation}` : "");
        }
        await safeSend(from, { text: text.trim() }, { quoted: msg });
    } catch (err) {
        console.error("Hadith failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نجيب الحديث: ${err.message}` }, { quoted: msg });
    }
}

async function performCrypto(sock, msg, from, coin) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const data = await callGemini("/crypto", { coin }, null, { retries: 1, retryDelayMs: 1500 });
        if (data.error || !data.ok) {
            await safeSend(from, { text: `ما لقيتش العملة: ${data.error || coin}` }, { quoted: msg });
            return;
        }
        const p = data.prices || {};
        const change = data.change_24h_pct;
        const arrow = (change || 0) >= 0 ? "📈" : "📉";
        const lines = [
            `*${data.coin.toUpperCase()}* ${arrow}`,
            "",
            p.usd != null ? `💵 USD: ${p.usd}` : null,
            p.eur != null ? `💶 EUR: ${p.eur}` : null,
            p.mad != null ? `🇲🇦 MAD: ${p.mad}` : null,
            p.gbp != null ? `💷 GBP: ${p.gbp}` : null,
            p.sar != null ? `🇸🇦 SAR: ${p.sar}` : null,
            p.aed != null ? `🇦🇪 AED: ${p.aed}` : null,
            "",
            change != null ? `_24h: ${change.toFixed(2)}%_` : null,
        ].filter(Boolean).join("\n");
        await safeSend(from, { text: lines }, { quoted: msg });
    } catch (err) {
        console.error("Crypto failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نجيب السعر: ${err.message}` }, { quoted: msg });
    }
}

async function performFootball(sock, msg, from, team) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const data = await callGemini("/football", { team }, null, { retries: 1, retryDelayMs: 2000 });
        if (data.error || !data.ok) {
            await safeSend(from, { text: `ما لقيتش الفريق: ${data.error || team}` }, { quoted: msg });
            return;
        }
        const last = (data.last_matches || []).map(m =>
            `• ${m.date}: ${m.home} ${m.score} ${m.away}`
        ).join("\n") || "—";
        const next = (data.next_matches || []).map(m =>
            `• ${m.date} ${m.time || ""}: ${m.home} 🆚 ${m.away} _(${m.league || ""})_`
        ).join("\n") || "—";
        const text =
            `⚽ *${data.team}*\n` +
            `${data.country || ""} — ${data.league || ""}\n` +
            (data.stadium ? `🏟️ ${data.stadium}\n` : "") +
            (data.founded ? `📅 تأسس ${data.founded}\n` : "") +
            `\n*آخر النتائج:*\n${last}\n\n*المباريات الجاية:*\n${next}`;
        if (data.badge) {
            await safeSend(from, { image: { url: data.badge }, caption: text }, { quoted: msg });
        } else {
            await safeSend(from, { text }, { quoted: msg });
        }
    } catch (err) {
        console.error("Football failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نجيب الفريق: ${err.message}` }, { quoted: msg });
    }
}

async function performJoke(sock, msg, from, lang) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const data = await callGemini("/joke", { lang }, null, { retries: 1, retryDelayMs: 1500 });
        if (data.error || !data.ok) {
            await safeSend(from, { text: `ما لقيت تا نكتة: ${data.error || ""}` }, { quoted: msg });
            return;
        }
        await safeSend(from, { text: `😂 ${data.joke}` }, { quoted: msg });
    } catch (err) {
        console.error("Joke failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نجيب نكتة: ${err.message}` }, { quoted: msg });
    }
}

async function performCountry(sock, msg, from, name) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const data = await callGemini("/country", { name }, null, { retries: 1, retryDelayMs: 1500 });
        if (data.error || !data.ok) {
            await safeSend(from, { text: `ما لقيتش الدولة: ${data.error || name}` }, { quoted: msg });
            return;
        }
        const text =
            `${data.flag_emoji || "🌍"} *${data.name_ar || data.name_en}*\n` +
            (data.name_official ? `_${data.name_official}_\n` : "") +
            "\n" +
            (data.capital ? `🏛️ العاصمة: ${data.capital}\n` : "") +
            (data.region ? `🌐 المنطقة: ${data.region} — ${data.subregion || ""}\n` : "") +
            (data.population ? `👥 السكان: ${Number(data.population).toLocaleString("en")}\n` : "") +
            (data.area_km2 ? `📐 المساحة: ${Number(data.area_km2).toLocaleString("en")} كم²\n` : "") +
            (data.currency ? `💰 العملة: ${data.currency}\n` : "") +
            (data.languages ? `🗣️ اللغات: ${data.languages}\n` : "") +
            (data.calling_code ? `📞 ${data.calling_code}\n` : "") +
            (data.tld ? `🌐 ${data.tld}\n` : "") +
            (data.maps ? `\n🗺️ ${data.maps}` : "");
        if (data.flag) {
            await safeSend(from, { image: { url: data.flag }, caption: text.trim() }, { quoted: msg });
        } else {
            await safeSend(from, { text: text.trim() }, { quoted: msg });
        }
    } catch (err) {
        console.error("Country failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نجيب معلومات الدولة: ${err.message}` }, { quoted: msg });
    }
}

async function performDictionary(sock, msg, from, word) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const data = await callGemini("/dictionary", { word }, null, { retries: 1, retryDelayMs: 1500 });
        if (data.error || !data.ok) {
            await safeSend(from, { text: `Word not found: ${data.error || word}` }, { quoted: msg });
            return;
        }
        const meanings = (data.meanings || []).map(m => {
            const defs = (m.definitions || []).map((d, i) =>
                `  ${i + 1}. ${d.definition}` + (d.example ? `\n     _e.g. ${d.example}_` : "")
            ).join("\n");
            const syn = m.synonyms?.length ? `\n  *syn:* ${m.synonyms.join(", ")}` : "";
            return `*[${m.part_of_speech}]*\n${defs}${syn}`;
        }).join("\n\n");
        const text =
            `📚 *${data.word}*` +
            (data.phonetic ? ` _${data.phonetic}_` : "") +
            `\n\n${meanings}`;
        await safeSend(from, { text }, { quoted: msg });
    } catch (err) {
        console.error("Dictionary failed:", err.message);
        await safeSend(from, { text: `Failed: ${err.message}` }, { quoted: msg });
    }
}

async function performHoroscope(sock, msg, from, sign) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const data = await callGemini("/horoscope", { sign }, null, { retries: 1, retryDelayMs: 1500 });
        if (data.error || !data.ok) {
            await safeSend(from, { text: `ما لقيتش البرج: ${data.error || sign}` }, { quoted: msg });
            return;
        }
        const block =
            `[Horoscope for ${data.sign} — ${data.date || "today"}]\n${data.horoscope}\n\n` +
            `ترجم النص فوق للدارجة المغربية بشكل ودود وقدمو للمستخدم كتوقعات اليوم ` +
            `لبرج ${data.sign}. ابدأ بـ "🔮 *برج ${data.sign}* — توقعات اليوم". ` +
            `بدون أي أداة وبدون Markdown مفرط.`;
        const summary = await summarizeViaOmar(from, block);
        await safeSend(from, { text: summary || `🔮 *${data.sign}*\n${data.horoscope}` }, { quoted: msg });
    } catch (err) {
        console.error("Horoscope failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نجيب البرج: ${err.message}` }, { quoted: msg });
    }
}

async function performShorten(sock, msg, from, url) {
    try {
        const data = await callGemini("/shorten", { url }, null, { retries: 1, retryDelayMs: 1500 });
        if (data.error || !data.ok) {
            await safeSend(from, { text: `ما قدرتش نقصر: ${data.error || ""}` }, { quoted: msg });
            return;
        }
        await safeSend(from, { text: `🔗 ${data.short}` }, { quoted: msg });
    } catch (err) {
        console.error("Shorten failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نقصر: ${err.message}` }, { quoted: msg });
    }
}

async function performSticker(sock, msg, from, opts, userCaption = null) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const body = {};
        if (opts.url) body.url = opts.url;
        if (opts.text) body.text = opts.text;
        const data = await callGemini("/sticker", body, null, { retries: 1, retryDelayMs: 2000 });
        if (data.error) {
            await safeSend(from, { text: `ما قدرتش نولّد ستيكر: ${data.error}` }, { quoted: msg });
            return;
        }
        const buf = Buffer.from(data.data_b64, "base64");
        await safeSend(from, { sticker: buf }, { quoted: msg });
        if (userCaption && userCaption.trim()) {
            await safeSend(from, { text: userCaption.trim() });
        }
    } catch (err) {
        console.error("Sticker failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نولّد ستيكر: ${err.message}` }, { quoted: msg });
    }
}

// Sticker built from a raw image buffer (used by the /sticker shortcut and
// when the user attaches an image asking for a sticker).
async function performStickerFromBuffer(sock, msg, from, imageBuf, mime = "image/png") {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const data = await callGemini(
            "/sticker",
            {},
            { bytes: imageBuf, mime, name: "input." + (mime.split("/")[1] || "png") },
            { retries: 1, retryDelayMs: 2000 }
        );
        if (data.error) {
            await safeSend(from, { text: `ما قدرتش نولّد ستيكر: ${data.error}` }, { quoted: msg });
            return;
        }
        const buf = Buffer.from(data.data_b64, "base64");
        await safeSend(from, { sticker: buf }, { quoted: msg });
    } catch (err) {
        console.error("Sticker buffer failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نولّد ستيكر: ${err.message}` }, { quoted: msg });
    }
}

async function performTranscript(sock, msg, from, url, lang) {
    try {
        await sock.sendPresenceUpdate("composing", from).catch(() => {});
        const body = { url };
        if (lang) body.lang = lang;
        const data = await callGemini("/transcript", body, null, { retries: 1, retryDelayMs: 3000 });
        if (data.error || !data.ok) {
            await safeSend(from, { text: `ما قدرتش نجيب التفريغ: ${data.error || ""}` }, { quoted: msg });
            return;
        }
        const header = `📝 *Transcript* (${data.lang}, ${data.char_count} حرف)\n\n`;
        const parts = chunkText(data.text, 3500);
        await safeSend(from, { text: header + parts[0] }, { quoted: msg });
        for (let i = 1; i < parts.length; i++) {
            await safeSend(from, { text: parts[i] });
        }
    } catch (err) {
        console.error("Transcript failed:", err.message);
        await safeSend(from, { text: `ما قدرتش نجيب التفريغ: ${err.message}` }, { quoted: msg });
    }
}

function detectMimeAndExt(msg) {
    const m = msg.message || {};
    if (m.imageMessage)
        return { mime: m.imageMessage.mimetype || "image/jpeg", name: "image.jpg" };
    if (m.audioMessage)
        return { mime: m.audioMessage.mimetype || "audio/ogg", name: "audio.ogg" };
    if (m.videoMessage)
        return { mime: m.videoMessage.mimetype || "video/mp4", name: "video.mp4" };
    if (m.documentMessage) {
        return {
            mime: m.documentMessage.mimetype || "application/octet-stream",
            name: m.documentMessage.fileName || "file.bin",
        };
    }
    return null;
}

async function extractFile(msg) {
    const info = detectMimeAndExt(msg);
    if (!info) return null;
    try {
        const bytes = await downloadMediaMessage(msg, "buffer", {}, { logger });
        return { bytes, mime: info.mime, name: info.name };
    } catch (err) {
        console.error("Failed to download media:", err.message);
        return null;
    }
}

function extractText(msg) {
    const m = msg.message || {};
    return (
        m.conversation ||
        m.extendedTextMessage?.text ||
        m.imageMessage?.caption ||
        m.videoMessage?.caption ||
        m.documentMessage?.caption ||
        ""
    );
}

// ---------------------------------------------------------------------------
// Cookie pool management (developer-only — see /cookie command above)
// ---------------------------------------------------------------------------
function _renderCookieList(slots, max) {
    if (!slots || !slots.length) {
        return "ما كاينش حتى كوكي.\nصيفط `/cookie add` مع ملف cookies.txt.";
    }
    const lines = slots.map((s) => {
        const tag = s.sick ? "🔴 (sick)" : "🟢";
        const remain = s.sick_remaining_secs
            ? ` — back in ${Math.round(s.sick_remaining_secs / 60)}m`
            : "";
        return `*${s.slot}.* ${tag} ${s.preview} (${s.size}b)${remain}`;
    });
    return `الكوكيز فالـ pool (${slots.length}/${max}):\n\n${lines.join("\n")}`;
}

async function handleCookieCommand(msg, from, action, slot) {
    try {
        if (action === "list" || action === "ls" || action === "status") {
            const data = await callAdmin("/admin/cookies");
            await safeSend(
                from,
                { text: _renderCookieList(data.slots, data.max_slots) },
                { quoted: msg }
            );
            return;
        }

        if (action === "add" || action === "set" || action === "upload") {
            const file = await extractFile(msg);
            if (!file) {
                await safeSend(
                    from,
                    {
                        text:
                            "صيفط ملف cookies.txt (أو JSON export) فنفس الرسالة\n" +
                            "مع caption: `/cookie add` (ولا `/cookie add 3` لخانة 3).",
                    },
                    { quoted: msg }
                );
                return;
            }
            const fields = {};
            if (slot) fields.slot = slot;
            const data = await callAdmin("/admin/cookies", {
                method: "POST",
                fields,
                file,
            });
            const status = data.test_ok
                ? "🟢 اختبار ناجح مع Gemini"
                : `🔴 الكوكي ما خدماتش: ${data.test_error}`;
            await safeSend(
                from,
                {
                    text:
                        `تمت إضافة الكوكي فالخانة *${data.slot}*.\n` +
                        `إجمالي الكوكيز: ${data.used}/10\n${status}`,
                },
                { quoted: msg }
            );
            return;
        }

        if (action === "del" || action === "rm" || action === "remove" || action === "delete") {
            if (!slot) {
                await safeSend(
                    from,
                    { text: "خاصني رقم الخانة. مثال: `/cookie del 3`" },
                    { quoted: msg }
                );
                return;
            }
            const data = await callAdmin(`/admin/cookies/${slot}`, { method: "DELETE" });
            await safeSend(
                from,
                { text: `تم مسح الخانة ${data.slot}. الباقي: ${data.used}/10` },
                { quoted: msg }
            );
            return;
        }

        if (action === "test" || action === "check" || action === "ping") {
            const fields = slot ? { slot } : null;
            const data = await callAdmin("/admin/cookies/test", {
                method: "POST",
                fields,
            });
            const lines = (data.results || []).map((r) =>
                r.ok
                    ? `*${r.slot}.* 🟢 OK`
                    : `*${r.slot}.* 🔴 ${(r.error || "").slice(0, 80)}`
            );
            await safeSend(
                from,
                { text: lines.length ? lines.join("\n") : "ما كاين والو." },
                { quoted: msg }
            );
            return;
        }

        await safeSend(
            from,
            {
                text:
                    "أوامر الكوكي:\n" +
                    "• `/cookie list` — عرض الخانات\n" +
                    "• `/cookie add [N]` — أضف كوكي (مع ملف)\n" +
                    "• `/cookie del N` — مسح خانة\n" +
                    "• `/cookie test [N]` — اختبار",
            },
            { quoted: msg }
        );
    } catch (err) {
        console.error("Cookie command failed:", err.message);
        await safeSend(
            from,
            { text: `خطأ: ${err.message}` },
            { quoted: msg }
        );
    }
}

// ---------------------------------------------------------------------------
// Main bot
// ---------------------------------------------------------------------------
async function startBot() {
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
    const { version, isLatest } = await fetchLatestBaileysVersion();

    console.log(`Using WA v${version.join(".")}, isLatest: ${isLatest}`);
    console.log(`Gemini bridge URL: ${GEMINI_SERVER}`);

    const sock = makeWASocket({
        version,
        logger,
        printQRInTerminal: false,
        auth: {
            creds: state.creds,
            keys: makeCacheableSignalKeyStore(state.keys, logger),
        },
        browser: Browsers.macOS("Safari"),
        generateHighQualityLinkPreview: true,
    });

    if (!sock.authState.creds.registered) {
        let phoneNumber = process.env.PHONE_NUMBER || fileConfig.PHONE_NUMBER || "";

        if (!phoneNumber) {
            // Only ask interactively when stdin is a real terminal (local dev)
            if (process.stdin.isTTY) {
                phoneNumber = await question(
                    "Enter your phone number with country code (e.g. 14155552671): "
                );
            } else {
                console.error(
                    "[ERROR] Session not registered and no PHONE_NUMBER env var set.\n" +
                    "Set the PHONE_NUMBER environment variable (e.g. 212612345678) " +
                    "or pair locally first and commit the auth_info folder."
                );
                process.exit(1);
            }
        }

        phoneNumber = phoneNumber.replace(/[^0-9]/g, "");

        if (!phoneNumber) {
            console.log("Invalid phone number. Please restart and try again.");
            process.exit(1);
        }

        try {
            const code = await sock.requestPairingCode(phoneNumber);
            const formatted = code?.match(/.{1,4}/g)?.join("-") || code;
            console.log(`\nYour pairing code: ${formatted}\n`);
            console.log(
                "Open WhatsApp > Settings > Linked Devices > Link a Device > Link with phone number"
            );
        } catch (err) {
            console.error("Failed to request pairing code:", err);
            process.exit(1);
        }
    }

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", (update) => {
        const { connection, lastDisconnect } = update;

        if (connection === "close") {
            const statusCode =
                lastDisconnect?.error?.output?.statusCode ||
                lastDisconnect?.error?.output?.payload?.statusCode;
            const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

            console.log(
                "Connection closed:",
                lastDisconnect?.error?.message || "unknown",
                "| Reconnecting:",
                shouldReconnect
            );

            if (shouldReconnect) {
                setTimeout(startBot, 3000);
            } else {
                console.log("Logged out. Delete the auth_info folder and restart to pair again.");
                process.exit(0);
            }
        } else if (connection === "open") {
            console.log("WhatsApp bot connected successfully!");
        }
    });

    // Module-level helpers (performDownload, performImageGen, executeToolCall)
    // need access to safeSend, so we expose it on the global socket reference.
    sockRef = sock;

    sock.ev.on("messages.upsert", async ({ messages, type }) => {
        if (type !== "notify") return;

        for (const msg of messages) {
            try {
                await handleMessage(msg);
            } catch (err) {
                console.error("Unhandled error in message handler:", err?.message || err);
            }
        }
    });

    async function handleMessage(msg) {
        if (!msg.message || msg.key.fromMe) return;

        const from = msg.key.remoteJid;
        if (!from || from.endsWith("@g.us")) return; // ignore groups for now

            const text = extractText(msg);
            const lower = text.trim().toLowerCase();

        console.log(`Message from ${from}: ${text || "(media)"}`);

        // Built-in quick commands
        if (lower === "ping") {
            await safeSend(from, { text: "pong" }, { quoted: msg });
            return;
        }
        if (lower === "/reset" || lower === "reset" || lower === "نسيان") {
            try {
                await callGemini("/reset", { user: from });
                await safeSend(from, { text: "تم مسح الذاكرة." }, { quoted: msg });
            } catch (err) {
                await safeSend(
                    from,
                    { text: `Reset failed: ${err.message}` },
                    { quoted: msg }
                );
            }
            return;
        }

        // -----------------------------------------------------------------
        // Developer-only cookie pool management. Recognized variants:
        //   /cookie list                 → show all slots and health
        //   /cookie add [N]              → upload an attached cookies.txt
        //   /cookie del N | /cookie rm N → remove slot N
        //   /cookie test [N]             → ping Gemini with slot N (or all)
        // Only the configured DEVELOPER_NUMBER may run these.
        // -----------------------------------------------------------------
        const cookieMatch = text.trim().match(/^\/cookie(?:s)?\s+(\w+)(?:\s+(\d+))?\s*$/i);
        if (cookieMatch) {
            if (!isDeveloper(msg)) {
                await safeSend(
                    from,
                    { text: "هاد الأمر خاص بالمطور فقط." },
                    { quoted: msg }
                );
                return;
            }
            await handleCookieCommand(
                msg,
                from,
                cookieMatch[1].toLowerCase(),
                cookieMatch[2] ? parseInt(cookieMatch[2], 10) : null
            );
            return;
        }

        // -----------------------------------------------------------------
        // Power-user shortcut commands. These bypass Omar entirely.
        //   /dl /video /vid <url|query> [360|480|720|1080]   → video
        //   /mp3 /audio /song <url|query>                    → audio
        //   /image /img /sora /صورة <prompt>                 → generate image
        // For everything else (including natural-language requests in any
        // language, plain URLs, and ordinary chat) we let Omar decide what
        // to do. Omar emits <tool>{...}</tool> blocks which we execute.
        // -----------------------------------------------------------------
        const dlMatch = text.match(
            /^\/(dl|download|video|vid|mp3|audio|song)\s+(.+)/i
        );
        if (dlMatch) {
            const cmd = dlMatch[1].toLowerCase();
            let rest = dlMatch[2].trim();
            const mode = ["mp3", "audio", "song"].includes(cmd) ? "audio" : "video";
            let quality = "720";
            const qMatch = rest.match(/\s+(360|480|720|1080)$/);
            if (qMatch) {
                quality = qMatch[1];
                rest = rest.slice(0, qMatch.index).trim();
            }
            await performDownload(sock, msg, from, rest, mode, quality);
            return;
        }

        const imgMatch = text.match(/^\/(image|img|sora|صورة)\s+(.+)/i);
        if (imgMatch) {
            const refFile = await extractFile(msg);
            await performImageGen(sock, msg, from, imgMatch[2].trim(), refFile);
            return;
        }

        // Sticker shortcut: user attaches/quotes an image and says
        // "sticker", "ستيكر", "stiker", or "/sticker". Convert the image
        // directly without going through Omar.
        const stickerTrigger = /^\s*[\/!]?\s*(sticker|stiker|ستيكر|ملصق)\s*$/i;
        if (stickerTrigger.test((text || "").trim())) {
            const refFile = await extractFile(msg);
            if (refFile && refFile.bytes && (refFile.mime || "").startsWith("image/")) {
                await performStickerFromBuffer(sock, msg, from, refFile.bytes, refFile.mime);
                return;
            }
            // No image attached → fall through (let Omar handle it / ask).
        }

        // -----------------------------------------------------------------
        // Default path: send the message (and any attached media) to Omar.
        // Then look for <tool>...</tool> blocks in his reply and execute
        // them. Whatever text he wrote outside the tool blocks is sent
        // back to the user as a normal message first.
        // -----------------------------------------------------------------
        const file = await extractFile(msg);
        if (!text && !file) return;

        // For audio-only messages, prepend a Darija system note so Omar
        // replies as if hearing a friend (not summarizing/describing it).
        let effectiveText = text;
        if (!effectiveText && file && (file.mime || "").startsWith("audio/")) {
            effectiveText =
                "هاد المستخدم بعت لي رسالة صوتية بلا نص. استمع للأوديو " +
                "وجاوب طبيعي بحال أنك صديق فمحادثة معه — رد على اللي قال " +
                "(مثلاً إيلا قال 'السلام عليكم' جاوب 'وعليكم السلام'، " +
                "إيلا سأل سؤال جاوبو مباشرة). ممنوع منعاً باتاً تلخص " +
                "أو توصف الأوديو ('في التسجيل قال...'، 'محتوى التسجيل...'، " +
                "'تأكيد...'). فقط رد طبيعي بحال محادثة WhatsApp عادية.";
        }

        try {
            await sock.sendPresenceUpdate("composing", from).catch(() => {});
            const data = await callGemini(
                "/ask",
                { user: from, text: effectiveText },
                file
            );

            const reply = data.text || "";
            const { cleanText, calls } = parseToolCalls(reply);

            // If Omar wrote a short intro sentence AND there is exactly
            // one tool call that produces a captionable attachment (image,
            // video, document...), use that sentence as the file's caption
            // instead of sending it as a separate text message. This gives
            // the user one combined WhatsApp message instead of two.
            const captionable = calls.filter(callProducesCaptionableAttachment);
            const inlineCaption =
                cleanText && calls.length === 1 && captionable.length === 1
                    ? cleanText
                    : null;

            if (cleanText && !inlineCaption) {
                await safeSend(from, { text: cleanText }, { quoted: msg });
            }

            for (const call of calls) {
                await executeToolCall(sock, msg, from, call, inlineCaption);
            }

            // Note: we deliberately do NOT forward Gemini's inline image_urls
            // here. On the /ask path those are almost always related-search
            // results (e.g. when describing an uploaded photo Gemini attaches
            // visually similar pictures), not something Omar meant to send.
            // The only legitimate way for Omar to send an image is via an
            // explicit <tool>{"name":"image",...}</tool> call.
        } catch (err) {
            console.error("Gemini call failed:", err.message);
            await safeSend(
                from,
                { text: `وقع خطأ: ${err.message}` },
                { quoted: msg }
            );
        }
    }
}

process.on("unhandledRejection", (reason) => {
    console.error("Unhandled rejection:", reason?.message || reason);
});
process.on("uncaughtException", (err) => {
    console.error("Uncaught exception:", err?.message || err);
});

startBot().catch((err) => {
    console.error("Fatal error:", err);
    process.exit(1);
});
