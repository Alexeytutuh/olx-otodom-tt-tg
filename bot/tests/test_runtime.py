import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests.helpers import calls, make_bot  # noqa: F401

from olxbot import runtime as R
from tools.fake_telegram import serve


class Watchdog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_heartbeat_file_and_hang_exit(self):
        hb = os.path.join(self.tmp, "heartbeat")
        marker = os.path.join(self.tmp, "marker.json")
        rt = R.Runtime(hb, hang_timeout_s=0.5, marker_path=marker)
        rt.beat("poller")
        self.assertTrue(os.path.exists(hb))
        self.assertLessEqual(abs(int(open(hb).read()) - time.time()), 2)
        exited = threading.Event()
        with mock.patch.object(R.os, "_exit", side_effect=lambda code: (exited.set(), rt.stop.set())), \
                mock.patch.object(R.logging, "shutdown"):
            t = threading.Thread(target=rt.watchdog_loop, kwargs={"check_every": 0.1}, daemon=True)
            t.start()
            self.assertTrue(exited.wait(5))          # poller never beat again -> exit
        m = json.load(open(marker))
        self.assertEqual((m["reason"], m["component"]), ("hang", "poller"))
        self.assertEqual(rt.pop_restart_marker()["component"], "poller")
        self.assertFalse(os.path.exists(marker))

    def test_stale_component_stops_heartbeat_file(self):
        hb = os.path.join(self.tmp, "heartbeat")
        rt = R.Runtime(hb, hang_timeout_s=0.3)
        rt.beat("poller")
        rt.beat("updates")
        first = os.path.getmtime(hb)
        time.sleep(0.5)
        rt._last_file_write = 0
        os.remove(hb)
        rt.beat("updates")                            # poller is stale -> file must NOT be refreshed
        self.assertFalse(os.path.exists(hb))
        self.assertTrue(first)

    def test_interruptible_wait(self):
        rt = R.Runtime(os.path.join(self.tmp, "hb"), 60)
        threading.Timer(0.2, rt.stop.set).start()
        t0 = time.time()
        self.assertTrue(rt.wait(30, "poller"))
        self.assertLess(time.time() - t0, 3)


class RestartAlert(unittest.TestCase):
    def test_alert_after_hang_restart(self):
        tmp = tempfile.mkdtemp()
        srv, st = serve(0, os.path.join(tmp, "files"))
        try:
            bot = make_bot(tmp, f"http://127.0.0.1:{srv.server_address[1]}")
            bot.db.set("owner_chat_id", 1)
            with open(os.path.join(tmp, "restart_marker.json"), "w") as f:
                json.dump({"reason": "hang", "component": "updates", "idle_s": 901}, f)
            self.assertTrue(bot.startup_checks())
            msgs = [c["params"]["text"] for c in calls(st, "sendMessage")]
            self.assertTrue(any("перезапустився після зависання" in m and "updates" in m for m in msgs))
            bot.s.token = ""
            self.assertFalse(bot.startup_checks())
            bot.db.close()
        finally:
            srv.shutdown()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_bad_token_is_config_error(self):
        tmp = tempfile.mkdtemp()
        srv, st = serve(0, os.path.join(tmp, "files"))
        try:
            bot = make_bot(tmp, f"http://127.0.0.1:{srv.server_address[1]}")
            bot.tg.base = bot.tg.base.replace("botTEST", "botBAD")
            self.assertEqual(bot.run(), R.EXIT_CONFIG)
        finally:
            srv.shutdown()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
