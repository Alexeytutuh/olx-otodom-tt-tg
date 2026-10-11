"""Process liveness: interruptible sleep, per-component heartbeats, hang watchdog, heartbeat file for the
Docker healthcheck, optional systemd notify (Type=notify + WatchdogSec)."""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from typing import Optional

log = logging.getLogger("olxbot.runtime")

EXIT_HANG = 3
EXIT_CONFIG = 78       # bad token / config: systemd RestartPreventExitStatus=78


def sd_notify(msg: str):
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
    except OSError as e:
        log.warning("sd_notify: %s", e)


class Runtime:
    def __init__(self, heartbeat_path: str, hang_timeout_s: float, marker_path: Optional[str] = None):
        self.stop = threading.Event()
        self.heartbeat_path = heartbeat_path
        self.marker_path = marker_path
        self.hang_timeout_s = hang_timeout_s
        self._beats: dict[str, float] = {}
        self._lock = threading.Lock()
        self._last_file_write = 0.0

    # components register by beating; a component may be "idle-ok" for a known long wait
    def beat(self, component: str):
        now = time.monotonic()
        with self._lock:
            self._beats[component] = now
            stale = self._stale_locked(now)
        if not stale and now - self._last_file_write >= 5:
            self._last_file_write = now
            sd_notify("WATCHDOG=1")
            try:
                tmp = self.heartbeat_path + ".tmp"
                with open(tmp, "w") as f:
                    f.write(str(int(time.time())))
                os.replace(tmp, self.heartbeat_path)
            except OSError:
                pass

    def forget(self, component: str):
        with self._lock:
            self._beats.pop(component, None)

    def _stale_locked(self, now: float) -> list[tuple[str, float]]:
        return [(c, now - t) for c, t in self._beats.items() if now - t > self.hang_timeout_s]

    def wait(self, seconds: float, component: Optional[str] = None) -> bool:
        """Sleep that wakes on stop and keeps the component's heartbeat fresh. Returns True if stopping."""
        end = time.monotonic() + max(0.0, seconds)
        while not self.stop.is_set():
            left = end - time.monotonic()
            if left <= 0:
                break
            self.stop.wait(min(left, 20))
            if component:
                self.beat(component)
        return self.stop.is_set()

    def watchdog_loop(self, check_every: float = 20):
        while not self.stop.wait(check_every):
            with self._lock:
                stale = self._stale_locked(time.monotonic())
            if stale:
                comp, idle = stale[0]
                log.error("WATCHDOG: no progress in '%s' for %.0fs; exiting so Docker/systemd restarts me",
                          comp, idle)
                if self.marker_path:
                    try:
                        with open(self.marker_path, "w") as f:
                            json.dump({"reason": "hang", "component": comp, "idle_s": int(idle),
                                       "at": time.time()}, f)
                    except OSError:
                        pass
                logging.shutdown()
                os._exit(EXIT_HANG)

    def start_watchdog(self):
        threading.Thread(target=self.watchdog_loop, name="watchdog", daemon=True).start()

    def pop_restart_marker(self) -> Optional[dict]:
        if not self.marker_path or not os.path.exists(self.marker_path):
            return None
        try:
            with open(self.marker_path) as f:
                d = json.load(f)
        except Exception:
            d = {"reason": "unknown"}
        try:
            os.remove(self.marker_path)
        except OSError:
            pass
        return d
