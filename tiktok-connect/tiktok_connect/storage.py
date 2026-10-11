"""Atomic, 0600 JSON token storage with an inter-process lock.

The web server (background refresher) and the CLI (docker exec) may both refresh
tokens; TikTok may rotate the refresh_token on refresh, so all read-modify-write
cycles are serialized with fcntl.flock on a lock file.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from collections.abc import Iterator

_thread_lock = threading.RLock()
_lock_depth = threading.local()


def atomic_write_json(path: Path, data: Any, mode: int = 0o600) -> None:
    """Write JSON atomically: temp file in same dir, chmod 0600, fsync, rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


class TokenStore:
    def __init__(self, path: Path, lock_path: Path):
        self.path = path
        self.lock_path = lock_path

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Exclusive lock across threads and processes (re-entrant per thread)."""
        with _thread_lock:
            depth = getattr(_lock_depth, "n", 0)
            if depth:
                _lock_depth.n = depth + 1
                try:
                    yield
                finally:
                    _lock_depth.n -= 1
                return
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                _lock_depth.n = 1
                yield
            finally:
                _lock_depth.n = 0
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _read_unlocked(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {"version": 1, "accounts": {}, "default_open_id": None}
        data.setdefault("version", 1)
        data.setdefault("accounts", {})
        data.setdefault("default_open_id", None)
        return data

    def load(self) -> dict:
        with self.locked():
            return self._read_unlocked()

    def save_account(self, account: dict, make_default: bool = True) -> None:
        with self.locked():
            data = self._read_unlocked()
            account = dict(account)
            account["updated_at"] = int(time.time())
            prev = data["accounts"].get(account["open_id"], {})
            account.setdefault("connected_at", prev.get("connected_at", account["updated_at"]))
            data["accounts"][account["open_id"]] = {**prev, **account}
            if make_default or not data.get("default_open_id"):
                data["default_open_id"] = account["open_id"]
            atomic_write_json(self.path, data)

    def remove_account(self, open_id: str) -> bool:
        with self.locked():
            data = self._read_unlocked()
            if open_id not in data["accounts"]:
                return False
            del data["accounts"][open_id]
            if data.get("default_open_id") == open_id:
                data["default_open_id"] = next(iter(data["accounts"]), None)
            atomic_write_json(self.path, data)
            return True

    def get_account(self, open_id: str | None = None) -> dict | None:
        data = self.load()
        oid = open_id or data.get("default_open_id")
        if not oid:
            return None
        return data["accounts"].get(oid)
