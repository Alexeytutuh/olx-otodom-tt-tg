"""OLX.pl parser: Wrocław whole-flat rentals from private owners.

Usage:  python -m parsers.olx --once --json
"""
from __future__ import annotations

import re
import time
from collections import Counter
from typing import Iterable, Optional
from urllib.parse import urlencode

from . import config
from .common import (Health, iso_local, HttpClient, SourceDownError, dig, log, looks_like_captcha, now_utc,
                     parse_dt, strip_html, to_float)
from .extract import extract_available_from, extract_costs
from .filters import AGENT_REASON_PREFIXES, check_criteria, normalize_building_type, text_fingerprint
from .models import FetchResult, Listing, Rejection

SOURCE = "olx"

# Reject an author with this many listings inside one fetched batch (hidden agency).
# Partner imports (Otodom) are exempt. None disables.
MAX_ADS_PER_USER_IN_BATCH = 4

_LOW = "a-ząćęłńóśźż"
_STREET_RE = re.compile(r"\b(?:ul\.|ulica|ulicy|al\.|aleja|alei|pl\.|plac|placu)[ \t]*"
                        rf"([A-ZŁŚŻŹĆÓ][{_LOW}][{_LOW}\-]*(?:[ \t]+(?:[A-ZŁŚŻŹĆÓ][{_LOW}][{_LOW}\-]*|[IVX]{{1,4}}\b|\d+[a-zA-Z]?\b)){{0,2}})")


def _param(o: dict, key: str) -> Optional[dict]:
    for p in o.get("params") or []:
        if p.get("key") == key:
            return p.get("value") or {}
    return None


def _floor(key: Optional[str]) -> Optional[int]:
    if not key:
        return None
    k = str(key).lower()
    if k in ("floor_0", "parter", "ground_floor"):
        return 0
    if "cellar" in k or "suterena" in k or k == "floor_-1":
        return -1
    m = re.search(r"higher_(\d+)", k)
    if m:
        return int(m.group(1)) + 1
    m = re.search(r"-?\d+", k)
    return int(m.group()) if m else None


def otodom_ref_from_url(url: Optional[str]) -> Optional[dict]:
    if not url or "otodom.pl" not in url:
        return None
    m = re.search(r"-(ID[0-9A-Za-z]+)(?:[/?#]|$)", url)
    return {"source": "otodom", "public_id": m.group(1) if m else None, "url": url.split("?")[0]}


class OlxParser:
    source = SOURCE

    def __init__(self, http: Optional[HttpClient] = None, max_pages: int = config.MAX_PAGES,
                 max_age_hours: float = config.NEW_MAX_AGE_HOURS, now_fn=now_utc):
        self.http = http or HttpClient(SOURCE)
        self.max_pages = max_pages
        self.max_age_hours = max_age_hours
        self.now_fn = now_fn
        self._health = Health(SOURCE)
        self.last_result: FetchResult = FetchResult()
        self._parse_failures: dict[str, int] = {}
        self._agent_fps: dict[str, str] = {}      # text fingerprint -> agent listing id (kept between runs)

    # ------------------------------------------------------------------ public
    def health(self) -> dict:
        return self._health.to_dict()

    def page_url(self, offset: int) -> str:
        q = [("offset", offset), ("limit", config.OLX_PAGE_LIMIT), ("category_id", config.OLX_CATEGORY_ID),
             ("city_id", config.OLX_CITY_ID), ("owner_type", "private"), ("sort_by", "created_at:desc")]
        rooms = [k for k, v in config.OLX_ROOMS_ENUM.items() if config.ROOMS_MIN <= v <= config.ROOMS_MAX]
        q += [(f"filter_enum_rooms[{i}]", k) for i, k in enumerate(rooms)]
        return config.OLX_API + "?" + urlencode(q)

    def fetch_new(self, seen_ids: Iterable[str] = ()) -> list[Listing]:
        """Return new matching listings (newest first). Pages only while unseen ids appear.
        Raises SourceDownError when OLX is unusable; health() is updated either way."""
        t0 = time.monotonic()
        req0 = self.http.requests
        try:
            res = self._fetch_new(set(map(str, seen_ids)))
        except SourceDownError as e:
            self._health.fail(e, time.monotonic() - t0)
            raise
        except Exception as e:  # unexpected bug/shape -> still a distinct, alertable error
            err = SourceDownError(SOURCE, "structure", f"unexpected {type(e).__name__}: {e}")
            self._health.fail(err, time.monotonic() - t0)
            raise err from e
        res.seconds = time.monotonic() - t0
        res.requests = self.http.requests - req0
        self.last_result = res
        self._health.ok(res.seconds, res.summary())
        return res.listings

    # ------------------------------------------------------------------ fetching
    def fetch_page(self, offset: int) -> dict:
        resp = self.http.get(self.page_url(offset), accept="application/json")
        if resp.status != 200:
            raise SourceDownError(SOURCE, "http", f"HTTP {resp.status} on search API", resp.status)
        try:
            import json
            data = json.loads(resp.text)
        except ValueError:
            marker = looks_like_captcha(resp.text)
            if marker:
                raise SourceDownError(SOURCE, "captcha", f"anti-bot page instead of JSON (marker: {marker})")
            raise SourceDownError(SOURCE, "structure", "search API returned non-JSON "
                                  f"(starts with {resp.text[:60]!r})")
        self.validate_page(data, first=(offset == 0))
        return data

    @staticmethod
    def validate_page(data: dict, first: bool):
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise SourceDownError(SOURCE, "structure", "JSON shape changed: no 'data' list")
        items = data["data"]
        if first and not items:
            raise SourceDownError(SOURCE, "structure", "page 1 is empty (API/filters changed?)")
        need = ("id", "created_time", "business", "params", "url")
        bad = sum(1 for o in items if not all(k in o for k in need))
        if items and bad > len(items) / 2:
            raise SourceDownError(SOURCE, "structure", f"{bad}/{len(items)} offers lack {need}")

    def _fetch_new(self, seen: set[str]) -> FetchResult:
        res = FetchResult()
        now_ts = self.now_fn().timestamp()
        cutoff = now_ts - self.max_age_hours * 3600
        lookback = now_ts - config.OLX_MIN_LOOKBACK_HOURS * 3600
        evaluated: dict[str, tuple[dict, bool]] = {}
        all_items: dict[str, dict] = {}
        for page in range(self.max_pages):
            data = self.fetch_page(page * config.OLX_PAGE_LIMIT)
            res.pages += 1
            items = data["data"]
            res.items_fetched += len(items)
            promoted = set(dig(data, "metadata", "source", "promoted", default=None)
                           or dig(data, "metadata", "promoted", default=[]) or [])
            organic_unseen, organic_lrts = False, []
            for idx, o in enumerate(items):
                oid = str(o.get("id"))
                all_items.setdefault(oid, o)
                is_promo = idx in promoted
                if not is_promo:
                    lrt = parse_dt(o.get("last_refresh_time")) or parse_dt(o.get("created_time"))
                    if lrt:
                        organic_lrts.append(lrt.timestamp())
                if oid in seen or oid in evaluated:
                    continue
                created = parse_dt(o.get("created_time"))
                if not is_promo and created and created.timestamp() >= cutoff:
                    organic_unseen = True        # unseen AND genuinely new (bumped old ones don't count)
                evaluated[oid] = (o, is_promo)
            # OLX orders by last_refresh_time (sort_by=created_at:desc is ignored). created_time <=
            # last_refresh_time, so once the oldest organic refresh on a page is older than the cutoff nothing
            # further down can be new. Promoted slots are ignored. A page with nothing unseen still leads to
            # the next one while the page reaches back less than OLX_MIN_LOOKBACK_HOURS (late-visible ads).
            # (OLX de-duplicates across pages, so a page may hold 39 organic items: not a short page.)
            oldest = min(organic_lrts) if organic_lrts else None
            if (oldest is None or oldest < cutoff
                    or (not organic_unseen and oldest < lookback)
                    or len(items) - len(promoted) < config.OLX_PAGE_LIMIT // 2
                    or not dig(data, "links", "next", "href")):
                break

        # authors with many ads on the fetched pages (seen ones included) look like hidden agencies
        per_user = Counter(str(dig(o, "user", "id")) for o in all_items.values() if not dig(o, "partner", "code"))
        passed: list[tuple[str, Listing]] = []
        for oid, (o, is_promo) in evaluated.items():
            created = parse_dt(o.get("created_time"))
            if created is None:
                res.seen_ids.add(oid)
                res.rejected.append(Rejection(oid, "bad_created_time", o.get("title", ""), o.get("url", "")))
                continue
            if created.timestamp() < cutoff:
                res.seen_ids.add(oid)
                res.rejected.append(Rejection(oid, "old", o.get("title", ""), o.get("url", "")))
                continue
            try:
                lst = self.normalize(o, is_promoted=is_promo)
                reason = self.olx_owner_reason(o, lst)
                if (reason is None and MAX_ADS_PER_USER_IN_BATCH and not dig(o, "partner", "code")
                        and per_user[str(dig(o, "user", "id"))] >= MAX_ADS_PER_USER_IN_BATCH):
                    reason = "many_ads_same_user"
                reason = reason or check_criteria(lst)
            except Exception as e:  # one malformed offer must not kill the run
                n = self._parse_failures[oid] = self._parse_failures.get(oid, 0) + 1
                log.warning("olx: offer %s failed to parse (%d/%d): %r", oid, n, config.MAX_ITEM_PARSE_FAILURES, e)
                if n >= config.MAX_ITEM_PARSE_FAILURES:
                    res.seen_ids.add(oid)
                    res.rejected.append(Rejection(oid, f"parse_error:{type(e).__name__}", o.get("title", ""),
                                                  o.get("url", "")))
                else:
                    res.retry_ids.add(oid)
                continue
            res.seen_ids.add(oid)
            fp = text_fingerprint(lst.title, lst.description_text)
            if reason:
                if fp and reason.startswith(AGENT_REASON_PREFIXES):
                    self._remember_agent_fp(fp, oid)
                res.rejected.append(Rejection(oid, reason, lst.title, lst.url))
            else:
                passed.append((fp, lst))
        for fp, lst in passed:     # second pass: same text as an agency ad (often a 2nd "private" account)
            if fp and fp in self._agent_fps and self._agent_fps[fp] != lst.source_id:
                res.rejected.append(Rejection(lst.source_id, f"agent_duplicate_text:{self._agent_fps[fp]}",
                                              lst.title, lst.url))
            else:
                res.listings.append(lst)
        if len(evaluated) >= 4 and len(res.retry_ids) > len(evaluated) / 2:
            raise SourceDownError(SOURCE, "structure", f"{len(res.retry_ids)}/{len(evaluated)} offers failed to parse")
        res.listings.sort(key=lambda l: parse_dt(l.created_at).timestamp() if l.created_at else 0, reverse=True)
        return res

    def _remember_agent_fp(self, fp: str, oid: str):
        self._agent_fps[fp] = oid
        if len(self._agent_fps) > 5000:                     # bounded; drop the oldest
            self._agent_fps.pop(next(iter(self._agent_fps)))

    # ------------------------------------------------------------------ parsing
    @staticmethod
    def olx_owner_reason(o: dict, lst: Listing) -> Optional[str]:
        if o.get("business") is not False:
            return "not_private:business_flag"
        user = o.get("user") or {}
        if user.get("b2c_business_page") or dig(o, "shop", "subdomain"):
            return "not_private:business_page"
        if dig(o, "category", "id") not in (None, config.OLX_CATEGORY_ID):
            return f"category:{dig(o, 'category', 'id')}"
        city_id = dig(o, "location", "city", "id")
        if city_id not in (None, config.OLX_CITY_ID):
            return f"not_city:{dig(o, 'location', 'city', 'name')}"
        if o.get("offer_type") not in (None, "offer"):
            return f"offer_type:{o.get('offer_type')}"     # "seek" = someone looking for a flat
        return None

    def normalize(self, o: dict, is_promoted: bool = False) -> Listing:
        desc = strip_html(o.get("description"))
        title = (o.get("title") or "").strip()
        price = to_float(dig(_param(o, "price") or {}, "value"))
        notes, signals = [], ["olx:business=false"]

        rent_v = _param(o, "rent")
        czynsz = to_float(rent_v.get("key")) if rent_v else None
        if czynsz is not None and czynsz < 10:      # placeholders like "1 zł"
            notes.append(f"czynsz: structured value {czynsz} ignored as placeholder")
            czynsz = None
        if czynsz is not None and price and czynsz == price:
            notes.append("czynsz: structured value equals price, ignored")
            czynsz = None
        if czynsz is not None:
            notes.append("czynsz: structured")
        costs, cost_notes = extract_costs(desc, price)
        czynsz_max = None
        if czynsz is None and "czynsz_pln" in costs:
            czynsz, czynsz_max = costs["czynsz_pln"], costs.get("czynsz_max_pln")
            notes += [n for n in cost_notes if n.startswith("czynsz")]
        notes += [n for n in cost_notes if not n.startswith("czynsz")]

        rooms_key = dig(_param(o, "rooms") or {}, "key")
        rooms = config.OLX_ROOMS_ENUM.get(rooms_key) if rooms_key else None
        if rooms is None and rooms_key:
            rooms = int(to_float(rooms_key)) if to_float(rooms_key) else None
        area = to_float(dig(_param(o, "m") or {}, "key"))
        bt_raw = dig(_param(o, "builttype") or {}, "key")
        parking_v = _param(o, "parking")
        pets_v = _param(o, "pets")

        user = o.get("user") or {}
        partner = dig(o, "partner", "code")
        cross = otodom_ref_from_url(o.get("external_url")) if partner or o.get("external_url") else None
        if partner:
            signals.append(f"olx:partner_import:{partner}")
        if not user.get("company_name"):
            signals.append("olx:no_company_name")
        else:
            signals.append(f"olx:company_name:{user.get('company_name')}")

        photos = []
        for ph in o.get("photos") or []:
            link = ph.get("link") if isinstance(ph, dict) else None
            if link:
                photos.append(re.sub(r"\{width\}x\{height\}", config.OLX_PHOTO_SIZE, link)
                              .replace(":443/", "/"))

        street_m = _STREET_RE.search(f"{title}\n{desc}")
        created = parse_dt(o.get("created_time"))
        refreshed = parse_dt(o.get("pushup_time")) or parse_dt(o.get("last_refresh_time"))
        lst = Listing(
            source=SOURCE, source_id=str(o.get("id")), url=o.get("url") or "", title=title,
            description_text=desc, price_pln=price, czynsz_pln=czynsz, czynsz_max_pln=czynsz_max,
            media_pln=costs.get("media_pln"), prad_pln=costs.get("prad_pln"),
            kaucja_pln=costs.get("kaucja_pln"), parking_pln=costs.get("parking_pln"),
            parking=(parking_v or {}).get("label"), pets=((pets_v or {}).get("label") or None),
            area_m2=area, rooms=rooms, floor=_floor(dig(_param(o, "floor_select") or {}, "key")),
            building_type=normalize_building_type(bt_raw),
            available_from=extract_available_from(f"{title}\n{desc}", (created or now_utc()).date()),
            street=street_m.group(1).strip(" .,") if street_m else None,
            district=dig(o, "location", "district", "name"), city=dig(o, "location", "city", "name"),
            lat=to_float(dig(o, "map", "lat")), lon=to_float(dig(o, "map", "lon")),
            photos=photos, seller_id=str(user["id"]) if user.get("id") is not None else None,
            seller_name=user.get("name") or dig(o, "contact", "name"),
            is_private=o.get("business") is False, private_signals=signals,
            created_at=iso_local(created),
            refreshed_at=iso_local(refreshed) if refreshed and refreshed != created else None,
            is_promoted=is_promoted, cross_source_ref=cross, extraction_notes=notes,
            raw_extra={k: v for k, v in {
                "furniture": dig(_param(o, "furniture") or {}, "label"),
                "elevator": dig(_param(o, "winda") or {}, "label"),
                "map_exact": dig(o, "map", "show_detailed"),
                "street_from_text": bool(street_m),
                "top_ad": dig(o, "promotion", "top_ad"),
            }.items() if v is not None},
        )
        return lst


# ---------------------------------------------------------------------- CLI
def main(argv=None):
    from .cli import run_cli
    return run_cli(OlxParser, SOURCE, argv)


if __name__ == "__main__":
    raise SystemExit(main())
