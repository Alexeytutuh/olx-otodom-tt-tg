"""Wrocław street names (data/wroclaw_streets.txt, from OpenStreetMap) to complete street names that the OLX
parser takes from free text: 'przy ulicy Róży wiatrów' gives only 'Róży', the list knows 'Róży Wiatrów'."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

DATA = Path(__file__).with_name("data") / "wroclaw_streets.txt"


@lru_cache(maxsize=1)
def _index() -> tuple[dict[str, str], dict[str, list[str]]]:
    """(lower name -> proper name, lower first word -> multi-word names, longest first)."""
    names: dict[str, str] = {}
    multi: dict[str, list[str]] = {}
    try:
        lines = DATA.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}, {}
    for n in lines:
        n = n.strip()
        if not n or n.startswith("#"):
            continue
        names[n.lower()] = n
        words = n.split()
        if len(words) > 1:
            multi.setdefault(words[0].lower(), []).append(n)
    for v in multi.values():
        v.sort(key=len, reverse=True)
    return names, multi


def complete(captured: str, text: str, degenitive: Callable[[str], str]) -> Optional[str]:
    """captured: street as found by the parser, without 'ul.' ('Róży', 'Braterskiej 5').
    Returns the proper name from the list (+ the house number if it was captured) or None if unknown."""
    names, multi = _index()
    parts = captured.split()
    if not names or not parts:
        return None
    number = next((p for p in parts[1:] if re.match(r"^\d", p)), None)
    first = parts[0]
    # multi-word: the text right after the captured first word must continue with the rest of the name
    pos = text.find(first)
    if pos >= 0:
        after = re.sub(r"\s+", " ", text[pos:pos + 80]).lower()
        glued = after.replace(" ", "")                  # 'ul. AntoniaVivaldiego' (typo without a space)
        for cand in multi.get(first.lower(), ()):
            c = cand.lower()
            g = c.replace(" ", "")
            if ((after.startswith(c) and not after[len(c):len(c) + 1].isalpha())
                    or (glued.startswith(g) and not glued[len(g):len(g) + 1].isalpha())):
                return f"{cand} {number}" if number else cand
    name_words = [p for p in parts if not re.match(r"^\d", p)]
    for key in (" ".join(name_words), " ".join(name_words[:-1] + [degenitive(name_words[-1])]) if name_words else ""):
        if key and key.lower() in names:
            proper = names[key.lower()]
            return f"{proper} {number}" if number else proper
    return None
