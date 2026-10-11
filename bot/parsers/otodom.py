"""Otodom.pl parser: Wrocław flat rentals from private owners.

Search pages (__NEXT_DATA__ -> props.pageProps.data.searchAds) give the list; for each new
private candidate the detail page (props.pageProps.ad) is fetched for building type, czynsz,
kaucja, free_from, coordinates and the full description.

Usage:  python -m parsers.otodom --once --json
"""
from __future__ import annotations

import re
import time
from typing import Iterable, Optional
from urllib.parse import quote

from . import config
from .common import (Health, iso_local, HttpClient, SourceDownError, dig, extract_next_data, log, now_utc,
                     parse_dt, strip_html, to_float)
from .extract import extract_available_from, extract_costs
from .filters import AGENT_REASON_PREFIXES, check_criteria, normalize_building_type, text_fingerprint
from .models import FetchResult, Listing, Rejection

SOURCE = "otodom"
ROOMS_ENUM = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5, "SIX": 6, "SIX_OR_MORE": 6,
              "SEVEN": 7, "EIGHT": 8, "NINE": 9, "TEN": 10, "MORE": 10}
FLOOR_ENUM = {"CELLAR": -1, "GROUND": 0, "FIRST": 1, "SECOND": 2, "THIRD": 3, "FOURTH": 4, "FIFTH": 5,
              "SIXTH": 6, "SEVENTH": 7, "EIGHTH": 8, "NINTH": 9, "TENTH": 10, "ABOVE_TENTH": 11}
MAX_CONSECUTIVE_DETAIL_FAILURES = 3


def public_id_from_slug(slug: Optional[str]) -> Optional[str]:
    m = re.search(r"-(ID[0-9A-Za-z]+)$", slug or "")
    return m.group(1) if m else None


def _floor(v) -> Optional[int]:
    if v is None:
        return None
    s = str(v).strip()
    up = s.upper()
    if up in FLOOR_ENUM:
        return FLOOR_ENUM[up]
    low = s.lower()
    if "ground" in low:
        return 0
    if "cellar" in low:
        return -1
    if "garret" in low:
        return None
    if "higher" in low or "above" in low:
        m = re.search(r"\d+", low)
        return int(m.group()) + 1 if m else 11
    m = re.search(r"-?\d+", low)
    return int(m.group()) if m else None


def _is_sentinel(item: dict) -> bool:
    """Otodom injects a duplicate tile with a 14-digit id and date '1999-02-29 ...'."""
    d = str(item.get("dateCreated") or "")
    try:
        big = int(item.get("id") or 0) >= 10 ** 12
    except (TypeError, ValueError):
        big = False
    return d.startswith("1999") or big


class OtodomParser:
    source = SOURCE

    def __init__(self, http: Optional[HttpClient] = None, max_pages: int = config.MAX_PAGES,
                 max_age_hours: float = config.NEW_MAX_AGE_HOURS, fetch_details: bool = True,
                 max_details: int = config.OTODOM_MAX_DETAILS_PER_RUN, now_fn=now_utc):
        self.http = http or HttpClient(SOURCE)
        self.max_pages = max_pages
        self.max_age_hours = max_age_hours
        self.fetch_details = fetch_details
        self.max_details = max_details
        self.now_fn = now_fn
        self._health = Health(SOURCE)
        self.last_result: FetchResult = FetchResult()
        self._parse_failures: dict[str, int] = {}
        self._agent_fps: dict[str, str] = {}

    def health(self) -> dict:
        return self._health.to_dict()

    def search_url(self, page: int = 1) -> str:
        params = dict(config.OTODOM_SEARCH_PARAMS)
        if page > 1:
            params["page"] = str(page)
        return config.OTODOM_SEARCH + "?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items())

    def fetch_new(self, seen_ids: Iterable[str] = ()) -> list[Listing]:
        t0 = time.monotonic()
        req0 = self.http.requests
        try:
            res = self._fetch_new(set(map(str, seen_ids)))
        except SourceDownError as e:
            self._health.fail(e, time.monotonic() - t0)
            raise
        except Exception as e:
            err = SourceDownError(SOURCE, "structure", f"unexpected {type(e).__name__}: {e}")
            self._health.fail(err, time.monotonic() - t0)
            raise err from e
        res.seconds = time.monotonic() - t0
        res.requests = self.http.requests - req0
        self.last_result = res
        self._health.ok(res.seconds, res.summary())
        return res.listings

    # ------------------------------------------------------------------ fetching
    def fetch_search(self, page: int = 1) -> dict:
        resp = self.http.get(self.search_url(page))
        if resp.status != 200:
            raise SourceDownError(SOURCE, "http", f"HTTP {resp.status} on search page", resp.status)
        return self.parse_search_html(resp.text, first=(page == 1))

    @staticmethod
    def parse_search_html(html_text: str, first: bool = True) -> dict:
        nd = extract_next_data(html_text, SOURCE)
        sa = dig(nd, "props", "pageProps", "data", "searchAds")
        if not isinstance(sa, dict) or not isinstance(sa.get("items"), list):
            raise SourceDownError(SOURCE, "structure", "props.pageProps.data.searchAds.items missing")
        items = sa["items"]
        if first and not items:
            raise SourceDownError(SOURCE, "structure", "search page 1 returned 0 items")
        need = ("id", "slug", "isPrivateOwner", "dateCreated")
        bad = sum(1 for i in items if not all(k in i for k in need))
        if items and bad > len(items) / 2:
            raise SourceDownError(SOURCE, "structure", f"{bad}/{len(items)} search items lack {need}")
        fqp = dig(nd, "props", "pageProps", "filteringQueryParams", default={})
        if first and config.OTODOM_SEARCH_PARAMS.get("isPrivateOwner") and not fqp.get("isPrivateOwner"):
            # The server silently dropped our private filter; we still filter client-side.
            log.warning("otodom: isPrivateOwner filter not echoed by server; client-side filter only")
        return {"items": items, "pagination": sa.get("pagination") or {}, "filtering": fqp}

    def fetch_detail(self, slug: str) -> Optional[dict]:
        """Return props.pageProps.ad, or None when the ad is gone (404/410/expired)."""
        resp = self.http.get(config.OTODOM_AD_URL.format(slug=slug))
        if resp.status in (404, 410):
            return None
        return self.parse_detail_html(resp.text)

    @staticmethod
    def parse_detail_html(html_text: str) -> Optional[dict]:
        nd = extract_next_data(html_text, SOURCE)
        pp = dig(nd, "props", "pageProps", default={})
        ad = pp.get("ad")
        if not isinstance(ad, dict):
            if pp.get("shouldShowExpiredAdPage") or "expired" in str(pp.get("pageType", "")).lower():
                return None
            raise SourceDownError(SOURCE, "structure", "detail page: props.pageProps.ad missing")
        if ad.get("shouldShowExpiredAdPage"):
            return None
        if "characteristics" not in ad or "description" not in ad:
            raise SourceDownError(SOURCE, "structure", "detail page: ad.characteristics/description missing")
        return ad

    # ------------------------------------------------------------------ main loop
    def _fetch_new(self, seen: set[str]) -> FetchResult:
        res = FetchResult()
        cutoff = self.now_fn().timestamp() - self.max_age_hours * 3600
        candidates: dict[str, dict] = {}
        for page in range(1, self.max_pages + 1):
            data = self.fetch_search(page)
            res.pages += 1
            items = data["items"]
            res.items_fetched += len(items)
            any_unseen, dates = False, []
            for it in items:
                if _is_sentinel(it):
                    continue
                oid = str(it.get("id"))
                d = parse_dt(it.get("dateCreated"))
                if d and not it.get("isPromoted"):
                    dates.append(d.timestamp())
                if oid in seen or oid in candidates:
                    continue
                c = parse_dt(it.get("createdAtFirst")) or d
                if not it.get("isPromoted") and c and c.timestamp() >= cutoff:
                    any_unseen = True            # unseen AND genuinely new
                candidates[oid] = it
            total_pages = int(data["pagination"].get("totalPages") or page)
            if not any_unseen or not dates or min(dates) < cutoff or page >= total_pages:
                break

        details_done, consecutive_fail = 0, 0
        passed: list = []
        for oid, it in candidates.items():
            title, url = it.get("title", ""), config.OTODOM_AD_URL.format(slug=it.get("slug", ""))
            created = parse_dt(it.get("createdAtFirst")) or parse_dt(it.get("dateCreated"))
            if created is None or created.timestamp() < cutoff:
                res.seen_ids.add(oid)
                res.rejected.append(Rejection(oid, "old" if created else "bad_created_time", title, url))
                continue
            pre = self.search_owner_reason(it)
            if pre:
                res.seen_ids.add(oid)
                res.rejected.append(Rejection(oid, pre, title, url))
                continue
            rooms = ROOMS_ENUM.get(str(it.get("roomsNumber") or ""))
            if rooms is not None and not (config.ROOMS_MIN <= rooms <= config.ROOMS_MAX):
                res.seen_ids.add(oid)
                res.rejected.append(Rejection(oid, f"rooms:{rooms}", title, url))
                continue
            ad = None
            if self.fetch_details:
                if details_done >= self.max_details:
                    res.retry_ids.add(oid)     # picked up next run
                    continue
                details_done += 1
                try:
                    ad = self.fetch_detail(it["slug"])
                    consecutive_fail = 0
                except SourceDownError as e:
                    consecutive_fail += 1
                    log.warning("otodom detail %s failed: %s", it.get("slug"), e)
                    if consecutive_fail >= MAX_CONSECUTIVE_DETAIL_FAILURES or e.kind in ("captcha", "blocked",
                                                                                       "rate_limited"):
                        raise SourceDownError(SOURCE, e.kind, f"detail pages failing: {e.reason}", e.status)
                    n = self._parse_failures[oid] = self._parse_failures.get(oid, 0) + 1
                    if n >= config.MAX_ITEM_PARSE_FAILURES:      # this one ad is broken, not the site
                        res.seen_ids.add(oid)
                        res.rejected.append(Rejection(oid, f"detail_error:{e.kind}", title, url))
                    else:
                        res.retry_ids.add(oid)
                    continue
                if ad is None:
                    res.seen_ids.add(oid)
                    res.rejected.append(Rejection(oid, "gone", title, url))
                    continue
            try:
                lst = self.normalize(it, ad)
                reason = self.detail_owner_reason(ad) if ad else None
                reason = reason or check_criteria(lst)
            except Exception as e:  # one malformed ad must not kill the run
                n = self._parse_failures[oid] = self._parse_failures.get(oid, 0) + 1
                log.warning("otodom: ad %s failed to parse (%d/%d): %r", oid, n, config.MAX_ITEM_PARSE_FAILURES, e)
                if n >= config.MAX_ITEM_PARSE_FAILURES:
                    res.seen_ids.add(oid)
                    res.rejected.append(Rejection(oid, f"parse_error:{type(e).__name__}", title, url))
                else:
                    res.retry_ids.add(oid)
                continue
            res.seen_ids.add(oid)
            fp = text_fingerprint(lst.title, lst.description_text) if ad else None
            if reason:
                if fp and reason.startswith(AGENT_REASON_PREFIXES):
                    self._agent_fps[fp] = oid
                    if len(self._agent_fps) > 5000:
                        self._agent_fps.pop(next(iter(self._agent_fps)))
                res.rejected.append(Rejection(oid, reason, lst.title, lst.url))
            else:
                passed.append((fp, lst))
        for fp, lst in passed:     # same text as an agency ad seen in this or an earlier run
            if fp and fp in self._agent_fps and self._agent_fps[fp] != lst.source_id:
                res.rejected.append(Rejection(lst.source_id, f"agent_duplicate_text:{self._agent_fps[fp]}",
                                              lst.title, lst.url))
            else:
                res.listings.append(lst)
        res.listings.sort(key=lambda l: parse_dt(l.created_at).timestamp() if l.created_at else 0, reverse=True)
        return res

    # ------------------------------------------------------------------ parsing
    @staticmethod
    def search_owner_reason(it: dict) -> Optional[str]:
        if it.get("isPrivateOwner") is not True:
            return f"not_private:{it.get('extendedAdvertiserType') or 'isPrivateOwner=false'}"
        eat = it.get("extendedAdvertiserType")
        if eat not in (None, "private_person"):
            return f"not_private:{eat}"
        if it.get("agency"):
            return "not_private:agency_attached"
        if it.get("estate") not in (None, "FLAT") or it.get("transaction") not in (None, "RENT"):
            return f"category:{it.get('estate')}/{it.get('transaction')}"
        return None

    @staticmethod
    def detail_owner_reason(ad: dict) -> Optional[str]:
        at = ad.get("advertiserType")
        if at and at != "private":
            return f"not_private:detail_{at}"
        if ad.get("agency"):
            return "not_private:detail_agency"
        if dig(ad, "owner", "type") not in (None, "private"):
            return f"not_private:owner_{dig(ad, 'owner', 'type')}"
        cat = ad.get("adCategory") or {}
        if cat.get("name") not in (None, "FLAT") or cat.get("type") not in (None, "RENT"):
            return f"category:{cat.get('name')}/{cat.get('type')}"
        return None

    def normalize(self, it: dict, ad: Optional[dict] = None) -> Listing:
        ad = ad or {}
        chars = {c.get("key"): c for c in ad.get("characteristics") or [] if isinstance(c, dict)}
        cval = lambda k: (chars.get(k) or {}).get("value")
        notes, signals = [], []
        if it.get("isPrivateOwner"):
            signals.append("otodom:isPrivateOwner")
        if it.get("extendedAdvertiserType") == "private_person":
            signals.append("otodom:private_person")
        if ad.get("advertiserType") == "private":
            signals.append("otodom:detail_advertiserType=private")
        src = it.get("source") or ad.get("source")
        if src and src != "urn:site:local":
            signals.append(f"otodom:source:{src}")

        desc = strip_html(ad.get("description")) if ad else strip_html(it.get("shortDescription"))
        price = to_float(cval("price")) or to_float(dig(it, "totalPrice", "value"))
        costs, cost_notes = extract_costs(desc, price)

        def pick(struct_v, key, label):
            if struct_v is not None:
                notes.append(f"{label}: structured")
                return struct_v
            if key in costs:
                notes.extend(n for n in cost_notes if n.startswith(label))
                return costs[key]
            return None

        czynsz = pick(to_float(cval("rent")) if cval("rent") is not None else to_float(dig(it, "rentPrice", "value")),
                      "czynsz_pln", "czynsz")
        kaucja = pick(to_float(cval("deposit")), "kaucja_pln", "kaucja")
        for n in cost_notes:
            if n.split(":")[0] in ("media", "prad", "parking"):
                notes.append(n)

        # The structured free_from is often just the publication date (the form's default); an explicit
        # date or "od zaraz" in the text wins over a structured date that is not after publication.
        created_d = (parse_dt(it.get("createdAtFirst")) or parse_dt(ad.get("createdAt")) or now_utc()).date()
        free_from = cval("free_from")
        text_from = extract_available_from(f"{it.get('title') or ''}\n{desc}", created_d)
        if (free_from and text_from and str(free_from) <= created_d.isoformat()
                and (text_from == "immediately" or text_from > str(free_from))):
            notes.append(f"available_from: description (structured {free_from} = publication default)")
            free_from = text_from
        elif free_from:
            notes.append("available_from: structured")
        else:
            free_from = text_from

        extras = []
        for block in (ad.get("additionalInformation") or []) + (ad.get("topInformation") or []):
            if block.get("label") == "extras_types":
                extras += [v.split("::")[-1] for v in block.get("values") or []]
        parking = "garage" if "garage" in extras else None

        street = dig(it, "location", "address", "street", "name") or dig(ad, "location", "address", "street", "name")
        district = city = None
        rg = dig(it, "location", "reverseGeocoding", "locations", default=[]) or \
            dig(ad, "location", "reverseGeocoding", "locations", default=[])
        for loc in rg:
            lvl = loc.get("locationLevel")
            if lvl == "city_or_village":
                city = loc.get("name")
            elif lvl == "district":
                district = loc.get("name")
            elif lvl == "residential" and district:
                district = f"{district} / {loc.get('name')}"

        imgs = ad.get("images") or it.get("images") or []
        photos = [i.get("large") or i.get("medium") or i.get("small") for i in imgs if isinstance(i, dict)]
        photos = [p for p in photos if p]

        slug = it.get("slug") or ad.get("slug") or ""
        created = parse_dt(it.get("createdAtFirst")) or parse_dt(ad.get("createdAt")) or parse_dt(it.get("dateCreated"))
        date_created = parse_dt(it.get("dateCreated"))
        refreshed = parse_dt(it.get("pushedUpAt")) or parse_dt(ad.get("pushedUpAt")) or (
            date_created if date_created and created and date_created > created else None)
        owner = ad.get("owner") or {}
        rooms = to_float(cval("rooms_num"))
        rooms = int(rooms) if rooms is not None else ROOMS_ENUM.get(str(it.get("roomsNumber") or ""))
        area = to_float(cval("m")) or to_float(it.get("areaInSquareMeters"))
        bt = normalize_building_type(cval("building_type"))
        return Listing(
            source=SOURCE, source_id=str(it.get("id") or ad.get("id")), url=config.OTODOM_AD_URL.format(slug=slug),
            title=(it.get("title") or ad.get("title") or "").strip(), description_text=desc,
            price_pln=price, czynsz_pln=czynsz, media_pln=costs.get("media_pln"), prad_pln=costs.get("prad_pln"),
            kaucja_pln=kaucja, parking_pln=costs.get("parking_pln"), parking=parking, pets=None,
            area_m2=area, rooms=rooms,
            floor=_floor(cval("floor_no")) if cval("floor_no") else _floor(it.get("floorNumber")),
            building_type=bt, available_from=free_from, street=street, district=district,
            city=city or ("Wrocław" if "wroclaw" in config.OTODOM_SEARCH else None),
            lat=to_float(dig(ad, "location", "coordinates", "latitude")),
            lon=to_float(dig(ad, "location", "coordinates", "longitude")),
            photos=photos,
            seller_id=str(owner["id"]) if owner.get("id") is not None else None,
            seller_name=owner.get("name") or dig(it, "advertOwner", "name"),
            is_private=bool(it.get("isPrivateOwner")) and it.get("extendedAdvertiserType") in (None, "private_person"),
            private_signals=signals, created_at=iso_local(created),
            refreshed_at=iso_local(refreshed),
            is_promoted=bool(it.get("isPromoted")),
            cross_source_ref=({"source": "otodom-crosslisted", "public_id": public_id_from_slug(slug)}
                              if it.get("isCrossListed") else None),
            dedupe_key=(f"otodom:{public_id_from_slug(slug)}" if public_id_from_slug(slug)
                        else f"otodom:{it.get('id') or ad.get('id')}"),
            extraction_notes=notes,
            raw_extra={k: v for k, v in {
                "public_id": public_id_from_slug(slug), "source_urn": src, "extras": extras or None,
                "build_year": cval("build_year"), "detail_fetched": bool(ad),
                "modified_at": ad.get("modifiedAt"),
            }.items() if v is not None},
        )


def main(argv=None):
    from .cli import run_cli
    return run_cli(OtodomParser, SOURCE, argv)


if __name__ == "__main__":
    raise SystemExit(main())
