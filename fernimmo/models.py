from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RawListing:
    """Ein Inserat, wie es von einer Quelle geholt wurde (noch nicht ausgewertet)."""

    source: str
    source_id: str
    url: str
    title: str = ""
    text: str = ""
    structured: dict[str, str] = field(default_factory=dict)
    address: str = ""
    listing_type: str | None = None  # "miete" | "kauf"
    image_url: str | None = None
    lat: float | None = None
    lon: float | None = None
    geo_precision: str | None = None  # portal | adresse | plz


@dataclass
class Listing:
    """Ausgewertetes Inserat, wie es in der Datenbank liegt."""

    id: int | None
    source: str
    source_id: str
    url: str
    title: str
    address: str
    listing_type: str | None
    data: dict[str, Any]
    first_seen: str = ""
    last_seen: str = ""
    match_level: str = "none"
    matched_profiles: list[str] = field(default_factory=list)
    matched_ids: list[int] = field(default_factory=list)
    notify_level: str = "none"   # beste Stufe über Suchen mit eingeschalteter Meldung
    status: str = "neu"
    duplicate_of: int | None = None
    image_url: str | None = None
    raw_text: str = ""

    @property
    def price(self) -> float | None:
        d = self.data
        if self.listing_type == "kauf":
            return d.get("purchase_price") or d.get("price_generic")
        return d.get("rent_cold") or d.get("price_generic") or d.get("rent_warm")
