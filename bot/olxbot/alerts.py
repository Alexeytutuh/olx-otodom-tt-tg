"""Alert / recovery state machine, one instance per monitored thing (olx, otodom, telegram, internal).

failure() N times in a row -> one 'down' alert (repeated at most every `realert_s` while it lasts);
success() after an alert -> one 'recovered' message. State is a plain dict so it can be persisted."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class AlertState:
    name: str
    threshold: int = 3
    realert_s: float = 6 * 3600
    consecutive: int = 0
    alerted: bool = False
    alerted_at: float = 0.0
    down_since: float = 0.0
    last_error: str = ""
    last_ok_at: float = 0.0
    last_fail_at: float = 0.0
    total_failures: int = 0

    def failure(self, reason: str, now: Optional[float] = None) -> Optional[str]:
        """Returns 'down' when an alert should be sent now, else None."""
        now = now or time.time()
        self.consecutive += 1
        self.total_failures += 1
        self.last_error = reason
        self.last_fail_at = now
        if self.consecutive == 1:
            self.down_since = now
        if self.consecutive >= self.threshold:
            if not self.alerted or now - self.alerted_at >= self.realert_s:
                self.alerted, self.alerted_at = True, now
                return "down"
        return None

    def success(self, now: Optional[float] = None) -> Optional[str]:
        """Returns 'recovered' when a recovery message should be sent now, else None."""
        now = now or time.time()
        was = self.alerted
        self.consecutive, self.alerted, self.alerted_at, self.down_since = 0, False, 0.0, 0.0
        self.last_ok_at = now
        return "recovered" if was else None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, name: str, d: dict, threshold: int, realert_s: float) -> "AlertState":
        st = cls(name, threshold, realert_s)
        for k, v in (d or {}).items():
            if k in st.__dataclass_fields__ and k not in ("name", "threshold", "realert_s"):
                setattr(st, k, v)
        return st
