"""HTTP client, errors, health tracking and small helpers shared by both parsers."""
from __future__ import annotations

import datetime as dt
import html as html_lib
import json
import logging
import random
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

from . import config

log = logging.getLogger("parsers")

try:  # preferred transport: real browser TLS fingerprint
    from curl_cffi import requests as cffi_requests  # type: ignore
except Exception:  # pragma: no cover - depends on environment
    cffi_requests = None


class SourceDownError(Exception):
    """The source is unusable right now (blocked, captcha, structure change, outage).

    kind: blocked | rate_limited | captcha | http | network | structure
    The future bot can alert:  f"{err.source} відвалився: {err.reason}".
    """

    def __init__(self, source: str, kind: str, reason: str, status: Optional[int] = None):
        super().__init__(f"{source}: [{kind}] {reason}")
        self.source, self.kind, self.reason, self.status = source, kind, reason, status


@dataclass
class Response:
    status: int
    text: str
    url: str
    elapsed: float


CAPTCHA_MARKERS = [
    "captcha", "cf-chl", "challenge-platform", "px-captcha", "perimeterx",
    "access denied", "request unsuccessful", "incapsula", "are you a robot",
    "jesteś robotem", "datadome", "_incapsula_resource",
]


def looks_like_captcha(text: str) -> Optional[str]:
    low = text[:200000].lower()
    for m in CAPTCHA_MARKERS:
        if m in low:
            return m
    return None


class HttpClient:
    """Small GET-only client: curl_cffi (Chrome impersonation) with curl subprocess fallback,
    timeouts, retries with exponential backoff + jitter, polite delay between requests."""

    def __init__(self, source: str, transport: Optional[str] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 delay: tuple[float, float] = config.REQUEST_DELAY_S,
                 retries: int = config.RETRIES):
        self.source = source
        self.sleep = sleep
        self.delay = delay
        self.retries = retries
        self.requests = 0
        self._last_request_at = 0.0
        if transport is None:
            transport = "curl_cffi" if cffi_requests else ("curl" if shutil.which("curl") else "none")
        self.transport = transport
        self._session = None
        if transport == "curl_cffi":
            self._session = cffi_requests.Session(impersonate=config.CURL_CFFI_IMPERSONATE)

    # -- transports ---------------------------------------------------------
    def _headers(self, accept: str) -> dict:
        h = {"Accept": accept, "Accept-Language": config.ACCEPT_LANGUAGE}
        if self.transport != "curl_cffi":       # curl_cffi sets a matching Chrome UA itself
            h["User-Agent"] = config.USER_AGENT
        return h

    def _do_cffi(self, url: str, accept: str) -> Response:
        t = time.monotonic()
        r = self._session.get(url, headers=self._headers(accept),
                              timeout=(config.CONNECT_TIMEOUT_S, config.REQUEST_TIMEOUT_S),
                              allow_redirects=True)
        return Response(r.status_code, r.text, str(r.url), time.monotonic() - t)

    def _do_curl(self, url: str, accept: str) -> Response:
        t = time.monotonic()
        cmd = ["curl", "-sS", "-L", "--compressed", "--connect-timeout", str(config.CONNECT_TIMEOUT_S),
               "-m", str(config.REQUEST_TIMEOUT_S), "-w", "\n%{http_code}"]
        for k, v in self._headers(accept).items():
            cmd += ["-H", f"{k}: {v}"]
        cmd.append(url)
        r = subprocess.run(cmd, capture_output=True, timeout=config.REQUEST_TIMEOUT_S + 15)
        if r.returncode != 0:
            raise OSError(f"curl exit {r.returncode}: {r.stderr.decode(errors='replace').strip()[:200]}")
        body, _, code = r.stdout.decode("utf-8", errors="replace").rpartition("\n")
        return Response(int(code) if code.strip().isdigit() else 0, body, url, time.monotonic() - t)

    # -- public -------------------------------------------------------------
    def _polite_wait(self):
        if self._last_request_at:
            gap = random.uniform(*self.delay) - (time.monotonic() - self._last_request_at)
            if gap > 0:
                self.sleep(gap)

    def get(self, url: str, accept: str = "text/html,application/xhtml+xml,*/*;q=0.8",
            ok_statuses=(200,)) -> Response:
        """Return Response with an OK status or raise SourceDownError after retries.
        404/410 are returned to the caller (not a source outage)."""
        if self.transport == "none":
            raise SourceDownError(self.source, "network", "no HTTP transport (install curl_cffi or curl)")
        last: Optional[SourceDownError] = None
        for attempt in range(self.retries):
            self._polite_wait()
            self.requests += 1
            try:
                resp = (self._do_cffi if self.transport == "curl_cffi" else self._do_curl)(url, accept)
            except Exception as e:  # network / timeout
                last = SourceDownError(self.source, "network", f"{type(e).__name__}: {str(e)[:200]}")
                resp = None
            finally:
                self._last_request_at = time.monotonic()
            if resp is not None:
                if resp.status in ok_statuses or resp.status in (404, 410):
                    return resp
                marker = looks_like_captcha(resp.text) if resp.status in (403, 429, 503) else None
                if resp.status == 429:
                    last = SourceDownError(self.source, "rate_limited", "HTTP 429 Too Many Requests", 429)
                elif resp.status == 403:
                    last = SourceDownError(self.source, "captcha" if marker else "blocked",
                                           f"HTTP 403 Forbidden" + (f" (marker: {marker})" if marker else ""), 403)
                elif marker:                      # e.g. 503 challenge pages (Cloudflare / DataDome)
                    last = SourceDownError(self.source, "captcha", f"HTTP {resp.status} anti-bot page "
                                           f"(marker: {marker})", resp.status)
                else:
                    last = SourceDownError(self.source, "http", f"HTTP {resp.status}", resp.status)
            log.warning("%s GET failed (attempt %d/%d): %s", self.source, attempt + 1, self.retries, last)
            if attempt + 1 < self.retries:
                wait = config.BACKOFF_BASE_S * 2 ** attempt + random.uniform(0, 1.5)
                if last and last.kind in ("rate_limited", "blocked", "captcha"):
                    wait += config.BACKOFF_BLOCKED_S * (attempt + 1)
                self.sleep(wait)
        assert last is not None
        raise last


@dataclass
class Health:
    source: str
    last_success_at: Optional[str] = None
    last_attempt_at: Optional[str] = None
    consecutive_failures: int = 0
    total_runs: int = 0
    total_failures: int = 0
    last_error: Optional[str] = None
    last_error_kind: Optional[str] = None
    last_run_seconds: Optional[float] = None
    last_summary: dict = field(default_factory=dict)

    def ok(self, seconds: float, summary: dict):
        now = now_iso()
        self.last_attempt_at = self.last_success_at = now
        self.consecutive_failures = 0
        self.total_runs += 1
        self.last_run_seconds = round(seconds, 2)
        self.last_summary = summary

    def fail(self, err: Exception, seconds: float):
        self.last_attempt_at = now_iso()
        self.consecutive_failures += 1
        self.total_runs += 1
        self.total_failures += 1
        self.last_run_seconds = round(seconds, 2)
        self.last_error = getattr(err, "reason", None) or f"{type(err).__name__}: {err}"
        self.last_error_kind = getattr(err, "kind", "unexpected")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = "ok" if self.consecutive_failures == 0 and self.last_success_at else (
            "never_ran" if not self.last_attempt_at else "failing")
        return d


# ---------------------------------------------------------------- helpers
def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def now_iso() -> str:
    return iso_local(now_utc().replace(microsecond=0))


def parse_dt(s) -> Optional[dt.datetime]:
    if not s or not isinstance(s, str):
        return None
    s = s.strip().replace(" ", "T", 1) if re.match(r"\d{4}-\d\d-\d\d \d", s.strip()) else s.strip()
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


_BLOCK_TAGS = re.compile(r"<\s*(br|/p|/div|/li|/h\d|/tr)\b[^>]*>", re.I)
_LI = re.compile(r"<\s*li\b[^>]*>", re.I)


def strip_html(s: Optional[str]) -> str:
    """HTML -> plain text. Keeps paragraph breaks (max one blank line), collapses spaces."""
    if not s:
        return ""
    s = _BLOCK_TAGS.sub("\n", s)
    s = _LI.sub("\n- ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html_lib.unescape(s).replace("\ufeff", "").replace("\xa0", " ").replace("\u200b", "")
    s = s.replace("\r", "")
    lines = [re.sub(r"[ \t\f\v]+", " ", ln).strip() for ln in s.split("\n")]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return out.strip()


def extract_next_data(html_text: str, source: str) -> dict:
    """Return parsed __NEXT_DATA__ or raise SourceDownError with a precise reason."""
    m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html_text, re.S)
    if not m:
        marker = looks_like_captcha(html_text)
        if marker:
            raise SourceDownError(source, "captcha", f"captcha/anti-bot page (marker: {marker})")
        raise SourceDownError(source, "structure", "__NEXT_DATA__ script missing from page "
                                                   f"(len={len(html_text)})")
    try:
        return json.loads(m.group(1))
    except ValueError as e:
        raise SourceDownError(source, "structure", f"__NEXT_DATA__ is not valid JSON: {e}")


def dig(obj, *path, default=None):
    for p in path:
        if isinstance(obj, dict):
            obj = obj.get(p)
        elif isinstance(obj, list) and isinstance(p, int) and -len(obj) <= p < len(obj):
            obj = obj[p]
        else:
            return default
        if obj is None:
            return default
    return obj


def to_float(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("\xa0", "").replace(" ", "").replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def iso_local(t: Optional[dt.datetime]) -> Optional[str]:
    if t is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        return t.astimezone(ZoneInfo(config.TIMEZONE)).isoformat()
    except Exception:  # tzdata missing
        return t.isoformat()


def num_or_int(v: Optional[float]):
    if v is None:
        return None
    return int(v) if float(v).is_integer() else round(v, 2)
