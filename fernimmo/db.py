"""SQLite-Speicher: Inserate, Änderungsverlauf, Likes, Kommentare, Laufprotokoll."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    address TEXT,
    listing_type TEXT,
    raw_text TEXT,
    structured TEXT,
    data TEXT,
    price REAL,
    rooms REAL,
    area REAL,
    year INTEGER,
    fernwaerme TEXT,
    plz TEXT,
    fingerprint TEXT,
    duplicate_of INTEGER,
    match_level TEXT DEFAULT 'none',
    matched_profiles TEXT DEFAULT '[]',
    match_reasons TEXT DEFAULT '[]',
    status TEXT DEFAULT 'neu',
    image_url TEXT,
    first_seen TEXT,
    last_seen TEXT,
    last_fetched TEXT,
    notified_at TEXT,
    active INTEGER DEFAULT 1,
    UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_listings_fp ON listings(fingerprint);
CREATE INDEX IF NOT EXISTS idx_listings_match ON listings(match_level);
CREATE TABLE IF NOT EXISTS price_history (
    listing_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    price REAL
);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    field TEXT NOT NULL,
    label TEXT NOT NULL,
    old TEXT,
    new TEXT,
    direction TEXT            -- 'down' | 'up' | NULL (für Preise/Zahlen)
);
CREATE INDEX IF NOT EXISTS idx_changes_listing ON changes(listing_id, ts);
CREATE TABLE IF NOT EXISTS likes (
    listing_id INTEGER NOT NULL,
    user TEXT NOT NULL,
    ts TEXT NOT NULL,
    PRIMARY KEY (listing_id, user)
);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL,
    user TEXT NOT NULL,
    ts TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comments_listing ON comments(listing_id, ts);
CREATE TABLE IF NOT EXISTS seen (
    listing_id INTEGER NOT NULL,
    user TEXT NOT NULL,
    ts TEXT NOT NULL,
    PRIMARY KEY (listing_id, user)
);
CREATE TABLE IF NOT EXISTS notes (
    listing_id INTEGER PRIMARY KEY,
    text TEXT,
    link TEXT,
    offer REAL,              -- eigenes Kaufpreisangebot
    updated TEXT,
    updated_by TEXT
);
CREATE TABLE IF NOT EXISTS tags (
    listing_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (listing_id, tag)
);
CREATE TABLE IF NOT EXISTS skipped (
    source TEXT,
    source_id TEXT,
    ts TEXT,
    PRIMARY KEY (source, source_id)
);
CREATE TABLE IF NOT EXISTS poi_cache (
    listing_id INTEGER PRIMARY KEY,
    lat REAL, lon REAL,
    ts TEXT,
    data TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started TEXT,
    finished TEXT,
    source TEXT,
    found INTEGER DEFAULT 0,
    new INTEGER DEFAULT 0,
    matches INTEGER DEFAULT 0,
    error TEXT
);
CREATE TABLE IF NOT EXISTS areas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,          -- 'search' (Suchgebiet) | 'fw' (Fernwärme-Gebiet)
    name TEXT,
    geometry TEXT NOT NULL,      -- GeoJSON Polygon/MultiPolygon
    created TEXT,
    created_by TEXT
);
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    position INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1,
    notify INTEGER DEFAULT 1,
    criteria TEXT NOT NULL,      -- JSON: Kriterien (siehe searches.py)
    plz TEXT DEFAULT '[]',       -- JSON: PLZ bzw. PLZ-Anfänge
    created TEXT,
    created_by TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS geocache (
    query TEXT PRIMARY KEY,
    lat REAL,
    lon REAL,
    ts TEXT
);
"""

# Spalten, die nachträglich dazugekommen sind (Migration bestehender Datenbanken)
MIGRATIONS = {"runs": {"note": "TEXT"},
              "areas": {"center_lat": "REAL", "center_lon": "REAL", "radius_km": "REAL", "search_id": "INTEGER"},
              "listings": {
    "last_fetched": "TEXT", "lat": "REAL", "lon": "REAL", "geo_precision": "TEXT",
    "loose_fp": "TEXT", "relisted_of": "INTEGER",
    "matched_searches": "TEXT DEFAULT ''",   # ",1,3," – IDs der passenden Suchen
}}

# Merkmale, deren Änderung im Verlauf protokolliert wird: Schlüssel -> (Bezeichnung, Einheit)
TRACKED_DATA = {
    "purchase_price": ("Kaufpreis", " €"),
    "rent_cold": ("Kaltmiete", " €"),
    "rent_warm": ("Warmmiete", " €"),
    "hausgeld": ("Hausgeld", " €"),
    "service_charges": ("Nebenkosten", " €"),
    "area": ("Wohnfläche", " m²"),
    "rooms": ("Zimmer", ""),
    "year": ("Baujahr", ""),
    "floor": ("Etage", ""),
    "energy_class": ("Energieklasse", ""),
    "energy_value": ("Energiekennwert", " kWh/(m²·a)"),
    "heating_sources": ("Energieträger", ""),
    "available_from": ("Verfügbar ab", ""),
    "features": ("Ausstattung", ""),
    "flags": ("Hinweise", ""),
}
PRICE_FIELDS = ("purchase_price", "rent_cold")
TRACKED_COLUMNS = {"title": "Titel", "address": "Adresse", "raw_text": "Beschreibung"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(data: dict[str, Any], price: float | None) -> str | None:
    """Grober Fingerabdruck, um dasselbe Objekt auf mehreren Portalen zu erkennen."""
    if not data.get("area") or not price:
        return None
    return f"{data.get('plz') or '?'}|{round(data['area'])}|{round(price / 10) * 10}|{data.get('rooms') or '?'}"


def loose_fingerprint(data: dict[str, Any]) -> str | None:
    """Fingerabdruck ohne Preis – erkennt dieselbe Wohnung nach Löschen und Neueinstellen."""
    if not data.get("area") or not data.get("plz"):
        return None
    return f"{data['plz']}|{round(data['area'])}|{data.get('rooms') or '?'}"


def median(values: list[float]) -> float | None:
    v = sorted(values)
    if not v:
        return None
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


def _fmt(value: Any, unit: str = "") -> str | None:
    if value is None or value == [] or value == "":
        return None
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, float):
        digits = 0 if value.is_integer() else (1 if unit != " €" else 2)
        s = f"{value:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return s + unit
    return f"{value}{unit}"


def diff_listing(old: dict, new_data: dict, new_cols: dict) -> list[dict]:
    """Vergleicht gespeicherten Stand mit neuem Abruf und liefert Änderungen."""
    out = []
    old_data = old.get("data") or {}
    for key, (label, unit) in TRACKED_DATA.items():
        a, b = old_data.get(key), new_data.get(key)
        if isinstance(a, list) or isinstance(b, list):
            if sorted(a or []) == sorted(b or []):
                continue
        elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if abs(a - b) < 0.01:
                continue
        elif a == b or (a in (None, "") and b in (None, "")):
            continue
        direction = None
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            direction = "down" if b < a else "up"
        out.append(dict(field=key, label=label, old=_fmt(a, unit), new=_fmt(b, unit), direction=direction))
    for col, label in TRACKED_COLUMNS.items():
        a, b = (old.get(col) or "").strip(), (new_cols.get(col) or "").strip()
        if a != b and b:
            out.append(dict(field=col, label=label, old=a or None, new=b, direction=None))
    return out


class DB:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            for table, cols in MIGRATIONS.items():
                have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                for col, typ in cols.items():
                    if col not in have:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            self.conn.commit()

    # ------------------------------------------------------------------ util
    def _exec(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def _query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def add_change(self, lid: int, field: str, label: str, old: str | None, new: str | None,
                   direction: str | None = None, ts: str | None = None) -> None:
        self._exec(
            "INSERT INTO changes(listing_id, ts, field, label, old, new, direction) VALUES (?,?,?,?,?,?,?)",
            (lid, ts or now(), field, label, old, new, direction),
        )

    # -------------------------------------------------------------- listings
    def known_ids(self, source: str) -> set[str]:
        return {r["source_id"] for r in self._query("SELECT source_id FROM listings WHERE source=?", (source,))}

    def touch(self, source: str, source_ids: list[str]) -> None:
        """Markiert Inserate als in der Suche gesehen; wieder aufgetauchte werden protokolliert."""
        ts = now()
        for sid in source_ids:
            rows = self._query("SELECT id, active FROM listings WHERE source=? AND source_id=?", (source, sid))
            if not rows:
                continue
            if not rows[0]["active"]:
                self.add_change(rows[0]["id"], "online", "Status", "offline", "wieder online", ts=ts)
            self._exec("UPDATE listings SET last_seen=?, active=1 WHERE id=?", (ts, rows[0]["id"]))

    def due_for_refresh(self, source: str, hours: float, limit: int) -> list[dict]:
        """Aktive, relevante Inserate (Treffer oder geliked), die länger nicht neu geladen wurden."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        rows = self._query(
            """SELECT id, source_id, url, listing_type FROM listings
               WHERE source=? AND active=1 AND status<>'ausgeblendet'
                 AND (match_level<>'none' OR id IN (SELECT listing_id FROM likes))
                 AND COALESCE(last_fetched, first_seen) <= ?
               ORDER BY COALESCE(last_fetched, first_seen) ASC LIMIT ?""",
            (source, cutoff, limit),
        )
        return [dict(r) for r in rows]

    def find_duplicate(self, fp: str | None, source: str) -> int | None:
        if not fp:
            return None
        rows = self._query(
            "SELECT id FROM listings WHERE fingerprint=? AND source<>? AND duplicate_of IS NULL ORDER BY id LIMIT 1",
            (fp, source),
        )
        return rows[0]["id"] if rows else None

    def upsert(self, l: Listing, structured: dict[str, str], reasons: list[str],
               track_changes: bool = True) -> tuple[int, bool, list[dict]]:
        """Speichert ein Inserat. Liefert (id, neu?, Änderungen gegenüber dem letzten Stand)."""
        self._mstats = None   # Marktstatistik neu berechnen
        price = l.price
        fp = fingerprint(l.data, price)
        ts = now()
        existing = self._query("SELECT * FROM listings WHERE source=? AND source_id=?", (l.source, l.source_id))
        vals = dict(
            source=l.source, source_id=l.source_id, url=l.url, title=l.title, address=l.address,
            listing_type=l.listing_type, raw_text=l.raw_text, structured=json.dumps(structured, ensure_ascii=False),
            data=json.dumps(l.data, ensure_ascii=False), price=price, rooms=l.data.get("rooms"),
            area=l.data.get("area"), year=l.data.get("year"), fernwaerme=l.data.get("fernwaerme"),
            plz=l.data.get("plz"), fingerprint=fp, match_level=l.match_level,
            matched_profiles=json.dumps(l.matched_profiles, ensure_ascii=False),
            matched_searches="".join(f",{i}" for i in l.matched_ids) + ("," if l.matched_ids else ""),
            match_reasons=json.dumps(reasons, ensure_ascii=False), image_url=l.image_url, ts=ts,
            lat=l.data.get("lat"), lon=l.data.get("lon"), geo_precision=l.data.get("geo_precision"),
            loose_fp=loose_fingerprint(l.data),
        )
        if existing:
            old = _row(existing[0])
            lid = old["id"]
            changes = diff_listing(old, l.data, {"title": l.title, "address": l.address, "raw_text": l.raw_text}) \
                if track_changes else []
            with self.lock:
                self.conn.execute(
                    """UPDATE listings SET url=:url, title=:title, address=:address, listing_type=:listing_type,
                    raw_text=:raw_text, structured=:structured, data=:data, price=:price, rooms=:rooms, area=:area,
                    year=:year, fernwaerme=:fernwaerme, plz=:plz, fingerprint=:fingerprint, match_level=:match_level,
                    matched_profiles=:matched_profiles, matched_searches=:matched_searches,
                    match_reasons=:match_reasons, image_url=:image_url,
                    lat=:lat, lon=:lon, geo_precision=:geo_precision, loose_fp=:loose_fp,
                    last_seen=:ts, last_fetched=:ts, active=1 WHERE id=:id""",
                    {**vals, "id": lid},
                )
                if not track_changes:
                    # Neuauswertung: last_seen/last_fetched/active nicht verfälschen
                    self.conn.execute("UPDATE listings SET last_seen=?, last_fetched=?, active=? WHERE id=?",
                                      (old["last_seen"], old["last_fetched"], old["active"], lid))
                if not old["active"] and track_changes:
                    changes.insert(0, dict(field="online", label="Status", old="offline", new="wieder online",
                                           direction=None))
                for c in changes:
                    self.conn.execute(
                        "INSERT INTO changes(listing_id, ts, field, label, old, new, direction) VALUES (?,?,?,?,?,?,?)",
                        (lid, ts, c["field"], c["label"], c["old"], c["new"], c["direction"]),
                    )
                if price and old["price"] and abs(price - old["price"]) >= 1:
                    self.conn.execute("INSERT INTO price_history(listing_id, ts, price) VALUES (?,?,?)",
                                      (lid, ts, price))
                self.conn.commit()
            for c in changes:
                c["old_price"] = old["price"]
            return lid, False, changes
        vals["duplicate_of"] = self.find_duplicate(fp, l.source)
        prev = self.find_relisting(vals["loose_fp"], l.listing_type, l.address)
        vals["relisted_of"] = prev["id"] if prev else None
        cur = self._exec(
            """INSERT INTO listings(source, source_id, url, title, address, listing_type, raw_text, structured,
            data, price, rooms, area, year, fernwaerme, plz, fingerprint, duplicate_of, match_level,
            matched_profiles, matched_searches, match_reasons, image_url, lat, lon, geo_precision, loose_fp,
            relisted_of,
            first_seen, last_seen, last_fetched)
            VALUES (:source, :source_id, :url, :title, :address, :listing_type, :raw_text, :structured, :data,
            :price, :rooms, :area, :year, :fernwaerme, :plz, :fingerprint, :duplicate_of, :match_level,
            :matched_profiles, :matched_searches, :match_reasons, :image_url, :lat, :lon, :geo_precision,
            :loose_fp, :relisted_of,
            :ts, :ts, :ts)""",
            vals,
        )
        lid = cur.lastrowid
        unit = " €"
        self.add_change(lid, "created", "Inserat", None,
                        "erstmals gefunden" + (f" – Preis {_fmt(float(price), unit)}" if price else ""), ts=ts)
        if price:
            self._exec("INSERT INTO price_history(listing_id, ts, price) VALUES (?,?,?)", (lid, ts, price))
        if not prev:
            return lid, True, []
        old_p = _fmt(float(prev["price"]), unit) if prev["price"] else "?"
        direction = None
        if prev["price"] and price and abs(price - prev["price"]) >= 1:
            direction = "down" if price < prev["price"] else "up"
        self.add_change(lid, "relisted", "Wiedereinstellung", f"#{prev['id']} ({old_p})",
                        f"neu eingestellt ({_fmt(float(price), unit) if price else '?'})", direction, ts=ts)
        self.add_change(prev["id"], "relisted_as", "Wiedereinstellung", None, f"erneut eingestellt als #{lid}", ts=ts)
        return lid, True, [dict(field="relisted", old_id=prev["id"], old_price=prev["price"], direction=direction)]

    def find_relisting(self, loose_fp: str | None, listing_type: str | None, address: str) -> dict | None:
        """Sucht ein früheres, inzwischen offline genommenes Inserat derselben Wohnung."""
        if not loose_fp:
            return None
        from .geo import has_street
        for r in self._query(
                """SELECT id, price, address FROM listings WHERE loose_fp=? AND active=0
                   AND COALESCE(listing_type,'')=COALESCE(?, '') ORDER BY last_seen DESC LIMIT 5""",
                (loose_fp, listing_type)):
            a, b = (r["address"] or "").lower(), (address or "").lower()
            if has_street(a) and has_street(b) and a.split(",")[0].strip() != b.split(",")[0].strip():
                continue  # andere Straße -> andere Wohnung
            return dict(r)
        return None

    def chain(self, lid: int) -> list[int]:
        """IDs der Vorgänger-Inserate (Wiedereinstellungen), älteste zuerst, ohne lid selbst."""
        out, seen = [], {lid}
        cur = self._query("SELECT relisted_of FROM listings WHERE id=?", (lid,))
        nxt = cur[0]["relisted_of"] if cur else None
        while nxt and nxt not in seen:
            out.append(nxt)
            seen.add(nxt)
            r = self._query("SELECT relisted_of FROM listings WHERE id=?", (nxt,))
            nxt = r[0]["relisted_of"] if r else None
        return list(reversed(out))

    def mark_notified(self, lid: int) -> None:
        self._exec("UPDATE listings SET notified_at=? WHERE id=?", (now(), lid))

    def set_status(self, lid: int, status: str) -> None:
        self._exec("UPDATE listings SET status=? WHERE id=?", (status, lid))

    def mark_offline(self, lid: int, reason: str = "Inserat nicht mehr abrufbar") -> None:
        rows = self._query("SELECT active FROM listings WHERE id=?", (lid,))
        if rows and rows[0]["active"]:
            self._exec("UPDATE listings SET active=0, last_fetched=? WHERE id=?", (now(), lid))
            self.add_change(lid, "offline", "Status", "online", reason)

    # Vorab (anhand der Ergebniskachel) aussortierte Inserate – damit bundesweite Feeds
    # wissen, wo sie aufhören können. Ändert sich eine Suche, wird die Liste geleert.
    def skipped_ids(self, source: str) -> set[str]:
        return {r["source_id"] for r in self._query("SELECT source_id FROM skipped WHERE source=?", (source,))}

    def add_skipped(self, source: str, source_ids: list[str]) -> None:
        if source_ids:
            with self.lock:
                self.conn.executemany("INSERT OR IGNORE INTO skipped(source, source_id, ts) VALUES (?,?,?)",
                                      [(source, i, now()) for i in source_ids])
                self.conn.commit()

    def clear_skipped(self, older_than: str | None = None) -> None:
        if older_than:
            self._exec("DELETE FROM skipped WHERE ts<?", (older_than,))
        else:
            self._exec("DELETE FROM skipped")

    def mark_inactive(self, source: str, older_than: str) -> None:
        for r in self._query("SELECT id FROM listings WHERE source=? AND active=1 AND last_seen<?",
                             (source, older_than)):
            self.mark_offline(r["id"], "seit Tagen nicht mehr in der Suche")

    def get(self, lid: int, user: str | None = None) -> dict | None:
        rows = self._query("SELECT * FROM listings WHERE id=?", (lid,))
        if not rows:
            return None
        d = _row(rows[0])
        self._attach_social([d], user)
        return d

    def price_history(self, lid: int) -> list[dict]:
        return [dict(r) for r in self._query("SELECT ts, price FROM price_history WHERE listing_id=? ORDER BY ts", (lid,))]

    def changes(self, lid: int, with_predecessors: bool = True) -> list[dict]:
        ids = ([*self.chain(lid), lid]) if with_predecessors else [lid]
        ph = ",".join("?" * len(ids))
        return [dict(r) for r in self._query(
            f"SELECT * FROM changes WHERE listing_id IN ({ph}) ORDER BY ts DESC, id DESC", ids)]

    def search(self, *, match: str | None = None, fernwaerme: str | None = None, source: str | None = None,
               status: str | None = None, listing_type: str | None = None, active_only: bool = True,
               hide_duplicates: bool = True, q: str | None = None, sort: str = "first_seen",
               liked: bool = False, user: str | None = None, limit: int = 300,
               rooms_min: float | None = None, rooms_max: float | None = None,
               price_max: float | None = None, search_id: int | None = None) -> list[dict]:
        where, params = [], []
        if q and q.strip().lstrip("#").isdigit():
            # Suche nach FernImmo-ID: alle übrigen Filter ignorieren
            where.append("id=?"); params.append(int(q.strip().lstrip("#")))
        else:
            if match == "treffer":
                where.append("match_level IN ('voll','teilweise')")
            elif match:
                where.append("match_level=?"); params.append(match)
            if fernwaerme:
                where.append("fernwaerme=?"); params.append(fernwaerme)
            if source:
                where.append("source=?"); params.append(source)
            if status:
                where.append("status=?"); params.append(status)
            else:
                where.append("status<>'ausgeblendet'")
            if listing_type:
                where.append("listing_type=?"); params.append(listing_type)
            if active_only:
                where.append("active=1")
            if hide_duplicates:
                where.append("duplicate_of IS NULL")
            if liked:
                where.append("id IN (SELECT listing_id FROM likes)")
            if search_id is not None:
                where.append("matched_searches LIKE ?"); params.append(f"%,{int(search_id)},%")
            if rooms_min is not None:
                where.append("rooms >= ?"); params.append(rooms_min)
            if rooms_max is not None:
                where.append("rooms <= ?"); params.append(rooms_max)
            if price_max is not None:
                where.append("price <= ?"); params.append(price_max)
            if q:
                where.append("(title LIKE ? OR raw_text LIKE ? OR address LIKE ?)"); params += [f"%{q}%"] * 3
        order = {
            "first_seen": "first_seen DESC", "price": "price IS NULL, price ASC",
            "online": "first_seen ASC",
            "area": "area IS NULL, area DESC", "ppsqm": "(price/area) IS NULL, price/area ASC",
            "likes": "(SELECT COUNT(*) FROM likes WHERE listing_id=listings.id) DESC, first_seen DESC",
            "changed": "(SELECT MAX(ts) FROM changes WHERE listing_id=listings.id AND field<>'created') IS NULL, "
                       "(SELECT MAX(ts) FROM changes WHERE listing_id=listings.id AND field<>'created') DESC",
        }.get(sort, "first_seen DESC")
        sql = "SELECT * FROM listings" + (" WHERE " + " AND ".join(where) if where else "")
        sql += f" ORDER BY {order}, id DESC LIMIT ?"   # id: stabile Reihenfolge bei Gleichstand
        params.append(limit)
        rows = [_row(r) for r in self._query(sql, tuple(params))]
        self._attach_social(rows, user)
        return rows

    def _attach_social(self, rows: list[dict], user: str | None) -> None:
        """Hängt Likes, Kommentare und letzte Änderung an die Zeilen an."""
        if not rows:
            return
        ids = [r["id"] for r in rows]
        ph = ",".join("?" * len(ids))
        likes: dict[int, list[str]] = {}
        for r in self._query(f"SELECT listing_id, user FROM likes WHERE listing_id IN ({ph}) ORDER BY ts", ids):
            likes.setdefault(r["listing_id"], []).append(r["user"])
        comments: dict[int, list[dict]] = {}
        for r in self._query(f"SELECT * FROM comments WHERE listing_id IN ({ph}) ORDER BY ts, id", ids):
            comments.setdefault(r["listing_id"], []).append(dict(r))
        changes: dict[int, dict] = {}
        counts: dict[int, int] = {}
        for r in self._query(
                f"""SELECT * FROM changes WHERE listing_id IN ({ph}) AND field<>'created'
                    ORDER BY ts DESC, id DESC""", ids):
            # Hinweis auf der Übersicht: letzte Preisänderung hat Vorrang, sonst letzte Änderung
            cur = changes.get(r["listing_id"])
            is_price = r["field"] in PRICE_FIELDS
            if cur is None or (is_price and cur["field"] not in PRICE_FIELDS):
                changes[r["listing_id"]] = dict(r)
            counts[r["listing_id"]] = counts.get(r["listing_id"], 0) + 1
        stats = self.market_stats()
        ts_now = datetime.now(timezone.utc)
        seen = {r["listing_id"] for r in self._query(
            f"SELECT listing_id FROM seen WHERE user=? AND listing_id IN ({ph})", [user or "", *ids])}
        # Dieselbe Wohnung auf weiteren Portalen (Dubletten) als zusätzliche Quellen
        dups: dict[int, list[dict]] = {}
        for d in self._query(f"""SELECT id, source, url, active, duplicate_of FROM listings
                                 WHERE duplicate_of IN ({ph}) ORDER BY id""", ids):
            dups.setdefault(d["duplicate_of"], []).append(dict(d))
        notes = {r["listing_id"]: dict(r) for r in self._query(
            f"SELECT * FROM notes WHERE listing_id IN ({ph})", ids)}
        tags: dict[int, list[str]] = {}
        for t in self._query(f"SELECT listing_id, tag FROM tags WHERE listing_id IN ({ph}) ORDER BY tag", ids):
            tags.setdefault(t["listing_id"], []).append(t["tag"])
        for r in rows:
            r["seen"] = r["id"] in seen
            r["sources"] = [{"id": r["id"], "source": r["source"], "url": r["url"], "active": r["active"]}] + \
                [{"id": d["id"], "source": d["source"], "url": d["url"], "active": d["active"]} for d in dups.get(r["id"], [])]
            r["note"] = notes.get(r["id"])
            r["tags"] = tags.get(r["id"], [])
            chain = self.chain(r["id"]) if r.get("relisted_of") else []
            first = self._query("SELECT first_seen FROM listings WHERE id=?", (chain[0],))[0]["first_seen"] \
                if chain else r["first_seen"]
            end = ts_now if r["active"] else datetime.fromisoformat(r["last_seen"])
            r["days_online"] = max(0, (end - datetime.fromisoformat(first)).days)
            r["predecessors"] = chain
            hist = [h["price"] for h in self._query(
                f"SELECT price FROM price_history WHERE listing_id IN ({','.join('?' * (len(chain) + 1))}) "
                "ORDER BY ts", [*chain, r["id"]]) if h["price"]]
            r["price_drops"] = sum(1 for a, b in zip(hist, hist[1:]) if b < a)
            r["price_raises"] = sum(1 for a, b in zip(hist, hist[1:]) if b > a)
            r["first_price"] = hist[0] if hist else None
            r["price_delta_pct"] = round((r["price"] / hist[0] - 1) * 100, 1) \
                if hist and r.get("price") and abs(r["price"] - hist[0]) >= 1 else None
            r["market"] = market_compare(stats, r)
            r["likes"] = likes.get(r["id"], [])
            r["liked"] = user in r["likes"]
            r["comments"] = comments.get(r["id"], [])
            r["last_change"] = changes.get(r["id"])
            r["change_count"] = counts.get(r["id"], 0)

    def market_stats(self, days: int = 365) -> dict:
        """Wie _market_stats, aber höchstens alle 2 Minuten neu berechnet (Übersicht ruft oft ab)."""
        cached = getattr(self, "_mstats", None)
        if cached and time.monotonic() - cached[0] < 120 and cached[1] == days:
            return cached[2]
        stats = self._market_stats(days)
        self._mstats = (time.monotonic(), days, stats)
        return stats

    def _market_stats(self, days: int = 365) -> dict:
        """Median-€/m² je PLZ und gesamt, getrennt nach Kauf/Miete (letzte `days` Tage,
        ohne Dubletten und ohne durch Wiedereinstellung ersetzte Inserate)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        rows = self._query(
            """SELECT listing_type, plz, price, area FROM listings
               WHERE duplicate_of IS NULL AND price > 0 AND area >= 15 AND first_seen >= ?
                 AND id NOT IN (SELECT relisted_of FROM listings WHERE relisted_of IS NOT NULL)""", (cutoff,))
        by: dict[tuple[str, str], list[float]] = {}
        for r in rows:
            ppsqm = r["price"] / r["area"]
            if not 3 < ppsqm < 25000:
                continue
            lt = r["listing_type"] or "?"
            by.setdefault((lt, r["plz"] or "?"), []).append(ppsqm)
            by.setdefault((lt, "*"), []).append(ppsqm)
        return {k: (median(v), len(v)) for k, v in by.items()}

    def _market_rows(self, listing_type: str, days: int):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        for r in self._query(
                """SELECT plz, price, area, first_seen FROM listings
                   WHERE listing_type=? AND duplicate_of IS NULL AND price > 0 AND area >= 15 AND first_seen >= ?
                     AND id NOT IN (SELECT relisted_of FROM listings WHERE relisted_of IS NOT NULL)""",
                (listing_type, cutoff)):
            ppsqm = r["price"] / r["area"]
            if 3 < ppsqm < 25000:
                yield r["plz"], ppsqm, r["first_seen"][:7]

    def rent_estimate(self, plz: str | None) -> tuple[float, str] | None:
        """Vergleichsmiete €/m² aus gesammelten Mietinseraten: PLZ (ab 3), sonst gesamt (ab 5)."""
        stats = self.market_stats()
        ref = stats.get(("miete", plz or "?"))
        if ref and ref[1] >= 3:
            return round(ref[0], 2), f"Median aus {ref[1]} Mietinseraten in {plz}"
        ref = stats.get(("miete", "*"))
        if ref and ref[1] >= 5:
            return round(ref[0], 2), f"Median aus {ref[1]} Mietinseraten (alle PLZ)"
        return None

    def market_table(self, days: int = 365, plz_prefix: str = "") -> list[dict]:
        """Je PLZ: Median-€/m² Kauf und Miete, Anzahl, grobe Bruttorendite (optional nur ein PLZ-Anfang)."""
        acc: dict[str, dict] = {}
        for lt in ("kauf", "miete"):
            for plz, v, _ in self._market_rows(lt, days):
                if plz_prefix and not (plz or "").startswith(plz_prefix):
                    continue
                acc.setdefault(plz or "?", {"kauf": [], "miete": []})[lt].append(v)
        out = []
        for plz, d in acc.items():
            k, m = median(d["kauf"]), median(d["miete"])
            out.append({"plz": plz, "kauf": round(k) if k else None, "n_kauf": len(d["kauf"]),
                        "miete": round(m, 2) if m else None, "n_miete": len(d["miete"]),
                        "rendite": round(m * 12 / k * 100, 2) if k and m else None})
        return sorted(out, key=lambda r: (-r["n_kauf"] - r["n_miete"], r["plz"]))

    def price_trend(self, listing_type: str = "kauf", plz: str | None = None, days: int = 730) -> list[dict]:
        """Median-€/m² je Monat (nach Erstfund), optional nur eine PLZ bzw. ein PLZ-Anfang."""
        by: dict[str, list[float]] = {}
        for p, v, month in self._market_rows(listing_type, days):
            if plz and not (p or "").startswith(plz):
                continue
            by.setdefault(month, []).append(v)
        return [{"month": m, "median": round(median(v)), "n": len(v)} for m, v in sorted(by.items())]

    def all_for_reprocess(self) -> list[dict]:
        return [_row(r) for r in self._query("SELECT * FROM listings")]

    def stats(self) -> dict:
        r = self._query(
            """SELECT COUNT(*) total, SUM(match_level='voll') voll, SUM(match_level='teilweise') teilweise,
            SUM(fernwaerme='ja') fw,
            (SELECT COUNT(DISTINCT listing_id) FROM likes) liked
            FROM listings WHERE active=1 AND duplicate_of IS NULL"""
        )[0]
        return dict(r)

    # ------------------------------------------------------- gebiete & co.
    def geocache_get(self, query: str) -> tuple[float, float] | tuple[()] | None:
        """None = unbekannt, () = früher erfolglos versucht, sonst (lat, lon)."""
        r = self._query("SELECT lat, lon FROM geocache WHERE query=?", (query,))
        if not r:
            return None
        return (r[0]["lat"], r[0]["lon"]) if r[0]["lat"] is not None else ()

    def geocache_put(self, query: str, point: tuple[float, float] | None) -> None:
        self._exec("INSERT OR REPLACE INTO geocache(query, lat, lon, ts) VALUES (?,?,?,?)",
                   (query, point[0] if point else None, point[1] if point else None, now()))

    def areas(self, kind: str | None = None, search_id: int | None = None) -> list[dict]:
        where, params = [], []
        if kind:
            where.append("kind=?"); params.append(kind)
        if search_id is not None:
            where.append("search_id=?"); params.append(search_id)
        sql = "SELECT * FROM areas" + (" WHERE " + " AND ".join(where) if where else "")
        out = []
        for r in self._query(sql + " ORDER BY id", tuple(params)):
            d = dict(r)
            d["geometry"] = json.loads(d["geometry"])
            out.append(d)
        return out

    def add_area(self, kind: str, name: str, geometry: dict, user: str,
                 center: tuple[float, float] | None = None, radius_km: float | None = None,
                 search_id: int | None = None) -> int:
        self.clear_skipped()
        return self._exec(
            """INSERT INTO areas(kind, name, geometry, created, created_by, center_lat, center_lon, radius_km,
               search_id) VALUES (?,?,?,?,?,?,?,?,?)""",
            (kind, name[:100], json.dumps(geometry), now(), user,
             center[0] if center else None, center[1] if center else None, radius_km,
             search_id if kind == "search" else None)).lastrowid

    def get_area(self, aid: int) -> dict | None:
        r = self._query("SELECT * FROM areas WHERE id=?", (aid,))
        return dict(r[0]) if r else None

    def assign_orphan_areas(self, search_id: int) -> None:
        self._exec("UPDATE areas SET search_id=? WHERE kind='search' AND search_id IS NULL", (search_id,))

    # ------------------------------------------------------------------ suchen
    def searches(self) -> list[dict]:
        out = []
        for r in self._query("SELECT * FROM searches ORDER BY position, id"):
            d = dict(r)
            d["criteria"] = json.loads(d["criteria"])
            d["plz"] = json.loads(d["plz"] or "[]")
            out.append(d)
        return out

    def get_search(self, sid: int) -> dict | None:
        return next((s for s in self.searches() if s["id"] == sid), None)

    def add_search(self, name: str, criteria: dict, plz: list[str], user: str) -> int:
        self.clear_skipped()
        pos = (self._query("SELECT COALESCE(MAX(position), 0) + 1 p FROM searches")[0]["p"])
        return self._exec(
            "INSERT INTO searches(name, position, criteria, plz, created, created_by) VALUES (?,?,?,?,?,?)",
            (name[:80], pos, json.dumps(criteria), json.dumps(plz), now(), user)).lastrowid

    def update_search(self, sid: int, **fields: Any) -> None:
        cols = {k: (json.dumps(v) if k in ("criteria", "plz") else v) for k, v in fields.items()
                if k in ("name", "active", "notify", "criteria", "plz", "position")}
        if cols:
            if set(cols) & {"active", "criteria", "plz"}:
                self.clear_skipped()
            self._exec(f"UPDATE searches SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?",
                       (*cols.values(), sid))

    def delete_search(self, sid: int) -> bool:
        with self.lock:
            n = self.conn.execute("DELETE FROM searches WHERE id=?", (sid,)).rowcount
            self.conn.execute("DELETE FROM areas WHERE search_id=?", (sid,))
            self.conn.commit()
        return n > 0

    def copy_search_areas(self, from_id: int, to_id: int) -> None:
        self._exec("""INSERT INTO areas(kind, name, geometry, created, created_by, center_lat, center_lon,
                      radius_km, search_id) SELECT kind, name, geometry, ?, created_by, center_lat, center_lon,
                      radius_km, ? FROM areas WHERE search_id=?""", (now(), to_id, from_id))

    def rename_area(self, aid: int, name: str) -> None:
        self._exec("UPDATE areas SET name=? WHERE id=?", (name[:100], aid))

    def delete_area(self, aid: int) -> bool:
        self.clear_skipped()
        return self._exec("DELETE FROM areas WHERE id=?", (aid,)).rowcount > 0

    def get_setting(self, key: str, default: Any = None) -> Any:
        r = self._query("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(r[0]["value"]) if r else default

    def set_setting(self, key: str, value: Any) -> None:
        self._exec("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", (key, json.dumps(value)))

    def map_points(self, rows: list[dict]) -> list[dict]:
        return [dict(id=r["id"], lat=r["lat"], lon=r["lon"], precision=r["geo_precision"], title=r["title"],
                     price=r["price"], area=r["area"], rooms=r["rooms"], match=r["match_level"],
                     fw=r["fernwaerme"], likes=len(r.get("likes", [])), url=r["url"],
                     market=(r.get("market") or {}).get("dev"), active=r["active"])
                for r in rows if r.get("lat") is not None]

    # ------------------------------------------------ gesehen, notizen, tags
    def mark_seen(self, lid: int, user: str) -> None:
        self._exec("INSERT OR IGNORE INTO seen(listing_id, user, ts) VALUES (?,?,?)", (lid, user, now()))

    def mark_all_seen(self, ids: list[int], user: str) -> None:
        ts = now()
        with self.lock:
            self.conn.executemany("INSERT OR IGNORE INTO seen(listing_id, user, ts) VALUES (?,?,?)",
                                  [(i, user, ts) for i in ids])
            self.conn.commit()

    def unseen_count(self, user: str, search_id: int | None = None) -> int:
        sql = """SELECT COUNT(*) n FROM listings WHERE active=1 AND duplicate_of IS NULL AND status<>'ausgeblendet'
                 AND match_level<>'none' AND id NOT IN (SELECT listing_id FROM seen WHERE user=?)"""
        params: list = [user]
        if search_id is not None:
            sql += " AND matched_searches LIKE ?"
            params.append(f"%,{int(search_id)},%")
        return self._query(sql, tuple(params))[0]["n"]

    def count_my_objects(self, board: list[str]) -> int:
        ph = ",".join("?" * len(board))
        return self._query(f"""SELECT COUNT(*) n FROM listings WHERE status IN ({ph})
                               OR id IN (SELECT listing_id FROM likes)""", tuple(board))[0]["n"]

    def save_note(self, lid: int, text: str, link: str, offer: float | None, user: str) -> None:
        self._exec("""INSERT INTO notes(listing_id, text, link, offer, updated, updated_by) VALUES (?,?,?,?,?,?)
                      ON CONFLICT(listing_id) DO UPDATE SET text=excluded.text, link=excluded.link,
                      offer=excluded.offer, updated=excluded.updated, updated_by=excluded.updated_by""",
                   (lid, text[:5000], link[:500], offer, now(), user))

    def add_tag(self, lid: int, tag: str) -> None:
        self._exec("INSERT OR IGNORE INTO tags(listing_id, tag) VALUES (?,?)", (lid, tag.strip()[:40]))

    def remove_tag(self, lid: int, tag: str) -> None:
        self._exec("DELETE FROM tags WHERE listing_id=? AND tag=?", (lid, tag))

    def all_tags(self) -> list[str]:
        return [r["tag"] for r in self._query("SELECT DISTINCT tag FROM tags ORDER BY tag")]

    def poi_get(self, lid: int) -> dict | None:
        r = self._query("SELECT * FROM poi_cache WHERE listing_id=?", (lid,))
        return dict(r[0]) if r else None

    def poi_put(self, lid: int, lat: float, lon: float, data: dict) -> None:
        self._exec("INSERT OR REPLACE INTO poi_cache(listing_id, lat, lon, ts, data) VALUES (?,?,?,?,?)",
                   (lid, lat, lon, now(), json.dumps(data, ensure_ascii=False)))

    # --------------------------------------------------- likes & kommentare
    def toggle_like(self, lid: int, user: str) -> bool:
        """Schaltet das Like des Nutzers um; liefert den neuen Zustand."""
        with self.lock:
            cur = self.conn.execute("DELETE FROM likes WHERE listing_id=? AND user=?", (lid, user))
            liked = cur.rowcount == 0
            if liked:
                self.conn.execute("INSERT INTO likes(listing_id, user, ts) VALUES (?,?,?)", (lid, user, now()))
            self.conn.commit()
        return liked

    def add_comment(self, lid: int, user: str, text: str) -> int:
        return self._exec("INSERT INTO comments(listing_id, user, ts, text) VALUES (?,?,?,?)",
                          (lid, user, now(), text.strip()[:4000])).lastrowid

    def delete_comment(self, cid: int, user: str) -> bool:
        return self._exec("DELETE FROM comments WHERE id=? AND user=?", (cid, user)).rowcount > 0

    # ------------------------------------------------------------------ runs
    def log_run(self, source: str, started: str, found: int, new: int, matches: int, error: str | None,
                note: str | None = None) -> None:
        self._exec(
            "INSERT INTO runs(started, finished, source, found, new, matches, error, note) VALUES (?,?,?,?,?,?,?,?)",
            (started, now(), source, found, new, matches, error, note),
        )

    def last_runs(self, source: str, n: int) -> list[dict]:
        return [dict(r) for r in self._query("SELECT * FROM runs WHERE source=? ORDER BY id DESC LIMIT ?",
                                             (source, n))]

    def backup(self, target: str | Path) -> None:
        """Konsistente Kopie der laufenden Datenbank (SQLite-Backup, auch während Schreibzugriffen)."""
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(str(target))
        try:
            with self.lock:
                self.conn.backup(dst)
        finally:
            dst.close()

    def recent_runs(self, limit: int = 30) -> list[dict]:
        return [dict(r) for r in self._query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]


def market_compare(stats: dict, r: dict) -> dict | None:
    """Abweichung des €/m² vom Median der PLZ (mind. 3 Vergleiche), sonst vom Gesamtmedian (mind. 5)."""
    if not r.get("price") or not r.get("area"):
        return None
    ppsqm = r["price"] / r["area"]
    lt = r.get("listing_type") or "?"
    ref = stats.get((lt, r.get("plz") or "?"))
    label = f"PLZ {r.get('plz')}"
    if not ref or ref[1] < 3:
        ref, label = stats.get((lt, "*")), "alle Inserate"
        if not ref or ref[1] < 5:
            return None
    return {"dev": round((ppsqm / ref[0] - 1) * 100), "median": round(ref[0]), "n": ref[1], "label": label}


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in ("data", "structured", "matched_profiles", "match_reasons"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (TypeError, ValueError):
                pass
    return d
