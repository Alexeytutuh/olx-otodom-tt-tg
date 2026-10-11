import contextlib
import io
import json
import os
import stat
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from PIL import Image

from tiktok_connect import test_draft
from tiktok_connect.tiktok_api import TikTokError
from tiktok_connect.tokens import NotConnected

from . import fake_tiktok
from .helpers import Env, http


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_health_pages_and_methods(self):
        b = self.env.base
        s, h, body = http("GET", b + "/health")
        self.assertEqual((s, json.loads(body)), (200, {"status": "ok"}))
        s, h, body = http("GET", b + "/")
        self.assertEqual(s, 200)
        self.assertIn("Підключити TikTok", body.decode())
        self.assertIn('href="/tiktok/connect"', body.decode())
        self.assertIn("Not connected", body.decode())
        self.assertIn("Content-Security-Policy", h)
        for p in ("/privacy", "/terms"):
            s, h, body = http("GET", b + p)
            self.assertEqual(s, 200)
            t = body.decode()
            self.assertIn("contact@example.com", t)          # defaults; real ones come from PAGES_* env
            self.assertIn("Your Name", t)
            self.assertIn("October 9, 2026", t)
        self.assertIn("Manage app permissions", http("GET", b + "/privacy")[2].decode())
        self.assertEqual(http("POST", b + "/", data=b"x")[0], 405)
        self.assertEqual(http("GET", b + "/nope")[0], 404)
        s, h, body = http("HEAD", b + "/health")
        self.assertEqual((s, body), (200, b""))

    def test_connect_redirect(self):
        state, cookie, h = self.env.start_oauth()
        loc = urllib.parse.urlsplit(h["Location"])
        self.assertEqual(f"{loc.scheme}://{loc.netloc}{loc.path}", "https://www.tiktok.com/v2/auth/authorize/")
        q = urllib.parse.parse_qs(loc.query)
        self.assertEqual(q["client_key"], [fake_tiktok.CLIENT_KEY])
        self.assertEqual(q["scope"], ["user.info.basic,video.upload"])
        self.assertEqual(q["response_type"], ["code"])
        self.assertEqual(q["redirect_uri"], [fake_tiktok.REDIRECT_URI])
        self.assertGreaterEqual(len(state), 40)
        self.assertIn("HttpOnly", h["Set-Cookie"])
        self.assertIn("SameSite=Lax", h["Set-Cookie"])
        self.assertNotIn(fake_tiktok.CLIENT_SECRET, h["Location"])

    def test_callback_success_and_storage(self):
        s, h, body = self.env.connect()
        t = body.decode()
        self.assertEqual(s, 200, t)
        self.assertIn("TikTok підключено", t)
        self.assertIn("Friend &lt;b&gt;Test&lt;/b&gt;", t)  # escaped display name
        self.assertIn("Max-Age=0", h["Set-Cookie"])
        f = self.env.config.tokens_file
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        data = json.loads(f.read_text())
        acc = data["accounts"]["open-id-friend-123456"]
        self.assertTrue(acc["access_token"].startswith("act."))
        self.assertTrue(acc["refresh_token"].startswith("rft."))
        self.assertAlmostEqual(acc["access_expires_at"], time.time() + 86400, delta=10)
        self.assertAlmostEqual(acc["refresh_expires_at"], time.time() + 31536000, delta=10)
        self.assertNotIn(acc["access_token"], t)
        home = http("GET", self.env.base + "/")[2].decode()
        self.assertIn("Friend &lt;b&gt;Test&lt;/b&gt;", home)
        self.assertNotIn(acc["access_token"], home)
        self.assertNotIn(acc["refresh_token"], home)
        # secrets/tokens/code never logged
        logs = self.env.log_buf.getvalue()
        for secret in (fake_tiktok.CLIENT_SECRET, acc["access_token"], acc["refresh_token"], "good-code"):
            self.assertNotIn(secret, logs)
        self.assertIn("/tiktok/callback", logs)
        # token exchange used form encoding with the right grant
        posts = [r for r in self.env.fake.requests if r[1] == "/v2/oauth/token/"]
        self.assertIn("grant_type=authorization_code", posts[0][2])

    def test_callback_state_errors(self):
        b = self.env.base
        # no state
        self.assertEqual(http("GET", b + "/tiktok/callback?code=good-code")[0], 400)
        # forged state
        self.assertEqual(http("GET", b + "/tiktok/callback?code=good-code&state=forged")[0], 400)
        # cookie mismatch
        state, cookie, _ = self.env.start_oauth()
        s, _, body = http("GET", b + "/tiktok/callback?" + urllib.parse.urlencode({"code": "good-code", "state": state}),
                          {"Cookie": "tt_oauth_state=0000"})
        self.assertEqual(s, 400)
        self.assertIn("CSRF", body.decode())
        # replay of a used state
        state, cookie, _ = self.env.start_oauth()
        url = b + "/tiktok/callback?" + urllib.parse.urlencode({"code": "good-code", "state": state})
        self.assertEqual(http("GET", url, {"Cookie": cookie})[0], 200)
        self.assertEqual(http("GET", url, {"Cookie": cookie})[0], 400)
        # missing cookie but valid state (mobile app hand-off) is accepted
        self.env.fake.codes["code2"] = "open-id-2"
        state, cookie, _ = self.env.start_oauth()
        url = b + "/tiktok/callback?" + urllib.parse.urlencode({"code": "code2", "state": state})
        self.assertEqual(http("GET", url)[0], 200)

    def test_callback_expired_state(self):
        self.env.app.states.ttl = 0
        state, cookie, _ = self.env.start_oauth()
        time.sleep(0.02)
        s, _, _ = http("GET", self.env.base + "/tiktok/callback?" + urllib.parse.urlencode(
            {"code": "good-code", "state": state}), {"Cookie": cookie})
        self.assertEqual(s, 400)

    def test_callback_user_denied_and_bad_code(self):
        state, cookie, _ = self.env.start_oauth()
        s, _, body = http("GET", self.env.base + "/tiktok/callback?" + urllib.parse.urlencode(
            {"error": "access_denied", "error_description": "User <script>x</script> cancelled", "state": state}),
            {"Cookie": cookie})
        self.assertEqual(s, 400)
        self.assertIn("Доступ не надано", body.decode())
        self.assertNotIn("<script>x", body.decode())
        s, _, body = self.env.connect(code="expired-code")
        self.assertEqual(s, 502)
        self.assertIn("invalid_grant", body.decode())
        self.assertFalse(self.env.config.tokens_file.exists())

    def test_partial_scopes_warns(self):
        self.env.fake.scope = "user.info.basic"
        s, _, body = self.env.connect()
        self.assertEqual(s, 200)
        self.assertIn("video.upload", body.decode())

    def test_media_serving(self):
        mdir = self.env.config.media_dir / "b1"
        mdir.mkdir(parents=True)
        Image.new("RGB", (100, 100), "red").save(mdir / "01.jpg")
        Image.new("RGB", (10, 10)).save(mdir / "x.webp", "WEBP")
        (mdir / "x.txt").write_text("nope")
        b = self.env.base
        s, h, body = http("GET", b + "/media/b1/01.jpg")
        self.assertEqual(s, 200)
        self.assertEqual(h["Content-Type"], "image/jpeg")
        self.assertIn("max-age", h["Cache-Control"])
        self.assertEqual(int(h["Content-Length"]), len(body))
        self.assertEqual(http("GET", b + "/media/b1/x.webp")[1]["Content-Type"], "image/webp")
        s, h2, body2 = http("HEAD", b + "/media/b1/01.jpg")
        self.assertEqual((s, body2, h2["Content-Length"]), (200, b"", h["Content-Length"]))
        s, h3, part = http("GET", b + "/media/b1/01.jpg", {"Range": "bytes=0-9"})
        self.assertEqual((s, part, h3["Content-Range"]), (206, body[:10], f"bytes 0-9/{len(body)}"))
        self.assertEqual(http("GET", b + "/media/b1/01.jpg", {"Range": "bytes=999999-"})[0], 416)
        self.assertEqual(http("GET", b + "/media/b1/01.jpg", {"If-None-Match": h["ETag"]})[0], 304)
        for bad in ["/media/b1/x.txt", "/media/b1/", "/media/", "/media/../tokens.json", "/media/%2e%2e/tokens.json",
                    "/media/b1/..%2f..%2ftokens.json", "/media/b1/missing.jpg", "/media/b1%2F01.jpg%00"]:
            with self.subTest(bad=bad):
                self.assertEqual(http("GET", b + bad)[0], 404)

    def test_cleanup_media(self):
        old = self.env.config.media_dir / "old"
        old.mkdir(parents=True)
        (old / "01.jpg").write_bytes(b"x")
        os.utime(old, (time.time() - 30 * 86400,) * 2)
        new = self.env.config.media_dir / "new"
        new.mkdir()
        self.assertEqual(self.env.app.cleanup_media(), 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.assertEqual(self.env.connect()[0], 200)
        self.tm = self.env.app.tokens
        self.oid = "open-id-friend-123456"

    def tearDown(self):
        self.env.close()

    def test_refresh_when_due_and_rotation(self):
        acc = self.tm.store.get_account(self.oid)
        self.assertEqual(self.tm.get_valid_account()["access_token"], acc["access_token"])  # not due
        acc["access_expires_at"] = int(time.time()) + 60  # due (margin 3600)
        self.tm.store.save_account(acc)
        new = self.tm.get_valid_account()
        self.assertNotEqual(new["access_token"], acc["access_token"])
        self.assertNotEqual(new["refresh_token"], acc["refresh_token"])  # rotated & persisted
        self.assertEqual(self.tm.store.get_account(self.oid)["refresh_token"], new["refresh_token"])
        self.assertEqual(new["display_name"], acc["display_name"])  # preserved on merge
        # background loop path
        new["access_expires_at"] = 0
        self.tm.store.save_account(new)
        self.tm.refresh_all_due()
        self.assertGreater(self.tm.store.get_account(self.oid)["access_expires_at"], time.time() + 80000)

    def test_refresh_errors(self):
        acc = self.tm.store.get_account(self.oid)
        self.env.fake.force_errors["/v2/oauth/token/"] = [(400, "invalid_grant", "Refresh token is invalid or expired.")]
        with self.assertRaises(TikTokError) as cm:
            self.tm.refresh_account(self.oid, force=True)
        self.assertEqual(cm.exception.code, "invalid_grant")
        self.assertEqual(self.tm.store.get_account(self.oid)["access_token"], acc["access_token"])  # untouched
        acc["refresh_expires_at"] = 0
        acc["access_expires_at"] = 0
        self.tm.store.save_account(acc)
        with self.assertRaises(NotConnected):
            self.tm.get_valid_account()
        self.env.fake.force_errors["/v2/oauth/token/"] = [(500, "internal_error", "boom")]
        acc["refresh_expires_at"] = int(time.time()) + 1000
        self.tm.store.save_account(acc)
        self.tm.refresh_all_due()  # logs, doesn't raise
        self.assertNotIn(acc["refresh_token"], self.env.log_buf.getvalue())

    def test_revoke(self):
        self.tm.revoke_and_forget(self.oid)
        self.assertIsNone(self.tm.store.get_account(self.oid))
        self.assertTrue(any(r[1] == "/v2/oauth/revoke/" for r in self.env.fake.requests))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.assertEqual(self.env.connect()[0], 200)
        self.environ = mock.patch.dict(os.environ, {
            "TIKTOK_CLIENT_KEY": fake_tiktok.CLIENT_KEY, "TIKTOK_CLIENT_SECRET": fake_tiktok.CLIENT_SECRET,
            "PUBLIC_BASE_URL": self.env.base, "REDIRECT_URI": fake_tiktok.REDIRECT_URI,
            "DATA_DIR": str(self.env.data_dir), "TIKTOK_API_BASE": self.env.fake_url, "LOG_LEVEL": "WARNING"})
        self.environ.start()
        self.poll = mock.patch.object(test_draft, "MIN_POLL_INTERVAL", 0.1)
        self.poll.start()

    def tearDown(self):
        self.poll.stop()
        self.environ.stop()
        self.env.close()

    def run_cli(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = test_draft.main(list(args))
        return rc, buf.getvalue()

    def last_publish(self):
        return list(self.env.fake.publishes.values())[-1]

    def test_demo_success(self):
        rc, out = self.run_cli("--demo", "--description", "Опис #test 🚀", "--poll-interval", "0.1", "--timeout", "20")
        self.assertEqual(rc, 0, out)
        self.assertIn("SEND_TO_USER_INBOX", out)
        self.assertIn("PROCESSING_DOWNLOAD", out)
        p = self.last_publish()
        body = p["body"]
        self.assertEqual(body["media_type"], "PHOTO")
        self.assertEqual(body["post_mode"], "MEDIA_UPLOAD")
        self.assertEqual(body["source_info"]["source"], "PULL_FROM_URL")
        self.assertEqual(body["source_info"]["photo_cover_index"], 0)
        self.assertEqual(len(body["source_info"]["photo_images"]), 3)
        self.assertEqual(body["post_info"]["description"], "Опис #test 🚀")
        self.assertIn("title", body["post_info"])
        self.assertNotIn("privacy_level", body["post_info"])
        self.assertTrue(all(f["format"] == "JPEG" and f["size"] == (1080, 1920) and f["ctype"] == "image/jpeg"
                            for f in p["fetched"]))
        self.assertTrue(all(u.startswith(self.env.base + "/media/") for u in body["source_info"]["photo_images"]))
        self.assertNotIn(self.env.app.tokens.store.get_account()["access_token"], out)

    def test_images_conversion(self):
        d = Path(self.env.data_dir) / "in"
        d.mkdir()
        Image.new("RGBA", (1600, 2400), (0, 0, 255, 200)).save(d / "a.png")
        Image.new("RGB", (2400, 1200), "yellow").save(d / "b.webp", "WEBP")
        rc, out = self.run_cli("--images", str(d / "a.png"), str(d / "b.webp"), "--cover-index", "1",
                               "--title", "Тест", "--poll-interval", "0.1", "--timeout", "20")
        self.assertEqual(rc, 0, out)
        p = self.last_publish()
        self.assertEqual([f["size"] for f in p["fetched"]], [(1080, 1620), (1920, 960)])
        self.assertEqual(p["body"]["source_info"]["photo_cover_index"], 1)
        self.assertNotIn("description", p["body"]["post_info"])

    def test_init_errors(self):
        self.env.fake.force_errors["/v2/post/publish/content/init/"] = [
            (403, "url_ownership_unverified", "URL ownership unverified")]
        rc, out = self.run_cli("--demo", "--skip-url-check")
        self.assertEqual(rc, 1)
        self.assertIn("url_ownership_unverified", out)
        self.assertIn("hint:", out)
        self.env.fake.force_errors["/v2/post/publish/content/init/"] = [
            (403, "spam_risk_too_many_pending_share", "too many pending")]
        rc, out = self.run_cli("--demo")
        self.assertEqual(rc, 1)
        self.assertIn("5 pending", out)

    def test_init_token_invalid_then_refresh_retry(self):
        self.env.fake.force_errors["/v2/post/publish/content/init/"] = [(401, "access_token_invalid", "expired")]
        rc, out = self.run_cli("--demo", "--poll-interval", "0.1", "--timeout", "20")
        self.assertEqual(rc, 0, out)
        self.assertIn("forcing refresh", out)

    def test_status_failed_and_rate_limited(self):
        self.env.fake.pull_mode = "fail"
        self.env.fake.force_errors["/v2/post/publish/status/fetch/"] = [(429, "rate_limit_exceeded", "slow down")]
        rc, out = self.run_cli("--demo", "--poll-interval", "0.1", "--timeout", "20")
        self.assertEqual(rc, 1, out)
        self.assertIn("will retry", out)
        self.assertIn("photo_pull_failed", out)

    def test_url_check_failure(self):
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": "http://127.0.0.1:1"}):
            rc, out = self.run_cli("--demo")
        self.assertEqual(rc, 1)
        self.assertIn("public URL check failed", out)
        self.assertEqual(self.env.fake.publishes, {})

    def test_validation_and_dry_run(self):
        rc, out = self.run_cli("--demo", "--title", "x" * 91)
        self.assertEqual(rc, 2)
        rc, out = self.run_cli("--demo", "--description", "😀" * 2001)
        self.assertEqual(rc, 2)
        rc, out = self.run_cli("--demo", "--cover-index", "5")
        self.assertEqual(rc, 2)
        rc, out = self.run_cli("--demo", "--dry-run", "--demo-count", "2")
        self.assertEqual(rc, 0)
        self.assertIn('"post_mode": "MEDIA_UPLOAD"', out)
        self.assertEqual(self.env.fake.publishes, {})

    def test_creator_info_expected_failure(self):
        rc, out = self.run_cli("--demo", "--creator-info", "--poll-interval", "0.1", "--timeout", "20")
        self.assertEqual(rc, 0, out)
        self.assertIn("creator_info not available", out)

    def test_not_connected(self):
        self.env.app.tokens.store.remove_account("open-id-friend-123456")
        rc, out = self.run_cli("--demo")
        self.assertEqual(rc, 1)
        self.assertIn("no connected TikTok account", out)


if __name__ == "__main__":
    unittest.main()


class InternalDraftTests(unittest.TestCase):
    """POST /internal/draft — the bot's way to create drafts."""

    def setUp(self):
        import dataclasses
        self.env = Env()
        self.assertEqual(self.env.connect()[0], 200)
        self.env.app.config = dataclasses.replace(self.env.config, internal_token="sekret",
                                                  public_base_url=self.env.base)

    def tearDown(self):
        self.env.close()

    def post(self, body, token="sekret", extra=None):
        import base64
        if isinstance(body, dict) and "images" in body:
            body = dict(body, images=[base64.b64encode(b).decode() for b in body["images"]])
        headers = {"Content-Type": "application/json", **(extra or {})}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        s, _, raw = http("POST", self.env.base + "/internal/draft", headers, json.dumps(body).encode())
        return s, json.loads(raw) if raw.startswith(b"{") else raw

    @staticmethod
    def jpeg(color="red"):
        buf = io.BytesIO()
        Image.new("RGB", (1080, 1920), color).save(buf, "JPEG")
        return buf.getvalue()

    def test_success(self):
        s, r = self.post({"title": "Kawalerka | 35 m²", "description": "Opis\nSzczegóły na zapytanie",
                          "images": [self.jpeg(), self.jpeg("blue")]})
        self.assertEqual(s, 200, r)
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "SEND_TO_USER_INBOX")
        body = list(self.env.fake.publishes.values())[-1]["body"]
        self.assertEqual(body["post_mode"], "MEDIA_UPLOAD")
        self.assertEqual(body["post_info"], {"title": "Kawalerka | 35 m²", "description": "Opis\nSzczegóły na zapytanie"})
        self.assertEqual(len(body["source_info"]["photo_images"]), 2)

    def test_auth_and_proxy_guard(self):
        img = {"title": "t", "description": "d", "images": [self.jpeg()]}
        self.assertEqual(self.post(img, token="wrong")[0], 401)
        self.assertEqual(self.post(img, token=None)[0], 401)
        self.assertEqual(self.post(img, extra={"X-Forwarded-For": "1.2.3.4"})[0], 404)
        self.assertEqual(self.post(img, extra={"CF-Connecting-IP": "1.2.3.4"})[0], 404)
        self.assertEqual(self.env.fake.publishes, {})

    def test_disabled_without_token(self):
        import dataclasses
        self.env.app.config = dataclasses.replace(self.env.app.config, internal_token="")
        self.assertEqual(self.post({"images": [self.jpeg()]}, token="")[0], 404)

    def test_bad_request_and_tiktok_error(self):
        self.assertEqual(self.post({"title": "t"})[0], 400)
        s, _, _ = http("POST", self.env.base + "/internal/draft",
                       {"Authorization": "Bearer sekret"}, b"not json")
        self.assertEqual(s, 400)
        self.env.fake.force_errors["/v2/post/publish/content/init/"] = [
            (403, "spam_risk_too_many_pending_share", "too many pending")]
        s, r = self.post({"title": "t", "description": "d", "images": [self.jpeg()]})
        self.assertEqual(s, 502)
        self.assertEqual(r["error"], "spam_risk_too_many_pending_share")
        self.assertIn("5 pending", r["hint"])

    def test_other_post_paths_still_405(self):
        self.assertEqual(http("POST", self.env.base + "/", data=b"x")[0], 405)
