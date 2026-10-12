"""Rule-based (no LLM) extraction of money amounts and dates from Polish listing text."""
from __future__ import annotations

import datetime as dt
import re
from typing import Optional

# ----------------------------------------------------------------- amounts
_AMOUNT = (r"(?P<num>\d{1,3}(?:[ \u00a0.,]\d{3})+(?!\d)|\d+)(?:[,.](?P<dec>\d{1,2}))?"
           r"[ \t]*(?P<tys>tys\.?|tyś\.?|k\b)?")
_UNIT = r"[ \t]*(?P<unit>zł\b|zl\b|złotych|pln\b|,-|-,)"
_GAP = r"[^\d\n!?;]{0,45}?"        # words between keyword and amount (no digits / newlines)

FIELDS = {
    "czynsz": r"czynsz\w*(?! najmu)(?:\s+administracyjn\w*)?|op[lł]at\w* administracyjn\w*|"
              r"op[lł]at\w* (?:do|dla) (?:sp[oó][lł]dzielni|wsp[oó]lnoty|administracji)|"
              r"administracj\w*",
    "czynsz_weak": r"op[lł]at\w*(?! za (?:pr[aą]d|media|gaz|internet))",
    "kaucja": r"kaucj\w*|depozyt\w*|zabezpieczeni\w*",
    "media": r"media\b|medi[oó]w|rachunk\w*|op[lł]at\w* za media|op[lł]at\w* licznikow\w*",
    "prad": r"pr[aą]d\w*|energi\w* elektryczn\w*",
    "parking": r"gara[zż]\w*|miejsc\w* (?:postojow|parkingow)\w*|hal\w* gara[zż]ow\w*|parking\w*",
}
OTHER_KEYS = re.compile(r"czynsz|kaucj|depozyt|media|pr[aą]d|gara[zż]|parking|postojow|cen[aąy]|"
                        r"op[lł]at|gaz|internet|najm", re.I)
INCLUDED = r"(?:w cenie|wliczon\w*|zawart\w* w|uwzgl[eę]dnion\w*|gratis|bezp[lł]atn\w*|w czynszu)"
# "w cenie 200 zł" means "for 200 zł", not "included": reject when an amount follows closely.
INCLUDED_NOAMT = INCLUDED + r"(?![^.\n\d]{0,12}\d)"
# "czynsz i prąd 500 zł", "opłaty do zarządcy i prąd ok 500 zł": one sum for several costs.
_COMBINED_BEFORE = re.compile(r"(?:czynsz|op[lł]at|zarz[aą]dc|administrac|media|medi[oó]w|gaz|wod[aąy]|ogrzewani|rachunk)\w*[^.\n\d]{0,8}?(?:\bi\b|\boraz\b|wraz z|\+|\bplus\b|,)\s*(?:op[lł]at\w* za?\s*)?$", re.I)

# "800–1000 zł", "od 800 do 1000 zł": a range; only the czynsz search reads it (as lo, hi remembered).
_RANGE = re.compile(r"(?:\bod[ \t]+)?(?P<lo>\d{1,3}(?:[ .]\d{3})+(?!\d)|\d+)[ \t]*(?:zł|zl|pln)?[ \t]*"
                    r"(?:[-–—]|\bdo\b)[ \t]*(?P<hi>\d{1,3}(?:[ .]\d{3})+(?!\d)|\d+)(?=[ \t]*(?:zł\b|zl\b|złotych|pln\b))",
                    re.I)

_MONTHS_WORDS = {"jedno": 1, "jednego": 1, "1": 1, "dwu": 2, "dwóch": 2, "dwoch": 2, "2": 2,
                 "trzy": 3, "trzech": 3, "3": 3}


def parse_amount(m: re.Match) -> Optional[float]:
    num = re.sub(r"[ \u00a0.,]", "", m.group("num"))
    try:
        v = float(num + ("." + m.group("dec") if m.group("dec") else ""))
    except ValueError:
        return None
    if m.group("tys") and v < 100:          # "4 tys." = 4000; "3000tyś" is a typo for 3000
        v *= 1000
    return v


def _find_amount(text: str, key_re: str, require_unit=True, exclude: tuple = (), combined_check=False):
    """Yield (amount, start) for 'keyword ... amount [zł]' and 'amount zł ... keyword'.
    Never crosses a line break (list-style descriptions put one cost per line)."""
    fwd = re.compile(rf"(?P<key>{key_re})(?P<gap>{_GAP})[ \t]*(?:ok\.?|około|ca\.?|~|do)?[ \t]*{_AMOUNT}(?:{_UNIT})?", re.I)
    for m in fwd.finditer(text):
        gap = m.group("gap")
        if any(not re.fullmatch(key_re, w.group(0) + _word_tail(gap, w.end()), re.I)
               for w in OTHER_KEYS.finditer(gap)):
            continue                         # gap crosses into another cost, e.g. "czynsz. Kaucja 3000"
        has_unit = bool(m.group("unit") or m.group("tys"))
        if require_unit and not has_unit:
            continue
        if not has_unit and not re.search(r"[:=\-–]\s*$|wynosi\s*$|wysoko\w*\s*$", gap):
            continue
        if combined_check and _COMBINED_BEFORE.search(text[max(0, m.start() - 60):m.start()]):
            continue                         # "czynsz i prąd 500 zł" is not the electricity price
        v = parse_amount(m)
        if v is not None and v not in exclude:
            yield v, m.start()
    rev = re.compile(rf"{_AMOUNT}{_UNIT}(?:[ \t]*/[ \t]*(?:mc|mies|m-c)\w*\.?)?[ \t]*(?:\(|za)?[ \t]*(?P<key>{key_re})", re.I)
    for m in rev.finditer(text):
        if m.group("key")[:1].isupper() and re.search(r"\.\s+$", text[m.start():m.start("key")]):
            continue                         # "200 zł/mies. Kaucja zwrotna: 2500" - new sentence, not "200 zł kaucji"
        v = parse_amount(m)
        if v is not None and v not in exclude:
            yield v, m.start()


def _word_tail(s: str, i: int) -> str:
    m = re.match(r"\w*", s[i:])
    return m.group(0) if m else ""


def _first(gen):
    best = None
    for v, pos in gen:
        if best is None or pos < best[1]:
            best = (v, pos)
    return best[0] if best else None


def _included(text: str, key_re: str) -> bool:
    return bool(re.search(rf"(?:{key_re})[^.\n]{{0,25}}{INCLUDED_NOAMT}", text, re.I) or
                re.search(rf"(?:w tym|wraz z|z|zawiera\w*|plus)\s+(?:{key_re})\b(?![^.\n]{{0,15}}\d)", text, re.I) and
                re.search(rf"(?:cen\w*|kwot\w*|\d\s*zł)[^.\n]{{0,40}}(?:w tym|wraz z|zawiera\w*)\s+(?:{key_re})", text, re.I))


def extract_costs(text: str, price: Optional[float] = None) -> tuple[dict, list[str]]:
    """Return ({czynsz_pln, kaucja_pln, media_pln, prad_pln, parking_pln}, notes)."""
    out: dict = {}
    notes: list[str] = []
    if not text:
        return out, notes
    t = text.replace("\xa0", " ")
    t = re.sub(r"\[\s*(\d[\d .,]*)\s*\]", r"\1", t)          # "Kaucja: [2500] zł"
    excl = (price,) if price else ()

    # czynsz (admin fee): strong keywords first, then generic "opłaty"
    if _included(t, FIELDS["czynsz"]):
        out["czynsz_pln"] = 0.0
        notes.append("czynsz: included in price (text)")
    else:
        ranges: dict = {}

        def collapse(m: re.Match) -> str:
            lo, hi = (float(re.sub(r"[ .]", "", m.group(g))) for g in ("lo", "hi"))
            if not lo < hi:
                return m.group(0)
            ranges[lo] = hi
            return m.group("lo")
        tc = _RANGE.sub(collapse, t)
        v = _first(_find_amount(tc, FIELDS["czynsz"], require_unit=False, exclude=excl))
        if v is None:
            v = _first(_find_amount(tc, FIELDS["czynsz_weak"], exclude=excl))
        if v is not None and 0 < v < 5000:
            out["czynsz_pln"] = v
            notes.append("czynsz: description")
            if v in ranges:
                out["czynsz_max_pln"] = ranges[v]
                notes.append(f"czynsz: range up to {ranges[v]:g}")

    # kaucja
    v = _first(_find_amount(t, FIELDS["kaucja"], require_unit=False))
    if v is not None and v >= 100:
        out["kaucja_pln"] = v
        notes.append("kaucja: description")
    else:
        m = re.search(r"(?:kaucj\w*|depozyt\w*)[^.\n\d]{0,40}?\b(jedno|dwu|trzy|[123])[ -]?(?:krotno\w*|-?krotn\w*|"
                      r"miesi[eę]czn\w*|x\b|mies\w*)", t, re.I) or \
            re.search(r"\b(jedno|dwu|trzy|[123])[ -]?(?:miesi[eę]czn\w*|krotn\w*)\s+(?:kaucj\w*|depozyt\w*)", t, re.I)
        months = _MONTHS_WORDS.get(m.group(1).lower()) if m else None
        if not months and re.search(r"(?:kaucj\w*|depozyt\w*)[^.\n\d]{0,40}?(?:miesi[eę]czn\w* czynsz\w*|"
                                    r"jednego czynszu|wysoko\w* czynszu|r[oó]wnowarto\w* czynszu)", t, re.I):
            months = 1
        if months and price:
            out["kaucja_pln"] = float(price * months)
            notes.append(f"kaucja: {months}x price (description)")

    # utilities
    if re.search(rf"(?:{FIELDS['media']})[^.\n]{{0,25}}{INCLUDED_NOAMT}", t, re.I):
        out["media_pln"] = 0.0
        notes.append("media: included (text)")
    else:
        v = _first(_find_amount(t, FIELDS["media"], combined_check=True))
        if v is not None and 0 < v < 3000:
            out["media_pln"] = v
            notes.append("media: description")
    v = _first(_find_amount(t, FIELDS["prad"], combined_check=True))
    if v is not None and 0 < v < 2000:
        out["prad_pln"] = v
        notes.append("prad: description")

    # garage / parking
    if re.search(rf"(?:{FIELDS['parking']})[^.\n]{{0,30}}{INCLUDED_NOAMT}", t, re.I):
        out["parking_pln"] = 0.0
        notes.append("parking: included (text)")
    else:
        v = _first(_find_amount(t, FIELDS["parking"], exclude=excl))
        if v is not None and 0 < v < 2000:
            out["parking_pln"] = v
            notes.append("parking: description")
    return out, notes


# ----------------------------------------------------------------- dates
MONTHS = {"stycz": 1, "lut": 2, "mar": 3, "kwie": 4, "maj": 5, "maja": 5, "czerw": 6, "lip": 7,
          "sierp": 8, "wrze": 9, "pa": 10, "listop": 11, "grud": 12}
_MONTH_RE = (r"(stycz\w*|lut\w*|mar(?:ca|zec)|kwie\w*|maj\w*|czerw\w*|lip\w*|sierp\w*|wrze[sś]\w*|"
             r"pa[zź]dziernik\w*|listopad\w*|grud\w*)")


def _month_num(word: str) -> Optional[int]:
    w = word.lower().replace("ź", "z").replace("ś", "s")
    for k, v in (("stycz", 1), ("lut", 2), ("mar", 3), ("kwie", 4), ("maj", 5), ("czerw", 6), ("lip", 7),
                 ("sierp", 8), ("wrze", 9), ("pazdz", 10), ("listop", 11), ("grud", 12)):
        if w.startswith(k):
            return v
    return None


def _mkdate(day: int, month: int, year: Optional[int], today: dt.date) -> Optional[str]:
    if year is not None and year < 100:
        year += 2000
    if year is None:
        year = today.year
        if month < today.month - 2:          # "od 15.01" written in October -> next year
            year += 1
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return None


_NOT_MOVE_IN = re.compile(r"(?:ogl[aą]d\w*|prezentac\w*|kontakt\w*|dzwoni\w*|godzin\w*|obejrze\w*|wizyt\w*|"
                         r"umowa\s+do|do\s*$)[^.\n]{0,25}$", re.I)


def extract_available_from(text: str, today: Optional[dt.date] = None) -> Optional[str]:
    """ISO date, 'immediately', or None. The earliest plausible 'od <date>' wins; dates that belong to
    viewings ('oglądanie od 07.10', 'kontakt od godziny 16') are skipped."""
    if not text:
        return None
    today = today or dt.date.today()
    t = text.replace("\xa0", " ")
    cands: list[tuple[int, str]] = []
    for m in re.finditer(r"\bod\s*(?:dnia\s*)?(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?(?![\d.,]*\s*(?:zł|pln|m|km|min))",
                         t, re.I):
        if _NOT_MOVE_IN.search(t[max(0, m.start() - 40):m.start()]):
            continue
        if 1 <= int(m.group(1)) <= 31 and 1 <= int(m.group(2)) <= 12:
            d = _mkdate(int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None, today)
            if d:
                cands.append((m.start(), d))
                break
    for m in re.finditer(rf"\bod\s*(?:dnia\s*)?(?:(\d{{1,2}})\s*|pocz[aą]tk\w*\s*|po[lł]ow\w*\s*)?{_MONTH_RE}(?:\s*(\d{{4}}))?",
                         t, re.I):
        if _NOT_MOVE_IN.search(t[max(0, m.start() - 40):m.start()]):
            continue
        mon = _month_num(m.group(2))
        if mon:
            day = int(m.group(1)) if m.group(1) else (15 if re.search(r"po[lł]ow", m.group(0), re.I) else 1)
            d = _mkdate(day, mon, int(m.group(3)) if m.group(3) else None, today)
            if d:
                cands.append((m.start(), d))
                break
    m = re.search(r"\bod\s+(?:zaraz|teraz|dzi[sś]\w*|ju[zż]|r[eę]ki)\b|natychmiast\w*|\bna\s+ju[zż]\b", t, re.I)
    if m:
        cands.append((m.start(), "immediately"))
    return min(cands)[1] if cands else None
