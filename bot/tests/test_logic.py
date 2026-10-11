import io
import json
import os
import shutil
import tempfile
import time
import unittest

from tests.helpers import (FakeParser, SourceDownError, calls, fake_download, listing, live_listings, make_bot,
                           make_jpeg)

from PIL import Image

from olxbot import images, schedule
from olxbot.alerts import AlertState
from olxbot.dedupe import find_duplicate, listing_record
from tools.fake_telegram import serve

OWNER = 5001


class Schedule(unittest.TestCase):
    def test_alignment(self):
        base = 1_760_000_100.0            # multiple of 300 + 100
        self.assertEqual(schedule.next_slot(1_760_000_100, 300) % 300, 0)
        self.assertEqual(schedule.next_slot(1_759_999_800, 300), 1_760_000_100)   # exactly on slot -> next
        self.assertEqual(schedule.next_slot(1_759_999_801.5, 300), 1_760_000_100)
        self.assertEqual(schedule.current_or_next_slot(1_759_999_801.0, 300), 1_759_999_800)  # within grace
        self.assertEqual(schedule.current_or_next_slot(1_759_999_810.0, 300), 1_760_000_100)
        self.assertEqual(schedule.skipped_slots(1_759_999_800, 1_759_999_800 + 299, 300), 0)
        self.assertEqual(schedule.skipped_slots(1_759_999_800, 1_759_999_800 + 650, 300), 2)
        # wall-clock minutes: 300 s slots fall on :00/:05/... local time (Warsaw offset is whole hours)
        import datetime as dt
        t = dt.datetime.fromtimestamp(schedule.next_slot(time.time(), 300))
        self.assertEqual((t.minute % 5, t.second), (0, 0))
        self.assertTrue(base)

    def test_no_drift(self):
        slot = schedule.next_slot(1000.0, 300)
        for run_len in (12, 55, 299, 310, 20):
            finished = slot + run_len
            slot = schedule.next_slot(finished, 300)
            self.assertEqual(slot % 300, 0)


class Alerts(unittest.TestCase):
    def test_transitions(self):
        a = AlertState("olx", threshold=3, realert_s=3600)
        self.assertIsNone(a.failure("x", now=0))
        self.assertIsNone(a.failure("x", now=300))
        self.assertEqual(a.failure("x", now=600), "down")
        self.assertIsNone(a.failure("x", now=900))          # no spam
        self.assertEqual(a.failure("x", now=600 + 3600), "down")   # re-alert after realert_s
        self.assertEqual(a.success(now=5000), "recovered")
        self.assertIsNone(a.success(now=5300))
        self.assertIsNone(a.failure("y", now=5600))         # counter restarted
        self.assertIsNone(a.success(now=5900))              # recovered before threshold: silent
        b = AlertState.from_dict("olx", a.to_dict(), 3, 3600)
        self.assertEqual(b.total_failures, a.total_failures)


class DHash(unittest.TestCase):
    def test_dhash(self):
        a = images.open_image(make_jpeg(1))
        b = a.resize((800, 600))                                # same picture, smaller
        c = images.open_image(make_jpeg(7))
        ha, hb, hc = images.dhash(a), images.dhash(b), images.dhash(c)
        self.assertEqual(len(ha), 16)
        self.assertLessEqual(images.hamming(ha, hb), 4)
        self.assertGreater(images.hamming(ha, hc), 10)

    def test_sized_url(self):
        u = "https://ireland.apollo.olxcdn.com/v1/files/abc/image;s=2048x1536;q=80"
        self.assertEqual(images.sized_url(u, "1280x1280"),
                         "https://ireland.apollo.olxcdn.com/v1/files/abc/image;s=1280x1280;q=80")
        self.assertEqual(images.sized_url("https://x/y.jpg", "1x1"), "https://x/y.jpg")


class Dedupe(unittest.TestCase):
    def rec(self, l, **kw):
        r = listing_record(l, "sent")
        r.update(kw)
        return r

    def test_exact_dedupe_key(self):
        a = self.rec(listing("1", source="otodom", dedupe_key="otodom:IDabc"))
        b = self.rec(listing("2", source="olx", dedupe_key="otodom:IDabc", price_pln=9999.0))
        self.assertEqual(find_duplicate(b, [a], []).kind, "exact")

    def test_exact_same_text_and_price(self):
        txt = "Do wynajęcia mieszkanie przy ulicy Brzoskwiniowej, 26 m2, po remoncie, w pełni umeblowane. " * 3
        a = self.rec(listing("1", source="olx", description_text=txt, title="Kawalerka Brzoskwiniowa"))
        b = self.rec(listing("2", source="otodom", description_text=txt, title="Mieszkanie 26 m2"))
        r = find_duplicate(b, [], [a])
        self.assertEqual((r.kind, r.other["key"]), ("exact", "olx:1"))

    def test_possible_by_numbers_and_street(self):
        a = self.rec(listing("1", source="olx", street="Braterskiej", description_text="Tekst A " * 30,
                             title="Mieszkanie Krzyki"))
        b = self.rec(listing("2", source="otodom", street="ul. Braterska 5", description_text="Inny opis B " * 30,
                             title="Mieszkanie na Krzykach"))
        r = find_duplicate(b, [], [a])
        self.assertEqual(r.kind, "possible", r)
        self.assertIn("вулиця", r.why)

    def test_photo_hash_makes_exact(self):
        h = images.dhash(images.open_image(make_jpeg(3)))
        a = self.rec(listing("1", street=None, district="Krzyki", description_text="aaa " * 40), dhash=h)
        b = self.rec(listing("2", source="otodom", street=None, district="Krzyki / Gaj",
                             description_text="bbb " * 40), dhash=None)
        r = find_duplicate(b, [], [a], dhash_of=lambda row: row["dhash"] or h)
        self.assertEqual(r.kind, "exact")

    def test_different_flat_is_not_dup(self):
        a = self.rec(listing("1", area_m2=35.0))
        b = self.rec(listing("2", area_m2=58.0, rooms=1))
        self.assertEqual(find_duplicate(b, [], [a]).kind, "none")
        c = self.rec(listing("3", rooms=2))
        self.assertEqual(find_duplicate(c, [], [a]).kind, "none")

    def test_live_listings_have_no_false_exact(self):
        recs = [self.rec(l) for l in live_listings()]
        exact = []
        for i, r in enumerate(recs):
            d = find_duplicate(r, [], recs[:i])
            if d.kind == "exact":
                exact.append((r["key"], d.other["key"], d.why))
        # the only exact matches allowed among real listings are true cross-posts (same dedupe key / same text)
        for k1, k2, why in exact:
            self.assertTrue("id" in why or "текст" in why, (k1, k2, why))


class BotFlows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = tempfile.mkdtemp()
        cls.srv, cls.st = serve(0, cls.files)
        cls.api = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.files, ignore_errors=True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        with self.st.lock:
            self.st.calls.clear()
            self.st.failures.clear()
        self.bot = make_bot(self.tmp, self.api)
        self.bot.tg.backoff_base = 0.01
        self.bot.db.set("owner_chat_id", OWNER)

    def tearDown(self):
        self.bot.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def olx(self):
        return self.bot.parsers["olx"]

    def init_silent(self):
        for n in ("olx", "otodom"):
            self.bot.parsers[n].batches.append([listing("100", source=n)])
            self.bot.run_source(n)

    def test_first_run_is_silent_then_new_is_sent(self):
        self.olx().batches += [[listing("1"), listing("2")], [listing("1"), listing("2"), listing("3")]]
        self.bot.run_source("olx")
        self.assertEqual(calls(self.st, "sendMediaGroup"), [])
        self.assertTrue(self.bot.db.is_seen("olx", "1"))
        self.assertTrue(self.bot.db.is_seen("olx", "rej1"))         # rejected ids are seen too
        self.bot.parsers["otodom"].batches.append([])
        self.bot.run_source("otodom")
        summary = [c for c in calls(self.st, "sendMessage") if "Бот запущено" in c["params"]["text"]]
        self.assertEqual(len(summary), 1)
        self.bot.run_source("olx")
        albums = calls(self.st, "sendMediaGroup")
        self.assertEqual(len(albums), 1)
        cap = albums[0]["params"]["media"][0]["caption"]
        self.assertIn("<b>Kawalerka | 35 m²", cap)
        info = [c for c in calls(self.st, "sendMessage") if c["params"].get("reply_markup")]
        self.assertEqual(len(info), 1)
        kb = info[0]["params"]["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([b["text"] for b in kb], ["🎬 Згенерувати", "🏢 Посередник"])
        self.assertTrue(self.bot.db.is_seen("olx", "3"))
        # sent ONCE: another run with the same listing sends nothing
        self.olx().batches.append([listing("3")])
        self.bot.run_source("olx")
        self.assertEqual(len(calls(self.st, "sendMediaGroup")), 1)

    def test_retry_ids_never_marked_seen(self):
        self.olx().batches.append(([listing("1")], {"77"}))
        self.bot.run_source("olx")
        self.assertFalse(self.bot.db.is_seen("olx", "77"))
        self.assertTrue(self.bot.db.is_seen("olx", "1"))

    def test_album_fallback_to_photo_then_text(self):
        self.init_silent()
        with self.st.lock:
            self.st.failures["sendMediaGroup"] = [{"code": 400, "description": "Bad Request: IMAGE_PROCESS_FAILED"}]
            self.st.failures["sendPhoto"] = [{"code": 400, "description": "Bad Request: IMAGE_PROCESS_FAILED"}] * 3
        self.olx().batches.append([listing("5")])
        self.bot.run_source("olx")
        self.assertEqual(len(calls(self.st, "sendPhoto")), 3)
        texts = [c["params"]["text"] for c in calls(self.st, "sendMessage")]
        self.assertTrue(any(t.startswith("<b>Kawalerka") for t in texts))       # post as plain text message
        self.assertTrue(self.bot.db.is_seen("olx", "5"))

    def test_album_fallback_to_single_photo_and_flood_control(self):
        self.init_silent()
        with self.st.lock:
            self.st.failures["sendMediaGroup"] = [{"code": 400, "description": "Bad Request: wrong file"}]
            self.st.failures["sendPhoto"] = [{"code": 429, "description": "Too Many Requests: retry after 1",
                                              "retry_after": 1}]
        self.olx().batches.append([listing("6")])
        t0 = time.time()
        self.bot.run_source("olx")
        self.assertGreaterEqual(time.time() - t0, 1.0)            # honoured retry_after
        ok = [c for c in calls(self.st, "sendPhoto") if "injected_error" not in c]
        self.assertEqual(len(ok), 1)
        self.assertIn("<b>Kawalerka", ok[0]["params"]["caption"])

    def test_long_post_goes_as_separate_text(self):
        self.init_silent()
        self.olx().batches.append([listing("7", street="ul. " + "Bardzo Długa Nazwa " * 60)])
        self.bot.run_source("olx")
        al = calls(self.st, "sendMediaGroup")[0]
        self.assertNotIn("caption", al["params"]["media"][0])
        self.assertTrue(any("WYNAJEM" in c["params"]["text"] for c in calls(self.st, "sendMessage")))

    def test_telegram_down_keeps_listing_unseen(self):
        self.init_silent()
        with self.st.lock:
            self.st.failures["sendMediaGroup"] = [{"code": 502, "description": "Bad Gateway"}] * 5
        self.olx().batches.append([listing("8")])
        self.bot.run_source("olx")
        self.assertFalse(self.bot.db.is_seen("olx", "8"))
        self.olx().batches.append([listing("8")])
        self.bot.run_source("olx")                               # Telegram is back
        self.assertTrue(self.bot.db.is_seen("olx", "8"))
        self.assertEqual(len([c for c in calls(self.st, "sendMediaGroup") if "injected_error" not in c]), 1)

    def test_exact_cross_source_dup_skipped_and_possible_flagged(self):
        self.init_silent()
        self.bot.parsers["otodom"].batches.append([listing("10", source="otodom", dedupe_key="otodom:IDx", rooms=2,
                                                                   area_m2=51.0, street="ul. Inna")])
        self.bot.run_source("otodom")
        self.olx().batches.append([listing("11", dedupe_key="otodom:IDx",
                                           cross_source_ref={"source": "otodom", "id": "IDx", "url": "u"})])
        self.bot.run_source("olx")
        self.assertEqual(len(calls(self.st, "sendMediaGroup")), 1)           # second copy skipped
        self.assertEqual(self.bot.db.get_listing("olx:11")["status"], "dup")
        self.olx().batches.append([listing("12", description_text="Zupełnie inny opis " * 10, title="Inny tytuł",
                                           photos=["http://img/500.jpg"], street="Inna 3", rooms=2, area_m2=51.0)])
        self.bot.run_source("olx")
        info = [c["params"]["text"] for c in calls(self.st, "sendMessage") if "Можливий дубль" in c["params"]["text"]]
        self.assertEqual(len(info), 1)
        self.assertIn("Otodom-оголошення", info[0])
        self.assertIn("reply_parameters", [c for c in calls(self.st, "sendMessage")
                                           if "Можливий дубль" in c["params"]["text"]][0]["params"])

    def test_blacklist_button_undo_and_skip(self):
        self.init_silent()
        self.olx().batches.append([listing("20", seller_id="S1", seller_name="Agent Jan",
                                           description_text="Super oferta " * 20)])
        self.bot.run_source("olx")
        post = self.bot.db.post_for_listing("olx:20")
        info_id = post["info_msg_id"]
        cq = {"id": "c1", "data": f"b:{post['id']}", "message": {"message_id": info_id, "chat": {"id": OWNER}}}
        self.bot.handle_callback(cq)
        edit = calls(self.st, "editMessageText")[-1]["params"]
        self.assertIn("Позначено як посередника", edit["text"])
        undo = edit["reply_markup"]["inline_keyboard"][0][1]
        self.assertTrue(undo["callback_data"].startswith("u:"))
        self.assertEqual(len(self.bot.db.blacklist_entries()), 1)
        # same seller, new listing -> skipped; same text from another account on Otodom -> skipped
        self.olx().batches.append([listing("21", seller_id="S1", description_text="Inny tekst " * 20)])
        self.bot.run_source("olx")
        self.bot.parsers["otodom"].batches.append([listing("22", source="otodom", seller_id="Z9",
                                                           description_text="Super oferta " * 20, area_m2=70.0)])
        self.bot.run_source("otodom")
        self.assertEqual(len(calls(self.st, "sendMediaGroup")), 1)
        self.assertEqual(self.bot.db.get_listing("olx:21")["status"], "blacklisted")
        self.assertEqual(self.bot.db.get_listing("otodom:22")["status"], "blacklisted")
        # undo
        self.bot.handle_callback({"id": "c2", "data": undo["callback_data"],
                                  "message": {"message_id": info_id, "chat": {"id": OWNER}}})
        self.assertEqual(len(self.bot.db.blacklist_entries()), 0)
        last = calls(self.st, "editMessageText")[-1]["params"]
        self.assertNotIn("Позначено", last["text"])
        self.assertEqual(last["reply_markup"]["inline_keyboard"][0][1]["text"], "🏢 Посередник")
        self.olx().batches.append([listing("23", seller_id="S1", description_text="Jeszcze inny " * 20,
                                           area_m2=44.0)])
        self.bot.run_source("olx")
        self.assertEqual(len(calls(self.st, "sendMediaGroup")), 2)

    def test_blacklist_command_and_unblock(self):
        bl = self.bot.db.add_blacklist("olx", "S9", "Biuro X", None, None)
        self.bot.handle_message({"chat": {"id": OWNER}, "text": "/blacklist"})
        m = calls(self.st, "sendMessage")[-1]["params"]
        self.assertIn("Biuro X", m["text"])
        self.bot.handle_message({"chat": {"id": OWNER}, "text": f"/unblock {bl}"})
        self.assertEqual(self.bot.db.blacklist_entries(), [])

    def test_generate_button_sends_documents_and_caption(self):
        self.init_silent()
        photos = [f"http://img/{i}.jpg" for i in range(30, 41)] + ["http://img/broken.jpg", "http://img/p7.jpg"]
        self.olx().batches.append([listing("30", photos=photos, description_text="Mieszkanie z balkonem. " * 5)])
        self.bot.run_source("olx")
        post = self.bot.db.post_for_listing("olx:30")
        self.assertEqual(len(calls(self.st, "sendMediaGroup")[0]["params"]["media"]), 10)
        paths = self.bot.generate(post["id"], OWNER)
        self.assertEqual(len(paths), 10)
        docs = calls(self.st, "sendMediaGroup")[-1]
        self.assertTrue(all(m["type"] == "document" for m in docs["params"]["media"]))
        for f in docs["stored"]:
            self.assertEqual((f["width"], f["height"], f["format"]), (1080, 1920, "JPEG"))
        cap = calls(self.st, "sendMessage")[-1]["params"]
        self.assertNotIn("parse_mode", cap)
        self.assertNotIn("#", cap["text"])
        self.assertNotIn("reply_markup", cap)                      # no channel / TikTok configured
        self.assertFalse(os.listdir(self.bot.s.tmp_dir))           # temp files cleaned

    def test_generate_via_callback_worker_and_double_click(self):
        self.init_silent()
        self.olx().batches.append([listing("31")])
        self.bot.run_source("olx")
        post = self.bot.db.post_for_listing("olx:31")
        cq = {"id": "g1", "data": f"g:{post['id']}", "message": {"message_id": post["info_msg_id"],
                                                                 "chat": {"id": OWNER}}}
        self.bot.handle_callback(cq)
        self.bot.handle_callback(dict(cq, id="g2"))
        answers = [c["params"]["text"] for c in calls(self.st, "answerCallbackQuery")]
        self.assertIn("Вже виконую", answers[-1])
        import threading
        t = threading.Thread(target=self.bot.generator_loop, name="generator", daemon=True)
        t.start()
        deadline = time.time() + 30
        while time.time() < deadline and not [c for c in calls(self.st, "sendMediaGroup")
                                              if c["params"]["media"][0]["type"] == "document"]:
            time.sleep(0.2)
        self.bot.rt.stop.set()
        t.join(25)
        docs = [c for c in calls(self.st, "sendMediaGroup") if c["params"]["media"][0]["type"] == "document"]
        self.assertEqual(len(docs), 1)

    def test_source_down_alert_and_recovery(self):
        self.init_silent()
        err = SourceDownError("olx", "blocked", "HTTP 403 Forbidden", 403)
        for _ in range(4):
            self.olx().batches.append(err)
            self.bot.skip_until.clear()
            self.bot.run_source("olx")
        alerts = [c["params"]["text"] for c in calls(self.st, "sendMessage") if "відвалився" in c["params"]["text"]]
        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0].startswith("⚠️ OLX відвалився: HTTP 403 Forbidden"))
        self.assertIn("olx", self.bot.skip_until)                 # backoff after repeated blocks
        self.olx().batches.append([])
        self.bot.run_source("olx")
        rec = [c["params"]["text"] for c in calls(self.st, "sendMessage") if "знову працює" in c["params"]["text"]]
        self.assertEqual(rec, [rec[0]])
        self.assertTrue(rec[0].startswith("✅ OLX знову працює"))

    def test_pause_resume_and_status(self):
        self.init_silent()
        self.bot.handle_message({"chat": {"id": OWNER}, "text": "/pause"})
        self.olx().batches.append([listing("40")])
        self.bot.run_source("olx")
        self.assertEqual(calls(self.st, "sendMediaGroup"), [])
        self.assertTrue(self.bot.db.is_seen("olx", "40"))
        self.bot.handle_message({"chat": {"id": OWNER}, "text": "/resume"})
        self.olx().batches.append([listing("41")])
        self.bot.run_source("olx")
        self.assertEqual(len(calls(self.st, "sendMediaGroup")), 1)
        self.bot.handle_message({"chat": {"id": OWNER}, "text": "/status"})
        st = calls(self.st, "sendMessage")[-1]["params"]["text"]
        for s in ("OLX: ✅", "Otodom: ✅", "Надіслано сьогодні: 1, всього: 1", "Чорний список: 0"):
            self.assertIn(s, st)

    def test_owner_and_foreign_chats(self):
        self.bot.db.set("owner_chat_id", None)
        self.bot.s.allowed_chat_ids = {7, 8}
        self.bot.handle_message({"chat": {"id": 666}, "text": "/start"})
        self.assertIsNone(self.bot.owner_chat())
        self.bot.handle_message({"chat": {"id": 7}, "text": "/start"})
        self.assertEqual(self.bot.owner_chat(), 7)
        n = len(calls(self.st))
        self.bot.handle_message({"chat": {"id": 666}, "text": "/status"})
        self.bot.handle_callback({"id": "x", "data": "b:1", "message": {"message_id": 1, "chat": {"id": 666}}})
        self.assertEqual([c["method"] for c in calls(self.st)[n:]], ["answerCallbackQuery"])
        self.bot.handle_message({"chat": {"id": 8}, "text": "/status"})     # allowed, not owner
        self.assertIn("Стан бота", calls(self.st, "sendMessage")[-1]["params"]["text"])

    def test_publish_to_channel_and_tiktok(self):
        from pathlib import Path
        from olxbot.tiktok_hook import DraftError

        class FakeSink:
            def __init__(self):
                self.calls, self.fail = [], None

            def push_draft(self, slides, title, description):
                if self.fail:
                    raise DraftError(self.fail, "x")
                self.calls.append(([Path(p).name for p in slides], title, description))
                return "SEND_TO_USER_INBOX"

        self.bot.s.channel_id = "-100777"
        self.bot.s.post_footer = "Szczegóły na zapytanie\n☎️ +48 000"
        self.bot.sink = sink = FakeSink()
        self.init_silent()
        self.olx().batches.append([listing("40", description_text="Mieszkanie z balkonem. " * 5)])
        self.bot.run_source("olx")
        post = self.bot.db.post_for_listing("olx:40")
        album = calls(self.st, "sendMediaGroup")[-1]["params"]
        self.assertIn("Szczegóły na zapytanie", album["media"][0]["caption"])      # footer in the bot chat too
        sent, orig = [], self.bot.tg.send_message
        self.bot.tg.send_message = lambda *a, **kw: sent.append(orig(*a, **kw)) or sent[-1]
        self.bot.generate(post["id"], OWNER)
        self.bot.tg.send_message = orig
        cap = calls(self.st, "sendMessage")[-1]
        kb = cap["params"]["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([b["text"] for b in kb], ["📢 В канал", "🎵 В чернетки TikTok"])
        cap_id = sent[-1]["message_id"]
        # channel
        self.bot.publish_channel(post["id"], OWNER, cap_id)
        ch = [c for c in calls(self.st, "sendMediaGroup") if str(c["params"]["chat_id"]) == "-100777"]
        self.assertEqual(len(ch), 1)
        self.assertEqual(ch[0]["params"]["media"][0]["caption"], album["media"][0]["caption"])
        edit = calls(self.st, "editMessageReplyMarkup")[-1]["params"]
        self.assertEqual(edit["reply_markup"]["inline_keyboard"][0][0]["text"], "✅ В каналі")
        self.bot.handle_callback({"id": "c9", "data": f"c:{post['id']}",
                                  "message": {"message_id": cap_id, "chat": {"id": OWNER}}})
        self.assertIn("Вже опубліковано", calls(self.st, "answerCallbackQuery")[-1]["params"]["text"])
        self.assertTrue(self.bot.jobs.empty())
        # TikTok: error first, then success
        sink.fail = "spam_risk_too_many_pending_share"
        self.bot.push_tiktok(post["id"], OWNER, cap_id)
        self.assertIn("5 невідкритих", calls(self.st, "sendMessage")[-1]["params"]["text"])
        self.assertIsNone(self.bot.db.get(f"published_tt:{post['id']}"))
        sink.fail = None
        self.bot.push_tiktok(post["id"], OWNER, cap_id)
        names, title, desc = sink.calls[-1]
        self.assertTrue(names)
        self.assertTrue(title.startswith("2 pokoje") or title.startswith("Kawalerka"))
        self.assertTrue(desc.endswith("Szczegóły na zapytanie\n☎️ +48 000"))
        self.assertNotIn("#", desc)
        self.assertIn("Чернетка в TikTok", calls(self.st, "sendMessage")[-1]["params"]["text"])
        edit = calls(self.st, "editMessageReplyMarkup")[-1]["params"]
        self.assertEqual([b["text"] for b in edit["reply_markup"]["inline_keyboard"][0]],
                         ["✅ В каналі", "✅ Чернетка TikTok"])
        self.assertFalse(os.listdir(self.bot.s.tmp_dir))

    def test_agent_fps_persist_and_prune(self):
        self.olx().batches.append([])
        self.olx()._agent_fps["abc" * 50] = "999"
        self.bot.run_source("olx")
        self.assertEqual(self.bot.db.load_agent_fps("olx"), {"abc" * 50: "999"})
        self.bot.db.mark_seen("olx", ["old1"], at=time.time() - 31 * 86400)
        self.bot.db.prune(30)
        self.assertFalse(self.bot.db.is_seen("olx", "old1"))
        self.assertTrue(self.bot.db.is_seen("olx", "rej1"))


if __name__ == "__main__":
    unittest.main()
