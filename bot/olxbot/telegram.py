"""Minimal Telegram Bot API client (stdlib only): JSON + multipart uploads, flood control (retry_after),
retries with backoff for network/5xx, per-chat pacing. Thread-safe."""
from __future__ import annotations

import json
import logging
import random
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Optional

log = logging.getLogger("olxbot.telegram")


class TgError(Exception):
    def __init__(self, method: str, code: Optional[int], description: str, retry_after: Optional[float] = None):
        super().__init__(f"{method}: {code} {description}")
        self.method, self.code, self.description, self.retry_after = method, code, description, retry_after

    @property
    def permanent(self) -> bool:
        """4xx except 429: retrying the same request will not help."""
        return self.code is not None and 400 <= self.code < 500 and self.code != 429

    @property
    def network(self) -> bool:
        return self.code is None or self.code >= 500


def _multipart(fields: dict[str, Any], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = "----olxbot" + uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        if v is None:
            continue
        if not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False)
        out.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n".encode() + v.encode()
                   + b"\r\n")
    for k, (fname, data, ctype) in files.items():
        out.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\n"
                   f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


class Telegram:
    def __init__(self, token: str, api_base: str = "https://api.telegram.org",
                 wait: Callable[[float], bool] = None, beat: Callable[[], None] = None,
                 send_delay_s: float = 1.1, on_result: Callable[[bool, Optional[str]], None] = None):
        """wait(seconds) -> True when the process should stop (interruptible sleep).
        on_result(ok, error) is called after every request that is not getUpdates (health tracking)."""
        self.base = f"{api_base.rstrip('/')}/bot{token}"
        self._wait = wait or (lambda s: (time.sleep(s), False)[1])
        self._beat = beat or (lambda: None)
        self.send_delay_s = send_delay_s
        self._pace_lock = threading.Lock()
        self._next_ok: dict[Any, float] = {}
        self.on_result = on_result
        self.backoff_base = 1.0       # seconds; tests make it tiny

    # ------------------------------------------------------------------ core
    def _request(self, method: str, params: dict, files: Optional[dict], timeout: float) -> Any:
        if files:
            body, ctype = _multipart(params, files)
        else:
            body, ctype = json.dumps({k: v for k, v in params.items() if v is not None},
                                     ensure_ascii=False).encode(), "application/json"
        req = urllib.request.Request(f"{self.base}/{method}", data=body, headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                res = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                res = json.loads(e.read().decode("utf-8") or "{}")
            except Exception:
                res = {}
            raise TgError(method, res.get("error_code", e.code), res.get("description", str(e)),
                          (res.get("parameters") or {}).get("retry_after"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise TgError(method, None, f"{type(e).__name__}: {e}")
        if not res.get("ok"):
            raise TgError(method, res.get("error_code"), res.get("description", "not ok"),
                          (res.get("parameters") or {}).get("retry_after"))
        return res.get("result")

    def _pace(self, chat_id, weight: int):
        if chat_id is None:
            return
        with self._pace_lock:
            now = time.monotonic()
            start = max(now, self._next_ok.get(chat_id, 0))
            self._next_ok[chat_id] = start + self.send_delay_s * max(1, weight)
        if start > now:
            self._wait(start - now)

    def call(self, method: str, params: Optional[dict] = None, files: Optional[dict] = None,
             tries: int = 5, timeout: float = 60, chat_weight: int = 1, max_flood_wait: float = 600) -> Any:
        params = dict(params or {})
        if method not in ("getUpdates", "answerCallbackQuery", "getMe"):
            self._pace(params.get("chat_id"), chat_weight)
        last: Optional[TgError] = None
        for attempt in range(tries):
            try:
                out = self._request(method, params, files, timeout)
                if self.on_result and method != "getUpdates":
                    self.on_result(True, None)
                return out
            except TgError as e:
                last = e
                if e.code == 429:
                    wait = min(float(e.retry_after or 5) + 1, max_flood_wait)
                    log.warning("Telegram flood control on %s: waiting %.0fs", method, wait)
                    if self._wait(wait):
                        break
                    continue
                if e.permanent:
                    break
                log.warning("Telegram %s failed (attempt %d/%d): %s", method, attempt + 1, tries, e)
                self._beat()
                if attempt + 1 < tries and self._wait(min(60, self.backoff_base * (2 ** attempt + random.random()))):
                    break
        assert last is not None
        if self.on_result and method != "getUpdates" and not last.permanent:
            self.on_result(False, str(last))
        raise last

    # ------------------------------------------------------------------ helpers
    def get_me(self):
        return self.call("getMe", tries=2, timeout=20)

    def get_updates(self, offset: int, timeout: int = 50) -> list:
        return self.call("getUpdates", {"offset": offset, "timeout": timeout,
                                        "allowed_updates": ["message", "callback_query"]},
                         tries=1, timeout=timeout + 15) or []

    def send_message(self, chat_id, text: str, html: bool = True, reply_markup: Optional[dict] = None,
                     reply_to: Optional[int] = None, silent: bool = False, preview: bool = False, **kw) -> dict:
        p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML" if html else None,
             "reply_markup": reply_markup, "disable_notification": silent or None,
             "link_preview_options": {"is_disabled": not preview}}
        if reply_to:
            p["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        return self.call("sendMessage", p, **kw)

    def send_photo(self, chat_id, jpeg: bytes, caption: Optional[str] = None, html: bool = True,
                   reply_markup: Optional[dict] = None) -> dict:
        p = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML" if (html and caption) else None,
             "photo": "attach://photo", "reply_markup": reply_markup}
        return self.call("sendPhoto", p, files={"photo": ("photo.jpg", jpeg, "image/jpeg")}, timeout=120)

    def send_media_group(self, chat_id, items: list[tuple[str, bytes, str]], kind: str = "photo",
                         caption: Optional[str] = None, html: bool = True) -> list:
        """items: (filename, bytes, mime). kind: photo | document. Caption goes on the first item
        (for documents Telegram shows it under the last one, so callers usually pass none)."""
        media, files = [], {}
        for i, (fname, data, mime) in enumerate(items):
            key = f"f{i}"
            m = {"type": kind, "media": f"attach://{key}"}
            if i == 0 and caption:
                m["caption"] = caption
                if html:
                    m["parse_mode"] = "HTML"
            media.append(m)
            files[key] = (fname, data, mime)
        return self.call("sendMediaGroup", {"chat_id": chat_id, "media": media}, files=files,
                         timeout=180, chat_weight=len(items))

    def send_document(self, chat_id, fname: str, data: bytes, mime: str, caption: Optional[str] = None) -> dict:
        return self.call("sendDocument", {"chat_id": chat_id, "caption": caption, "document": "attach://doc"},
                         files={"doc": (fname, data, mime)}, timeout=120)

    def edit_message_text(self, chat_id, message_id: int, text: str, reply_markup: Optional[dict] = None):
        try:
            return self.call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text,
                                                 "parse_mode": "HTML", "reply_markup": reply_markup,
                                                 "link_preview_options": {"is_disabled": True}}, tries=3)
        except TgError as e:
            if "message is not modified" in (e.description or ""):
                return None
            raise

    def answer_callback(self, callback_id: str, text: str = "", alert: bool = False):
        try:
            return self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190],
                                                     "show_alert": alert}, tries=2, timeout=15)
        except TgError as e:          # too old query etc. - never fatal
            log.info("answerCallbackQuery: %s", e)
