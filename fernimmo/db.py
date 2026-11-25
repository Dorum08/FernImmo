"""SQLite-Speicher für Inserate, Preisverlauf und Laufprotokoll."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
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
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(data: dict[str, Any], price: float | None) -> str | None:
    """Grober Fingerabdruck, um dasselbe Objekt auf mehreren Portalen zu erkennen."""
    if not data.get("area") or not price:
        return None
    return f"{data.get('plz') or '?'}|{round(data['area'])}|{round(price / 10) * 10}|{data.get('rooms') or '?'}"


class DB:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)
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

    # -------------------------------------------------------------- listings
    def known_ids(self, source: str) -> set[str]:
        return {r["source_id"] for r in self._query("SELECT source_id FROM listings WHERE source=?", (source,))}

    def touch(self, source: str, source_ids: list[str]) -> None:
        ts = now()
        with self.lock:
            self.conn.executemany(
                "UPDATE listings SET last_seen=?, active=1 WHERE source=? AND source_id=?",
                [(ts, source, sid) for sid in source_ids],
            )
            self.conn.commit()

    def find_duplicate(self, fp: str | None, source: str) -> int | None:
        if not fp:
            return None
        rows = self._query(
            "SELECT id FROM listings WHERE fingerprint=? AND source<>? AND duplicate_of IS NULL ORDER BY id LIMIT 1",
            (fp, source),
        )
        return rows[0]["id"] if rows else None

    def upsert(self, l: Listing, structured: dict[str, str], reasons: list[str]) -> tuple[int, bool, float | None]:
        """Speichert ein Inserat. Liefert (id, neu?, alter_preis)."""
        price = l.price
        fp = fingerprint(l.data, price)
        ts = now()
        existing = self._query("SELECT id, price FROM listings WHERE source=? AND source_id=?", (l.source, l.source_id))
        vals = dict(
            source=l.source, source_id=l.source_id, url=l.url, title=l.title, address=l.address,
            listing_type=l.listing_type, raw_text=l.raw_text, structured=json.dumps(structured, ensure_ascii=False),
            data=json.dumps(l.data, ensure_ascii=False), price=price, rooms=l.data.get("rooms"),
            area=l.data.get("area"), year=l.data.get("year"), fernwaerme=l.data.get("fernwaerme"),
            plz=l.data.get("plz"), fingerprint=fp, match_level=l.match_level,
            matched_profiles=json.dumps(l.matched_profiles, ensure_ascii=False),
            match_reasons=json.dumps(reasons, ensure_ascii=False), image_url=l.image_url, ts=ts,
        )
        if existing:
            lid, old_price = existing[0]["id"], existing[0]["price"]
            self._exec(
                """UPDATE listings SET url=:url, title=:title, address=:address, listing_type=:listing_type,
                raw_text=:raw_text, structured=:structured, data=:data, price=:price, rooms=:rooms, area=:area,
                year=:year, fernwaerme=:fernwaerme, plz=:plz, fingerprint=:fingerprint, match_level=:match_level,
                matched_profiles=:matched_profiles, match_reasons=:match_reasons, image_url=:image_url,
                last_seen=:ts, active=1 WHERE id=:id""",
                {**vals, "id": lid},
            )
            if price and old_price and abs(price - old_price) >= 1:
                self._exec("INSERT INTO price_history(listing_id, ts, price) VALUES (?,?,?)", (lid, ts, price))
                return lid, False, old_price
            return lid, False, None
        vals["duplicate_of"] = self.find_duplicate(fp, l.source)
        cur = self._exec(
            """INSERT INTO listings(source, source_id, url, title, address, listing_type, raw_text, structured,
            data, price, rooms, area, year, fernwaerme, plz, fingerprint, duplicate_of, match_level,
            matched_profiles, match_reasons, image_url, first_seen, last_seen)
            VALUES (:source, :source_id, :url, :title, :address, :listing_type, :raw_text, :structured, :data,
            :price, :rooms, :area, :year, :fernwaerme, :plz, :fingerprint, :duplicate_of, :match_level,
            :matched_profiles, :match_reasons, :image_url, :ts, :ts)""",
            vals,
        )
        lid = cur.lastrowid
        if price:
            self._exec("INSERT INTO price_history(listing_id, ts, price) VALUES (?,?,?)", (lid, ts, price))
        return lid, True, None

    def mark_notified(self, lid: int) -> None:
        self._exec("UPDATE listings SET notified_at=? WHERE id=?", (now(), lid))

    def set_status(self, lid: int, status: str) -> None:
        self._exec("UPDATE listings SET status=? WHERE id=?", (status, lid))

    def mark_inactive(self, source: str, older_than: str) -> None:
        self._exec("UPDATE listings SET active=0 WHERE source=? AND last_seen<?", (source, older_than))

    def get(self, lid: int) -> dict | None:
        rows = self._query("SELECT * FROM listings WHERE id=?", (lid,))
        return _row(rows[0]) if rows else None

    def price_history(self, lid: int) -> list[dict]:
        return [dict(r) for r in self._query("SELECT ts, price FROM price_history WHERE listing_id=? ORDER BY ts", (lid,))]

    def search(self, *, match: str | None = None, fernwaerme: str | None = None, source: str | None = None,
               status: str | None = None, listing_type: str | None = None, active_only: bool = True,
               hide_duplicates: bool = True, q: str | None = None, sort: str = "first_seen",
               limit: int = 300) -> list[dict]:
        where, params = [], []
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
        if q:
            where.append("(title LIKE ? OR raw_text LIKE ? OR address LIKE ?)"); params += [f"%{q}%"] * 3
        order = {
            "first_seen": "first_seen DESC", "price": "price IS NULL, price ASC",
            "area": "area IS NULL, area DESC", "ppsqm": "(price/area) IS NULL, price/area ASC",
        }.get(sort, "first_seen DESC")
        sql = "SELECT * FROM listings" + (" WHERE " + " AND ".join(where) if where else "")
        sql += f" ORDER BY {order} LIMIT ?"
        params.append(limit)
        return [_row(r) for r in self._query(sql, tuple(params))]

    def all_for_reprocess(self) -> list[dict]:
        return [_row(r) for r in self._query("SELECT * FROM listings")]

    def stats(self) -> dict:
        r = self._query(
            """SELECT COUNT(*) total, SUM(match_level='voll') voll, SUM(match_level='teilweise') teilweise,
            SUM(fernwaerme='ja') fw FROM listings WHERE active=1 AND duplicate_of IS NULL"""
        )[0]
        return dict(r)

    # ------------------------------------------------------------------ runs
    def log_run(self, source: str, started: str, found: int, new: int, matches: int, error: str | None) -> None:
        self._exec(
            "INSERT INTO runs(started, finished, source, found, new, matches, error) VALUES (?,?,?,?,?,?,?)",
            (started, now(), source, found, new, matches, error),
        )

    def recent_runs(self, limit: int = 30) -> list[dict]:
        return [dict(r) for r in self._query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in ("data", "structured", "matched_profiles", "match_reasons"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (TypeError, ValueError):
                pass
    return d
