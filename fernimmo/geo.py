"""Lage: Koordinaten aus Inseraten, Geocoding (OSM Nominatim), PLZ-Gebiete,
Punkt-in-Polygon und Suchgebiets-Prüfung. Bewusst ohne Geo-Bibliotheken."""
from __future__ import annotations

import gzip
import json
import logging
import math
import re
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import httpx

log = logging.getLogger(__name__)

PLZ_DIR = Path(__file__).parent / "static" / "plz"
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


# --------------------------------------------------------------------------- Umkreis
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Luftlinie in km."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def circle_polygon(lat: float, lon: float, radius_km: float, n: int = 64) -> dict:
    """Kreis als GeoJSON-Polygon (für Anzeige und Überschneidung mit PLZ-Gebieten)."""
    ring = []
    for i in range(n):
        a = 2 * math.pi * i / n
        dlat = radius_km / 111.32 * math.cos(a)
        dlon = radius_km / (111.32 * math.cos(math.radians(lat))) * math.sin(a)
        ring.append([round(lon + dlon, 6), round(lat + dlat, 6)])
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


# --------------------------------------------------------------------------- PLZ-Gebiete
# Alle deutschen PLZ-Gebiete (OSM), nach den ersten zwei Ziffern aufgeteilt und
# gzip-komprimiert; der Index enthält je PLZ Mittelpunkt, Umriss-Rahmen und Bundesland.
@lru_cache(maxsize=1)
def plz_index() -> dict[str, Any]:
    try:
        return json.loads(gzip.decompress((PLZ_DIR / "index.json.gz").read_bytes()))
    except OSError:
        log.warning("PLZ-Daten fehlen in %s", PLZ_DIR)
        return {"plz": {}, "prefixes": {}}


@lru_cache(maxsize=24)
def plz_group(prefix: str) -> dict[str, dict]:
    """Alle PLZ-Gebiete mit diesen zwei Anfangsziffern: PLZ -> Feature."""
    if not re.fullmatch(r"\d{2}", prefix):
        return {}
    try:
        fc = json.loads(gzip.decompress((PLZ_DIR / f"{prefix}.geojson.gz").read_bytes()))
    except OSError:
        return {}
    return {f["properties"]["plz"]: f for f in fc["features"]}


def plz_feature(plz: str | None) -> dict | None:
    return plz_group(plz[:2]).get(plz) if plz and len(plz) == 5 else None


def plz_exists(plz: str | None) -> bool:
    return bool(plz) and plz in plz_index()["plz"]


def plz_center(plz: str | None) -> tuple[float, float] | None:
    v = plz_index()["plz"].get(plz or "")
    return (v["c"][0], v["c"][1]) if v else None


def plz_state(plz: str | None) -> str | None:
    """Bundesland-Kürzel (BY, BE, …) der PLZ – bei PLZ auf Landesgrenzen nach Lage des Mittelpunkts."""
    v = plz_index()["plz"].get(plz or "")
    return v["s"] if v else None


def plz_for_point(lat: float, lon: float) -> str | None:
    for plz, v in plz_index()["plz"].items():
        b = v["b"]
        if b[0] <= lat <= b[2] and b[1] <= lon <= b[3]:
            f = plz_feature(plz)
            if f and point_in(lat, lon, f["geometry"]):
                return plz
    return None


def plz_selected(plz: str | None, selection: Iterable[str]) -> bool:
    """Auswahl kann ganze PLZ (90443) oder Anfänge (904, 9) enthalten."""
    return bool(plz) and any(plz.startswith(s) for s in selection)


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
        self.city_hint = cfg.get("city_hint", "")      # optional, z.B. "Nürnberg"
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

    @staticmethod
    def _near_plz(point: tuple[float, float], plz: str | None) -> bool:
        """Liegt das Geocoding-Ergebnis im (leicht erweiterten) Rahmen der PLZ? Schützt vor
        Treffern in einer gleichnamigen Straße einer anderen Stadt."""
        v = plz_index()["plz"].get(plz or "")
        if not v:
            return True
        b, m = v["b"], 0.02
        return b[0] - m <= point[0] <= b[2] + m and b[1] - m <= point[1] <= b[3] + m

    def locate(self, address: str, plz: str | None, portal_coords: tuple[float, float] | None
               ) -> tuple[float | None, float | None, str | None]:
        """Liefert (lat, lon, Genauigkeit). Genauigkeit: portal | adresse | plz."""
        if portal_coords:
            return portal_coords[0], portal_coords[1], "portal"
        if address and has_street(address):
            # Ohne PLZ/Ort wäre "Hauptstr. 5" deutschlandweit mehrdeutig -> dann lieber nur PLZ-genau
            has_place = bool(re.search(r"\b\d{5}\b", address)) or "," in address
            q = address if (plz and plz in address) else f"{address}, {plz}" if plz else \
                (address if has_place else (f"{address}, {self.city_hint}" if self.city_hint else None))
            p = self.geocode(q) if q else None
            if p and self._near_plz(p, plz):
                return p[0], p[1], "adresse"
        c = plz_center(plz)
        if c:
            return c[0], c[1], "plz"
        return None, None, None


# --------------------------------------------------------------------------- Suchgebiet
class AreaFilter:
    """Suchgebiet aus der Weboberfläche: ausgewählte PLZ und/oder frei gezeichnete Flächen."""

    def __init__(self, plz: Iterable[str] = (), polygons: Iterable[dict] = (), fw_zones: Iterable[dict] = (),
                 circles: Iterable[tuple[float, float, float]] = ()):
        self.plz = set(plz)
        self.circles = list(circles)                      # (lat, lon, radius_km) – Umkreissuche
        self.free = list(polygons)                        # frei gezeichnete Flächen
        # Für Überschneidungen mit PLZ-Gebieten zählen Kreise als Polygon
        self.polygons = self.free + [circle_polygon(*c) for c in self.circles]
        self.fw_zones = list(fw_zones)

    @property
    def active(self) -> bool:
        return bool(self.plz or self.polygons)

    def _inside_exact(self, lat: float, lon: float) -> bool:
        return any(point_in(lat, lon, g) for g in self.free) or \
            any(haversine_km(lat, lon, c[0], c[1]) <= c[2] for c in self.circles)

    def check(self, data: dict) -> bool | None:
        """True = im Gebiet, False = außerhalb, None = unklar (Lage zu ungenau bekannt)."""
        plz, lat, lon, prec = data.get("plz"), data.get("lat"), data.get("lon"), data.get("geo_precision")
        if plz_selected(plz, self.plz):
            return True
        if not self.polygons:
            return False if plz or lat is not None else None
        if lat is not None and prec in ("portal", "adresse"):
            return self._inside_exact(lat, lon)
        # Nur die PLZ ist bekannt: überschneidet sich das PLZ-Gebiet mit einer gezeichneten Fläche?
        feat = plz_feature(plz)
        if feat:
            return None if any(overlaps(feat["geometry"], g) for g in self.polygons) else False
        return None

    def in_fw_zone(self, data: dict) -> bool:
        lat, lon = data.get("lat"), data.get("lon")
        if lat is None or data.get("geo_precision") not in ("portal", "adresse"):
            return False
        return any(point_in(lat, lon, g) for g in self.fw_zones)
