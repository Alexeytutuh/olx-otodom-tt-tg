"""Fake Telegram Bot API for tests (point TELEGRAM_API_BASE at it).

Implements: getMe, getUpdates (long poll), sendMessage, sendPhoto, sendMediaGroup, sendDocument,
editMessageText, editMessageReplyMarkup, answerCallbackQuery, deleteWebhook. Validates the limits that
matter (caption 1024, text 4096, album 2..10, callback_data 64 bytes, HTML tags) and stores uploads.
Control API: POST /_ctl/update {update}, POST /_ctl/fail {method,code,description,retry_after,times},
GET /_ctl/calls, POST /_ctl/reset.

    python tools/fake_telegram.py --port 8081 --files /tmp/fake_tg_files"""
from __future__ import annotations

import argparse
import html
import io
import json
import os
import re
import threading
import time
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "s", "a", "code", "pre", "tg-spoiler", "blockquote"}


def html_plain(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s))


def check_html(s: str) -> str | None:
    stack = []
    for m in re.finditer(r"<(/?)([a-zA-Z-]+)[^>]*>", s):
        closing, tag = m.group(1), m.group(2).lower()
        if tag not in ALLOWED_TAGS:
            return f"Bad Request: can't parse entities: Unsupported start tag \"{tag}\""
        if closing:
            if not stack or stack[-1] != tag:
                return "Bad Request: can't parse entities: unmatched end tag"
            stack.pop()
        else:
            stack.append(tag)
    if stack:
        return "Bad Request: can't parse entities: unclosed tag"
    if re.search(r"&(?!(?:amp|lt|gt|quot|#\d+);)", s):
        return "Bad Request: can't parse entities: unescaped &"
    return None


class State:
    def __init__(self, files_dir: str):
        self.lock = threading.Condition()
        self.calls: list[dict] = []
        self.updates: list[dict] = []
        self.next_update = 1
        self.next_msg = 100
        self.failures: dict[str, list[dict]] = {}
        self.messages: dict[int, dict] = {}
        self.files_dir = files_dir
        os.makedirs(files_dir, exist_ok=True)


class Handler(BaseHTTPRequestHandler):
    st: State = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code: int, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code: int, desc: str, retry_after=None):
        o = {"ok": False, "error_code": code, "description": desc}
        if retry_after is not None:
            o["parameters"] = {"retry_after": retry_after}
        self._send(code, o)

    def _body(self) -> tuple[dict, dict]:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("multipart/form-data"):
            msg = BytesParser(policy=email_policy).parsebytes(
                b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw)
            params, files = {}, {}
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                fname = part.get_filename()
                data = part.get_payload(decode=True)
                if fname:
                    files[name] = (fname, data)
                else:
                    v = data.decode()
                    try:
                        v = json.loads(v) if v[:1] in "[{" else v
                    except ValueError:
                        pass
                    params[name] = v
            return params, files
        if raw:
            return json.loads(raw.decode()), {}
        return {}, {}

    def do_GET(self):
        if self.path.startswith("/_ctl/calls"):
            with self.st.lock:
                return self._send(200, self.st.calls)
        self._err(404, "Not Found")

    def do_POST(self):
        st = self.st
        if self.path.startswith("/_ctl/"):
            body, _ = self._body()
            what = self.path[6:]
            with st.lock:
                if what == "update":
                    body["update_id"] = st.next_update
                    st.next_update += 1
                    st.updates.append(body)
                    st.lock.notify_all()
                elif what == "fail":
                    for _ in range(int(body.get("times", 1))):
                        st.failures.setdefault(body["method"], []).append(body)
                elif what == "reset":
                    st.calls.clear()
            return self._send(200, {"ok": True})
        m = re.match(r"^/bot([^/]+)/(\w+)$", self.path)
        if not m:
            return self._err(404, "Not Found")
        token, method = m.groups()
        if token == "BAD":
            return self._err(401, "Unauthorized")
        params, files = self._body()
        with st.lock:
            fl = st.failures.get(method)
            fail = fl.pop(0) if fl else None
        rec = {"method": method, "params": params, "t": time.time(),
               "files": {k: {"name": v[0], "size": len(v[1])} for k, v in files.items()}}
        if fail:
            rec["injected_error"] = fail
            with st.lock:
                st.calls.append(rec)
            return self._err(fail.get("code", 400), fail.get("description", "Bad Request: injected"),
                             fail.get("retry_after"))
        try:
            result = getattr(self, f"m_{method}")(params, files, rec)
        except AttributeError:
            result = ("err", 404, f"Not Found: method {method}")
        if isinstance(result, tuple) and result and result[0] == "err":
            rec["error"] = result[2]
            with st.lock:
                st.calls.append(rec)
            return self._err(result[1], result[2])
        if method != "getUpdates" or result:
            with st.lock:
                st.calls.append(rec)
        self._send(200, {"ok": True, "result": result})

    # ------------------------------------------------------------------ helpers
    def _store(self, name: str, data: bytes) -> dict:
        info = {"name": name, "size": len(data)}
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(data))
            info.update(width=im.width, height=im.height, format=im.format)
        except Exception:
            pass
        path = os.path.join(self.st.files_dir, f"{int(time.time() * 1000)}_{name}")
        with open(path, "wb") as f:
            f.write(data)
        info["path"] = path
        return info

    def _msg(self, chat_id, **kw) -> dict:
        with self.st.lock:
            mid = self.st.next_msg
            self.st.next_msg += 1
        m = {"message_id": mid, "date": int(time.time()), "chat": {"id": int(chat_id), "type": "private"}, **kw}
        self.st.messages[mid] = m
        return m

    def _check_markup(self, p):
        rm = p.get("reply_markup")
        if isinstance(rm, str):
            rm = json.loads(rm)
        for row in (rm or {}).get("inline_keyboard", []):
            for b in row:
                if len(b.get("callback_data", "").encode()) > 64:
                    return ("err", 400, "Bad Request: BUTTON_DATA_INVALID")
        return None

    def _check_text(self, text, mode, limit):
        if mode == "HTML":
            e = check_html(text)
            if e:
                return ("err", 400, e)
            text = html_plain(text)
        if len(text.encode("utf-16-le")) // 2 > limit:
            return ("err", 400, "Bad Request: message is too long" if limit > 1024
                    else "Bad Request: message caption is too long")
        return None

    def _file(self, p, files, field, rec):
        ref = p.get(field, "")
        if isinstance(ref, str) and ref.startswith("attach://"):
            key = ref[9:]
            if key not in files:
                return None
            info = self._store(files[key][0], files[key][1])
            rec.setdefault("stored", []).append(info)
            return info
        return None

    # ------------------------------------------------------------------ methods
    def m_getMe(self, p, f, rec):
        return {"id": 42, "is_bot": True, "first_name": "Fake", "username": "fake_olx_bot"}

    def m_deleteWebhook(self, p, f, rec):
        return True

    def m_getUpdates(self, p, f, rec):
        offset = int(p.get("offset") or 0)
        timeout = float(p.get("timeout") or 0)
        end = time.time() + timeout
        with self.st.lock:
            while True:
                self.st.updates = [u for u in self.st.updates if u["update_id"] >= offset]
                if self.st.updates or time.time() >= end:
                    return list(self.st.updates)
                self.st.lock.wait(max(0.05, end - time.time()))

    def m_sendMessage(self, p, f, rec):
        e = self._check_text(p.get("text", ""), p.get("parse_mode"), 4096) or self._check_markup(p)
        if e:
            return e
        return self._msg(p["chat_id"], text=p.get("text"), reply_markup=p.get("reply_markup"))

    def m_sendPhoto(self, p, f, rec):
        e = self._check_text(p.get("caption") or "", p.get("parse_mode"), 1024) or self._check_markup(p)
        if e:
            return e
        info = self._file(p, f, "photo", rec)
        if not info or info.get("format") not in ("JPEG", "PNG"):
            return ("err", 400, "Bad Request: IMAGE_PROCESS_FAILED")
        return self._msg(p["chat_id"], photo=[info], caption=p.get("caption"))

    def m_sendDocument(self, p, f, rec):
        info = self._file(p, f, "document", rec)
        if not info:
            return ("err", 400, "Bad Request: there is no document in the request")
        return self._msg(p["chat_id"], document=info, caption=p.get("caption"))

    def m_sendMediaGroup(self, p, f, rec):
        media = p.get("media")
        if isinstance(media, str):
            media = json.loads(media)
        if not isinstance(media, list) or not 2 <= len(media) <= 10:
            return ("err", 400, "Bad Request: media must include 2-10 items")
        kinds = {m.get("type") for m in media}
        if "document" in kinds and len(kinds) > 1:
            return ("err", 400, "Bad Request: documents can't be mixed with other media types")
        out = []
        for m in media:
            e = self._check_text(m.get("caption") or "", m.get("parse_mode"), 1024)
            if e:
                return e
        for m in media:
            info = self._file(m, f, "media", rec)
            if not info:
                return ("err", 400, "Bad Request: wrong file identifier/HTTP URL specified")
            out.append(self._msg(p["chat_id"], media_type=m["type"], file=info, caption=m.get("caption")))
        return out

    def m_editMessageText(self, p, f, rec):
        mid = int(p.get("message_id"))
        if mid not in self.st.messages:
            return ("err", 400, "Bad Request: message to edit not found")
        e = self._check_text(p.get("text", ""), p.get("parse_mode"), 4096) or self._check_markup(p)
        if e:
            return e
        m = self.st.messages[mid]
        if m.get("text") == p.get("text") and m.get("reply_markup") == p.get("reply_markup"):
            return ("err", 400, "Bad Request: message is not modified")
        m.update(text=p.get("text"), reply_markup=p.get("reply_markup"))
        return m

    def m_editMessageReplyMarkup(self, p, f, rec):
        mid = int(p.get("message_id"))
        if mid not in self.st.messages:
            return ("err", 400, "Bad Request: message to edit not found")
        self.st.messages[mid]["reply_markup"] = p.get("reply_markup")
        return self.st.messages[mid]

    def m_answerCallbackQuery(self, p, f, rec):
        return True


def serve(port: int = 0, files_dir: str = "/tmp/fake_tg_files"):
    st = State(files_dir)
    handler = type("H", (Handler,), {"st": st})
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, st


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--files", default="/tmp/fake_tg_files")
    a = ap.parse_args()
    srv, _ = serve(a.port, a.files)
    print(f"fake Telegram on http://127.0.0.1:{srv.server_address[1]}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
