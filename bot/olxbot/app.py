"""The bot: aligned poller (OLX -> Otodom), Telegram long polling (commands + buttons), slide generator worker.

Threads: main = poller, 'updates' = getUpdates loop, 'generator' = one slide job at a time, 'watchdog'.
Every thread beats its own heartbeat; a silent thread for > HANG_TIMEOUT_S makes the process exit
(Docker / systemd restart it)."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import queue
import shutil
import threading
import time
from typing import Optional

from parsers import config as pconfig
from parsers.common import HttpClient, SourceDownError
from parsers.filters import text_fingerprint
from parsers.models import Listing

from . import __version__, images, slides
from .alerts import AlertState
from .dedupe import WINDOW_DAYS, find_duplicate, listing_record
from .formatting import (SOURCE_NAMES, TZ, build_info, build_post, tiktok_caption, tiktok_title, ts_local,
                         utf16_len)
from .runtime import EXIT_CONFIG, Runtime, sd_notify
from .schedule import current_or_next_slot, next_slot, skipped_slots
from .settings import Settings
from .storage import Storage
from .telegram import Telegram, TgError
from .tiktok_hook import DraftError, get_sink

log = logging.getLogger("olxbot")

BLOCKED_KINDS = ("blocked", "captcha", "rate_limited")


COMPONENTS = ("poller", "updates", "generator")


def _component() -> Optional[str]:
    n = threading.current_thread().name
    return n if n in COMPONENTS else None


class TelegramDown(Exception):
    pass


class BeatingHttp(HttpClient):
    """parsers' HttpClient whose sleeps are interruptible and which keeps the poller heartbeat fresh."""

    def __init__(self, source: str, runtime: Runtime):
        self._rt = runtime
        super().__init__(source, sleep=lambda s: runtime.wait(s, "poller"))

    def get(self, *a, **kw):
        self._rt.beat("poller")
        try:
            return super().get(*a, **kw)
        finally:
            self._rt.beat("poller")


def make_parsers(names, runtime: Runtime) -> dict:
    out = {}
    for n in names:
        if n == "olx":
            from parsers.olx import OlxParser
            out[n] = OlxParser(http=BeatingHttp("olx", runtime))
        elif n == "otodom":
            from parsers.otodom import OtodomParser
            out[n] = OtodomParser(http=BeatingHttp("otodom", runtime))
    return out


def kb_post(post_id: int, bl_id: Optional[int] = None) -> dict:
    second = ({"text": "↩️ Не посередник", "callback_data": f"u:{bl_id}"} if bl_id
              else {"text": "🏢 Посередник", "callback_data": f"b:{post_id}"})
    return {"inline_keyboard": [[{"text": "🎬 Згенерувати", "callback_data": f"g:{post_id}"}, second]]}


def kb_publish(post_id: int, channel: bool, tiktok: bool, done_tg: bool = False, done_tt: bool = False) -> Optional[dict]:
    """Buttons under the generated TikTok text. Pressed ones turn into ✅ (pressing again only reminds)."""
    row = []
    if channel:
        row.append({"text": "✅ В каналі" if done_tg else "📢 В канал", "callback_data": f"c:{post_id}"})
    if tiktok:
        row.append({"text": "✅ Чернетка TikTok" if done_tt else "🎵 В чернетки TikTok", "callback_data": f"t:{post_id}"})
    return {"inline_keyboard": [row]} if row else None


HELP = ("Я стежу за OLX і Otodom: нові квартири в оренду у Вроцлаві від власників (1–3 кімнати). "
        "Кожне нове оголошення надсилаю один раз: фото + готовий пост.\n\n"
        "Кнопки під оголошенням:\n"
        "🎬 Згенерувати: слайди для TikTok файлами + готовий текст, під ним кнопки "
        "📢 В канал і 🎵 В чернетки TikTok\n"
        "🏢 Посередник: більше не показувати цього продавця (можна скасувати)\n\n"
        "Команди:\n"
        "/status: стан перевірок і статистика\n"
        "/pause: призупинити надсилання\n"
        "/resume: відновити\n"
        "/blacklist: список посередників (і зняти позначку)\n"
        "/unblock N: зняти позначку з запису N")


class Bot:
    def __init__(self, settings: Settings, storage: Optional[Storage] = None, parsers: Optional[dict] = None,
                 tg: Optional[Telegram] = None, runtime: Optional[Runtime] = None):
        self.s = settings
        os.makedirs(settings.data_dir, exist_ok=True)
        os.makedirs(settings.tmp_dir, exist_ok=True)
        self.rt = runtime or Runtime(settings.heartbeat_path, settings.hang_timeout_s,
                                     os.path.join(settings.data_dir, "restart_marker.json"))
        self.db = storage or Storage(settings.db_path)
        self.tg = tg or Telegram(settings.token, settings.api_base,
                                 wait=lambda sec: self.rt.wait(sec, _component()),
                                 beat=lambda: _component() and self.rt.beat(_component()),
                                 send_delay_s=settings.send_delay_s, on_result=self._tg_result)
        if tg is not None and tg.on_result is None:
            tg.on_result = self._tg_result
        self.parsers = parsers if parsers is not None else make_parsers(settings.sources, self.rt)
        realert = settings.realert_hours * 3600
        self.alerts = {n: AlertState.from_dict(n, self.db.load_health(n).get("alert", {}), settings.alert_after,
                                               realert) for n in list(settings.sources) + ["telegram", "internal"]}
        self.skip_until: dict[str, float] = {}
        self.jobs: "queue.Queue[tuple[str, int, int, Optional[int]]]" = queue.Queue()   # (kind, post, chat, msg)
        self.jobs_active: set[tuple[str, int]] = set()
        self._jobs_lock = threading.Lock()
        self._notices: list[tuple[str, int]] = []
        self._notice_lock = threading.Lock()
        self.started_at = time.time()
        self.next_run_at: Optional[float] = None
        self.runs = 0
        self.sink = get_sink(settings)
        for name, p in self.parsers.items():
            if hasattr(p, "_agent_fps"):
                p._agent_fps.update(self.db.load_agent_fps(name))

    # ================================================================== chats
    def owner_chat(self) -> Optional[int]:
        return self.db.get("owner_chat_id") or self.s.owner_chat_id

    def alert_chat(self) -> Optional[int]:
        return self.s.alert_chat_id or self.owner_chat()

    def is_allowed(self, chat_id: int) -> bool:
        return chat_id == self.owner_chat() or chat_id in self.s.allowed_chat_ids or chat_id == self.s.alert_chat_id

    def may_become_owner(self, chat_id: int) -> bool:
        return self.owner_chat() is None and (not self.s.allowed_chat_ids or chat_id in self.s.allowed_chat_ids)

    # ================================================================== notices / alerts
    def notify(self, text: str, chat_id: Optional[int] = None):
        """Service message (alerts). Never raises."""
        chat_id = chat_id or self.alert_chat()
        if not chat_id:
            log.warning("No chat for notice: %s", text)
            return
        try:
            self.tg.send_message(chat_id, text, tries=3)
        except TgError as e:
            log.warning("Notice not delivered (%s): %s", e, text[:200])
            if not e.permanent:
                with self._notice_lock:
                    self._notices.append((text, chat_id))

    def flush_notices(self):
        with self._notice_lock:
            pending, self._notices = self._notices, []
        for text, chat in pending[-5:]:
            self.notify(text, chat)

    def _tg_result(self, ok: bool, err: Optional[str]):
        st = self.alerts["telegram"]
        if ok:
            if st.consecutive or st.alerted:
                since = st.down_since
                if st.success() == "recovered":
                    with self._notice_lock:
                        self._notices.append((f"✅ Telegram знову доступний (збої з {ts_local(since)}). "
                                              f"Остання помилка: {st.last_error[:200]}", self.alert_chat()))
                self._save_health("telegram")
        else:
            if st.failure(err or "?") == "down":
                log.error("ALERT telegram down: %s (cannot alert via Telegram itself)", err)
            self._save_health("telegram")

    def _save_health(self, name: str, extra: Optional[dict] = None):
        cur = self.db.load_health(name)
        cur["alert"] = self.alerts[name].to_dict()
        if extra:
            cur.update(extra)
        self.db.save_health(name, cur)

    # ================================================================== poller
    def poller_loop(self):
        interval = self.s.interval_s
        slot = current_or_next_slot(time.time(), interval)
        while not self.rt.stop.is_set():
            self.next_run_at = slot
            if self.rt.wait(slot - time.time(), "poller"):
                break
            self.rt.beat("poller")
            t0 = time.time()
            try:
                self.run_cycle()
                if self.alerts["internal"].success() == "recovered":
                    self.notify("✅ Внутрішня помилка зникла, перевірки йдуть нормально.")
            except Exception as e:  # never die on a bug in one cycle
                log.exception("Cycle crashed: %s", e)
                if self.alerts["internal"].failure(f"{type(e).__name__}: {e}") == "down":
                    self.notify(f"⚠️ Внутрішня помилка бота {self.alerts['internal'].consecutive} рази поспіль: "
                                f"{type(e).__name__}: {str(e)[:300]}")
            self._save_health("internal")
            self.runs += 1
            self.db.set("last_cycle_at", time.time())
            if self.s.max_runs and self.runs >= self.s.max_runs:
                log.info("MAX_RUNS=%d reached, stopping", self.s.max_runs)
                self.rt.stop.set()
                break
            now = time.time()
            missed = skipped_slots(slot, now, interval)
            if missed:
                log.warning("Run took %.0fs and overran %d slot(s); skipping to the next one", now - t0, missed)
            slot = next_slot(now, interval)

    def run_cycle(self):
        self.flush_notices()
        for name in self.s.sources:
            if self.rt.stop.is_set():
                break
            if time.time() < self.skip_until.get(name, 0):
                log.info("%s: backing off after blocks until %s", name, ts_local(self.skip_until[name]))
                continue
            self.run_source(name)
        self.flush_notices()
        last = self.db.get("last_prune_at", 0)
        if time.time() - last > 6 * 3600:
            log.info("Pruned old rows: %s", self.db.prune(self.s.seen_keep_days))
            if not self.db.integrity_ok():
                self.notify("⚠️ База даних бота пошкоджена (quick_check). Варто перевірити /data/bot.sqlite3.")
            self.db.set("last_prune_at", time.time())

    def run_source(self, name: str):
        parser = self.parsers[name]
        title = SOURCE_NAMES.get(name, name)
        t0 = time.time()
        seen = self.db.seen_ids(name)
        try:
            listings = parser.fetch_new(seen)
        except SourceDownError as e:
            st = self.alerts[name]
            log.error("%s down [%s]: %s", name, e.kind, e.reason)
            if st.failure(f"[{e.kind}] {e.reason}") == "down":
                self.notify(f"⚠️ {title} відвалився: {e.reason}\n"
                            f"Тип: {e.kind}; невдалих перевірок поспіль: {st.consecutive} "
                            f"(з {ts_local(st.down_since)}). Пробую далі сам, напишу, коли запрацює.")
            if e.kind in BLOCKED_KINDS and st.consecutive >= 2:
                self.skip_until[name] = time.time() + min(900, self.s.interval_s * 2 ** (st.consecutive - 2)) - 5
            self._save_health(name, {"last_attempt_at": t0, "last_error": f"[{e.kind}] {e.reason}",
                                     "last_run_seconds": round(time.time() - t0, 1)})
            return
        res = parser.last_result
        self.skip_until.pop(name, None)
        st = self.alerts[name]
        since = st.down_since
        if st.success() == "recovered":
            self.notify(f"✅ {title} знову працює (збої з {ts_local(since)}).")
        if hasattr(parser, "_agent_fps"):
            self.db.save_agent_fps(name, parser._agent_fps)
        passed = {l.source_id for l in listings}
        self.db.mark_seen(name, set(res.seen_ids) - passed)      # rejected ones; retry_ids are never in seen_ids
        sent = 0
        if not self.db.get(f"initialized:{name}"):
            for l in listings:
                self.db.upsert_listing(listing_record(l, "silent"))
            self.db.mark_seen(name, passed)
            self.db.set(f"initialized:{name}", time.time())
            log.info("%s: first run is silent, %d current listings marked as seen", name, len(listings))
            self.db.set(f"silent_count:{name}", len(listings))
            self._maybe_startup_summary()
        else:
            todo = list(reversed(listings))[: self.s.max_posts_per_run]      # oldest first
            if len(listings) > len(todo):
                log.info("%s: %d new, sending %d now, the rest next slot", name, len(listings), len(todo))
            for l in todo:
                if self.rt.stop.is_set():
                    break
                try:
                    if self.handle_listing(l) == "sent":
                        sent += 1
                except TelegramDown as e:
                    log.error("Telegram unavailable, %s stays unseen and is retried next slot: %s", l.source_id, e)
                    break
                self.rt.beat("poller")
        self._save_health(name, {"last_attempt_at": t0, "last_ok_at": time.time(),
                                 "last_run_seconds": round(time.time() - t0, 1), "last_error": None,
                                 "last_new": len(listings), "last_sent": sent,
                                 "last_summary": res.summary() if hasattr(res, "summary") else {}})
        log.info("%s ok in %.1fs: fetched=%s passed=%d sent=%d retry=%d", name, time.time() - t0,
                 getattr(res, "items_fetched", "?"), len(listings), sent, len(getattr(res, "retry_ids", ())))

    def _maybe_startup_summary(self):
        if not all(self.db.get(f"initialized:{n}") for n in self.s.sources):
            return
        if self.db.get("startup_summary_sent") or not self.s.startup_summary:
            return
        chat = self.owner_chat()
        if not chat:
            return
        n = sum(self.db.get(f"silent_count:{x}", 0) for x in self.s.sources)
        names = " і ".join(SOURCE_NAMES[x] for x in self.s.sources)
        self.notify(f"✅ Бот запущено, стежу за {names}.\nПоточні оголошення ({n}) позначив як бачені, "
                    f"надсилатиму тільки нові. Перевірка кожні {self.s.interval_s // 60} хв. /status покаже стан.",
                    chat)
        self.db.set("startup_summary_sent", True)

    # ================================================================== one listing
    def handle_listing(self, l: Listing) -> str:
        key = f"{l.source}:{l.source_id}"
        if self.db.post_for_listing(key):                      # crashed after sending, before mark_seen
            self.db.mark_seen(l.source, [l.source_id])
            return "already"
        fp = text_fingerprint(l.title, l.description_text)
        bl = self.db.blacklist_match(l.source, l.seller_id, fp)
        if bl:
            log.info("%s skipped: blacklisted seller (%s)", key, bl.get("seller_name"))
            self.db.upsert_listing(listing_record(l, "blacklisted"))
            self.db.mark_seen(l.source, [l.source_id])
            return "blacklisted"
        chat = self.owner_chat()
        if self.db.get("paused") or not chat:
            self.db.upsert_listing(listing_record(l, "paused" if chat else "no_chat"))
            self.db.mark_seen(l.source, [l.source_id])
            return "paused" if chat else "no_chat"
        rec = listing_record(l, "pending")
        dup = find_duplicate(rec, self.db.listings_by_dedupe_key(rec["dedupe_key"], key),
                             self.db.recent_listings(time.time() - WINDOW_DAYS * 86400, key), self._dhash_of)
        if dup.kind == "exact":
            log.info("%s skipped: exact duplicate of %s (%s)", key, dup.other["key"], dup.why)
            rec.update(status="dup", dup_of=dup.other["key"])
            self.db.upsert_listing(rec)
            self.db.mark_seen(l.source, [l.source_id])
            return "dup"
        dup_info, dup_post = None, None
        if dup.kind == "possible":
            o = dict(dup.other)
            if o.get("json") is None:
                o["json"] = (self.db.get_listing(o["key"]) or {}).get("json")
            dup_post = self.db.post_for_listing(o["key"])
            ol = Listing.from_dict(json.loads(o["json"])) if o.get("json") else None
            dup_info = {"source": o["source"], "url": ol.url if ol else "", "why": dup.why,
                        "sent_at": dup_post["created_at"] if dup_post else o.get("first_seen_at")}
            rec["dup_of"] = o["key"]
        post_id = self.send_post(l, chat, rec, dup_info, dup_post)
        if post_id is None:
            return "failed"
        rec.update(status="sent", post_id=post_id)
        self.db.upsert_listing(rec)
        self.db.mark_seen(l.source, [l.source_id])
        self.db.incr("sent_total")
        return "sent"

    def _dhash_of(self, row: dict) -> Optional[str]:
        if row.get("dhash") is not None:
            return row["dhash"] or None
        url = None
        if row.get("json") is None and row.get("status") != "pending":
            full = self.db.get_listing(row["key"]) or {}
            row["json"] = full.get("json")
        try:
            d = json.loads(row.get("json") or "{}")
            url = d.get("first_photo_url") or (d.get("photos") or [None])[0]
        except ValueError:
            pass
        h = images.dhash_from_url(url, beat=lambda: self.rt.beat("poller")) if url else None
        row["dhash"] = h or ""
        if row.get("status") != "pending":
            self.db.set_listing_dhash(row["key"], row["dhash"])
        return h

    def _telegram_photos(self, l: Listing, rec: dict) -> list[bytes]:
        out = []
        for url in (l.photos or [])[: self.s.max_album_photos + 2]:
            if len(out) >= self.s.max_album_photos:
                break
            try:
                data = images.download(images.sized_url(url, "1280x1280"), beat=lambda: self.rt.beat("poller"))
                jpeg, im = images.telegram_jpeg(data)
                if not out and not rec.get("dhash"):
                    rec["dhash"] = images.dhash(im)
                im.close()
                out.append(jpeg)
            except Exception as e:
                log.warning("photo skipped (%s): %s", url[:80], e)
        return out

    def send_post(self, l: Listing, chat: int, rec: dict, dup_info: Optional[dict],
                  dup_post: Optional[dict]) -> Optional[int]:
        key = rec["key"]
        post = build_post(l, footer=self.s.post_footer)
        prev = self.db.unfinished_post(key)
        if prev:
            post_id, album_ids, text_id = prev["id"], prev["album_ids"], prev["text_msg_id"]
        else:
            post_id = self.db.create_post(key, chat, l.to_json(), dup_post["id"] if dup_post else None)
            album_ids, text_id = [], None
        attempts = (prev["attempts"] if prev else 0) + 1
        self.db.update_post(post_id, attempts=attempts, status="sending")
        try:
            if not album_ids and l.photos:
                photos = self._telegram_photos(l, rec)
                caption = post.html if post.fits_caption else None
                album_ids, used_caption = self._send_album(chat, photos, caption)
                if album_ids:
                    self.db.update_post(post_id, album_ids=album_ids)
                    if used_caption:
                        text_id = album_ids[0]
                        self.db.update_post(post_id, text_msg_id=text_id)
            if not text_id:
                text_id = self._send_text(chat, post)["message_id"]
                self.db.update_post(post_id, text_msg_id=text_id)
            info = build_info(l, dup_info)
            m = self.tg.send_message(chat, info, reply_markup=kb_post(post_id), silent=True,
                                     reply_to=dup_post["info_msg_id"] if dup_post else None)
            self.db.update_post(post_id, info_msg_id=m["message_id"], status="sent")
            log.info("Sent %s as post #%d (%d photos)%s", key, post_id, len(album_ids),
                     " [possible dup]" if dup_info else "")
            return post_id
        except TgError as e:
            self.db.update_post(post_id, status="failed")
            if e.permanent and attempts >= 3:
                log.error("Giving up on %s after %d attempts: %s", key, attempts, e)
                self.notify(f"⚠️ Не вдалося надіслати оголошення {l.url} після {attempts} спроб: {e.description}")
                rec.update(status="failed")
                self.db.upsert_listing(rec)
                self.db.mark_seen(l.source, [l.source_id])
                return None
            raise TelegramDown(str(e))

    def _send_album(self, chat: int, photos: list[bytes], caption: Optional[str]) -> tuple[list[int], bool]:
        """album -> single photo -> nothing (text is sent separately). Returns (message_ids, caption_used)."""
        if len(photos) >= 2:
            try:
                msgs = self.tg.send_media_group(chat, [(f"{i + 1}.jpg", b, "image/jpeg")
                                                       for i, b in enumerate(photos[:10])], caption=caption)
                return [m["message_id"] for m in msgs], bool(caption)
            except TgError as e:
                if not e.permanent:
                    raise
                log.warning("Album rejected (%s); falling back to a single photo", e)
        if photos:
            for b in photos[:3]:
                try:
                    m = self.tg.send_photo(chat, b, caption=caption)
                    return [m["message_id"]], bool(caption)
                except TgError as e:
                    if not e.permanent:
                        raise
                    log.warning("Photo rejected (%s); trying the next / text only", e)
        return [], False

    def _send_text(self, chat: int, post) -> dict:
        try:
            return self.tg.send_message(chat, post.html)
        except TgError as e:
            if e.permanent and "parse" in (e.description or "").lower():
                return self.tg.send_message(chat, post.plain, html=False)
            raise

    # ================================================================== Telegram updates
    def updates_loop(self):
        offset = self.db.get("update_offset", 0)
        delay = 1
        name = threading.current_thread().name
        while not self.rt.stop.is_set():
            self.rt.beat(name)
            try:
                updates = self.tg.get_updates(offset, timeout=self.s.updates_timeout)
                delay = 1
            except TgError as e:
                if e.code in (401, 404):
                    log.error("Bot token rejected by Telegram: %s", e)
                    if self.rt.wait(300, name):
                        break
                    continue
                if e.code == 409:
                    log.error("409 Conflict: another bot instance or a webhook uses this token: %s", e)
                    try:
                        self.tg.call("deleteWebhook", {}, tries=1)
                    except TgError:
                        pass
                log.warning("getUpdates failed: %s (retry in %ds)", e, delay)
                if self.rt.wait(delay, name):
                    break
                delay = min(60, delay * 2)
                continue
            for u in updates:
                offset = max(offset, u["update_id"] + 1)
                self.db.set("update_offset", offset)          # at-most-once: a poison update can't loop us
                try:
                    if "callback_query" in u:
                        self.handle_callback(u["callback_query"])
                    elif "message" in u:
                        self.handle_message(u["message"])
                except Exception as e:
                    log.exception("Update %s failed: %s", u.get("update_id"), e)
                self.rt.beat(name)
            self.flush_notices()

    # ---------------------------------------------------------------- messages
    def handle_message(self, msg: dict):
        chat_id = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if not chat_id or not text.startswith("/"):
            return
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd == "/start" and self.may_become_owner(chat_id):
            self.db.set("owner_chat_id", chat_id)
            log.info("Owner chat set to %s", chat_id)
            self.tg.send_message(chat_id, "👋 Привіт! Цей чат збережено як основний.\n\n" + HELP, html=False)
            if all(self.db.get(f"initialized:{n}") for n in self.s.sources):
                self.db.set("startup_summary_sent", True)
                self.tg.send_message(chat_id, "✅ Бот запущено, стежу за OLX і Otodom.", html=False)
            return
        if not self.is_allowed(chat_id):
            log.info("Ignoring %s from chat %s (not allowed)", cmd, chat_id)
            return
        if cmd in ("/start", "/help"):
            self.tg.send_message(chat_id, HELP, html=False)
        elif cmd == "/status":
            self.tg.send_message(chat_id, self.status_text(), html=False)
        elif cmd == "/pause":
            self.db.set("paused", True)
            self.tg.send_message(chat_id, "⏸ Пауза. Перевірки тривають, але нові оголошення не надсилаю "
                                          "(і не накопичую: після /resume прийдуть тільки ті, що з'являться потім).",
                                 html=False)
        elif cmd == "/resume":
            self.db.set("paused", False)
            self.tg.send_message(chat_id, "▶️ Відновлено. Надсилатиму нові оголошення.", html=False)
        elif cmd == "/blacklist":
            text, kb = self.blacklist_view()
            self.tg.send_message(chat_id, text, html=False, reply_markup=kb)
        elif cmd == "/unblock":
            try:
                bl_id = int(arg.strip().lstrip("#"))
            except ValueError:
                self.tg.send_message(chat_id, "Напиши номер запису: /unblock 3 (номери є в /blacklist).", html=False)
                return
            row = self.unblock(bl_id)
            self.tg.send_message(chat_id, f"↩️ Знято позначку: {row.get('seller_name') or row.get('seller_id')}"
                                 if row else "Такого запису немає.", html=False)

    def status_text(self) -> str:
        now = time.time()
        lines = [f"📊 Стан бота v{__version__}",
                 "Режим: " + ("⏸ пауза (/resume)" if self.db.get("paused") else "▶️ працює")]
        if self.next_run_at:
            lines.append(f"Наступна перевірка: {dt.datetime.fromtimestamp(self.next_run_at, TZ):%H:%M}")
        for n in self.s.sources:
            h, st = self.db.load_health(n), self.alerts[n]
            last_ok = h.get("last_ok_at")
            when = ts_local(last_ok) if last_ok else "ще не було"
            if st.consecutive:
                lines.append(f"{SOURCE_NAMES[n]}: ❌ збоїв поспіль {st.consecutive} ({st.last_error[:150]}); "
                             f"остання вдала: {when}")
            elif not self.db.get(f"initialized:{n}"):
                lines.append(f"{SOURCE_NAMES[n]}: ⏳ перша перевірка ще не завершилась")
            else:
                lines.append(f"{SOURCE_NAMES[n]}: ✅ остання перевірка {when} ({h.get('last_run_seconds', '?')} с), "
                             f"нових: {h.get('last_new', 0)}")
        midnight = dt.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        lines.append(f"Надіслано сьогодні: {self.db.count_posts(midnight)}, всього: {self.db.count_posts(0)}")
        lines.append(f"Чорний список: {len(self.db.blacklist_entries())}")
        tgst = self.alerts["telegram"]
        lines.append("Telegram: " + ("✅" if not tgst.consecutive else f"⚠️ збої ({tgst.last_error[:100]})"))
        up = int(now - self.started_at)
        lines.append(f"Працює з {ts_local(self.started_at)} ({up // 3600} год {up % 3600 // 60} хв)")
        return "\n".join(lines)

    def blacklist_view(self) -> tuple[str, Optional[dict]]:
        rows = self.db.blacklist_entries()
        if not rows:
            return "Чорний список порожній.", None
        lines = ["🏢 Посередники (їхні оголошення не надсилаю). Натисни, щоб зняти позначку:"]
        kb = []
        for r in rows[-40:]:
            name = r.get("seller_name") or r.get("seller_id") or "?"
            lines.append(f"#{r['id']} {SOURCE_NAMES.get(r['source'], r['source'])}: {name} "
                         f"(з {ts_local(r['created_at'])})")
            kb.append([{"text": f"❌ #{r['id']} {name}"[:60], "callback_data": f"r:{r['id']}"}])
        return "\n".join(lines), {"inline_keyboard": kb}

    def unblock(self, bl_id: int) -> Optional[dict]:
        row = self.db.deactivate_blacklist(bl_id)
        if row and row.get("post_id"):
            post = self.db.get_post(row["post_id"])
            if post and post.get("info_msg_id"):
                try:
                    lst = Listing.from_dict(json.loads(post["listing_json"]))
                    self.tg.edit_message_text(post["chat_id"], post["info_msg_id"], build_info(lst, self._dup_info(post)),
                                              kb_post(post["id"]))
                except TgError as e:
                    log.info("could not edit post card after unblock: %s", e)
        return row

    def _dup_info(self, post: dict) -> Optional[dict]:
        if not post.get("dup_of_post"):
            return None
        o = self.db.get_post(post["dup_of_post"])
        if not o:
            return None
        ol = Listing.from_dict(json.loads(o["listing_json"]))
        return {"source": ol.source, "url": ol.url, "sent_at": o["created_at"]}

    # ---------------------------------------------------------------- buttons
    def handle_callback(self, cq: dict):
        cid = cq.get("id")
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        data = cq.get("data") or ""
        if not chat_id or not self.is_allowed(chat_id):
            self.tg.answer_callback(cid, "Немає доступу")
            return
        kind, _, arg = data.partition(":")
        try:
            num = int(arg)
        except ValueError:
            self.tg.answer_callback(cid)
            return
        if kind in ("g", "c", "t"):
            post = self.db.get_post(num)
            if not post:
                self.tg.answer_callback(cid, "Оголошення вже немає в базі", alert=True)
                return
            if kind == "c" and self.db.get(f"published_tg:{num}"):
                self.tg.answer_callback(cid, "✅ Вже опубліковано в каналі", alert=True)
                return
            if kind == "t" and self.db.get(f"published_tt:{num}"):
                self.tg.answer_callback(cid, "✅ Чернетка вже в TikTok (вхідні → сповіщення)", alert=True)
                return
            with self._jobs_lock:
                if (kind, num) in self.jobs_active:
                    self.tg.answer_callback(cid, "Вже виконую, зачекай…")
                    return
                self.jobs_active.add((kind, num))
            self.jobs.put((kind, num, chat_id, msg.get("message_id")))
            self.tg.answer_callback(cid, {"g": "🎬 Генерую слайди…", "c": "📢 Публікую в канал…",
                                          "t": "🎵 Відправляю в TikTok…"}[kind])
        elif kind == "b":
            post = self.db.get_post(num)
            if not post:
                self.tg.answer_callback(cid, "Оголошення вже немає в базі", alert=True)
                return
            existing = self.db.active_blacklist_for_post(num)
            lst = Listing.from_dict(json.loads(post["listing_json"]))
            bl_id = existing["id"] if existing else self.db.add_blacklist(
                lst.source, lst.seller_id, lst.seller_name, text_fingerprint(lst.title, lst.description_text), num)
            try:
                self.tg.edit_message_text(chat_id, msg["message_id"], build_info(lst, self._dup_info(post), True),
                                          kb_post(num, bl_id))
            except TgError as e:
                log.info("edit after blacklist failed: %s", e)
            self.tg.answer_callback(cid, f"🏢 {lst.seller_name or 'Продавця'} позначено як посередника")
        elif kind == "u":
            row = self.unblock(num)
            self.tg.answer_callback(cid, "↩️ Позначку знято" if row else "Запис не знайдено")
        elif kind == "r":
            row = self.unblock(num)
            text, kb = self.blacklist_view()
            try:
                self.tg.edit_message_text(chat_id, msg["message_id"], text, kb)
            except TgError as e:
                log.info("edit blacklist view failed: %s", e)
            self.tg.answer_callback(cid, "↩️ Позначку знято" if row else "Запис не знайдено")
        else:
            self.tg.answer_callback(cid)

    # ================================================================== slide generator
    def generator_loop(self):
        name = threading.current_thread().name
        while not self.rt.stop.is_set():
            self.rt.beat(name)
            try:
                kind, post_id, chat_id, msg_id = self.jobs.get(timeout=5)
            except queue.Empty:
                continue
            what = {"g": "згенерувати слайди", "c": "опублікувати в канал", "t": "відправити в TikTok"}[kind]
            try:
                if kind == "g":
                    self.generate(post_id, chat_id)
                elif kind == "c":
                    self.publish_channel(post_id, chat_id, msg_id)
                else:
                    self.push_tiktok(post_id, chat_id, msg_id)
            except Exception as e:
                log.exception("Job %s for post %s failed: %s", kind, post_id, e)
                try:
                    self.tg.send_message(chat_id, f"⚠️ Не вдалося {what}: {type(e).__name__}: {str(e)[:200]}",
                                         html=False, reply_to=msg_id)
                except TgError:
                    pass
            finally:
                with self._jobs_lock:
                    self.jobs_active.discard((kind, post_id))

    def generate(self, post_id: int, chat_id: int) -> list:
        name = threading.current_thread().name
        post = self.db.get_post(post_id)
        lst = Listing.from_dict(json.loads(post["listing_json"]))
        out_dir = os.path.join(self.s.tmp_dir, f"gen_{post_id}_{int(time.time())}")
        try:
            paths = slides.generate(lst, out_dir, self.s.max_slides, beat=lambda: self.rt.beat(name))
            reply = post.get("info_msg_id")
            items = []
            for p in paths:
                with open(p, "rb") as f:
                    items.append((f"{lst.source}_{lst.source_id}_{p.name}", f.read(), "image/jpeg"))
            if len(items) >= 2:
                try:
                    self.tg.send_media_group(chat_id, items, kind="document")
                except TgError as e:
                    if not e.permanent:
                        raise
                    log.warning("Document album rejected (%s), sending one by one", e)
                    for fn, data, mime in items:
                        self.tg.send_document(chat_id, fn, data, mime)
            else:
                self.tg.send_document(chat_id, *items[0])
            del items
            cap = tiktok_caption(lst, footer=self.s.post_footer)
            self.tg.send_message(chat_id, cap, html=False, reply_to=reply,
                                 reply_markup=self._kb_publish(post_id))
            log.info("Generated %d slides for post #%d (caption %d chars)", len(paths), post_id, utf16_len(cap))
            return paths
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    # ================================================================== publishing
    def _kb_publish(self, post_id: int) -> Optional[dict]:
        return kb_publish(post_id, bool(self.s.channel_id), bool(self.sink),
                          bool(self.db.get(f"published_tg:{post_id}")), bool(self.db.get(f"published_tt:{post_id}")))

    def _refresh_publish_kb(self, chat_id: int, msg_id: Optional[int], post_id: int):
        if not msg_id:
            return
        try:
            self.tg.call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": msg_id,
                                                    "reply_markup": self._kb_publish(post_id)}, tries=2)
        except TgError as e:
            log.info("could not update publish buttons: %s", e)

    def publish_channel(self, post_id: int, chat_id: int, msg_id: Optional[int]):
        """Album + post (the same text as in the bot chat) to TELEGRAM_CHANNEL_ID."""
        if not self.s.channel_id:
            self.tg.send_message(chat_id, "⚠️ Канал не налаштовано (TELEGRAM_CHANNEL_ID).", html=False)
            return
        post = self.db.get_post(post_id)
        lst = Listing.from_dict(json.loads(post["listing_json"]))
        text = build_post(lst, footer=self.s.post_footer)
        try:
            photos = self._telegram_photos(lst, {"dhash": "-"})
            ids, used_caption = self._send_album(self.s.channel_id, photos, text.html if text.fits_caption else None)
            if not used_caption:
                self._send_text(self.s.channel_id, text)
        except TgError as e:
            log.warning("channel publish of post #%d failed: %s", post_id, e)
            hint = (" Перевір, що бот є адміном каналу з правом публікувати."
                    if e.permanent and ("chat not found" in e.description or "rights" in e.description
                                        or "forbidden" in e.description.lower()) else "")
            self.tg.send_message(chat_id, f"⚠️ Не вдалося опублікувати в канал: {e.description}.{hint}",
                                 html=False, reply_to=msg_id)
            return
        self.db.set(f"published_tg:{post_id}", time.time())
        log.info("Post #%d published to channel %s (%d photos)", post_id, self.s.channel_id, len(ids))
        self._refresh_publish_kb(chat_id, msg_id, post_id)
        self.tg.send_message(chat_id, "✅ Опубліковано в канал.", html=False, reply_to=msg_id, silent=True)

    def push_tiktok(self, post_id: int, chat_id: int, msg_id: Optional[int]):
        """Slides (generated again, same result) + post text -> TikTok draft through tiktok-connect."""
        if not self.sink:
            self.tg.send_message(chat_id, "⚠️ TikTok не налаштовано (TIKTOK_CONNECT_*).", html=False)
            return
        name = threading.current_thread().name
        post = self.db.get_post(post_id)
        lst = Listing.from_dict(json.loads(post["listing_json"]))
        out_dir = os.path.join(self.s.tmp_dir, f"tt_{post_id}_{int(time.time())}")
        try:
            paths = slides.generate(lst, out_dir, self.s.max_slides, beat=lambda: self.rt.beat(name))
            try:
                status = self.sink.push_draft(paths, tiktok_title(lst), tiktok_caption(lst, footer=self.s.post_footer))
            except DraftError as e:
                log.warning("TikTok draft for post #%d failed: %s", post_id, e)
                self.tg.send_message(chat_id, f"⚠️ TikTok: {e.human()}", html=False, reply_to=msg_id)
                return
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
        self.db.set(f"published_tt:{post_id}", time.time())
        log.info("Post #%d sent to TikTok drafts (%s, %d slides)", post_id, status, len(paths))
        self._refresh_publish_kb(chat_id, msg_id, post_id)
        note = ("" if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE")
                else f" (TikTok ще обробляє: {status}, зазвичай з'являється за хвилину)")
        self.tg.send_message(chat_id, "✅ Чернетка в TikTok: вхідні → системні сповіщення → додай музику й "
                                      "опублікуй." + note, html=False, reply_to=msg_id, silent=True)

    # ================================================================== lifecycle
    def start_threads(self):
        self.rt.start_watchdog()
        self.threads = []
        for target, name in ((self.updates_loop, "updates"), (self.generator_loop, "generator")):
            t = threading.Thread(target=self._guard(target, name), name=name, daemon=True)
            t.start()
            self.threads.append(t)

    def _guard(self, fn, name):
        def run():
            while not self.rt.stop.is_set():
                try:
                    fn()
                    return
                except Exception as e:  # a crashed thread restarts itself; the watchdog covers hangs
                    log.exception("Thread %s crashed: %s; restarting in 5s", name, e)
                    self.rt.wait(5, name)
        return run

    def startup_checks(self) -> bool:
        if not self.s.token:
            log.error("TELEGRAM_BOT_TOKEN is not set")
            return False
        try:
            me = self.tg.get_me()
            log.info("Telegram bot @%s", (me or {}).get("username"))
        except TgError as e:
            if e.code in (401, 404):
                log.error("Telegram rejected the token: %s", e)
                return False
            log.warning("Telegram unreachable at start (%s); continuing", e)
        marker = self.rt.pop_restart_marker()
        if marker and marker.get("reason") == "hang":
            self.notify(f"♻️ Бот перезапустився після зависання ({marker.get('component')}, "
                        f"{marker.get('idle_s')} с без прогресу). Працюю далі.")
        return True

    def run(self) -> int:
        if not self.startup_checks():
            return EXIT_CONFIG
        threading.current_thread().name = "poller"
        self.start_threads()
        sd_notify("READY=1")
        log.info("Started v%s: sources=%s interval=%ss data=%s owner=%s", __version__, ",".join(self.s.sources),
                 self.s.interval_s, self.s.data_dir, self.owner_chat())
        try:
            self.poller_loop()
        finally:
            self.rt.stop.set()
            sd_notify("STOPPING=1")
            for t in getattr(self, "threads", []):
                t.join(timeout=5)
            if not any(t.is_alive() for t in getattr(self, "threads", [])):
                self.db.close()          # otherwise leave it: WAL keeps the file consistent anyway
            log.info("Stopped")
        return 0
