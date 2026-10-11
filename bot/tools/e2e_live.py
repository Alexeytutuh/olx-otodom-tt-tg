"""End-to-end: the real bot process (python -m olxbot) against LIVE OLX/Otodom and the fake Telegram API.

Cycle 1 is silent (first run). Then a few of the just-seen listings are "forgotten" in the DB so cycle 2
treats them as new and posts them. The script then clicks the buttons (callback_query), sends /status,
and checks everything that arrived at the fake Telegram. Peak memory of the bot process is sampled
from /proc (VmHWM/VmRSS).

    python tools/e2e_live.py [--interval 120] [--runs 3] [--out samples/e2e]"""
from __future__ import annotations

import argparse
import json
import os
import resource
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BOT_DIR), str(BOT_DIR.parent)]
from tools.fake_telegram import serve  # noqa: E402

OWNER = 777000111


def ctl(api, what, obj):
    req = urllib.request.Request(f"{api}/_ctl/{what}", data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


def meta(db, key):
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        con.close()
        return json.loads(r[0]) if r else None
    except sqlite3.Error:
        return None


def wait_for(fn, timeout, what):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(1)
    raise SystemExit(f"TIMEOUT waiting for {what}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=120)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--forget-olx", type=int, default=3)
    ap.add_argument("--forget-otodom", type=int, default=2)
    ap.add_argument("--out", default=str(BOT_DIR / "samples" / "e2e"))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="olxbot_e2e_")
    data_dir = os.path.join(tmp, "data")
    srv, st = serve(0, os.path.join(tmp, "tg_files"))
    api = f"http://127.0.0.1:{srv.server_address[1]}"
    ctl(api, "update", {"message": {"message_id": 1, "chat": {"id": OWNER, "type": "private"},
                                    "from": {"id": OWNER}, "text": "/start"}})
    ctl(api, "fail", {"method": "sendMediaGroup", "code": 400,
                      "description": "Bad Request: failed to send message #1 with the error message "
                                     "\"WEBPAGE_MEDIA_EMPTY\"", "times": 1})
    env = dict(os.environ, TELEGRAM_BOT_TOKEN="E2E", TELEGRAM_API_BASE=api, DATA_DIR=data_dir,
               POLL_INTERVAL_S=str(a.interval), MAX_RUNS=str(a.runs), UPDATES_TIMEOUT_S="5",
               PYTHONPATH=f"{BOT_DIR}:{BOT_DIR.parent}", LOG_LEVEL="INFO", PYTHONUNBUFFERED="1")
    log_path = out / "bot.log"
    logf = open(log_path, "w")
    t_start = time.time()
    proc = subprocess.Popen([sys.executable, "-m", "olxbot"], cwd=str(BOT_DIR), env=env, stdout=logf,
                            stderr=subprocess.STDOUT)
    peak = {"rss_kb": 0, "hwm_kb": 0}

    def sampler():
        while proc.poll() is None:
            try:
                for line in open(f"/proc/{proc.pid}/status"):
                    if line.startswith("VmRSS:"):
                        peak["rss_kb"] = max(peak["rss_kb"], int(line.split()[1]))
                    if line.startswith("VmHWM:"):
                        peak["hwm_kb"] = max(peak["hwm_kb"], int(line.split()[1]))
            except OSError:
                pass
            time.sleep(0.3)
    threading.Thread(target=sampler, daemon=True).start()
    db = os.path.join(data_dir, "bot.sqlite3")
    report = {"started": t_start}
    try:
        wait_for(lambda: meta(db, "owner_chat_id") == OWNER, 60, "/start handled")
        wait_for(lambda: meta(db, "initialized:olx") and meta(db, "initialized:otodom"), a.interval + 400,
                 "first (silent) cycle")
        c1 = wait_for(lambda: meta(db, "last_cycle_at"), 120, "cycle 1 end")
        with st.lock:
            report["calls_after_cycle1"] = [c["method"] for c in st.calls]
        posts_c1 = [c for c in st.calls if c["method"] in ("sendMediaGroup", "sendPhoto")]
        assert not posts_c1, "first run must be silent"
        con = sqlite3.connect(db, timeout=30)
        forgotten = []
        for src, n in (("olx", a.forget_olx), ("otodom", a.forget_otodom)):
            # Otodom ads that were also imported into OLX would be (correctly) skipped as duplicates of the
            # OLX copy, so for the Otodom part pick ads without an OLX twin
            rows = con.execute("SELECT key, source_id FROM listings WHERE source=? AND status='silent' "
                               "AND dedupe_key NOT IN (SELECT dedupe_key FROM listings WHERE source<>?) "
                               "ORDER BY json_extract(json,'$.created_at') DESC LIMIT ?", (src, src, n)).fetchall()
            for key, sid in rows:
                con.execute("DELETE FROM seen WHERE source=? AND id=?", (src, sid))
                con.execute("DELETE FROM listings WHERE key=?", (key,))
                forgotten.append(key)
        con.commit()
        con.close()
        report["forgotten"] = forgotten
        print("cycle 1 silent OK; forgotten for cycle 2:", forgotten, flush=True)
        wait_for(lambda: (meta(db, "last_cycle_at") or 0) > c1, a.interval + 400, "cycle 2 end")
        with st.lock:
            info_msgs = [c for c in st.calls if c["method"] == "sendMessage"
                         and (c["params"].get("reply_markup") or {}).get("inline_keyboard")
                         and c["params"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("g:")]
        print(f"cycle 2: {len(info_msgs)} posts with buttons", flush=True)
        assert info_msgs, "cycle 2 must post the forgotten listings"
        oto = [m for m in info_msgs if "Otodom</b>" in m["params"]["text"]]
        report["otodom_posts"] = len(oto)
        g_id = (oto or info_msgs)[0]["params"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        b_id = info_msgs[-1]["params"]["reply_markup"]["inline_keyboard"][0][1]["callback_data"]
        msg_ids = {}
        with st.lock:
            for mid, m in st.messages.items():
                rm = m.get("reply_markup") or {}
                for row in rm.get("inline_keyboard", []):
                    for b in row:
                        msg_ids[b.get("callback_data")] = mid
        for cid, data in (("cbq-gen", g_id), ("cbq-bl", b_id)):
            ctl(api, "update", {"callback_query": {"id": cid, "from": {"id": OWNER}, "data": data,
                                                   "message": {"message_id": msg_ids[data],
                                                               "chat": {"id": OWNER, "type": "private"}}}})
        ctl(api, "update", {"message": {"message_id": 2, "chat": {"id": OWNER, "type": "private"},
                                        "text": "/status"}})
        ctl(api, "update", {"message": {"message_id": 3, "chat": {"id": 999, "type": "private"},
                                        "text": "/status"}})          # stranger: must be ignored
        wait_for(lambda: [c for c in st.calls if c["method"] == "sendMediaGroup"
                          and c["params"]["media"][0]["type"] == "document"], 180, "slides as documents")
        wait_for(lambda: proc.poll() is not None, a.interval * 2 + 400, "bot exit after MAX_RUNS")
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(30)
        logf.close()
        srv.shutdown()
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    with st.lock:
        allc = list(st.calls)
    json.dump(allc, open(out / "telegram_calls.json", "w"), ensure_ascii=False, indent=1, default=str)
    methods = {}
    for c in allc:
        methods[c["method"]] = methods.get(c["method"], 0) + 1
    docs = [c for c in allc if c["method"] == "sendMediaGroup" and c["params"]["media"][0]["type"] == "document"]
    slides = docs[0]["stored"] if docs else []
    caption_msgs = [c for c in allc if c["method"] == "sendMessage" and "#wroclaw" in c["params"].get("text", "")]
    edits = [c for c in allc if c["method"] == "editMessageText"]
    status = [c["params"]["text"] for c in allc if c["method"] == "sendMessage" and "Стан бота" in c["params"]["text"]]
    fallback = [c for c in allc if c["method"] == "sendMediaGroup" and c.get("injected_error")]
    to_stranger = [c for c in allc if str(c["params"].get("chat_id")) == "999"]
    res = {
        "exit_code": proc.returncode, "seconds": round(time.time() - t_start),
        "methods": methods, "forgotten": report.get("forgotten"), "otodom_posts": report.get("otodom_posts"),
        "album_fallback_injected": len(fallback),
        "photo_fallback_used": methods.get("sendPhoto", 0),
        "slides": [(s.get("width"), s.get("height"), s.get("format"), s.get("size")) for s in slides],
        "tiktok_caption_chars": len(caption_msgs[0]["params"]["text"]) if caption_msgs else None,
        "blacklist_edit": bool(edits and "Позначено як посередника" in edits[-1]["params"]["text"]),
        "status_reply": status[0] if status else None,
        "stranger_answered": bool(to_stranger),
        "peak_rss_mb": round(peak["rss_kb"] / 1024, 1), "peak_hwm_mb": round(peak["hwm_kb"] / 1024, 1),
        "ru_maxrss_mb_children": round(ru.ru_maxrss / 1024, 1),
    }
    json.dump(res, open(out / "result.json", "w"), ensure_ascii=False, indent=1)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    ok = (res["exit_code"] == 0 and res["slides"] and all(s[:3] == (1080, 1920, "JPEG") for s in res["slides"])
          and res["blacklist_edit"] and res["status_reply"] and not res["stranger_answered"]
          and res["album_fallback_injected"] == 1 and res["tiktok_caption_chars"])
    print("E2E", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
