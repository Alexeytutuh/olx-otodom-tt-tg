"""Live smoke test: hits the real sites once per parser and prints a report.

    cd /workspace/olx-tiktok && python -m parsers.smoke_live [--max-age-hours 48] [--json-out FILE]

Exit code 0 when both sources worked and returned >0 items, 2 otherwise.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import config
from .common import SourceDownError
from .olx import OlxParser
from .otodom import OtodomParser

COVER_FIELDS = ["price_pln", "czynsz_pln", "kaucja_pln", "building_type", "available_from", "area_m2",
                "rooms", "floor", "street", "lat", "photos", "media_pln", "prad_pln", "parking_pln"]


def coverage(listings):
    n = len(listings) or 1
    out = {}
    for f in COVER_FIELDS:
        have = sum(1 for l in listings if (getattr(l, f) not in (None, [], "")))
        out[f] = f"{have}/{len(listings)} ({100 * have // n}%)"
    return out


def abridged(l):
    d = l.to_dict()
    d["description_text"] = d["description_text"][:160] + ("…" if len(d["description_text"]) > 160 else "")
    d["photos"] = d["photos"][:2] + ([f"... +{len(l.photos) - 2} more"] if len(l.photos) > 2 else [])
    d.pop("raw_extra", None)
    return d


def run(parser, name):
    t = time.monotonic()
    try:
        out = parser.fetch_new(set())
        res = parser.last_result
        return {"ok": True, "summary": res.summary(), "coverage": coverage(out),
                "samples": [abridged(l) for l in out[:3]], "health": parser.health(),
                "wall_s": round(time.monotonic() - t, 1)}, out
    except SourceDownError as e:
        return {"ok": False, "error": f"{e.kind}: {e.reason}", "alert": f"{name} відвалився: {e.reason}",
                "health": parser.health(), "wall_s": round(time.monotonic() - t, 1)}, []


def cross_listing(olx_parser, oto_listings, pages=3):
    """Look at raw OLX pages (same private search) for Otodom imports and match them
    against the Otodom private listings we just fetched."""
    raw = []
    for i in range(pages):
        raw += olx_parser.fetch_page(i * config.OLX_PAGE_LIMIT)["data"]
    imports = [o for o in raw if (o.get("partner") or {}).get("code") or o.get("external_url")]
    oto_by_pid = {l.raw_extra.get("public_id"): l for l in oto_listings}
    matches = []
    for o in imports:
        pid = (o.get("external_url") or "").rsplit("-", 1)[-1]
        if pid in oto_by_pid:
            ol = oto_by_pid[pid]
            matches.append({"olx_id": o["id"], "olx_title": o["title"], "otodom_id": ol.source_id,
                            "public_id": pid, "same_title": o["title"].strip() == ol.title.strip(),
                            "olx_seller": (o.get("user") or {}).get("name"), "otodom_seller": ol.seller_name,
                            "olx_created": o.get("created_time"), "otodom_created": ol.created_at})
    return {"olx_raw_items": len(raw), "olx_otodom_imports": len(imports),
            "import_external_urls_sample": [o.get("external_url") for o in imports[:3]],
            "matched_with_otodom_private_run": matches}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age-hours", type=float, default=config.NEW_MAX_AGE_HOURS)
    ap.add_argument("--json-out")
    ap.add_argument("--no-cross", action="store_true")
    a = ap.parse_args(argv)
    olx, oto = OlxParser(max_age_hours=a.max_age_hours), OtodomParser(max_age_hours=a.max_age_hours)
    report = {"max_age_hours": a.max_age_hours, "transport": olx.http.transport}
    report["olx"], _ = run(olx, "OLX")
    report["otodom"], oto_list = run(oto, "Otodom")
    if not a.no_cross and report["olx"]["ok"]:
        try:
            report["cross_listing"] = cross_listing(olx, oto_list)
        except SourceDownError as e:
            report["cross_listing"] = {"error": str(e)}
    txt = json.dumps(report, ensure_ascii=False, indent=1)
    print(txt)
    if a.json_out:
        with open(a.json_out, "w") as f:
            f.write(txt)
    ok = all(report[s]["ok"] and report[s]["summary"]["items_fetched"] > 0 for s in ("olx", "otodom"))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
