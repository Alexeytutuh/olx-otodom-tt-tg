"""Criteria checks shared by both sources. Every rejection gets a short reason string."""
from __future__ import annotations

import re
from typing import Optional

from . import config
from .models import Listing

# ----------------------------------------------------------------- building type
_BUILDING_MAP = {
    # OLX "builttype" keys
    "blok": "blok", "kamienica": "kamienica", "apartamentowiec": "apartamentowiec",
    "wolnostojacy": "dom_wolnostojacy", "dom_wolnostojacy": "dom_wolnostojacy",
    "szeregowiec": "szeregowiec", "plomba": "plomba", "loft": "loft", "pozostale": "pozostale",
    # Otodom characteristics "building_type" / URL enum
    "block": "blok", "tenement": "kamienica", "apartment": "apartamentowiec", "house": "dom_wolnostojacy",
    "detached": "dom_wolnostojacy", "infill": "plomba", "ribbon": "szeregowiec", "other": "pozostale",
}


def normalize_building_type(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    k = str(raw).strip().lower().replace("building_type::", "")
    k = (k.replace("ł", "l").replace("ó", "o").replace("ą", "a").replace("ę", "e")
          .replace(" ", "_").replace("-", "_"))
    return _BUILDING_MAP.get(k, k)


# ----------------------------------------------------------------- agent text heuristic
NEGATION_BEFORE = re.compile(
    r"(?:\bbez|\bbrak\w*|\bnie\b(?:\s+\w+){0,2}|\b0\s*%?|\bzero|\b[zż]adn\w*|\bwoln\w* od|\bnie jestem|"
    r"\bnie pobiera\w*|\bnie wsp[oó][lł]pracuj\w*(?:\s+z)?)\s*(?:\w+[\s/,]+|[/,]\s*|i\s+|oraz\s+){0,3}$", re.I)
NEGATION_AFTER = re.compile(
    r"^\W{0,3}(?:\w+\W+){0,2}?(?:brak|nie ma|nie pobier\w*|0\s*%|0\s*z[lł]|zero|dzi[eę]kuj\w*|prosz\w* o nie|"
    r"prosz\w* nie|nie dzwoni\w*|odpada|nie wchodz\w*|wykluczon\w*)", re.I)
# A sentence that tells agents to go away negates every agent word inside it.
NEGATING_SENTENCE = re.compile(
    r"\bbez\b|dzi[eę]kuj|nie\s*kontakt|niekontakt|nie\s*dzwo|prosz\w*[^.\n]{0,60}\bo nie|prosimy[^.\n]{0,60}\bo nie|"
    r"nie\s+wsp[oó][lł]prac|nie jestem|nie jeste[sś]my|odpada|wykluczon|pro[sś]b\w*|pomini\w*|pomija\w*|"
    r"omija\w*|ignoruj\w*|nie\s+zainteresowan\w*", re.I)
_AGENT_RES = [re.compile(p, re.I) for p in config.AGENT_PATTERNS]
_AGENT_STRONG_RES = [re.compile(p, re.I) for p in config.AGENT_STRONG_PATTERNS]
_SELLER_RES = [re.compile(p, re.I) for p in config.SELLER_NAME_AGENT_PATTERNS]
_AGENT_WEAK_RES = [re.compile(p, re.I) for p in config.AGENT_WEAK_PATTERNS]
_PRIVATE_RES = [re.compile(p, re.I) for p in config.PRIVATE_PATTERNS]


def agent_text_signals(text: str) -> list[str]:
    """Return non-negated agency phrases found in text (e.g. ['prowizja 50%'])."""
    hits = []
    if not text:
        return hits
    t = text.replace("\xa0", " ")
    for rx in _AGENT_STRONG_RES:
        m = rx.search(t)
        if m:
            hits.append(m.group(0).lower())
    for rx in _AGENT_RES:
        for m in rx.finditer(t):
            # negation context stays on the same line ("Wolne od zaraz\n\nJan Kowalski\nMetrohouse")
            before = t[max(0, m.start() - 40, t.rfind("\n", 0, m.start()) + 1):m.start()]
            nl = t.find("\n", m.end())
            after = t[m.end():min(m.end() + 30, nl if nl != -1 else len(t))]
            if NEGATION_BEFORE.search(before) or NEGATION_AFTER.search(after):
                continue
            s0 = max(t.rfind(c, 0, m.start()) for c in ".!?\n") + 1
            ends = [i for i in (t.find(c, m.end()) for c in ".!?\n") if i != -1]
            if NEGATING_SENTENCE.search(t[s0:min(ends) if ends else len(t)]):
                continue
            hits.append(m.group(0).lower())
            break
    # de-duplicate overlapping (e.g. "agencja nieruchomości" + "agencja")
    uniq = []
    for h in hits:
        if not any(h != o and h in o for o in hits) and h not in uniq:
            uniq.append(h)
    return uniq


def private_text_signals(text: str) -> list[str]:
    out = []
    for rx in _PRIVATE_RES:
        m = rx.search(text or "")
        if m:
            out.append(m.group(0).lower())
    return out


# ----------------------------------------------------------------- rooms vs whole flat
_ROOM_STRONG = re.compile(r"\bpok[oó]j\w*\s+(?:\d-?\s*osobow\w*\s+)?w\s+(?:\d-?\s*pok\w*\s+)?mieszkaniu|"
                          r"\bosobn\w*\s+(?:\d\s+)?pokoj\w*\s+w\b|\bpokoj\w*\s+w\s+(?:\d-?\s*pok\w*\s+)?mieszkaniu", re.I)
_ROOM_TITLE = re.compile(r"(?<![\d\-–])\s*\bpok[oó]j\b(?!\w)|\bstancj\w*|\bmiejsce w pokoju|\bwsp[oó][lł]lokator\w*|"
                         r"\bpok[oó]j(?:e)? dla (?:student|os[oó]b|pary|pani|pana)", re.I)
_FLAT_WORDS = re.compile(r"mieszka\w*|kawaler\w*|apartament\w*|studio\b|lokal|\d\s*-?\s*pok", re.I)


def looks_like_room(title: str, description: str = "") -> bool:
    t = title or ""
    if _ROOM_STRONG.search(t) and not re.search(r"kawaler\w*|\bstudio\b", t, re.I):
        return True
    if _ROOM_TITLE.search(t) and not _FLAT_WORDS.search(t):
        return True
    return bool(re.search(r"\bwynajm\w* pok[oó]j\b|\bpok[oó]j do wynaj[eę]cia\b", t, re.I))


# ----------------------------------------------------------------- full evaluation
def check_criteria(lst: Listing, agent_text: Optional[str] = None) -> Optional[str]:
    """Return None if listing passes, else a rejection reason. Also fills private_signals."""
    if lst.city and config.CITY_NAME.lower() not in lst.city.lower():
        return f"not_city:{lst.city}"
    if looks_like_room(lst.title, lst.description_text):
        return "room_not_flat"
    if lst.rooms is None:
        if not config.ALLOW_UNKNOWN_ROOMS:
            return "rooms_unknown"
    elif not (config.ROOMS_MIN <= lst.rooms <= config.ROOMS_MAX):
        return f"rooms:{lst.rooms}"
    if lst.price_pln is not None:
        if config.PRICE_MIN_PLN is not None and lst.price_pln < config.PRICE_MIN_PLN:
            return "price_low"
        if config.PRICE_MAX_PLN is not None and lst.price_pln > config.PRICE_MAX_PLN:
            return "price_high"
    if lst.area_m2 is not None:
        if config.AREA_MIN_M2 is not None and lst.area_m2 < config.AREA_MIN_M2:
            return "area_small"
        if config.AREA_MAX_M2 is not None and lst.area_m2 > config.AREA_MAX_M2:
            return "area_big"
    bt = lst.building_type
    if bt in config.BUILDING_EXCLUDED:
        return f"building:{bt}"
    if bt and bt not in config.BUILDING_ALLOWED and not config.ALLOW_UNKNOWN_BUILDING:
        return f"building_unknown:{bt}"
    if not lst.is_private:
        return "not_private"
    if lst.seller_name and not any(sig.startswith("olx:partner_import") for sig in lst.private_signals):
        for rx in _SELLER_RES:
            if rx.search(lst.seller_name):
                return f"agent_seller_name:{lst.seller_name}"
    text = agent_text if agent_text is not None else f"{lst.title}\n{lst.description_text}"
    hits = agent_text_signals(text)
    if hits:
        return "agent_text:" + ",".join(hits[:3])
    for rx in _AGENT_WEAK_RES:
        m = rx.search(text)
        if m:
            lst.private_signals.append(f"weak_agent:{m.group(0).lower()}")
    for s in private_text_signals(text):
        sig = f"text:{s}"
        if sig not in lst.private_signals:
            lst.private_signals.append(sig)
    return None


# ----------------------------------------------------------------- duplicate-text agents
def text_fingerprint(title: str, description: str) -> Optional[str]:
    """Coarse fingerprint of a description (letters/digits only, first 400 chars). Agencies often repost
    the same text from a second 'private' account; used to reject those copies."""
    t = re.sub(r"[^0-9a-ząćęłńóśźż]+", "", (description or "").lower())
    if len(t) < 120:
        return None
    return t[:400]


AGENT_REASON_PREFIXES = ("agent_", "not_private", "many_ads_same_user")
