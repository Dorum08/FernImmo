"""Lage: Koordinaten aus Inseraten, Geocoding (OSM Nominatim), PLZ-Gebiete,
Punkt-in-Polygon und Suchgebiets-Prüfung. Bewusst ohne Geo-Bibliotheken."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import httpx

log = logging.getLogger(__name__)

PLZ_FILE = Path(__file__).parent / "static" / "plz_nuernberg.geojson"
# Grober Rahmen Deutschland – schützt vor vertauschten oder unsinnigen Werten
DE_LAT, DE_LON = (47.0, 55.2), (5.5, 15.5)
STREET_RE = re.compile(
    r"[A-Za-zÄÖÜäöüß.\- ]{3,}(?:straße|strasse|str\.|weg|gasse|platz|ring|allee|damm|ufer|anlage|graben|"
    r"markt|steig|berg|hof|park|zeile)\s*\d+", re.IGNORECASE)


# --------------------------------------------------------------------------- Geometrie
def _rings(geom: dict) -> list[list[list[list[float]]]]:
    """Liefert Polygone als Liste von [äußerer Ring, Löcher...]; Koordinaten [lon, lat]."""
    if geom["type"] == "Polygon":
        return [geom["coordinates"]]
    if geom["type"] == "MultiPolygon":
        return geom["coordinates"]
    if geom["type"] == "Feature":
        return _rings(geom["geometry"])
    if geom["type"] == "FeatureCollection":
        return [p for f in geom["features"] for p in _rings(f["geometry"])]
    return []


def _in_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-15) + xi:
            inside = not inside
        j = i
    return inside


def point_in(lat: float, lon: float, geom: dict) -> bool:
    for poly in _rings(geom):
        if poly and _in_ring(lon, lat, poly[0]) and not any(_in_ring(lon, lat, h) for h in poly[1:]):
            return True
    return False


def bbox(geom: dict) -> tuple[float, float, float, float]:
    pts = [p for poly in _rings(geom) for ring in poly for p in ring]
    return (min(p[1] for p in pts), min(p[0] for p in pts), max(p[1] for p in pts), max(p[0] for p in pts))


def _segments_cross(a, b, c, d) -> bool:
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    return (orient(a, b, c) * orient(a, b, d) < 0) and (orient(c, d, a) * orient(c, d, b) < 0)


def overlaps(g1: dict, g2: dict) -> bool:
    """Ob sich zwei (Multi-)Polygone überschneiden (Näherung, für Gebiets-Checks ausreichend)."""
    b1, b2 = bbox(g1), bbox(g2)
    if b1[2] < b2[0] or b2[2] < b1[0] or b1[3] < b2[1] or b2[3] < b1[1]:
        return False
    for poly in _rings(g1):
        for p in poly[0][:: max(1, len(poly[0]) // 200)]:
            if point_in(p[1], p[0], g2):
                return True
    for poly in _rings(g2):
        for p in poly[0][:: max(1, len(poly[0]) // 200)]:
            if point_in(p[1], p[0], g1):
                return True
    edges1 = [(r[i], r[i + 1]) for poly in _rings(g1) for r in poly[:1] for i in range(len(r) - 1)]
    edges2 = [(r[i], r[i + 1]) for poly in _rings(g2) for r in poly[:1] for i in range(len(r) - 1)]
    return any(_segments_cross(a, b, c, d) for a, b in edges1 for c, d in edges2)


def valid_geometry(geom: Any) -> bool:
    try:
        polys = _rings(geom)
        return bool(polys) and all(
            len(poly[0]) >= 4 and all(DE_LON[0] - 5 < p[0] < DE_LON[1] + 5 and DE_LAT[0] - 5 < p[1] < DE_LAT[1] + 5
                                      for p in poly[0]) for poly in polys)
    except (KeyError, TypeError, IndexError):
        return False


# --------------------------------------------------------------------------- PLZ-Gebiete
@lru_cache(maxsize=1)
def plz_areas() -> dict[str, dict]:
    """PLZ -> Feature (mitgelieferte OSM-Grenzen für den Raum Nürnberg)."""
    try:
        fc = json.loads(PLZ_FILE.read_text(encoding="utf-8"))
    except OSError:
        return {}
    return {f["properties"]["plz"]: f for f in fc["features"]}


def plz_center(plz: str | None) -> tuple[float, float] | None:
    f = plz_areas().get(plz or "")
    return tuple(f["properties"]["center"]) if f else None  # type: ignore[return-value]


def plz_for_point(lat: float, lon: float) -> str | None:
    for plz, f in plz_areas().items():
        if point_in(lat, lon, f["geometry"]):
            return plz
    return None


# --------------------------------------------------------------------------- Koordinaten aus HTML
_META_PAIRS = [
    ("og:latitude", "og:longitude"),
    ("place:location:latitude", "place:location:longitude"),
    ("latitude", "longitude"),
]
_JSON_PAIR = re.compile(
    r'"(?:lat|latitude|geoLat)"\s*:\s*"?(-?\d{1,2}\.\d{3,})"?\s*,\s*"(?:lng|lon|long|longitude|geoLng)"\s*:\s*"?(-?\d{1,3}\.\d{3,})',
    re.IGNORECASE)


def _plausible(lat: float, lon: float) -> bool:
    return DE_LAT[0] <= lat <= DE_LAT[1] and DE_LON[0] <= lon <= DE_LON[1]


def coords_from_html(html: str) -> tuple[float, float] | None:
    """Sucht Koordinaten in Meta-Tags, JSON-LD und eingebetteten JSON-Daten der Detailseite."""
    for a, b in _META_PAIRS:
        ma = re.search(rf'<meta[^>]+(?:property|name)=["\']{re.escape(a)}["\'][^>]+content=["\'](-?[\d.]+)', html)
        mb = re.search(rf'<meta[^>]+(?:property|name)=["\']{re.escape(b)}["\'][^>]+content=["\'](-?[\d.]+)', html)
        if ma and mb:
            lat, lon = float(ma.group(1)), float(mb.group(1))
            if _plausible(lat, lon):
                return lat, lon
    m = re.search(r'<meta[^>]+name=["\'](?:geo\.position|ICBM)["\'][^>]+content=["\'](-?[\d.]+)[;,]\s*(-?[\d.]+)', html)
    if m and _plausible(float(m.group(1)), float(m.group(2))):
        return float(m.group(1)), float(m.group(2))
    for s in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        m = re.search(r'"geo"\s*:\s*\{[^}]*?"latitude"\s*:\s*"?(-?[\d.]+)"?[^}]*?"longitude"\s*:\s*"?(-?[\d.]+)', s)
        if m and _plausible(float(m.group(1)), float(m.group(2))):
            return float(m.group(1)), float(m.group(2))
    m = _JSON_PAIR.search(html)
    if m and _plausible(float(m.group(1)), float(m.group(2))):
        return float(m.group(1)), float(m.group(2))
    return None


def has_street(address: str) -> bool:
    return bool(STREET_RE.search(address or ""))


# --------------------------------------------------------------------------- Geocoding
class Geocoder:
    """OSM Nominatim mit Cache in der Datenbank und höchstens 1 Anfrage/Sekunde
    (Nutzungsrichtlinie: https://operations.osmfoundation.org/policies/nominatim/)."""

    def __init__(self, db, cfg: dict[str, Any]):
        self.db = db
        self.enabled = bool(cfg.get("enabled", True))
        self.url = cfg.get("url", "https://nominatim.openstreetmap.org/search")
        ua = cfg.get("user_agent") or "FernImmo/1.0 (selbst gehostete Wohnungssuche)"
        if cfg.get("email"):
            ua += f" {cfg['email']}"
        self.headers = {"User-Agent": ua, "Accept-Language": "de"}
        self.city_hint = cfg.get("city_hint", "Nürnberg")
        self._lock = threading.Lock()
        self._last = 0.0

    def geocode(self, query: str) -> tuple[float, float] | None:
        query = re.sub(r"\s+", " ", query).strip()
        if not query:
            return None
        cached = self.db.geocache_get(query)
        if cached is not None:
            return cached or None
        if not self.enabled:
            return None
        with self._lock:
            wait = 1.1 - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = httpx.get(self.url, params={"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "de"},
                              headers=self.headers, timeout=20)
                r.raise_for_status()
                res = r.json()
            except Exception as e:  # noqa: BLE001 - Geocoding ist optional
                log.warning("Geocoding fehlgeschlagen für %r: %s", query, e)
                return None
        point = (float(res[0]["lat"]), float(res[0]["lon"])) if res else None
        if point and not _plausible(*point):
            point = None
        self.db.geocache_put(query, point)
        return point

    def locate(self, address: str, plz: str | None, portal_coords: tuple[float, float] | None
               ) -> tuple[float | None, float | None, str | None]:
        """Liefert (lat, lon, Genauigkeit). Genauigkeit: portal | adresse | plz."""
        if portal_coords:
            return portal_coords[0], portal_coords[1], "portal"
        if address and has_street(address):
            q = address if (plz and plz in address) or self.city_hint.lower() in address.lower() \
                else f"{address}, {plz or ''} {self.city_hint}"
            p = self.geocode(q)
            if p:
                return p[0], p[1], "adresse"
        c = plz_center(plz)
        if c:
            return c[0], c[1], "plz"
        if plz:
            p = self.geocode(f"{plz}, Deutschland")
            if p:
                return p[0], p[1], "plz"
        return None, None, None


# --------------------------------------------------------------------------- Suchgebiet
class AreaFilter:
    """Suchgebiet aus der Weboberfläche: ausgewählte PLZ und/oder frei gezeichnete Flächen."""

    def __init__(self, plz: Iterable[str] = (), polygons: Iterable[dict] = (), fw_zones: Iterable[dict] = ()):
        self.plz = set(plz)
        self.polygons = list(polygons)
        self.fw_zones = list(fw_zones)

    @property
    def active(self) -> bool:
        return bool(self.plz or self.polygons)

    def check(self, data: dict) -> bool | None:
        """True = im Gebiet, False = außerhalb, None = unklar (Lage zu ungenau bekannt)."""
        plz, lat, lon, prec = data.get("plz"), data.get("lat"), data.get("lon"), data.get("geo_precision")
        if plz and plz in self.plz:
            return True
        if not self.polygons:
            return False if plz or lat is not None else None
        if lat is not None and prec in ("portal", "adresse"):
            return any(point_in(lat, lon, g) for g in self.polygons)
        # Nur die PLZ ist bekannt: überschneidet sich das PLZ-Gebiet mit einer gezeichneten Fläche?
        feat = plz_areas().get(plz or "")
        if feat:
            return None if any(overlaps(feat["geometry"], g) for g in self.polygons) else False
        return None

    def in_fw_zone(self, data: dict) -> bool:
        lat, lon = data.get("lat"), data.get("lon")
        if lat is None or data.get("geo_precision") not in ("portal", "adresse"):
            return False
        return any(point_in(lat, lon, g) for g in self.fw_zones)
