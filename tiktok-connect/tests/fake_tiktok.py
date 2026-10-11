"""In-process fake of the TikTok Open API v2 endpoints used by tiktok-connect.

Mimics documented request/response shapes (OAuth flat JSON; v2 {data, error}
envelope), and for content/init actually downloads the photo URLs like TikTok's
PULL_FROM_URL would, validating JPEG/WebP and the 1080p limit.
"""

from __future__ import annotations

import io
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

CLIENT_KEY = "test_client_key_abc"
CLIENT_SECRET = "test_client_secret_SUPERSECRET"
REDIRECT_URI = "https://media.example.test/tiktok/callback"


class FakeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.codes = {"good-code": "open-id-friend-123456"}
        self.access = {}    # access_token -> open_id
        self.refresh = {}   # refresh_token -> open_id
        self.publishes = {}  # publish_id -> dict
        self.requests = []  # (method, path, body)
        self.force_errors = {}  # path -> list of (http_status, code, message) consumed FIFO
        self.scope = "user.info.basic,video.upload"
        self.expires_in = 86400
        self.pull_mode = "real"  # real | fail
        self.status_delay = 0.3

    def new_tokens(self, open_id: str) -> dict:
        at = "act." + secrets.token_hex(16)
        rt = "rft." + secrets.token_hex(16)
        self.access[at] = open_id
        self.refresh[rt] = open_id
        return {"access_token": at, "expires_in": self.expires_in, "open_id": open_id,
                "refresh_expires_in": 31536000, "refresh_token": rt, "scope": self.scope,
                "token_type": "Bearer"}


def _env(data=None, code="ok", message=""):
    return {"data": data or {}, "error": {"code": code, "message": message, "log_id": "FAKELOG" + secrets.token_hex(4)}}


class FakeHandler(BaseHTTPRequestHandler):
    st: FakeState

    def log_message(self, *a):
        pass

    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer_open_id(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        return self.st.access.get(auth[7:])

    def _forced(self, path):
        q = self.st.force_errors.get(path)
        if q:
            status, code, msg = q.pop(0)
            if path == "/v2/oauth/token/":
                self._json(status, {"error": code, "error_description": msg, "log_id": "FAKE"})
            else:
                self._json(status, _env(None, code, msg))
            return True
        return False

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        self.st.requests.append(("GET", u.path, u.query))
        if self._forced(u.path):
            return
        if u.path == "/v2/user/info/":
            oid = self._bearer_open_id()
            if not oid:
                return self._json(401, _env(None, "access_token_invalid", "The access token is invalid"))
            q = urllib.parse.parse_qs(u.query)
            if "fields" not in q:
                return self._json(400, _env(None, "invalid_params", "fields required"))
            return self._json(200, _env({"user": {"open_id": oid, "union_id": "u-1", "display_name": "Friend <b>Test</b>",
                                                   "avatar_url": "https://p16.example/avatar.jpeg"}}))
        self._json(404, _env(None, "not_found", "no route"))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        self.st.requests.append(("POST", u.path, raw))
        if self._forced(u.path):
            return
        st = self.st
        if u.path == "/v2/oauth/token/":
            if self.headers.get("Content-Type") != "application/x-www-form-urlencoded":
                return self._json(400, {"error": "invalid_request", "error_description": "bad content type"})
            f = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            if f.get("client_key") != CLIENT_KEY or f.get("client_secret") != CLIENT_SECRET:
                return self._json(401, {"error": "invalid_client", "error_description": "Client key or secret is incorrect.",
                                        "log_id": "L1"})
            with st.lock:
                if f.get("grant_type") == "authorization_code":
                    if f.get("redirect_uri") != REDIRECT_URI:
                        return self._json(400, {"error": "invalid_request",
                                                "error_description": "Redirect_uri is not matched with the uri when requesting code.",
                                                "log_id": "L2"})
                    oid = st.codes.pop(f.get("code"), None)
                    if not oid:
                        return self._json(400, {"error": "invalid_grant", "error_description": "Authorization code is expired.",
                                                "log_id": "L3"})
                    return self._json(200, st.new_tokens(oid))
                if f.get("grant_type") == "refresh_token":
                    oid = st.refresh.pop(f.get("refresh_token"), None)
                    if not oid:
                        return self._json(400, {"error": "invalid_grant", "error_description": "Refresh token is invalid or expired.",
                                                "log_id": "L4"})
                    return self._json(200, st.new_tokens(oid))
            return self._json(400, {"error": "unsupported_grant_type", "error_description": "bad grant"})
        if u.path == "/v2/oauth/revoke/":
            f = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            st.access.pop(f.get("token"), None)
            return self._json(200, {})
        oid = self._bearer_open_id()
        if not oid:
            return self._json(401, _env(None, "access_token_invalid", "The access token is invalid or not found in the request."))
        try:
            body = json.loads(raw or "{}")
        except ValueError:
            return self._json(400, _env(None, "invalid_param", "bad json"))
        if u.path == "/v2/post/publish/creator_info/query/":
            return self._json(401, _env(None, "scope_not_authorized", "The user did not authorize the scope required"))
        if u.path == "/v2/post/publish/content/init/":
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._json(400, _env(None, "invalid_param", "content type"))
            si = body.get("source_info") or {}
            imgs = si.get("photo_images") or []
            if (body.get("media_type") != "PHOTO" or body.get("post_mode") not in ("MEDIA_UPLOAD", "DIRECT_POST")
                    or si.get("source") != "PULL_FROM_URL" or not 1 <= len(imgs) <= 35
                    or not isinstance(si.get("photo_cover_index"), int) or not 0 <= si["photo_cover_index"] < len(imgs)):
                return self._json(400, _env(None, "invalid_param", "invalid photo post params"))
            pi = body.get("post_info") or {}
            if len(pi.get("title", "").encode("utf-16-le")) // 2 > 90:
                return self._json(400, _env(None, "invalid_param", "title too long"))
            pid = "p_inbox_url~v2." + secrets.token_hex(6)
            st.publishes[pid] = {"status": "PROCESSING_DOWNLOAD", "body": body, "open_id": oid, "fetched": [],
                                 "downloaded_bytes": 0}
            threading.Thread(target=self._process, args=(pid,), daemon=True).start()
            return self._json(200, _env({"publish_id": pid}))
        if u.path == "/v2/post/publish/status/fetch/":
            p = st.publishes.get(body.get("publish_id"))
            if not p:
                return self._json(400, _env(None, "invalid_publish_id", "The publish_id does not exist"))
            if p["open_id"] != oid:
                return self._json(400, _env(None, "token_not_authorized_for_specified_publish_id", "nope"))
            d = {"status": p["status"], "downloaded_bytes": p["downloaded_bytes"]}
            if p.get("fail_reason"):
                d["fail_reason"] = p["fail_reason"]
            return self._json(200, _env(d))
        self._json(404, _env(None, "not_found", "no route"))

    def _process(self, pid):
        st = self.st
        p = st.publishes[pid]
        time.sleep(st.status_delay)
        if st.pull_mode == "fail":
            p.update(status="FAILED", fail_reason="photo_pull_failed")
            return
        for url in p["body"]["source_info"]["photo_images"]:
            try:
                with urllib.request.urlopen(url, timeout=5) as r:
                    data = r.read()
                    ctype = r.headers.get("Content-Type")
                img = Image.open(io.BytesIO(data))
                p["fetched"].append({"url": url, "ctype": ctype, "format": img.format, "size": img.size})
                p["downloaded_bytes"] += len(data)
                if img.format not in ("JPEG", "WEBP"):
                    p.update(status="FAILED", fail_reason="file_format_check_failed")
                    return
                if min(img.size) > 1080 or max(img.size) > 1920:
                    p.update(status="FAILED", fail_reason="picture_size_check_failed")
                    return
            except Exception as exc:  # noqa: BLE001
                p.update(status="FAILED", fail_reason="photo_pull_failed", error=str(exc))
                return
        time.sleep(st.status_delay)
        p["status"] = "SEND_TO_USER_INBOX"


def start_fake():
    st = FakeState()
    handler = type("H", (FakeHandler,), {"st": st})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, st, f"http://127.0.0.1:{srv.server_address[1]}"
