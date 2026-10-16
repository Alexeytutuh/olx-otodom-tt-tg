"""Runtime settings from environment variables (.env). Search criteria live in parsers/config.py."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    v = os.environ.get(name, "").strip()
    try:
        return int(v) if v else default
    except ValueError:
        raise SystemExit(f"Env {name} must be an integer, got {v!r}")


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on", "так")


def _ids(name: str) -> set[int]:
    out = set()
    for part in os.environ.get(name, "").replace(";", ",").split(","):
        part = part.strip()
        if part:
            try:
                out.add(int(part))
            except ValueError:
                raise SystemExit(f"Env {name}: {part!r} is not a numeric chat id")
    return out


@dataclass
class Settings:
    token: str = ""
    api_base: str = "https://api.telegram.org"
    data_dir: str = "/data"
    allowed_chat_ids: set[int] = field(default_factory=set)
    owner_chat_id: int | None = None          # optional preset; otherwise first /start
    alert_chat_id: int | None = None          # default: owner chat
    interval_s: int = 300                     # fixed, aligned schedule (:00, :05, ...)
    sources: tuple[str, ...] = ("olx", "otodom")
    alert_after: int = 3                      # consecutive SourceDownError before alert (3 x 5 min = 15 min)
    realert_hours: int = 6
    hang_timeout_s: int = 900                 # internal watchdog: no progress -> exit -> restart
    seen_keep_days: int = 30
    max_posts_per_run: int = 15               # per source; the rest is sent on the next slot
    max_album_photos: int = 10
    max_slides: int = 10
    startup_summary: bool = True
    tiktok_connect_enabled: bool = False      # "🎵 В TikTok" button (draft at once), see olxbot/tiktok_hook.py
    tiktok_connect_url: str = ""
    tiktok_connect_token: str = ""            # = INTERNAL_TOKEN of tiktok-connect
    channel_id: str = ""                      # channel for CHANNEL_AUTOPOST: -100... or @username
    channel_autopost: bool = False            # every new offer goes to the channel at once (not possible duplicates)
    post_footer: str = ""                     # contact lines at the end of every post ("\n" = new line)
    log_level: str = "INFO"
    send_delay_s: float = 1.1                 # min gap between two messages in one chat
    updates_timeout: int = 50                 # getUpdates long-poll seconds
    max_runs: int = 0                         # tests only: stop after N scheduled runs (0 = forever)

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "bot.sqlite3")

    @property
    def heartbeat_path(self) -> str:
        return os.path.join(self.data_dir, "heartbeat")

    @property
    def tmp_dir(self) -> str:
        return os.path.join(self.data_dir, "tmp")


def load() -> Settings:
    s = Settings()
    s.token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    s.api_base = (os.environ.get("TELEGRAM_API_BASE", "").strip() or s.api_base).rstrip("/")
    s.data_dir = os.environ.get("DATA_DIR", "").strip() or s.data_dir
    s.allowed_chat_ids = _ids("ALLOWED_CHAT_IDS")
    owner = _ids("OWNER_CHAT_ID")
    s.owner_chat_id = next(iter(owner)) if owner else None
    alert = _ids("ALERT_CHAT_ID")
    s.alert_chat_id = next(iter(alert)) if alert else None
    s.interval_s = max(10, _int("POLL_INTERVAL_S", s.interval_s))
    src = tuple(x.strip().lower() for x in os.environ.get("SOURCES", "olx,otodom").split(",") if x.strip())
    bad = [x for x in src if x not in ("olx", "otodom")]
    if bad or not src:
        raise SystemExit(f"Env SOURCES: unknown source(s) {bad}; allowed: olx,otodom")
    s.sources = src
    s.alert_after = max(1, _int("ALERT_AFTER_FAILURES", s.alert_after))
    s.realert_hours = _int("REALERT_HOURS", s.realert_hours)
    s.hang_timeout_s = max(120, _int("HANG_TIMEOUT_S", s.hang_timeout_s))
    s.seen_keep_days = _int("SEEN_KEEP_DAYS", s.seen_keep_days)
    s.max_posts_per_run = max(1, _int("MAX_POSTS_PER_RUN", s.max_posts_per_run))
    s.startup_summary = _bool("STARTUP_SUMMARY", s.startup_summary)
    s.tiktok_connect_enabled = _bool("TIKTOK_CONNECT_ENABLED", False)
    s.tiktok_connect_url = os.environ.get("TIKTOK_CONNECT_URL", "").strip()
    s.tiktok_connect_token = os.environ.get("TIKTOK_CONNECT_TOKEN", "").strip()
    s.channel_id = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()
    s.channel_autopost = _bool("CHANNEL_AUTOPOST", False)
    s.post_footer = os.environ.get("POST_FOOTER", "").strip().replace("\\n", "\n")
    s.log_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    s.max_runs = _int("MAX_RUNS", 0)
    s.updates_timeout = max(1, min(50, _int("UPDATES_TIMEOUT_S", s.updates_timeout)))
    return s
