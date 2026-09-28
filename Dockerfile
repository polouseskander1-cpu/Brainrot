# Brainrot Bot on a server (Linux, Raspberry Pi 4/5, NAS...): see docs/SERVER.md
FROM denoland/deno:bin-2.9.7 AS deno

FROM python:3.12-slim
# ffmpeg with captions (libass), fonts for translated captions, rclone for cloud folders.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-core rclone ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
# Deno runs YouTube's JavaScript for yt-dlp (video links).
COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY brainrot.py config.yaml ./
COPY brainrot_bot ./brainrot_bot
COPY fonts ./fonts
COPY assets ./assets
# The bot runs as the owner of the data folder when that isn't root (see docs/SERVER.md): let it keep
# its lock and work files here, and compile the code now since it can't write here later.
RUN python -m compileall -q brainrot_bot && mkdir -p .work && chmod 1777 /app /app/.work

# Everything you keep (settings, logins, clips, gameplay, reels, the speech model) lives in /data.
ENV BRAINROT_SERVER=1 \
    BRAINROT_MODELS_DIR=/data/models \
    RCLONE_CONFIG=/data/rclone.conf \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8770
HEALTHCHECK --interval=5m --timeout=30s CMD python /app/brainrot.py --config /data/config.yaml --status | grep -q "is running" || exit 1
ENTRYPOINT ["python", "/app/brainrot.py", "--config", "/data/config.yaml"]
CMD ["--run"]
