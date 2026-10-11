import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from PIL import Image

from tiktok_connect.images import convert_image, prepare_images
from tiktok_connect.media import parse_range, resolve_media_path
from tiktok_connect.oauth_state import StateStore, state_hash
from tiktok_connect.storage import TokenStore, atomic_write_json
from tiktok_connect.test_draft import utf16_len


class StorageTests(unittest.TestCase):
    def test_atomic_write_perms_and_no_tmp_left(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "tokens.json"
            atomic_write_json(p, {"a": 1})
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            self.assertEqual(sorted(os.listdir(d)), ["tokens.json"])

    def test_concurrent_saves_and_reentrant_lock(self):
        with tempfile.TemporaryDirectory() as d:
            s = TokenStore(Path(d) / "t.json", Path(d) / ".lock")

            def worker(i):
                s.save_account({"open_id": f"id{i}", "access_token": "x", "access_expires_at": 1, "refresh_expires_at": 2})

            ts = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            self.assertEqual(len(s.load()["accounts"]), 20)
            with s.locked():
                with s.locked():  # must not deadlock
                    s.save_account({"open_id": "nested", "access_token": "y"})
            self.assertEqual(s.get_account()["open_id"], "nested")
            self.assertTrue(s.remove_account("nested"))
            self.assertNotEqual(s.load()["default_open_id"], "nested")


class MediaPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "media"
        (self.root / "batch1").mkdir(parents=True)
        (self.root / "batch1" / "01.jpg").write_bytes(b"x")
        (self.root / "batch1" / "note.txt").write_text("x")
        (self.root / ".hidden.jpg").write_bytes(b"x")
        outside = Path(self.tmp.name) / "secret.jpg"
        outside.write_bytes(b"s")
        (self.root / "link.jpg").symlink_to(outside)
        (self.root / "dir.jpg").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_allowed(self):
        self.assertIsNotNone(resolve_media_path(self.root, "batch1/01.jpg"))

    def test_rejected(self):
        for bad in ["../secret.jpg", "%2e%2e/secret.jpg", "batch1/../../secret.jpg", "batch1%2f..%2f..%2fsecret.jpg",
                    ".hidden.jpg", "batch1/note.txt", "batch1/", "batch1", "", "/etc/passwd", "link.jpg", "dir.jpg",
                    "batch1/01.jpg%00.png", "batch1\\01.jpg", "batch1/missing.jpg", "a/b/c/d/e.jpg", "%ff.jpg",
                    "batch1//01.jpg", "..%5c..%5csecret.jpg"]:
            with self.subTest(bad=bad):
                self.assertIsNone(resolve_media_path(self.root, bad))

    def test_range(self):
        self.assertEqual(parse_range("bytes=0-9", 100), (0, 9))
        self.assertEqual(parse_range("bytes=90-", 100), (90, 99))
        self.assertEqual(parse_range("bytes=-10", 100), (90, 99))
        self.assertEqual(parse_range("bytes=50-500", 100), (50, 99))
        self.assertEqual(parse_range("bytes=100-", 100), "invalid")
        self.assertIsNone(parse_range("bytes=0-1,5-6", 100))
        self.assertIsNone(parse_range("items=0-1", 100))
        self.assertIsNone(parse_range(None, 100))


class StateTests(unittest.TestCase):
    def test_single_use_expiry_cookie(self):
        s = StateStore(ttl_seconds=600)
        st = s.issue()
        self.assertEqual(s.consume(st, state_hash(st)), "ok")
        self.assertEqual(s.consume(st, state_hash(st)), "unknown")  # replay
        st2 = s.issue()
        self.assertEqual(s.consume(st2, "deadbeef"), "mismatch")
        self.assertEqual(s.consume(st2, state_hash(st2)), "unknown")  # consumed by failed attempt
        st3 = s.issue()
        self.assertEqual(s.consume(st3, None), "ok_nocookie")
        self.assertEqual(s.consume(None, None), "missing")
        self.assertEqual(s.consume("forged", state_hash("forged")), "unknown")
        short = StateStore(ttl_seconds=0)
        st4 = short.issue()
        time.sleep(0.01)
        self.assertEqual(short.consume(st4, state_hash(st4)), "unknown")


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_png_alpha_large_converted(self):
        p = self.d / "big.png"
        Image.new("RGBA", (1500, 2000), (255, 0, 0, 128)).save(p)
        data, w, h, warns = convert_image(p)
        self.assertEqual(data[:3], b"\xff\xd8\xff")
        self.assertEqual((w, h), (1080, 1440))
        huge = self.d / "huge.png"
        Image.new("RGB", (3000, 4000)).save(huge)
        with self.assertRaises(ValueError):  # memory guard for non-JPEG
            convert_image(huge)
        bigjpg = self.d / "big.jpg"
        Image.new("RGB", (6000, 8000), "red").save(bigjpg)
        self.assertEqual(convert_image(bigjpg)[1:3], (1080, 1440))  # JPEG decoded via draft()

    def test_landscape_and_webp_and_exif(self):
        p = self.d / "wide.webp"
        Image.new("RGB", (3200, 800), "blue").save(p, "WEBP")
        _, w, h, _ = convert_image(p)
        self.assertEqual((w, h), (1920, 480))
        # EXIF orientation 6 (rotate 90 CW) should swap dimensions
        j = self.d / "rot.jpg"
        img = Image.new("RGB", (800, 600), "green")
        exif = img.getexif()
        exif[0x0112] = 6
        img.save(j, "JPEG", exif=exif.tobytes())
        _, w, h, _ = convert_image(j)
        self.assertEqual((w, h), (600, 800))

    def test_small_warns_and_limits(self):
        p = self.d / "s.jpg"
        Image.new("RGB", (200, 300)).save(p)
        _, w, h, warns = convert_image(p)
        self.assertEqual((w, h), (200, 300))
        self.assertTrue(warns)
        with self.assertRaises(ValueError):
            prepare_images([p] * 36, self.d / "media")
        with self.assertRaises(FileNotFoundError):
            prepare_images([self.d / "nope.jpg"], self.d / "media")
        bad = self.d / "bad.jpg"
        bad.write_bytes(b"not an image")
        with self.assertRaises(OSError):
            prepare_images([bad], self.d / "media")

    def test_utf16(self):
        self.assertEqual(utf16_len("abc"), 3)
        self.assertEqual(utf16_len("😀"), 2)
        self.assertEqual(utf16_len("Привіт"), 6)


if __name__ == "__main__":
    unittest.main()
