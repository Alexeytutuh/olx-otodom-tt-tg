"""Normalized listing schema shared by all sources."""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Listing:
    source: str                         # "olx" | "otodom"
    source_id: str
    url: str
    title: str
    description_text: str = ""          # HTML stripped, whitespace normalized
    price_pln: Optional[float] = None   # monthly rent asked by owner
    czynsz_pln: Optional[float] = None  # administrative fee; 0 = explicitly included in price
    czynsz_max_pln: Optional[float] = None  # upper bound when the text gives a range ("800–1000 zł")
    media_pln: Optional[float] = None   # utilities estimate if stated
    prad_pln: Optional[float] = None    # electricity estimate if stated
    kaucja_pln: Optional[float] = None  # deposit
    parking_pln: Optional[float] = None # garage / parking spot price; 0 = included
    parking: Optional[str] = None       # free text, e.g. "w garażu", "garage"
    pets: Optional[str] = None          # "tak"/"nie" if structured
    area_m2: Optional[float] = None
    rooms: Optional[int] = None
    floor: Optional[int] = None         # 0 = parter, -1 = suterena
    building_type: Optional[str] = None # canonical: blok/plomba/szeregowiec/apartamentowiec/
                                        # kamienica/dom_wolnostojacy/loft/pozostale
    available_from: Optional[str] = None  # ISO date "2026-10-15" or "immediately"
    street: Optional[str] = None
    district: Optional[str] = None
    city: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    photos: list[str] = field(default_factory=list)
    first_photo_url: Optional[str] = None
    seller_id: Optional[str] = None
    seller_name: Optional[str] = None
    is_private: bool = True
    private_signals: list[str] = field(default_factory=list)
    created_at: Optional[str] = None    # ISO-8601 with offset (first publication)
    refreshed_at: Optional[str] = None  # last bump/refresh if known
    is_promoted: bool = False
    cross_source_ref: Optional[dict] = None   # e.g. {"source":"otodom","id":"ID4Dsnv","url":...}
    # Same value for the same physical ad on both sites when it is detectable (OLX import of an Otodom ad):
    # "otodom:ID4Dsnv". Otherwise "<source>:<source_id>". The bot should dedupe on this key.
    dedupe_key: Optional[str] = None
    extraction_notes: list[str] = field(default_factory=list)  # e.g. "czynsz: from description"
    raw_extra: dict[str, Any] = field(default_factory=dict)    # small extra facts (furniture, lift...)

    def __post_init__(self):
        if self.photos and not self.first_photo_url:
            self.first_photo_url = self.photos[0]
        if not self.dedupe_key:
            pid = (self.cross_source_ref or {}).get("public_id")
            self.dedupe_key = f"otodom:{pid}" if pid else f"{self.source}:{self.source_id}"

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    def to_json(self, **kw) -> str:
        kw.setdefault("ensure_ascii", False)
        return json.dumps(self.to_dict(), **kw)

    @classmethod
    def from_dict(cls, d: dict) -> "Listing":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


@dataclass
class Rejection:
    source_id: str
    reason: str          # short machine-ish key, e.g. "building:kamienica"
    title: str = ""
    url: str = ""

    def to_dict(self):
        return dataclasses.asdict(self)


@dataclass
class FetchResult:
    """Everything one fetch_new() call learned. fetch_new() returns .listings;
    the full object is kept on parser.last_result."""
    listings: list[Listing] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    seen_ids: set[str] = field(default_factory=set)      # all ids evaluated; mark them seen
    retry_ids: set[str] = field(default_factory=set)     # transient per-item failures; do NOT mark seen
    pages: int = 0
    requests: int = 0
    items_fetched: int = 0
    seconds: float = 0.0

    def rejected_by_reason(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rejected:
            key = r.reason.split(":")[0] if r.reason.startswith(("agent_text", "agent_seller_name",
                                                                 "not_city")) else r.reason
            out[key] = out.get(key, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def summary(self) -> dict:
        return {"items_fetched": self.items_fetched, "passed": len(self.listings),
                "rejected": len(self.rejected), "rejected_by_reason": self.rejected_by_reason(),
                "pages": self.pages, "requests": self.requests,
                "retry_ids": sorted(self.retry_ids), "seconds": round(self.seconds, 2)}
