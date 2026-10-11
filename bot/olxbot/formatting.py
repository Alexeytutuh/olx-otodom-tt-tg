"""Post text in the friend's format (Polish), the info card (Ukrainian), TikTok caption and slide labels.
Pure functions over parsers.models.Listing: no network, no AI. Unknown values -> the line is omitted."""
from __future__ import annotations

import datetime as dt
import html
import re
from dataclasses import dataclass
from typing import Optional

from parsers.models import Listing

from . import streets

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Warsaw")
except Exception:  # pragma: no cover
    TZ = dt.timezone(dt.timedelta(hours=2))

LANG_LINE = "PL | UA | RU | EN"
CAPTION_LIMIT = 1024          # Telegram media caption (after entity parsing)
MESSAGE_LIMIT = 4096
TIKTOK_CAPTION_LIMIT = 4000   # TikTok photo-post description (UTF-16 units)
TIKTOK_TITLE_LIMIT = 90       # TikTok photo-post title (UTF-16 units)

SOURCE_NAMES = {"olx": "OLX", "otodom": "Otodom"}


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


# ----------------------------------------------------------------------------- numbers
def fmt_num(v: Optional[float]) -> str:
    """2400 -> '2400', 35.5 -> '35,5' (Polish decimal comma, no thousands separator like the friend)."""
    if v is None:
        return ""
    v = round(float(v), 2)
    if v.is_integer():
        return str(int(v))
    return f"{v:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def zl(v: float) -> str:
    return f"{fmt_num(v)} zł"


# ----------------------------------------------------------------------------- type / features
def room_label(rooms: Optional[int]) -> str:
    if rooms == 1:
        return "Kawalerka"
    if rooms in (2, 3, 4):
        return f"{rooms} pokoje"
    if rooms and rooms >= 5:
        return f"{rooms} pokoi"
    return "Mieszkanie"


_NEG = re.compile(r"\b(?:bez|brak\w*|nie ma|nie posiada\w*|nie posiadamy)\b[^\n.,;]{0,12}$", re.I)
# (header word, slide text, regex) in priority order: the first one present is the "key feature"
FEATURES = [
    ("taras", "Taras", re.compile(r"\btaras(?:em|u|ie|ów)?\b|\btarasy\b(?!\s+grabisz)", re.I)),   # not the mall
    # only private gardens: diminutive "ogródek" or "z ogrodem / własny ogród"; "ogród w patio" is shared
    ("ogródek", "Ogródek", re.compile(r"\bogr[oó]d(?:ek|ka|kiem|ku)\b(?!\s+(?:plażow|piwn|letni|działkow|kawiar|restaurac))|\b(?:z|ze swoim|własn\w*|prywatn\w*)\s+"
                                      r"ogr[oó]d\w*\b|\bogr[oó]d\w*\s+na wyłączność", re.I)),
    ("balkon", "Balkon", re.compile(r"\bbalkon\w*", re.I)),
    ("loggia", "Loggia", re.compile(r"\blogg?i[aąię]\w*", re.I)),
    ("garaż", "Garaż", re.compile(r"\bgara[żz]\w*|\bhal\w*\s+gara[żz]ow\w*", re.I)),
]
OTODOM_EXTRAS = {"terrace": "taras", "garden": "ogródek", "balcony": "balkon", "garage": "garaż"}


def _mentions(rx: re.Pattern, text: str) -> bool:
    for m in rx.finditer(text):
        if not _NEG.search(text[max(0, m.start() - 40):m.start()]):
            return True
    return False


def detect_features(lst: Listing) -> list[str]:
    found = set()
    for e in (lst.raw_extra or {}).get("extras") or []:
        if e in OTODOM_EXTRAS:
            found.add(OTODOM_EXTRAS[e])
    text = f"{lst.title}\n{lst.description_text or ''}"
    for key, _, rx in FEATURES:
        if key not in found and _mentions(rx, text):
            found.add(key)
    if lst.parking_pln is not None and _parking_label(lst) == "Garaż":
        found.add("garaż")
    return [k for k, _, _ in FEATURES if k in found]


def key_feature(lst: Listing) -> Optional[str]:
    f = detect_features(lst)
    return f[0] if f else None


def feature_display(key: str) -> str:
    return {k: d for k, d, _ in FEATURES}.get(key, key.capitalize())


# ----------------------------------------------------------------------------- location
_STREET_PREFIX = re.compile(r"^\s*(ul|al|pl|os|aleja|aleje|plac|osiedle|bulwar)\b\.?\s*", re.I)

def _degenitive(word: str) -> str:
    """'Braterskiej' -> 'Braterska' (OLX street taken from 'przy ulicy Braterskiej'). Only adjectival
    feminine endings; nouns ('Saperów', 'Kościuszki') stay as they are."""
    if len(word) < 5 or not word[:1].isupper():
        return word
    low = word.lower()
    for a, b in (("dzkiej", "dzka"), ("skiej", "ska"), ("ckiej", "cka"), ("giej", "ga"), ("kiej", "ka"),
                 ("owej", "owa"), ("nej", "na"), ("łej", "ła"), ("rej", "ra"), ("tej", "ta"), ("lnej", "lna")):
        if low.endswith(a):
            return word[: len(word) - len(a)] + b
    return word


def normalize_street(lst: Listing) -> Optional[str]:
    s = (lst.street or "").strip()
    if not s:
        return None
    s = re.sub(r"[,\s]+(?:we\s+)?wroc[lł]aw\w*\s*$", "", s, flags=re.I).strip(" ,")
    if not s or len(s) < 3:
        return None
    m = _STREET_PREFIX.match(s)
    prefix = "ul."
    if m:
        p = m.group(1).lower()
        prefix = {"al": "al.", "aleja": "al.", "aleje": "al.", "pl": "pl.", "plac": "pl.", "os": "os.",
                  "osiedle": "os.", "bulwar": "ul. Bulwar"}.get(p, "ul.")
        s = s[m.end():].strip()
    if not s:
        return None
    s = s[0].upper() + s[1:]
    if (lst.raw_extra or {}).get("street_from_text"):
        full = streets.complete(s, f"{lst.title}\n{lst.description_text or ''}", _degenitive)
        if full:
            return f"{prefix} {full}"
        parts = s.split()
        # last word that is not a house number gets de-declined
        for i in range(len(parts) - 1, -1, -1):
            if not re.match(r"^\d", parts[i]):
                if i == len(parts) - 1 or re.match(r"^\d", parts[i + 1]):
                    parts[i] = _degenitive(parts[i])
                break
        s = " ".join(parts)
    return f"{prefix} {s}"


def district_short(lst: Listing) -> Optional[str]:
    """'Krzyki / Tarnogaj' -> 'Tarnogaj' (most specific part)."""
    d = (lst.district or "").strip()
    if not d:
        return None
    parts = [p.strip() for p in d.split("/") if p.strip()]
    return parts[-1] if parts else None


def location_line(lst: Listing) -> str:
    street = normalize_street(lst)
    if street:
        return f"Wrocław | {street}"
    d = (lst.district or "").strip()
    if d:
        parts = [p.strip() for p in d.split("/") if p.strip()]
        return "Wrocław | " + (f"{parts[-1]} ({parts[0]})" if len(parts) > 1 else parts[0])
    return "Wrocław"


# ----------------------------------------------------------------------------- template sentence
def template_sentence(rooms: Optional[int], area: Optional[float]) -> str:
    a = area or 0
    if rooms == 1 or (rooms is None and 0 < a < 35):
        return "Mieszkanie idealne dla pary, singla lub osoby pracującej zdalnie."
    if rooms == 2:
        if a and a < 45:
            return "Mieszkanie idealne dla pary lub dwóch osób pracujących."
        return "Przestronne mieszkanie idealne dla pary lub małej rodziny."
    if rooms == 3:
        if a and a < 60:
            return "Mieszkanie idealne dla rodziny lub współlokatorów."
        return "Przestronne mieszkanie idealne dla rodziny z dziećmi lub współlokatorów."
    return "Mieszkanie idealne dla pary lub rodziny."


# ----------------------------------------------------------------------------- costs
def _parking_label(lst: Listing) -> str:
    t = f"{lst.parking or ''} {lst.description_text or ''}".lower()
    if re.search(r"gara[żz]|hal\w* gara", t):
        return "Garaż"
    return "Miejsce parkingowe"


_PARKING_TEXT = re.compile(r"\bgara[żz]\w*|\bmiejsc\w* (?:postojow|parkingow)\w*|\bhal\w* gara[żz]ow\w*|"
                           r"\bparking\w* podziemn\w*", re.I)
_PARKING_PUBLIC = re.compile(r"^[^.\n]{0,25}(?:na ulicy|publiczn|ogólnodostępn|og[oó]lnodost[eę]pn)|"
                             r"^[^.\n\w]{0,3}(?:\w+\s+){0,1}(?:nie ma|brak|nie posiad|nie przys[lł]uguj|nie jest)", re.I)
_PARKING_PUBLIC_BEFORE = re.compile(r"(?:publiczn|og[oó]lnodost[eę]pn|bezp[lł]atn|darmow)\w*[^.\n]{0,15}$", re.I)
_ALL_INCLUSIVE = re.compile(r"za wszystko|wszystko w cenie|wszystko wliczon|bez (?:żadnych |zadnych )?dodatkowych "
                            r"op[lł]at|all.?inclusive", re.I)


def parking_without_price(lst: Listing) -> Optional[str]:
    """Parking is offered but no amount was found: 'w cenie' (all-inclusive price) or 'dostępne'. None = no parking."""
    param = (lst.parking or "").lower()
    text = f"{lst.title}\n{lst.description_text or ''}"
    offered = bool(param) and not re.search(r"brak|ulic", param)
    for m in _PARKING_TEXT.finditer(text):
        before = text[max(0, m.start() - 40):m.start()]
        if not _NEG.search(before) and not _PARKING_PUBLIC_BEFORE.search(before) \
                and not _PARKING_PUBLIC.search(text[m.end():]):
            offered = True
            break
    if not offered:
        return None
    if _ALL_INCLUSIVE.search(text):
        return "w cenie"
    return "dostępny" if _parking_label(lst) == "Garaż" else "dostępne"


def cost_lines(lst: Listing) -> tuple[list[str], Optional[float]]:
    """Returns (lines, razem). razem is None when it would be meaningless (no price, or nothing besides it)."""
    lines: list[str] = []
    parts: list[float] = []
    extra_known = False
    if lst.price_pln:
        lines.append(f"Cena: {zl(lst.price_pln)}")
        parts.append(lst.price_pln)
    c, m = lst.czynsz_pln, lst.media_pln
    if c is not None and m is not None:
        lines.append("Czynsz + media: " + ("w cenie" if (c + m) == 0 else zl(c + m)))
        parts += [c, m]
        extra_known = True
    elif c is not None:
        lines.append("Czynsz: " + ("w cenie" if c == 0 else zl(c)))
        parts.append(c)
        extra_known = True
    elif m is not None:
        lines.append("Media: " + ("w cenie" if m == 0 else zl(m)))
        parts.append(m)
        extra_known = True
    if lst.parking_pln is not None:
        lines.append(f"{_parking_label(lst)}: " + ("w cenie" if lst.parking_pln == 0 else zl(lst.parking_pln)))
        parts.append(lst.parking_pln)
        extra_known = True
    elif (pk := parking_without_price(lst)):
        lines.append(f"{_parking_label(lst)}: {pk}")              # no amount -> not in Razem
    if lst.prad_pln is not None:
        lines.append("Prąd: " + ("w cenie" if lst.prad_pln == 0 else f"ok. {zl(lst.prad_pln)}"))
        parts.append(lst.prad_pln)
        extra_known = True
    razem = sum(parts) if (lst.price_pln and extra_known) else None
    return lines, razem


def available_text(v: Optional[str], today: Optional[dt.date] = None) -> Optional[str]:
    if not v:
        return None
    if v == "immediately":
        return "od zaraz"
    try:
        d = dt.date.fromisoformat(v[:10])
    except ValueError:
        return None
    today = today or dt.datetime.now(TZ).date()
    if d <= today:
        return "od zaraz"
    return d.strftime("%d.%m.%Y")


# ----------------------------------------------------------------------------- post
@dataclass
class Line:
    text: str
    bold: bool = False


def post_lines(lst: Listing, today: Optional[dt.date] = None, footer: str = "") -> list[Line]:
    head = [room_label(lst.rooms)]
    if lst.area_m2:
        head.append(f"{fmt_num(lst.area_m2)} m²")
    feat = key_feature(lst)
    if feat:
        head.append(feat)
    out = [Line(" | ".join(head), True), Line(""), Line(LANG_LINE), Line(""), Line("WYNAJEM", True), Line(""),
           Line("Do wynajęcia komfortowe mieszkanie"), Line(location_line(lst)), Line(""),
           Line(template_sentence(lst.rooms, lst.area_m2))]
    costs, razem = cost_lines(lst)
    if costs:
        out.append(Line(""))
        out += [Line(x) for x in costs]
    if razem is not None:
        out += [Line(""), Line(f"Razem: {zl(razem)}/mies.", True)]
    tail = []
    if lst.kaucja_pln is not None:
        tail.append(Line("Kaucja: " + ("brak" if lst.kaucja_pln == 0 else zl(lst.kaucja_pln))))
    av = available_text(lst.available_from, today)
    if av:
        tail.append(Line(f"Dostępne {av}" if av == "od zaraz" else f"Dostępne od: {av}"))
    if tail:
        out.append(Line(""))
        out += tail
    if footer.strip():
        out.append(Line(""))
        out += [Line(x) for x in footer.strip().split("\n")]
    return out


def render_html(lines: list[Line]) -> str:
    return "\n".join(f"<b>{html.escape(l.text)}</b>" if l.bold and l.text else html.escape(l.text)
                     for l in lines)


def render_plain(lines: list[Line]) -> str:
    return "\n".join(l.text for l in lines)


@dataclass
class Post:
    html: str
    plain: str

    @property
    def fits_caption(self) -> bool:
        return utf16_len(self.plain) <= CAPTION_LIMIT


def build_post(lst: Listing, today: Optional[dt.date] = None, footer: str = "") -> Post:
    lines = post_lines(lst, today, footer)
    return Post(render_html(lines), render_plain(lines))


# ----------------------------------------------------------------------------- info card (Ukrainian)
BUILDING_UA = {"blok": "blok (блок)", "plomba": "plomba (пломба)", "szeregowiec": "szeregowiec (шерегова)",
               "apartamentowiec": "apartamentowiec (апартаментовець)", "pozostale": "інше (pozostałe)"}


def local_time(iso: Optional[str]) -> Optional[str]:
    if not iso:
        return None
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(TZ).strftime("%d.%m %H:%M")


def ts_local(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M")


def build_info(lst: Listing, dup: Optional[dict] = None, blacklisted: bool = False) -> str:
    """dup: {'source','url','sent_at'(ts)|None,'why'} for a possible duplicate."""
    e = html.escape
    src = SOURCE_NAMES.get(lst.source, lst.source)
    rows = [f"🔗 <b>{src}</b>: <a href=\"{e(lst.url, quote=True)}\">відкрити оголошення</a>"]
    ref = lst.cross_source_ref or {}
    if ref.get("url"):
        rows.append(f"↪️ Імпорт з {e(SOURCE_NAMES.get(ref.get('source'), ref.get('source') or ''))}: "
                    f"<a href=\"{e(ref['url'], quote=True)}\">оригінал</a>")
    rows.append("🏗 Тип будинку: " + e(BUILDING_UA.get(lst.building_type or "", lst.building_type or "не вказано")))
    when = local_time(lst.created_at)
    if when:
        rows.append(f"🕒 Опубліковано: {when}")
    weak = [s.split(":", 1)[1] for s in lst.private_signals or [] if s.startswith("weak_agent:")]
    if weak:
        rows.append("❔ Слабкі ознаки агента: " + e(", ".join(weak[:3])))
    if dup:
        when = f" від {ts_local(dup['sent_at'])}" if dup.get("sent_at") else ""
        rows.append(f"⚠️ <b>Можливий дубль</b>: схоже на {e(SOURCE_NAMES.get(dup.get('source'), dup.get('source') or ''))}"
                    f"-оголошення{when} (<a href=\"{e(dup.get('url') or '', quote=True)}\">посилання</a>)"
                    + (f". Збіг: {e(dup['why'])}" if dup.get("why") else ""))
    if blacklisted:
        rows.append("🏢 <b>Позначено як посередника.</b> Оголошення цього продавця більше не надсилаю.")
    return "\n".join(rows)


# ----------------------------------------------------------------------------- TikTok
def tiktok_caption(lst: Listing, today: Optional[dt.date] = None, footer: str = "") -> str:
    """Description under the TikTok carousel: the Telegram post without its first line, which is the title
    (TikTok drops a description that starts with the title). Plain text, no hashtags."""
    lines = post_lines(lst, today, footer)[1:]
    while lines and not lines[0].text:
        lines = lines[1:]
    text = render_plain(lines)
    while utf16_len(text) > TIKTOK_CAPTION_LIMIT:      # practically never
        text = text[: len(text) - 50]
    return text


def tiktok_title(lst: Listing) -> str:
    """Title of the TikTok post: the bold first line of the post ('Kawalerka | 35 m² | taras')."""
    title = post_lines(lst)[0].text
    while utf16_len(title) > TIKTOK_TITLE_LIMIT:
        title = title[:-1]
    return title


def slide_labels(lst: Listing) -> tuple[str, str]:
    top = [room_label(lst.rooms)]
    if lst.area_m2:
        top.append(f"{fmt_num(lst.area_m2)} m²")
    d = district_short(lst)
    top.append(f"Wrocław ({d})" if d else "Wrocław")
    feat = key_feature(lst)
    bottom = feature_display(feat) if feat else (f"{zl(lst.price_pln)}/mies." if lst.price_pln else "Do wynajęcia")
    return " | ".join(top), bottom
