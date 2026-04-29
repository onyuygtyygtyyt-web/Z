################################################################################
# Omar — WhatsApp + Gemini Bot — Hugging Face Spaces (Docker SDK)
#
# Two processes inside one container:
#   1. Gunicorn ............. Flask Gemini bridge on internal port 5000
#   2. Node + Baileys ....... WhatsApp client (talks to localhost:5000)
#   3. Tiny HTTP server ..... exposed on $PORT (default 7860) so the HF
#                              reverse proxy can reach the container.
#
# Build & run locally:
#   docker build -t omar-bot .
#   docker run --rm -it -p 7860:7860 \
#       -e PHONE_NUMBER=212688898322 \
#       -e DEVELOPER_NUMBER=212688898322 \
#       -v $PWD/data:/data \
#       omar-bot
################################################################################
FROM node:20-bookworm-slim

# --- system deps (Python + ffmpeg + curl for healthcheck) ---------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        ffmpeg \
        curl ca-certificates \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# --- non-root user (Hugging Face requires UID 1000) ---------------------------
# On HF Spaces, UID 1000 may already exist as a different name (e.g. "node").
# Remove that user first, then create our own "user" with UID 1000.
RUN if id -u 1000 >/dev/null 2>&1; then \
        existing=$(getent passwd 1000 | cut -d: -f1); \
        userdel -r "$existing" 2>/dev/null || userdel "$existing" 2>/dev/null || true; \
    fi && \
    if getent group 1000 >/dev/null 2>&1; then \
        existing_grp=$(getent group 1000 | cut -d: -f1); \
        groupdel "$existing_grp" 2>/dev/null || true; \
    fi && \
    useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR /home/user/app

# --- Python deps via venv (avoids PEP 668 "externally managed" lock) ---------
RUN python3 -m venv /home/user/venv
ENV PATH=/home/user/venv/bin:$PATH

COPY --chown=user requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# --- Node deps ---------------------------------------------------------------
COPY --chown=user package.json package-lock.json* ./
RUN npm install --omit=dev

# --- App source --------------------------------------------------------------
COPY --chown=user . .

# --- Runtime config ----------------------------------------------------------
# Persistent state (WhatsApp session + Gemini cookies) lives under /data so
# you can mount HF persistent storage there. When /data isn't mounted the
# bot still works, you'll just have to re-pair WhatsApp on every restart.
ENV PORT=7860 \
    GEMINI_SERVER=http://127.0.0.1:5000 \
    AUTH_DIR=/data/auth \
    COOKIES_DIR=/data/cookies \
    DEVELOPER_NUMBER=212688898322

# Make /data writable by UID 1000 even when not mounted as a volume
USER root
RUN mkdir -p /data /data/auth /data/cookies && chown -R user:user /data
USER user

EXPOSE 7860

CMD ["bash", "start.sh"]
