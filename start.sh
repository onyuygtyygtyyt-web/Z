#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Container entrypoint:
#   1. Make sure persistent dirs exist
#   2. Launch the Gemini bridge (gunicorn) on 127.0.0.1:5000
#   3. Launch the WhatsApp bot (Node + Baileys) — pairing code printed in logs
#   4. Run a tiny HTTP server on $PORT so HF's reverse proxy is happy
# ---------------------------------------------------------------------------
set -e

mkdir -p "${COOKIES_DIR:-/data/cookies}" "${AUTH_DIR:-/data/auth}"

# If the bot's hard-coded auth folder doesn't exist yet, symlink it to the
# persistent location (Baileys keeps session files in ./auth_info by default).
if [ ! -e /home/user/app/auth_info ]; then
    ln -sf "${AUTH_DIR}" /home/user/app/auth_info
fi

# 1. Backend
echo "[start] launching Gemini bridge on 127.0.0.1:5000 …"
gunicorn --bind 127.0.0.1:5000 \
         --workers 1 --threads 4 --timeout 180 \
         main:app &
BRIDGE_PID=$!

# Wait for the bridge to answer /health
for i in $(seq 1 30); do
    if curl -fs http://127.0.0.1:5000/health >/dev/null 2>&1; then
        echo "[start] bridge ready."
        break
    fi
    sleep 1
done

# 2. WhatsApp bot
echo "[start] launching WhatsApp bot …"
node index.js &
BOT_PID=$!

# 3. Keep-alive HTTP server on $PORT (HF reverse proxy will hit this).
PORT="${PORT:-7860}"
echo "[start] keep-alive HTTP server on 0.0.0.0:${PORT}"
exec node -e "
const http = require('http');
const { execSync } = require('child_process');
http.createServer((req, res) => {
    if (req.url === '/health') {
        try {
            execSync('curl -fs http://127.0.0.1:5000/health', { timeout: 3000 });
            res.writeHead(200); res.end('ok');
        } catch (e) {
            res.writeHead(503); res.end('bridge down');
        }
        return;
    }
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    res.end(\`
        <html><body style=\\\"font-family:system-ui;padding:32px;line-height:1.5\\\">
        <h2>Omar — WhatsApp + Gemini Bot</h2>
        <p>Container is alive. Check the Space logs to scan the WhatsApp pairing code.</p>
        <p><a href=\\\"/health\\\">/health</a></p>
        </body></html>
    \`);
}).listen(${PORT}, '0.0.0.0', () => console.log('[keep-alive] listening on ${PORT}'));
"
