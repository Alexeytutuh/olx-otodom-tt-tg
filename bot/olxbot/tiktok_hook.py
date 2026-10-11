"""Push generated slides to the TikTok inbox as a DRAFT via the separate `tiktok-connect` service
(POST http://tiktok-connect:8090/internal/draft over the docker network n8n_default).

TikTok limits: title <= 90 UTF-16 units, description <= 4000, JPEG <= 1080p, at most 5 pending
(unopened) drafts per 24 h, 6 init requests/min. tiktok-connect turns TikTok error codes into hints."""
from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Protocol

log = logging.getLogger("olxbot.tiktok")

ERRORS_UA = {
    "spam_risk_too_many_pending_share": "у TikTok уже 5 невідкритих чернеток за добу. Відкрий або видали старі "
                                        "у вхідних TikTok і спробуй ще раз",
    "not_connected": "TikTok-акаунт не підключено: відкрий сторінку tiktok-connect і натисни «Підключити TikTok»",
    "access_token_invalid": "TikTok відкликав доступ: перепідключи акаунт на сторінці tiktok-connect",
    "scope_not_authorized": "при підключенні не дали дозвіл на завантаження: перепідключи акаунт і дозволь усе",
    "app_version_check_failed": "онови застосунок TikTok до 31.8 або новішого",
    "rate_limit_exceeded": "забагато запитів до TikTok, спробуй за хвилину",
    "photo_pull_failed": "TikTok не зміг завантажити фото з сервера tiktok-connect (PUBLIC_BASE_URL)",
    "busy": "інша чернетка ще відправляється, спробуй за хвилину",
}


class DraftError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def human(self) -> str:
        return ERRORS_UA.get(self.code) or f"{self.code}: {self.message[:200]}"


class DraftSink(Protocol):
    def push_draft(self, slides: list[Path], title: str, description: str) -> str: ...


class TikTokConnectClient:
    def __init__(self, base_url: str, token: str, timeout: float = 150):
        self.url = base_url.rstrip("/") + "/internal/draft"
        self.token = token
        self.timeout = timeout

    def push_draft(self, slides: list[Path], title: str, description: str) -> str:
        """-> TikTok status (SEND_TO_USER_INBOX normally). Raises DraftError."""
        body = json.dumps({"title": title, "description": description,
                           "images": [base64.b64encode(p.read_bytes()).decode() for p in slides]}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                res = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                res = json.loads(e.read())
            except ValueError:
                raise DraftError(f"http_{e.code}", "tiktok-connect error") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise DraftError("connect_unreachable", f"tiktok-connect недоступний ({e})") from None
        if not res.get("ok"):
            raise DraftError(res.get("error") or "unknown", res.get("message") or "")
        return res.get("status") or "?"


def get_sink(settings) -> Optional[DraftSink]:
    if not getattr(settings, "tiktok_connect_enabled", False):
        return None
    if not settings.tiktok_connect_url or not settings.tiktok_connect_token:
        log.warning("TIKTOK_CONNECT_ENABLED=true, but TIKTOK_CONNECT_URL / TIKTOK_CONNECT_TOKEN are empty; "
                    "the TikTok button is off")
        return None
    return TikTokConnectClient(settings.tiktok_connect_url, settings.tiktok_connect_token)
