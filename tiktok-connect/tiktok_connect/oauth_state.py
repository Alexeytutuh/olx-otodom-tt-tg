"""Server-side, single-use, expiring OAuth `state` (CSRF) store.

State is additionally bound to the browser with an HttpOnly cookie holding its
hash; a present-but-different cookie is rejected.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

MAX_PENDING = 1000


def state_hash(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


class StateStore:
    def __init__(self, ttl_seconds: int = 600):
        self.ttl = ttl_seconds
        self._items: dict[str, float] = {}
        self._lock = threading.Lock()

    def _purge(self, now: float) -> None:
        for k in [k for k, exp in self._items.items() if exp <= now]:
            del self._items[k]

    def issue(self) -> str:
        state = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            if len(self._items) >= MAX_PENDING:  # cap memory under abuse
                oldest = min(self._items, key=self._items.__getitem__)
                del self._items[oldest]
            self._items[state_hash(state)] = now + self.ttl
        return state

    def consume(self, state: str | None, cookie_hash: str | None) -> str:
        """Return 'ok', 'ok_nocookie', 'missing', 'unknown' (invalid/expired/used) or 'mismatch'.

        'ok_nocookie': state is valid server-side but the browser binding cookie is
        absent. This happens legitimately on phones when TikTok hands the flow from
        the in-app/TikTok app back to a different browser, so it is accepted
        (state is still unguessable, single-use and short-lived). A cookie that is
        present but different is rejected.
        """
        if not state:
            return "missing"
        h = state_hash(state)
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            exp = self._items.pop(h, None)  # single use, even on mismatch
        if exp is None:
            return "unknown"
        if not cookie_hash:
            return "ok_nocookie"
        if not hmac.compare_digest(cookie_hash, h):
            return "mismatch"
        return "ok"
