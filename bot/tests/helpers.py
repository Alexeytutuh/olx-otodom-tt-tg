"""Shared test helpers: import paths, fake parsers, local photo bytes, a Bot wired to the fake Telegram."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOT_DIR = HERE.parent
for p in (str(BOT_DIR), str(BOT_DIR.parent)):          # bot/ and the folder that holds parsers/
    if p not in sys.path:
        sys.path.insert(0, p)
if not (BOT_DIR / "parsers").exists() and not (BOT_DIR.parent / "parsers").exists():
    raise RuntimeError("parsers package not found next to bot/")

from PIL import Image, ImageDraw  # noqa: E402

from parsers.common import SourceDownError  # noqa: E402
from parsers.models import FetchResult, Listing  # noqa: E402

FIXTURES = json.load(open(HERE / "fixtures" / "listings_live_2026-10-09.json", encoding="utf-8"))


def live_listings(source: str | None = None) -> list[Listing]:
    return [Listing.from_dict(d) for d in FIXTURES if source is None or d["source"] == source]


def make_jpeg(seed: int = 0, size=(1600, 1200)) -> bytes:
    im = Image.new("RGB", size, ((seed * 53) % 255, (seed * 97) % 255, (seed * 31) % 255))
    d = ImageDraw.Draw(im)
    for i in range(12):
        x = (seed * 37 + i * 131) % size[0]
        d.rectangle([x, (i * 97) % size[1], x + 200, (i * 97) % size[1] + 150],
                    fill=((i * 40 + seed * 11) % 255, (i * 70) % 255, (200 - i * 15) % 255))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def fake_download(url: str, timeout: float = 25, beat=None) -> bytes:
    """URLs like http://img/<seed>.jpg -> deterministic JPEG; 'broken' -> error."""
    if "broken" in url:
        raise OSError("HTTP 404 for photo")
    name = url.rsplit("/", 1)[-1].split(";")[0].split(".")[0]
    seed = int(name) if name.isdigit() else abs(hash(name)) % 1000
    portrait = "p" in url.rsplit("/", 1)[-1]
    return make_jpeg(seed, (900, 1400) if portrait else (1600, 1200))


def listing(sid: str, source: str = "olx", **kw) -> Listing:
    d = dict(source=source, source_id=sid, url=f"https://example.test/{source}/{sid}", title=f"Mieszkanie {sid}",
             description_text=kw.pop("description_text", f"Opis mieszkania numer {sid}. " * 3),
             price_pln=2400.0, area_m2=35.0, rooms=1, street="ul. Testowa", district="Krzyki",
             photos=[f"http://img/{int(sid) % 1000 + i}.jpg" for i in range(3)], seller_id=f"s{sid}",
             seller_name=f"Seller {sid}", created_at="2026-10-09T17:00:00+02:00", building_type="blok")
    d.update(kw)
    return Listing(**d)


class FakeParser:
    """Mimics OlxParser/OtodomParser: fetch_new(seen) -> list, .last_result, ._agent_fps."""

    def __init__(self, name: str):
        self.source = name
        self.batches: list = []         # each: list[Listing] | SourceDownError | (list, retry_ids)
        self.last_result = FetchResult()
        self._agent_fps: dict[str, str] = {}
        self.calls = 0

    def fetch_new(self, seen_ids=()):
        self.calls += 1
        seen = set(seen_ids)
        b = self.batches.pop(0) if self.batches else []
        if isinstance(b, Exception):
            raise b
        retry = set()
        if isinstance(b, tuple):
            b, retry = b
        new = [l for l in b if l.source_id not in seen]
        res = FetchResult(listings=new, seen_ids={l.source_id for l in new} | {"rej1"}, retry_ids=set(retry))
        self.last_result = res
        return new


def make_bot(tmp: str, api_base: str, sources=("olx", "otodom"), **overrides):
    from olxbot import images
    from olxbot.app import Bot
    from olxbot.settings import Settings
    images.download = fake_download
    s = Settings(token="TEST", api_base=api_base, data_dir=tmp, sources=tuple(sources), send_delay_s=0.0,
                 hang_timeout_s=600, **overrides)
    parsers = {n: FakeParser(n) for n in sources}
    return Bot(s, parsers=parsers)


def calls(st, method=None):
    with st.lock:
        return [c for c in st.calls if method is None or c["method"] == method]
