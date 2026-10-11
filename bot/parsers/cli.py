"""Shared CLI for `python -m parsers.olx` / `python -m parsers.otodom`."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
import time

from . import config
from .common import HttpClient, SourceDownError, now_iso


class RecordingHttp(HttpClient):
    """HttpClient that also saves every response body (for fixtures/debugging)."""

    def __init__(self, source, out_dir, **kw):
        super().__init__(source, **kw)
        self.out_dir = out_dir
        os.makedirs(out_dir, exist_ok=True)
        self.n = 0

    def get(self, url, **kw):
        r = super().get(url, **kw)
        self.n += 1
        name = re.sub(r"[^A-Za-z0-9]+", "_", url.split("//", 1)[-1])[:120]
        ext = "json" if r.text.lstrip().startswith("{") else "html"
        with open(os.path.join(self.out_dir, f"{self.n:03d}_{name}.{ext}"), "w", encoding="utf-8") as f:
            f.write(r.text)
        return r


def _load_seen(path):
    if path and os.path.exists(path):
        with open(path) as f:
            return set(map(str, json.load(f)))
    return set()


def _save_seen(path, ids):
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(ids), f)
    os.replace(tmp, path)


def run_cli(parser_cls, source: str, argv=None) -> int:
    ap = argparse.ArgumentParser(prog=f"python -m parsers.{source}",
                                 description=f"{source}: Wrocław private flat rentals")
    ap.add_argument("--once", action="store_true", help="single run (default)")
    ap.add_argument("--loop", action="store_true", help=f"poll every {config.POLL_INTERVAL_S}s")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a text summary")
    ap.add_argument("--max-pages", type=int, default=config.MAX_PAGES)
    ap.add_argument("--max-age-hours", type=float, default=config.NEW_MAX_AGE_HOURS)
    ap.add_argument("--seen-file", help="JSON list of seen ids; loaded and updated")
    ap.add_argument("--show-rejected", action="store_true", help="include rejected items in JSON")
    ap.add_argument("--save-raw", metavar="DIR", help="save raw HTTP responses into DIR")
    ap.add_argument("--transport", choices=["curl_cffi", "curl"], help="force HTTP transport")
    ap.add_argument("--no-details", action="store_true", help="otodom: skip detail pages (fast, less data)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")

    http = (RecordingHttp(source, a.save_raw, transport=a.transport) if a.save_raw
            else HttpClient(source, transport=a.transport))
    kw = dict(http=http, max_pages=a.max_pages, max_age_hours=a.max_age_hours)
    if source == "otodom":
        kw["fetch_details"] = not a.no_details
    parser = parser_cls(**kw)
    seen = _load_seen(a.seen_file)

    while True:
        out = {"source": source, "run_at": now_iso(), "transport": http.transport}
        code = 0
        try:
            listings = parser.fetch_new(seen)
            res = parser.last_result
            seen |= res.seen_ids
            _save_seen(a.seen_file, seen)
            out["summary"] = res.summary()
            out["listings"] = [l.to_dict() for l in listings]
            if a.show_rejected:
                out["rejected"] = [r.to_dict() for r in res.rejected]
        except SourceDownError as e:
            code = 2
            out["error"] = {"kind": e.kind, "reason": e.reason, "status": e.status,
                            "alert": f"{source.upper()} відвалився: {e.reason}"}
        out["health"] = parser.health()
        if a.json:
            print(json.dumps(out, ensure_ascii=False, indent=1), flush=True)
        else:
            _print_text(out)
        if not a.loop:
            return code
        time.sleep(config.POLL_INTERVAL_S)


def _print_text(out):
    if "error" in out:
        print(f"[{out['source']}] ERROR {out['error']['kind']}: {out['error']['reason']}")
        return
    s = out["summary"]
    print(f"[{out['source']}] fetched={s['items_fetched']} passed={s['passed']} rejected={s['rejected']} "
          f"pages={s['pages']} requests={s['requests']} {s['seconds']}s via {out['transport']}")
    for k, v in s["rejected_by_reason"].items():
        print(f"   rejected {v:3d}  {k}")
    for l in out["listings"]:
        print(f" - {l['created_at']}  {l['price_pln']} zł + {l['czynsz_pln']}  {l['rooms']}p {l['area_m2']}m² "
              f"{l['building_type']}  {l['district']}  {l['url']}")
