"""SQLite (WAL) persistence: seen ids, known listings (for dedupe), posts, blacklist, agent-text memory,
health and small key/value state. One connection shared by threads behind a lock; every write is a
short transaction, so a crash never leaves half-written state."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS seen (
    source TEXT NOT NULL, id TEXT NOT NULL, seen_at REAL NOT NULL, PRIMARY KEY (source, id));
CREATE TABLE IF NOT EXISTS listings (
    key TEXT PRIMARY KEY,               -- "<source>:<source_id>"
    source TEXT, source_id TEXT, dedupe_key TEXT, first_seen_at REAL,
    price REAL, area REAL, rooms INTEGER, street TEXT, district TEXT, title TEXT,
    text_fp TEXT, dhash TEXT, seller_id TEXT, seller_name TEXT,
    status TEXT,                        -- silent | sent | dup | blacklisted | paused | no_chat
    dup_of TEXT, post_id INTEGER, json TEXT);
CREATE INDEX IF NOT EXISTS listings_dedupe ON listings(dedupe_key);
CREATE INDEX IF NOT EXISTS listings_seen ON listings(first_seen_at);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_key TEXT, chat_id INTEGER, created_at REAL,
    status TEXT,                        -- sending | sent | failed
    album_ids TEXT, text_msg_id INTEGER, info_msg_id INTEGER,
    dup_of_post INTEGER, blacklist_id INTEGER, listing_json TEXT, attempts INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS posts_listing ON posts(listing_key);
CREATE TABLE IF NOT EXISTS blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT, seller_id TEXT, seller_name TEXT, text_fp TEXT, post_id INTEGER,
    created_at REAL, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS agent_fps (
    source TEXT, fp TEXT, oid TEXT, added_at REAL, PRIMARY KEY (source, fp));
CREATE TABLE IF NOT EXISTS health (source TEXT PRIMARY KEY, json TEXT);
"""


class Storage:
    def __init__(self, path: str):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        with self._lock:
            self.db.executescript(SCHEMA)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    # ------------------------------------------------------------------ helpers
    def _x(self, sql: str, args: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.db.execute(sql, tuple(args))

    def _tx(self, fn):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                out = fn(self.db)
                self.db.execute("COMMIT")
                return out
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def close(self):
        with self._lock:
            try:
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self.db.close()

    def integrity_ok(self) -> bool:
        return self._x("PRAGMA quick_check").fetchone()[0] == "ok"

    # ------------------------------------------------------------------ meta
    def get(self, key: str, default: Any = None) -> Any:
        r = self._x("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, key: str, value: Any):
        self._x("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)))

    def incr(self, key: str, by: int = 1) -> int:
        def f(db):
            r = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            v = (json.loads(r[0]) if r else 0) + by
            db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, json.dumps(v)))
            return v
        return self._tx(f)

    # ------------------------------------------------------------------ seen
    def seen_ids(self, source: str) -> set[str]:
        return {r[0] for r in self._x("SELECT id FROM seen WHERE source=?", (source,))}

    def mark_seen(self, source: str, ids: Iterable[str], at: Optional[float] = None):
        at = at or time.time()
        rows = [(source, str(i), at) for i in ids]
        if rows:
            self._tx(lambda db: db.executemany(
                "INSERT INTO seen(source,id,seen_at) VALUES(?,?,?) ON CONFLICT DO NOTHING", rows))

    def is_seen(self, source: str, sid: str) -> bool:
        return self._x("SELECT 1 FROM seen WHERE source=? AND id=?", (source, str(sid))).fetchone() is not None

    def prune(self, keep_days: int) -> dict:
        cutoff = time.time() - keep_days * 86400

        def f(db):
            a = db.execute("DELETE FROM seen WHERE seen_at < ?", (cutoff,)).rowcount
            b = db.execute("DELETE FROM listings WHERE first_seen_at < ?", (cutoff,)).rowcount
            c = db.execute("DELETE FROM agent_fps WHERE added_at < ?", (cutoff,)).rowcount
            # posts keep their listing JSON for "regenerate"; drop very old ones (3x the window)
            d = db.execute("DELETE FROM posts WHERE created_at < ?", (time.time() - 3 * keep_days * 86400,)).rowcount
            return {"seen": a, "listings": b, "agent_fps": c, "posts": d}
        return self._tx(f)

    # ------------------------------------------------------------------ listings (dedupe index)
    def upsert_listing(self, rec: dict):
        cols = ["key", "source", "source_id", "dedupe_key", "first_seen_at", "price", "area", "rooms", "street",
                "district", "title", "text_fp", "dhash", "seller_id", "seller_name", "status", "dup_of", "post_id",
                "json"]
        vals = [rec.get(c) for c in cols]
        upd = ",".join(f"{c}=COALESCE(excluded.{c},{c})" for c in cols if c not in ("key", "first_seen_at"))
        self._x(f"INSERT INTO listings({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
                f"ON CONFLICT(key) DO UPDATE SET {upd}", vals)

    def get_listing(self, key: str) -> Optional[dict]:
        r = self._x("SELECT * FROM listings WHERE key=?", (key,)).fetchone()
        return dict(r) if r else None

    def listings_by_dedupe_key(self, dkey: str, exclude_key: str) -> list[dict]:
        return [dict(r) for r in self._x("SELECT * FROM listings WHERE dedupe_key=? AND key<>? ORDER BY first_seen_at",
                                         (dkey, exclude_key))]

    def recent_listings(self, since: float, exclude_key: str) -> list[dict]:
        """Without the (large) json column: ~2000 rows in 30 days must stay cheap in memory."""
        cols = ("key, source, source_id, dedupe_key, first_seen_at, price, area, rooms, street, district, title, "
                "text_fp, dhash, seller_id, seller_name, status, dup_of, post_id")
        return [dict(r) for r in self._x(f"SELECT {cols} FROM listings WHERE first_seen_at>=? AND key<>? "
                                         "ORDER BY first_seen_at DESC", (since, exclude_key))]

    def set_listing_dhash(self, key: str, dhash: str):
        self._x("UPDATE listings SET dhash=? WHERE key=?", (dhash, key))

    # ------------------------------------------------------------------ posts
    def create_post(self, listing_key: str, chat_id: int, listing_json: str, dup_of_post: Optional[int]) -> int:
        cur = self._x("INSERT INTO posts(listing_key,chat_id,created_at,status,listing_json,dup_of_post,attempts) "
                      "VALUES(?,?,?,?,?,?,0)", (listing_key, chat_id, time.time(), "sending", listing_json, dup_of_post))
        return cur.lastrowid

    def update_post(self, post_id: int, **kw):
        if not kw:
            return
        sets = ",".join(f"{k}=?" for k in kw)
        vals = [json.dumps(v) if k == "album_ids" else v for k, v in kw.items()]
        self._x(f"UPDATE posts SET {sets} WHERE id=?", vals + [post_id])

    def get_post(self, post_id: int) -> Optional[dict]:
        r = self._x("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["album_ids"] = json.loads(d["album_ids"]) if d.get("album_ids") else []
        return d

    def unfinished_post(self, listing_key: str) -> Optional[dict]:
        r = self._x("SELECT id FROM posts WHERE listing_key=? AND status IN ('sending','failed') ORDER BY id DESC LIMIT 1",
                    (listing_key,)).fetchone()
        return self.get_post(r[0]) if r else None

    def post_for_listing(self, listing_key: str) -> Optional[dict]:
        r = self._x("SELECT id FROM posts WHERE listing_key=? AND status='sent' ORDER BY id LIMIT 1",
                    (listing_key,)).fetchone()
        return self.get_post(r[0]) if r else None

    def count_posts(self, since: float = 0) -> int:
        return self._x("SELECT COUNT(*) FROM posts WHERE status='sent' AND created_at>=?", (since,)).fetchone()[0]

    # ------------------------------------------------------------------ blacklist
    def add_blacklist(self, source: str, seller_id: Optional[str], seller_name: Optional[str],
                      text_fp: Optional[str], post_id: Optional[int]) -> int:
        cur = self._x("INSERT INTO blacklist(source,seller_id,seller_name,text_fp,post_id,created_at,active) "
                      "VALUES(?,?,?,?,?,?,1)", (source, seller_id, seller_name, text_fp, post_id, time.time()))
        return cur.lastrowid

    def deactivate_blacklist(self, bl_id: int) -> Optional[dict]:
        r = self._x("SELECT * FROM blacklist WHERE id=?", (bl_id,)).fetchone()
        if not r:
            return None
        self._x("UPDATE blacklist SET active=0 WHERE id=?", (bl_id,))
        return dict(r)

    def blacklist_entries(self, active_only: bool = True) -> list[dict]:
        q = "SELECT * FROM blacklist" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
        return [dict(r) for r in self._x(q)]

    def blacklist_match(self, source: str, seller_id: Optional[str], text_fp: Optional[str]) -> Optional[dict]:
        if seller_id:
            r = self._x("SELECT * FROM blacklist WHERE active=1 AND source=? AND seller_id=?",
                        (source, str(seller_id))).fetchone()
            if r:
                return dict(r)
        if text_fp:
            r = self._x("SELECT * FROM blacklist WHERE active=1 AND text_fp=?", (text_fp,)).fetchone()
            if r:
                return dict(r)
        return None

    def active_blacklist_for_post(self, post_id: int) -> Optional[dict]:
        r = self._x("SELECT * FROM blacklist WHERE active=1 AND post_id=? ORDER BY id DESC LIMIT 1",
                    (post_id,)).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------------ agent text memory
    def load_agent_fps(self, source: str) -> dict[str, str]:
        return {r[0]: r[1] for r in self._x("SELECT fp, oid FROM agent_fps WHERE source=? ORDER BY added_at",
                                            (source,))}

    def save_agent_fps(self, source: str, fps: dict[str, str]):
        now = time.time()
        rows = [(source, fp, oid, now) for fp, oid in fps.items()]
        if rows:
            self._tx(lambda db: db.executemany(
                "INSERT INTO agent_fps(source,fp,oid,added_at) VALUES(?,?,?,?) ON CONFLICT DO NOTHING", rows))

    # ------------------------------------------------------------------ health
    def save_health(self, source: str, data: dict):
        self._x("INSERT INTO health(source,json) VALUES(?,?) ON CONFLICT(source) DO UPDATE SET json=excluded.json",
                (source, json.dumps(data, ensure_ascii=False)))

    def load_health(self, source: str) -> dict:
        r = self._x("SELECT json FROM health WHERE source=?", (source,)).fetchone()
        return json.loads(r[0]) if r else {}
