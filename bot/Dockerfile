FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    TZ=Europe/Warsaw

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && python -c "import curl_cffi, PIL; print('deps ok', PIL.__version__)"

COPY parsers/ parsers/
COPY olxbot/ olxbot/
RUN rm -rf parsers/tests \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin bot \
    && mkdir -p /data && chown bot:bot /data

USER bot
VOLUME /data

# healthy = heartbeat file refreshed in the last 20 min. The file stops being refreshed as soon as any
# internal thread hangs; the internal watchdog then exits the process and `restart: unless-stopped`
# brings it back (Docker itself does not restart "unhealthy" containers).
HEALTHCHECK --interval=2m --timeout=10s --start-period=5m --retries=3 \
  CMD python -c "import os,sys,time; p='/data/heartbeat'; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 1200 else 1)"

CMD ["python", "-m", "olxbot"]
