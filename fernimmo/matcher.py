"""Abgleich ausgewerteter Inserate mit Suchprofilen."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Rangfolge der Treffer-Stufen
LEVELS = {"none": 0, "teilweise": 1, "voll": 2}


@dataclass
class Profile:
    name: str
    listing_types: list[str] = field(default_factory=lambda: ["miete", "kauf"])
    rooms_min: float | None = None
    rooms_max: float | None = None
    area_min: float | None = None
    area_max: float | None = None
    price_max: float | None = None          # Kaltmiete bzw. Kaufpreis
    rent_warm_max: float | None = None
    year_min: int | None = None
    year_max: int | None = None
    heating_required: list[str] = field(default_factory=list)  # z.B. ["fernwaerme"]
    unknown_heating: str = "teilweise"      # "teilweise" | "ignorieren"
    plz: list[str] = field(default_factory=list)  # PLZ oder Präfixe, leer = alle
    exclude_flags: list[str] = field(default_factory=lambda: ["tausch", "wg"])
    exclude_keywords: list[str] = field(default_factory=list)
    required_features: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Profile":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class MatchResult:
    level: str
    reasons: list[str]


def _range_ok(value, lo, hi) -> bool | None:
    """True/False, oder None wenn der Wert unbekannt ist."""
    if lo is None and hi is None:
        return True
    if value is None:
        return None
    if lo is not None and value < lo:
        return False
    if hi is not None and value > hi:
        return False
    return True


def match(profile: Profile, listing_type: str | None, data: dict[str, Any], text: str,
          area=None) -> MatchResult:
    """area: optionales geo.AreaFilter (Suchgebiet aus der Weboberfläche); ersetzt profile.plz."""
    reasons: list[str] = []
    unknown = False

    if listing_type and profile.listing_types and listing_type not in profile.listing_types:
        return MatchResult("none", [f"Vermarktungsart {listing_type}"])

    low = text.lower()
    for flag in profile.exclude_flags:
        if flag in data.get("flags", []):
            return MatchResult("none", [f"Ausschluss: {flag}"])
    for kw in profile.exclude_keywords:
        if re.search(re.escape(kw.lower()), low):
            return MatchResult("none", [f"Ausschluss-Stichwort: {kw}"])

    if listing_type == "kauf":
        price = data.get("purchase_price") or data.get("price_generic")
    else:
        price = data.get("rent_cold") or data.get("price_generic")

    checks = [
        ("Zimmer", data.get("rooms"), profile.rooms_min, profile.rooms_max),
        ("Fläche", data.get("area"), profile.area_min, profile.area_max),
        ("Preis", price, None, profile.price_max),
        ("Warmmiete", data.get("rent_warm"), None, profile.rent_warm_max),
        ("Baujahr", data.get("year"), profile.year_min, profile.year_max),
    ]
    for label, value, lo, hi in checks:
        ok = _range_ok(value, lo, hi)
        if ok is False:
            return MatchResult("none", [f"{label} {value} außerhalb"])
        if ok is None:
            unknown = True
            reasons.append(f"{label} unbekannt")

    if area is not None and area.active:
        inside = area.check(data)
        if inside is False:
            return MatchResult("none", ["außerhalb des Suchgebiets"])
        if inside is None:
            unknown = True
            reasons.append("Lage unklar (nur PLZ/ohne Adresse bekannt)")
    elif profile.plz:
        plz = data.get("plz")
        if plz is None:
            unknown = True
            reasons.append("PLZ unbekannt")
        elif not any(plz.startswith(p) for p in profile.plz):
            return MatchResult("none", [f"PLZ {plz}"])

    for feat in profile.required_features:
        if feat not in data.get("features", []):
            return MatchResult("none", [f"ohne {feat}"])

    if profile.heating_required:
        sources = set(data.get("heating_sources", []))
        if sources & set(profile.heating_required):
            reasons.append("Heizung passt: " + ", ".join(sorted(sources & set(profile.heating_required))))
        elif data.get("heating_from") == "struktur":
            return MatchResult("none", ["Heizung: " + (", ".join(sorted(sources)) or "?")])
        else:
            # Nur Freitext-Hinweise oder gar nichts bekannt
            if profile.unknown_heating == "ignorieren":
                return MatchResult("none", ["Heizung unbekannt"])
            unknown = True
            reasons.append("Heizung unklar" + (f" (Freitext: {', '.join(sorted(sources))})" if sources else "")
                           + (" – liegt aber im eingezeichneten Fernwärme-Gebiet" if data.get("fw_zone") else ""))

    return MatchResult("teilweise" if unknown else "voll", reasons)


def best_match(profiles: list[Profile], listing_type, data, text, area=None) -> tuple[str, list[str], list[str]]:
    """Liefert (beste Stufe, passende Profilnamen, Gründe des besten Profils)."""
    best = "none"
    names: list[str] = []
    best_reasons: list[str] = []
    for p in profiles:
        r = match(p, listing_type, data, text, area)
        if r.level != "none":
            names.append(p.name)
        if LEVELS[r.level] > LEVELS[best] or (not best_reasons and r.level == best):
            best, best_reasons = r.level, r.reasons
    return best, names, best_reasons
