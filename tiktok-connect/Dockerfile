# tiktok-connect — tiny stdlib HTTP service + Pillow for image conversion
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    PORT=8090

# DejaVu font so demo slides can render Ukrainian text
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tiktok_connect ./tiktok_connect

RUN useradd --system --uid 10001 --user-group --home-dir /app --shell /usr/sbin/nologin app \
 && mkdir -p /data && chown 10001:10001 /data

USER 10001:10001
EXPOSE 8090

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=4).status == 200 else 1)"]

CMD ["python", "-m", "tiktok_connect"]
