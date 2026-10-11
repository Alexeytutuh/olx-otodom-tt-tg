"""HTTP server (stdlib ThreadingHTTPServer) for tiktok-connect."""

from __future__ import annotations

import base64
import binascii
import email.utils
import hmac
import json
import logging
import os
import shutil
import signal
import tempfile
import threading
import time
import urllib.parse
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, pages
from .config import SCOPES, Config, load_config, setup_logging
from .drafts import create_draft
from .media import CONTENT_TYPES, parse_range, resolve_media_path
from .oauth_state import StateStore, state_hash
from .tiktok_api import TikTokError
from .tokens import TokenManager, mask
from datetime import UTC

log = logging.getLogger("tiktok_connect.server")
STATE_COOKIE = "tt_oauth_state"
INTERNAL_MAX_BODY = 30 * 1024 * 1024  # 10 base64 JPEG slides fit easily

HTML_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
                               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _load_tz(name: str):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - tzdata missing etc.
        return UTC


class App:
    def __init__(self, config: Config, tokens: TokenManager | None = None):
        self.config = config
        self.tokens = tokens or TokenManager(config)
        self.states = StateStore(config.state_ttl_seconds)
        self.tz = _load_tz(config.display_tz)
        self.configured = bool(config.client_key and config.client_secret)
        self.draft_lock = threading.Lock()  # one draft at a time (TikTok: 6 init/min, 5 pending/24h)
        config.media_dir.mkdir(parents=True, exist_ok=True)

    def authorize_url(self, state: str) -> str:
        q = urllib.parse.urlencode({
            "client_key": self.config.client_key,
            "scope": ",".join(SCOPES),
            "response_type": "code",
            "redirect_uri": self.config.redirect_uri,
            "state": state,
        })
        return f"{self.config.authorize_url}?{q}"

    # ----------------------------------------------------------- maintenance
    def cleanup_media(self) -> int:
        cutoff = time.time() - self.config.media_retention_days * 86400
        removed = 0
        root = self.config.media_dir
        if not root.is_dir():
            return 0
        for entry in root.iterdir():
            try:
                if entry.is_dir() and not entry.is_symlink() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry)
                    removed += 1
            except OSError as exc:
                log.warning("media cleanup failed for %s: %s", entry.name, exc)
        if removed:
            log.info("Removed %d media batch dir(s) older than %d days", removed, self.config.media_retention_days)
        return removed

    def maintenance_loop(self, stop: threading.Event, interval: int = 900) -> None:
        last_cleanup = 0.0
        while not stop.is_set():
            if self.configured:
                try:
                    self.tokens.refresh_all_due()
                except Exception:  # noqa: BLE001
                    log.exception("token refresh loop error")
            if time.time() - last_cleanup > 6 * 3600:
                self.cleanup_media()
                last_cleanup = time.time()
            stop.wait(interval)


class Handler(BaseHTTPRequestHandler):
    server_version = "tiktok-connect/" + __version__
    sys_version = ""
    timeout = 30  # per-connection socket timeout (slowloris protection)
    app: App  # injected

    # ----------------------------------------------------------- helpers
    def log_message(self, fmt: str, *args) -> None:  # never log query strings (OAuth code!)
        pass

    def log_request(self, code="-", size="-") -> None:
        path = urllib.parse.urlsplit(self.path).path
        client = (self.headers.get("CF-Connecting-IP") or
                  (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip() or self.client_address[0])
        log.info('%s "%s %s" %s %s ua="%s"', client, self.command, path[:200], code, size,
                 (self.headers.get("User-Agent") or "-")[:120])

    def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_html(self, status: int, html_text: str, extra: dict | None = None) -> None:
        self._send(status, html_text.encode("utf-8"), "text/html; charset=utf-8", {**HTML_HEADERS, **(extra or {})})

    def send_json(self, status: int, obj: dict) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json",
                   {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    def send_text(self, status: int, text: str, extra: dict | None = None) -> None:
        self._send(status, text.encode(), "text/plain; charset=utf-8",
                   {"X-Content-Type-Options": "nosniff", **(extra or {})})

    def not_found(self) -> None:
        self.send_text(404, "Not found\n", {"Cache-Control": "no-store"})

    def _cookie(self, name: str) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            c = SimpleCookie()
            c.load(raw)
        except CookieError:
            return None
        return c[name].value if name in c else None

    def _state_cookie(self, value: str, max_age: int) -> str:
        parts = [f"{STATE_COOKIE}={value}", "Path=/tiktok/", f"Max-Age={max_age}", "HttpOnly", "SameSite=Lax"]
        if self.app.config.secure_cookies:
            parts.append("Secure")
        return "; ".join(parts)

    # ----------------------------------------------------------- dispatch
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        try:
            path = urllib.parse.urlsplit(self.path).path
            if path == "/":
                return self.page_home()
            if path == "/health":
                return self.send_json(200, {"status": "ok"})
            if path in ("/tiktok/connect", "/tiktok/connect/"):
                return self.page_connect()
            if path in ("/tiktok/callback", "/tiktok/callback/"):
                return self.page_callback()
            if path.startswith("/media/"):
                return self.serve_media(path[len("/media/"):])
            if path in ("/privacy", "/privacy/"):
                return self.send_html(200, pages.privacy_page(), {"Cache-Control": "public, max-age=3600"})
            if path in ("/terms", "/terms/"):
                return self.send_html(200, pages.terms_page(), {"Cache-Control": "public, max-age=3600"})
            if path == "/robots.txt":
                return self.send_text(200, "User-agent: *\nDisallow: /\n")
            return self.not_found()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("unhandled error")
            try:
                self.send_text(500, "Internal error\n")
            except OSError:
                pass

    def _method_not_allowed(self) -> None:
        self.send_text(405, "Method not allowed\n", {"Allow": "GET, HEAD"})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _method_not_allowed

    def do_POST(self) -> None:
        try:
            if urllib.parse.urlsplit(self.path).path == "/internal/draft":
                return self.internal_draft()
            return self._method_not_allowed()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("unhandled error")
            try:
                self.send_json(500, {"ok": False, "error": "internal_error", "message": "see tiktok-connect logs"})
            except OSError:
                pass

    # ----------------------------------------------------------- internal API (bot -> drafts)
    def internal_draft(self) -> None:
        """POST /internal/draft, only from the docker network (Caddy answers 404 for /internal/* outside).
        Header: Authorization: Bearer <INTERNAL_TOKEN>. JSON: {"title", "description", "images": [base64 JPEG]}."""
        token = self.app.config.internal_token
        if not token or self.headers.get("X-Forwarded-For") or self.headers.get("CF-Connecting-IP"):
            return self.not_found()  # disabled, or came through the reverse proxy
        auth = self.headers.get("Authorization") or ""
        if not hmac.compare_digest(auth.encode(), f"Bearer {token}".encode()):
            return self.send_json(401, {"ok": False, "error": "unauthorized", "message": "bad internal token"})
        if not self.app.configured:
            return self.send_json(503, {"ok": False, "error": "not_configured", "message": "TikTok keys not set"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not 0 < length <= INTERNAL_MAX_BODY:
            return self.send_json(413, {"ok": False, "error": "bad_size", "message": f"body size {length}"})
        try:
            req = json.loads(self.rfile.read(length))
            images = [base64.b64decode(b, validate=True) for b in req["images"]]
            title, description = str(req.get("title") or ""), str(req.get("description") or "")
        except (ValueError, KeyError, TypeError, binascii.Error) as exc:
            return self.send_json(400, {"ok": False, "error": "bad_request", "message": str(exc)[:200]})
        if not self.app.draft_lock.acquire(timeout=120):
            return self.send_json(429, {"ok": False, "error": "busy", "message": "another draft is in progress"})
        try:
            with tempfile.TemporaryDirectory(prefix="tt-draft-") as tmp:
                paths = []
                for i, data in enumerate(images, 1):
                    p = Path(tmp) / f"{i:02d}.jpg"
                    p.write_bytes(data)
                    paths.append(p)
                del images
                result = create_draft(self.app.config, self.app.tokens, paths, title, description)
        finally:
            self.app.draft_lock.release()
        if not result["ok"]:
            log.warning("internal draft failed: %s %s", result["error"], result["message"][:200])
        self.send_json(200 if result["ok"] else 502, result)

    # ----------------------------------------------------------- pages
    def page_home(self) -> None:
        accounts = []
        if self.app.configured:
            try:
                accounts = list(self.app.tokens.store.load()["accounts"].values())
            except Exception:  # noqa: BLE001
                log.exception("failed to read token store")
        self.send_html(200, pages.home_page(accounts, self.app.tz, self.app.configured))

    def page_connect(self) -> None:
        if not self.app.configured:
            return self.send_html(503, pages.result_page(False, "Сервіс не налаштовано", "Service not configured"))
        state = self.app.states.issue()
        self.send_response(302)
        self.send_header("Location", self.app.authorize_url(state))
        self.send_header("Set-Cookie", self._state_cookie(state_hash(state), self.app.config.state_ttl_seconds))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def page_callback(self) -> None:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
        g = lambda k: (q.get(k) or [None])[0]  # noqa: E731
        clear = {"Set-Cookie": self._state_cookie("", 0)}
        state_result = self.app.states.consume(g("state"), self._cookie(STATE_COOKIE))

        if g("error"):
            log.info("OAuth callback error from TikTok: %s", (g("error") or "")[:80])
            return self.send_html(400, pages.result_page(
                False, "Доступ не надано", "Authorization was not granted (cancelled or denied).",
                f"{g('error')}: {g('error_description') or ''}"), clear)
        if state_result not in ("ok", "ok_nocookie"):
            log.warning("OAuth callback rejected: state %s", state_result)
            return self.send_html(400, pages.result_page(
                False, "Недійсний або прострочений запит", "Invalid or expired state (CSRF check failed).",
                "Почніть знову з головної сторінки / Please start again from the home page."), clear)
        if state_result == "ok_nocookie":
            log.info("OAuth callback: state valid, browser cookie absent (accepted)")
        code = g("code")
        if not code:
            return self.send_html(400, pages.result_page(False, "Немає коду авторизації",
                                                         "Missing authorization code."), clear)
        try:
            record = self.app.tokens.connect_with_code(code)
        except TikTokError as exc:
            log.warning("Token exchange failed: %s", exc.describe())
            return self.send_html(502, pages.result_page(
                False, "Не вдалося отримати токен", "Token exchange with TikTok failed.",
                f"{exc.code}{(' · log_id ' + exc.log_id) if exc.log_id else ''}"), clear)
        granted = set(filter(None, (record.get("scope") or "").split(",")))
        missing = [s for s in SCOPES if s not in granted]
        name = record.get("display_name") or "TikTok"
        if missing:
            return self.send_html(200, pages.result_page(
                False, f"Підключено ({name}), але без дозволу: {', '.join(missing)}",
                "Connected, but some permissions were not granted — uploads will fail. Please reconnect "
                "and allow all permissions."), clear)
        return self.send_html(200, pages.result_page(
            True, f"TikTok підключено: {name}",
            f"TikTok account connected: {name}. You can close this page.",
            f"open_id {mask(record['open_id'], 6)}"), clear)

    # ----------------------------------------------------------- media
    def serve_media(self, rel: str) -> None:
        f = resolve_media_path(self.app.config.media_dir, rel)
        if f is None:
            return self.not_found()
        st = f.stat()
        size = st.st_size
        etag = f'"{st.st_mtime_ns:x}-{size:x}"'
        lastmod = email.utils.formatdate(st.st_mtime, usegmt=True)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        rng = parse_range(self.headers.get("Range"), size)
        if rng == "invalid":
            return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
        start, end = rng if isinstance(rng, tuple) else (0, size - 1)
        length = end - start + 1 if size else 0
        self.send_response(206 if isinstance(rng, tuple) else 200)
        self.send_header("Content-Type", CONTENT_TYPES[f.suffix.lower()])
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if isinstance(rng, tuple):
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", lastmod)
        self.send_header("Cache-Control", "public, max-age=86400, immutable")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        self.end_headers()
        if self.command == "HEAD" or not length:
            return
        with open(f, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


def make_server(app: App, host: str, port: int) -> Server:
    handler = type("BoundHandler", (Handler,), {"app": app})
    return Server((host, port), handler)


def main() -> None:
    setup_logging()
    config = load_config(require_secrets=False)
    app = App(config)
    if not app.configured:
        log.error("TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET not set — OAuth disabled, static pages only")
    if not config.public_base_url.startswith("https://"):
        log.warning("PUBLIC_BASE_URL is not https — TikTok PULL_FROM_URL requires https")
    os.umask(0o022)
    server = make_server(app, config.host, config.port)
    stop = threading.Event()
    threading.Thread(target=app.maintenance_loop, args=(stop,), daemon=True, name="maintenance").start()

    def _shutdown(signum, _frame):
        log.info("signal %s received, shutting down", signum)
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    log.info("tiktok-connect %s listening on %s:%s public=%s redirect_uri=%s client_key=%s",
             __version__, config.host, config.port, config.public_base_url, config.redirect_uri,
             mask(config.client_key, 4))
    try:
        server.serve_forever(poll_interval=1.0)
    finally:
        server.server_close()
        log.info("stopped")


if __name__ == "__main__":
    main()
