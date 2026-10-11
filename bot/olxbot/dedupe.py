"""Cross-source / repost duplicate detection.

exact   -> the second copy is skipped (same dedupe_key = OLX import of an Otodom ad; or the same text and price;
           or a near-identical first photo with the same price and area)
possible-> sent with a 'можливий дубль' note pointing at the earlier listing
none    -> normal post
"""
from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

from parsers.filters import text_fingerprint
from parsers.models import Listing

from .images import hamming

WINDOW_DAYS = 30
POSSIBLE_THRESHOLD = 6


@dataclass
class DupResult:
    kind: str                      # exact | possible | none
    other: Optional[dict] = None   # listings-table row of the earlier copy
    why: str = ""
    score: int = 0


def norm_text(s: Optional[str]) -> str:
    s = (s or "").lower()
    s = re.sub(r"\b(ul|al|pl|os)\.?\s*", "", s)
    return re.sub(r"[^0-9a-ząćęłńóśźż]+", " ", s).strip()


def street_key(s: Optional[str]) -> str:
    """'ul. Braterskiej 5' / 'Braterska' -> 'braters' (stem: first word, first 7 letters)."""
    t = norm_text(s)
    words = [w for w in t.split() if not w.isdigit()]
    return words[0][:7] if words else ""


def listing_record(lst: Listing, status: str, dhash: Optional[str] = None, dup_of: Optional[str] = None,
                   post_id: Optional[int] = None, now: Optional[float] = None) -> dict:
    return {"key": f"{lst.source}:{lst.source_id}", "source": lst.source, "source_id": lst.source_id,
            "dedupe_key": lst.dedupe_key, "first_seen_at": now or time.time(), "price": lst.price_pln,
            "area": lst.area_m2, "rooms": lst.rooms, "street": lst.street, "district": lst.district,
            "title": lst.title, "text_fp": text_fingerprint(lst.title, lst.description_text),
            "dhash": dhash, "seller_id": lst.seller_id, "seller_name": lst.seller_name, "status": status,
            "dup_of": dup_of, "post_id": post_id, "json": lst.to_json()}


def _close(a: Optional[float], b: Optional[float], rel: float, absolute: float = 0) -> Optional[bool]:
    if a is None or b is None:
        return None
    return abs(a - b) <= max(absolute, rel * max(abs(a), abs(b)))


def _desc_ratio(fp_a: Optional[str], fp_b: Optional[str]) -> float:
    if not fp_a or not fp_b:
        return 0.0
    sm = difflib.SequenceMatcher(None, fp_a[:300], fp_b[:300], autojunk=False)
    if sm.real_quick_ratio() < 0.7 or sm.quick_ratio() < 0.7:     # cheap upper bounds first
        return 0.0
    return sm.ratio()


def compare(rec: dict, other: dict, dhash_of: Optional[Callable[[dict], Optional[str]]] = None) -> DupResult:
    """rec/other are listings-table rows (see listing_record). dhash_of(row) may fetch a missing hash lazily
    and is only called when the numbers already look alike (keeps downloads rare)."""
    if rec.get("dedupe_key") and rec["dedupe_key"] == other.get("dedupe_key"):
        return DupResult("exact", other, "той самий id оголошення (імпорт Otodom → OLX)", 100)
    price = _close(rec.get("price"), other.get("price"), 0.01, 1)
    area = _close(rec.get("area"), other.get("area"), 0.03, 1)
    rooms = (rec.get("rooms") == other.get("rooms")) if rec.get("rooms") and other.get("rooms") else None
    if price is False and not _close(rec.get("price"), other.get("price"), 0.10):
        price_far = True
    else:
        price_far = False
    if area is False and not _close(rec.get("area"), other.get("area"), 0.10, 2):
        return DupResult("none")          # a different flat
    if rooms is False:
        return DupResult("none")

    same_text = bool(rec.get("text_fp")) and rec.get("text_fp") == other.get("text_fp")
    desc_r = 1.0 if same_text else _desc_ratio(rec.get("text_fp"), other.get("text_fp"))
    if same_text and price:
        return DupResult("exact", other, "однаковий текст опису і ціна", 100)

    score, why = 0, []
    if price:
        score += 2; why.append("ціна")
    if area:
        score += 2; why.append("площа")
    if rooms:
        score += 1; why.append("кімнати")
    sa, sb = street_key(rec.get("street")), street_key(other.get("street"))
    if sa and sb and sa == sb:
        score += 2; why.append("вулиця")
    elif not (sa and sb) and rec.get("district") and other.get("district") and \
            norm_text(rec["district"]).split()[:1] == norm_text(other["district"]).split()[:1]:
        score += 1; why.append("район")
    tr = difflib.SequenceMatcher(None, norm_text(rec.get("title")), norm_text(other.get("title"))).ratio()
    if tr >= 0.75:
        score += 1; why.append("заголовок")
    if desc_r >= 0.7:
        score += 3; why.append("текст")

    dist = None
    if dhash_of is not None and (score >= 4 or desc_r >= 0.7):
        ha, hb = dhash_of(rec), dhash_of(other)
        if ha and hb:
            dist = hamming(ha, hb)
            if dist <= 10:
                score += 3; why.append(f"фото (dHash {dist})")
    if dist is not None and dist <= 4 and price and area:
        return DupResult("exact", other, "те саме перше фото, ціна і площа", 100)
    if price_far and score < 8:
        return DupResult("none")
    if score >= POSSIBLE_THRESHOLD:
        return DupResult("possible", other, ", ".join(why), score)
    return DupResult("none", None, "", score)


def find_duplicate(rec: dict, candidates_exact: list[dict], candidates_recent: list[dict],
                   dhash_of: Optional[Callable[[dict], Optional[str]]] = None) -> DupResult:
    """candidates_exact: rows with the same dedupe_key; candidates_recent: other rows from the window."""
    for o in candidates_exact:
        if o["key"] != rec["key"]:
            return DupResult("exact", o, "той самий id оголошення (імпорт Otodom → OLX)", 100)
    best = DupResult("none")
    for o in candidates_recent:
        if o["key"] == rec["key"] or o.get("status") == "dup":
            continue
        r = compare(rec, o, dhash_of)
        if r.kind == "exact":
            return r
        if r.kind == "possible" and r.score > best.score:
            best = r
    return best
