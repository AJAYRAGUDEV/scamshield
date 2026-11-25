# ScamShield for Render (or any Docker host). Python 3.12 + FFmpeg (ffprobe validates uploaded videos).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=production

# ffprobe comes with the Debian "ffmpeg" package and ends up on PATH (the default FFPROBE_PATH=ffprobe works).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Only what the server needs. Secrets (.env), tests, legacy backups and docs are not copied (see .dockerignore).
COPY app ./app
COPY frontend ./frontend
COPY server.py .

# Run as an unprivileged user. Uploads live in the container's temp directory and are deleted after each analysis.
RUN useradd --create-home --uid 10001 scamshield && chown -R scamshield /app
USER scamshield

# Render injects PORT. One process only: video jobs and rate-limit counters live in this process's memory.
# --no-proxy-headers: client addresses are taken from X-Forwarded-For only for the proxies listed in TRUSTED_PROXY_IPS.
EXPOSE 10000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000} --no-proxy-headers"]
