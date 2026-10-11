from __future__ import annotations

import io
import logging
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

from tiktok_connect.config import Config
from tiktok_connect.server import App, make_server

from . import fake_tiktok


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http(method, url, headers=None, data=None):
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        with _opener.open(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class Env:
    """Fake TikTok + real app server on ephemeral ports, temp DATA_DIR, captured logs."""

    def __init__(self, state_ttl=600):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.fake_srv, self.fake, self.fake_url = fake_tiktok.start_fake()
        self.config = Config(
            client_key=fake_tiktok.CLIENT_KEY, client_secret=fake_tiktok.CLIENT_SECRET,
            public_base_url="http://127.0.0.1", redirect_uri=fake_tiktok.REDIRECT_URI,
            data_dir=self.data_dir, api_base=self.fake_url,
            authorize_url="https://www.tiktok.com/v2/auth/authorize/", state_ttl_seconds=state_ttl,
            display_tz="Europe/Warsaw",
        )
        self.app = App(self.config)
        self.srv = make_server(self.app, "127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.log_buf = io.StringIO()
        self.log_handler = logging.StreamHandler(self.log_buf)
        self.log_handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(self.log_handler)
        logging.getLogger().setLevel(logging.DEBUG)

    def close(self):
        logging.getLogger().removeHandler(self.log_handler)
        self.srv.shutdown()
        self.srv.server_close()
        self.fake_srv.shutdown()
        self.fake_srv.server_close()
        self.tmp.cleanup()

    def start_oauth(self):
        """GET /tiktok/connect -> (state, cookie_value)."""
        import urllib.parse
        status, headers, _ = http("GET", self.base + "/tiktok/connect")
        assert status == 302, status
        loc = urllib.parse.urlsplit(headers["Location"])
        q = urllib.parse.parse_qs(loc.query)
        cookie = headers["Set-Cookie"].split(";")[0]
        return q["state"][0], cookie, headers

    def connect(self, code="good-code"):
        import urllib.parse
        state, cookie, _ = self.start_oauth()
        return http("GET", self.base + "/tiktok/callback?" + urllib.parse.urlencode(
            {"code": code, "state": state, "scopes": "user.info.basic,video.upload"}), {"Cookie": cookie})
