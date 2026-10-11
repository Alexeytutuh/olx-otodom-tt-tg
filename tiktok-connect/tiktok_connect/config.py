"""Configuration from environment variables. Secrets are never logged."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Official endpoints (see README "Verified API facts").
DEFAULT_AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
DEFAULT_API_BASE = "https://open.tiktokapis.com"
SCOPES = ("user.info.basic", "video.upload")


class ConfigError(RuntimeError):
    pass


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is None:
        return default
    val = val.strip()
    return val if val else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


@dataclass(frozen=True)
class Config:
    client_key: str
    client_secret: str = field(repr=False)
    public_base_url: str
    redirect_uri: str
    data_dir: Path
    host: str = "0.0.0.0"
    port: int = 8090
    api_base: str = DEFAULT_API_BASE
    authorize_url: str = DEFAULT_AUTHORIZE_URL
    display_tz: str = "Europe/Warsaw"
    state_ttl_seconds: int = 600
    media_retention_days: int = 14
    refresh_margin_seconds: int = 3600
    http_timeout: int = 20
    internal_token: str = field(default="", repr=False)  # shared secret for POST /internal/draft (bot)

    @property
    def tokens_file(self) -> Path:
        return self.data_dir / "tokens.json"

    @property
    def lock_file(self) -> Path:
        return self.data_dir / ".tokens.lock"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def secure_cookies(self) -> bool:
        return self.public_base_url.startswith("https://")

    def media_url(self, rel_path: str) -> str:
        return f"{self.public_base_url}/media/{rel_path.lstrip('/')}"


def load_config(require_secrets: bool = True) -> Config:
    client_key = _env("TIKTOK_CLIENT_KEY", "") or ""
    client_secret = _env("TIKTOK_CLIENT_SECRET", "") or ""
    if require_secrets and (not client_key or not client_secret):
        raise ConfigError("TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET must be set (env file)")
    public_base_url = (_env("PUBLIC_BASE_URL", "https://media.example.com") or "").rstrip("/")
    redirect_uri = _env("REDIRECT_URI", f"{public_base_url}/tiktok/callback") or ""
    if not public_base_url.startswith(("https://", "http://")):
        raise ConfigError("PUBLIC_BASE_URL must be an absolute URL")
    return Config(
        client_key=client_key,
        client_secret=client_secret,
        public_base_url=public_base_url,
        redirect_uri=redirect_uri,
        data_dir=Path(_env("DATA_DIR", "/data") or "/data"),
        host=_env("HOST", "0.0.0.0") or "0.0.0.0",
        port=_env_int("PORT", 8090),
        api_base=(_env("TIKTOK_API_BASE", DEFAULT_API_BASE) or DEFAULT_API_BASE).rstrip("/"),
        authorize_url=_env("TIKTOK_AUTHORIZE_URL", DEFAULT_AUTHORIZE_URL) or DEFAULT_AUTHORIZE_URL,
        display_tz=_env("DISPLAY_TZ", "Europe/Warsaw") or "UTC",
        state_ttl_seconds=_env_int("STATE_TTL_SECONDS", 600),
        media_retention_days=_env_int("MEDIA_RETENTION_DAYS", 14),
        refresh_margin_seconds=_env_int("REFRESH_MARGIN_SECONDS", 3600),
        http_timeout=_env_int("HTTP_TIMEOUT", 20),
        internal_token=_env("INTERNAL_TOKEN", "") or "",
    )


def setup_logging() -> None:
    level = getattr(logging, (_env("LOG_LEVEL", "INFO") or "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
