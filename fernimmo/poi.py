"""„In der Nähe“: Einkaufen, ÖPNV, Bildung, Gesundheit aus OpenStreetMap (Overpass-API).

Nur für Wohnungen mit genauer Lage (Portal-Koordinaten oder Adresse). Ergebnisse werden
30 Tage in der Datenbank gespeichert; die Overpass-API wird höchstens einmal pro Wohnung
und Zeitraum gefragt. Daten © OpenStreetMap-Mitwirkende (ODbL).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .geo import haversine_km

log = logging.getLogger(__name__)

RADIUS_M = 1000
CATEGORIES = {
    "einkaufen": {"label": "Einkaufen", "filters": ['nwr["shop"~"^(supermarket|bakery|convenience|chemist)$"]']},
    "oepnv": {"label": "ÖPNV", "filters": ['node["highway"="bus_stop"]', 'node["railway"~"^(tram_stop|station|halt)$"]',
                                          'node["railway"="subway_entrance"]']},
    "bildung": {"label": "Bildung", "filters": ['nwr["amenity"~"^(school|kindergarten|childcare)$"]']},
    "gesundheit": {"label": "Gesundheit", "filters": ['nwr["amenity"~"^(doctors|pharmacy|hospital|clinic|dentist)$"]']},
}


def _query(lat: float, lon: float) -> str:
    parts = [f'{f}(around:{RADIUS_M},{lat},{lon});' for c in CATEGORIES.values() for f in c["filters"]]
    return f"[out:json][timeout:25];({''.join(parts)});out center tags 400;"


def _category(tags: dict) -> str | None:
    if tags.get("shop") in ("supermarket", "bakery", "convenience", "chemist"):
        return "einkaufen"
    if tags.get("highway") == "bus_stop" or tags.get("railway") in ("tram_stop", "station", "halt", "subway_entrance"):
        return "oepnv"
    a = tags.get("amenity")
    if a in ("school", "kindergarten", "childcare"):
        return "bildung"
    if a in ("doctors", "pharmacy", "hospital", "clinic", "dentist"):
        return "gesundheit"
    return None


def _kind(tags: dict) -> str:
    if tags.get("railway") in ("station", "halt"):
        return "🚆"
    if tags.get("railway") == "subway_entrance":
        return "Ⓤ"
    if tags.get("railway") == "tram_stop":
        return "🚊"
    if tags.get("highway") == "bus_stop":
        return "🚌"
    return ""


def parse(data: dict, lat: float, lon: float, per_cat: int = 4) -> dict[str, Any]:
    out: dict[str, list] = {k: [] for k in CATEGORIES}
    seen: set[tuple] = set()
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        cat = _category(tags)
        name = tags.get("name") or tags.get("brand")
        if not cat or not name:
            continue
        la = el.get("lat") or (el.get("center") or {}).get("lat")
        lo = el.get("lon") or (el.get("center") or {}).get("lon")
        if la is None:
            continue
        key = (cat, name.lower())
        dist = round(haversine_km(lat, lon, la, lo) * 1000)
        if key in seen:   # z. B. Haltestelle mit mehreren Masten: nur die nächste
            prev = next(p for p in out[cat] if p["name"].lower() == name.lower())
            if dist < prev["m"]:
                prev.update(m=dist, lat=la, lon=lo)
            continue
        seen.add(key)
        out[cat].append({"name": name, "m": dist, "lat": la, "lon": lo, "kind": _kind(tags)})
    return {"radius_m": RADIUS_M, "categories": {
        k: {"label": CATEGORIES[k]["label"], "items": sorted(v, key=lambda p: p["m"])[:per_cat],
            "count": len(v)} for k, v in out.items()}}


def nearby(db, lid: int, lat: float, lon: float, cfg: dict[str, Any]) -> dict | None:
    """Gespeichertes Ergebnis (bis 30 Tage alt) oder neue Abfrage bei Overpass."""
    cached = db.poi_get(lid)
    if cached and cached["lat"] == lat and cached["lon"] == lon and cached["ts"] > (
            datetime.now(timezone.utc) - timedelta(days=30)).isoformat():
        import json
        return json.loads(cached["data"])
    if not cfg.get("enabled", True):
        return None
    url = cfg.get("url", "https://overpass-api.de/api/interpreter")
    try:
        r = httpx.post(url, data={"data": _query(lat, lon)}, timeout=40,
                       headers={"User-Agent": "FernImmo/1.0 (selbst gehostete Wohnungssuche)"})
        r.raise_for_status()
        result = parse(r.json(), lat, lon)
    except Exception as e:  # noqa: BLE001 - Umgebung ist ein Zusatz
        log.warning("Umgebung für #%s nicht abrufbar: %s", lid, e)
        return None
    db.poi_put(lid, lat, lon, result)
    return result
