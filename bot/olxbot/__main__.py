"""Entry point: python -m olxbot"""
from __future__ import annotations

import fcntl
import logging
import os
import signal
import sys


def main() -> int:
    from . import settings as settings_mod
    s = settings_mod.load()
    logging.basicConfig(level=getattr(logging, s.log_level, logging.INFO), stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    for noisy in ("PIL", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("olxbot")
    try:
        os.makedirs(s.data_dir, exist_ok=True)
        probe = os.path.join(s.data_dir, ".write_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        log.error("DATA_DIR %s is not writable (%s). On the server run: sudo chown -R 10001:10001 ./data",
                  s.data_dir, e)
        return 78
    lock = open(os.path.join(s.data_dir, "bot.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("Another bot instance already uses %s; exiting", s.data_dir)
        return 78
    from .app import Bot
    bot = Bot(s)

    def on_signal(sig, _frame):
        log.info("Signal %s received, stopping", sig)
        bot.rt.stop.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    return bot.run()


if __name__ == "__main__":
    sys.exit(main())
