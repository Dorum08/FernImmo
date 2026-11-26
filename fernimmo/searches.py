"""Mehrere benannte Suchen.

Jede Suche hat eigene Kriterien (Kauf/Miete, Zimmer, Preis, Fläche, Heizung, Ausschlüsse),
ein eigenes Gebiet (PLZ, Flächen, Umkreise) und lässt sich einzeln pausieren oder
stummschalten. Fernwärme-Gebiete gelten für alle Suchen gemeinsam.

Die Suchen liegen in der Datenbank und werden in der Weboberfläche gepflegt. Beim ersten
Start werden sie aus den `profiles` der config.yaml (und früheren Einstellungen der
Weboberfläche) angelegt; danach ist die Datenbank maßgeblich.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .geo import AreaFilter
from .matcher import LEVELS, Profile, match

HEATING_CHOICES = {"fernwaerme": ["fernwaerme"], "fern_nah": ["fernwaerme", "nahwaerme"], "egal": []}
FLAG_CHOICES = ["gesuch", "versteigerung", "erbpacht", "vermietet", "denkmal", "wbs", "senioren", "tausch",
                "wg", "befristet"]
TYPE_CHOICES = ("kauf", "miete")

DEFAULT_CRITERIA: dict[str, Any] = {
    "listing_types": ["kauf"], "rooms_min": None, "rooms_max": None, "price_max": None,
    "area_min": None, "area_max": None, "year_min": None,
    "heating_required": ["fernwaerme"], "unknown_heating": "teilweise",
    "exclude_flags": ["gesuch", "versteigerung"], "exclude_keywords": [], "required_features": [],
}


@dataclass
class Search:
    id: int
    name: str
    active: bool
    notify: bool
    criteria: dict[str, Any]
    plz: list[str]
    profile: Profile = field(repr=False, default=None)  # type: ignore[assignment]
    area: AreaFilter = field(repr=False, default=None)  # type: ignore[assignment]


@dataclass
class Outcome:
    level: str = "none"                 # beste Stufe über alle Suchen
    ids: list[int] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    notify_level: str = "none"          # beste Stufe über Suchen mit eingeschalteter Meldung


# --------------------------------------------------------------------------- Kriterien
def _num(v: Any) -> float | None:
    if v in (None, ""):
        return None
    f = float(str(v).replace(",", "."))
    return f if f > 0 else None


def clean_criteria(raw: dict[str, Any], base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Prüft Kriterien aus der Weboberfläche; unbekannte Felder werden ignoriert.
    Wirft ValueError mit verständlicher Meldung."""
    c = dict(base or DEFAULT_CRITERIA)
    if "listing_types" in raw:
        lt = [t for t in (raw["listing_types"] or []) if t in TYPE_CHOICES]
        if not lt:
            raise ValueError("Mindestens Kauf oder Miete wählen")
        c["listing_types"] = lt
    for k in ("rooms_min", "rooms_max", "price_max", "area_min", "area_max", "year_min"):
        if k in raw:
            try:
                c[k] = _num(raw[k])
            except ValueError:
                raise ValueError(f"Ungültige Zahl bei {k}") from None
    if c["rooms_min"] and c["rooms_max"] and c["rooms_min"] > c["rooms_max"]:
        raise ValueError("„Zimmer von“ ist größer als „bis“")
    if c["area_min"] and c["area_max"] and c["area_min"] > c["area_max"]:
        raise ValueError("„Fläche ab“ ist größer als „bis“")
    if "heating" in raw:
        if raw["heating"] not in HEATING_CHOICES:
            raise ValueError("Unbekannte Heizungs-Auswahl")
        c["heating_required"] = HEATING_CHOICES[raw["heating"]]
    if "unknown_heating" in raw:
        if raw["unknown_heating"] not in ("teilweise", "ignorieren"):
            raise ValueError("Unbekannte Einstellung für fehlende Heizungsangabe")
        c["unknown_heating"] = raw["unknown_heating"]
    if "exclude_flags" in raw:
        c["exclude_flags"] = [f for f in raw["exclude_flags"] or [] if f in FLAG_CHOICES]
    if "exclude_keywords" in raw:
        kws = raw["exclude_keywords"]
        if isinstance(kws, str):
            kws = kws.split(",")
        c["exclude_keywords"] = [k.strip()[:60] for k in kws if k and k.strip()][:30]
    return c


def heating_choice(c: dict[str, Any]) -> str:
    req = set(c.get("heating_required") or [])
    if not req:
        return "egal"
    return "fern_nah" if "nahwaerme" in req else "fernwaerme"


def criteria_from_profile(p: Profile) -> dict[str, Any]:
    d = asdict(p)
    return {k: d.get(k, v) for k, v in DEFAULT_CRITERIA.items()}


def profile_for(name: str, c: dict[str, Any]) -> Profile:
    # Die Lage prüft das AreaFilter der Suche, daher kein plz im Profil
    return Profile.from_dict({**c, "name": name, "plz": []})


# --------------------------------------------------------------------------- Laden
def ensure_searches(cfg, db) -> None:
    """Legt beim ersten Start Suchen aus config.yaml bzw. alten Einstellungen an."""
    if db.searches():
        return
    old_crit = db.get_setting("criteria") or {}
    old_plz = db.get_setting("search_plz")
    profiles = cfg.profiles or [Profile(name="Meine Suche")]
    for i, p in enumerate(profiles):
        c = criteria_from_profile(p)
        for k in ("rooms_min", "rooms_max", "price_max", "area_min"):
            if k in old_crit:
                c[k] = old_crit[k]
        plz = old_plz if old_plz is not None else list(p.plz)
        sid = db.add_search(p.name or f"Suche {i + 1}", c, plz, user="config")
        if i == 0:
            db.assign_orphan_areas(sid)   # früher gezeichnete Flächen gehören zur ersten Suche


def build_area(db, s: dict, fw_zones: list[dict]) -> AreaFilter:
    areas = db.areas("search", search_id=s["id"])
    return AreaFilter(
        plz=s["plz"],
        polygons=[a["geometry"] for a in areas if a.get("radius_km") is None],
        circles=[(a["center_lat"], a["center_lon"], a["radius_km"]) for a in areas if a.get("radius_km") is not None],
        fw_zones=fw_zones,
    )


def load_searches(db, only_active: bool = True) -> list[Search]:
    fw = [a["geometry"] for a in db.areas("fw")]
    out = []
    for s in db.searches():
        if only_active and not s["active"]:
            continue
        out.append(Search(id=s["id"], name=s["name"], active=bool(s["active"]), notify=bool(s["notify"]),
                          criteria=s["criteria"], plz=s["plz"], profile=profile_for(s["name"], s["criteria"]),
                          area=build_area(db, s, fw)))
    return out


def fw_filter(db) -> AreaFilter:
    return AreaFilter(fw_zones=[a["geometry"] for a in db.areas("fw")])


# --------------------------------------------------------------------------- Abgleich
def match_all(searches: list[Search], listing_type: str | None, data: dict, text: str) -> Outcome:
    out = Outcome()
    for s in searches:
        r = match(s.profile, listing_type, data, text, s.area)
        if r.level != "none":
            out.ids.append(s.id)
            out.names.append(s.name)
            if s.notify and LEVELS[r.level] > LEVELS[out.notify_level]:
                out.notify_level = r.level
        if LEVELS[r.level] > LEVELS[out.level] or (not out.reasons and r.level == out.level):
            out.level, out.reasons = r.level, ([f"{s.name}: " + x for x in r.reasons] if len(searches) > 1
                                              else r.reasons)
    return out
