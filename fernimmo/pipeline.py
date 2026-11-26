"""Ein Suchlauf: Quellen abfragen, neue Inserate auswerten, speichern, melden."""
from __future__ import annotations

import copy
import logging
import re
import random
import threading
import time
from datetime import datetime, timedelta, timezone

from .config import Config
from .db import DB, now
from .extract import extract, normalize_rooms
from .geo import AreaFilter, Geocoder, plz_for_point, plz_state
from .fetch import BlockedError, NotFoundError, make_fetcher
from .matcher import best_match
from .models import Listing, RawListing
from .calc import costs
from .notify import Notifier, format_message


def _calc(row: dict, cfg: Config) -> dict | None:
    if row.get("listing_type") != "kauf":
        return None
    d = row["data"]
    return costs(row.get("price"), d.get("area"), d.get("hausgeld"), d.get("flags"), cfg.calculator,
                 state=plz_state(row.get("plz")))
from .sources import build_source
from .sources.base import SearchHit

log = logging.getLogger(__name__)


def load_area(db: DB) -> AreaFilter:
    """Suchgebiet und Fernwärme-Gebiete aus der Weboberfläche."""
    return AreaFilter(
        plz=db.get_setting("search_plz", []),
        polygons=[a["geometry"] for a in db.areas("search") if a.get("radius_km") is None],
        circles=[(a["center_lat"], a["center_lon"], a["radius_km"]) for a in db.areas("search")
                 if a.get("radius_km") is not None],
        fw_zones=[a["geometry"] for a in db.areas("fw")],
    )


CRITERIA_KEYS = ("rooms_min", "rooms_max", "price_max", "area_min")


def effective_profiles(cfg: Config, db: DB) -> list:
    """Profile aus config.yaml, überschrieben mit den Suchkriterien aus der Weboberfläche."""
    crit = db.get_setting("criteria")
    if not crit:
        return cfg.profiles
    out = []
    for p in cfg.profiles:
        q = copy.copy(p)
        for k in CRITERIA_KEYS:
            if k in crit:
                setattr(q, k, crit[k])
        out.append(q)
    return out


def prefiltered(hit: SearchHit, cfg: Config, area: AreaFilter, profiles: list) -> bool:
    """True, wenn schon der Kurztext aus der Ergebnisliste klar gegen alle Profile spricht
    (z. B. falsche PLZ bei bundesweiter Suche, 4 Zimmer bei Suche nach 1–2). Spart Abrufe."""
    if not hit.snippet or not profiles:
        return False
    data = extract(hit.snippet)
    # Nur eindeutige Angaben verwenden: nennt die Kachel mehrere Zimmerzahlen, Flächen oder
    # Preise (z. B. im Beschreibungsanriss), lieber die Detailseite laden als falsch aussortieren.
    snip = normalize_rooms(hit.snippet)
    if len(set(re.findall(r"(\d{1,2}(?:[.,]5)?)\s*-?\s*(?:zimmer|zi\b|zi\.)", snip, re.I))) > 1:
        data["rooms"] = None
    if len(set(re.findall(r"(\d{2,3}(?:[.,]\d+)?)\s*(?:m²|m2|qm)", snip, re.I))) > 1:
        data["area"] = None
    if len(set(re.findall(r"(\d{1,3}(?:\.\d{3})+|\d{5,7})(?:,\d+)?\s*(?:€|eur)", snip, re.I))) > 1:
        data["purchase_price"] = data["price_generic"] = data["rent_cold"] = None
    level, _, _ = best_match(profiles, hit.listing_type, data, hit.snippet, area)
    return level == "none"


def evaluate(raw: RawListing, cfg: Config, geocoder: Geocoder | None = None,
             area: AreaFilter | None = None, profiles: list | None = None) -> tuple[Listing, list[str]]:
    data = extract(raw.text, raw.title, raw.structured)
    # Generischer "Preis" (z.B. Kleinanzeigen) = Kaltmiete bzw. Kaufpreis
    if data.get("price_generic"):
        if raw.listing_type == "miete" and not data.get("rent_cold"):
            data["rent_cold"] = data["price_generic"]
        elif raw.listing_type == "kauf" and not data.get("purchase_price"):
            data["purchase_price"] = data["price_generic"]
    # Lage bestimmen: Koordinaten vom Portal > Adresse geocodiert > Mittelpunkt der PLZ
    if raw.geo_precision:
        lat, lon, prec = raw.lat, raw.lon, raw.geo_precision
    elif geocoder:
        lat, lon, prec = geocoder.locate(raw.address, data.get("plz"),
                                         (raw.lat, raw.lon) if raw.lat is not None else None)
    else:
        lat, lon, prec = None, None, None
    data.update(lat=lat, lon=lon, geo_precision=prec)
    if not data.get("plz") and lat is not None and prec in ("portal", "adresse"):
        data["plz"] = plz_for_point(lat, lon)
    data["fw_zone"] = bool(area and area.in_fw_zone(data))
    full_text = f"{raw.title}\n{raw.text}"
    level, names, reasons = best_match(profiles if profiles is not None else cfg.profiles,
                                       raw.listing_type, data, full_text, area)
    listing = Listing(
        id=None, source=raw.source, source_id=raw.source_id, url=raw.url, title=raw.title,
        address=raw.address, listing_type=raw.listing_type, data=data, match_level=level,
        matched_profiles=names, image_url=raw.image_url, raw_text=raw.text,
    )
    return listing, reasons


def run_source(name: str, scfg: dict, cfg: Config, db: DB, notifier: Notifier) -> dict:
    started = now()
    stats = {"found": 0, "new": 0, "matches": 0, "error": None}
    fetcher = make_fetcher(scfg.get("fetcher", cfg.fetcher), cfg.delay, cfg.user_agent, cfg.proxy)
    geocoder = Geocoder(db, cfg.geocoding)
    area = load_area(db)
    profiles = effective_profiles(cfg, db)
    try:
        src = build_source(name, scfg, fetcher)
        known = db.known_ids(src.name)
        first_run = not known
        hits = src.search()
        stats["found"] = len(hits)
        db.touch(src.name, [h.source_id for h in hits if h.source_id in known])
        new_hits = [h for h in hits if h.source_id not in known]
        todo = [h for h in new_hits if not prefiltered(h, cfg, area, profiles)]
        stats["prefiltered"] = len(new_hits) - len(todo)
        todo = todo[: src.max_details]
        # Bekannte Treffer und gelikte Inserate regelmäßig neu laden, damit
        # Änderungen (Preis, Beschreibung, offline …) im Verlauf landen.
        due = db.due_for_refresh(src.name, float(scfg.get("refresh_hours", 6)), int(scfg.get("refresh_per_run", 15)))
        refresh = [SearchHit(r["source_id"], r["url"], r["listing_type"]) for r in due]
        refresh_ids = {r["source_id"]: r["id"] for r in due}
        first_run_matches = []
        for hit in todo + refresh:
            try:
                raw = src.fetch_detail(hit)
            except BlockedError:
                raise
            except NotFoundError as e:
                if hit.source_id in refresh_ids:
                    db.mark_offline(refresh_ids[hit.source_id])
                log.info("[%s] %s", name, e)
                continue
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] Detailseite %s fehlgeschlagen: %s", name, hit.url, e)
                continue
            listing, reasons = evaluate(raw, cfg, geocoder, area, profiles)
            lid, is_new, changes = db.upsert(listing, raw.structured, reasons)
            row = db.get(lid)
            price_drop = next((c for c in changes if c["field"] in ("purchase_price", "rent_cold")
                               and c["direction"] == "down"), None)
            if is_new:
                stats["new"] += 1
            if row.get("status") == "ausgeblendet":
                continue
            # Preissenkung: bei gemeldeten Treffern und bei gelikten Wohnungen (auch ohne Treffer)
            if price_drop and cfg.notify_price_drops and (row.get("notified_at") or row["likes"]):
                subject, body = format_message(row, kind="preis", old_price=price_drop["old_price"],
                                               calc=_calc(row, cfg))
                notifier.send(subject, body, row["url"])
            if listing.match_level == "none":
                continue
            if is_new:
                stats["matches"] += 1
            if is_new and row["duplicate_of"] is None and listing.match_level in cfg.notify_levels:
                if first_run and not cfg.notify_first_run:
                    first_run_matches.append(row)
                    continue
                subject, body = format_message(row, calc=_calc(row, cfg))
                notifier.send(subject, body, row["url"])
                db.mark_notified(lid)
        if first_run_matches:
            notifier.send(
                f"FernImmo: erster Lauf {name}",
                f"Erster Suchlauf auf {name}: {len(first_run_matches)} passende Inserate gefunden. "
                "Diese wurden nicht einzeln gemeldet – siehe Weboberfläche. Ab jetzt kommt jedes neue Inserat einzeln.",
            )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(scfg.get("inactive_after_days", 7)))).isoformat(timespec="seconds")
        db.mark_inactive(src.name, cutoff)
    except BlockedError as e:
        stats["error"] = f"{e} – evtl. fetcher: playwright, längere Pausen oder seltener abfragen"
        log.error("[%s] %s", name, stats["error"])
    except Exception as e:  # noqa: BLE001
        stats["error"] = str(e)
        log.exception("[%s] Lauf fehlgeschlagen", name)
    finally:
        fetcher.close()
        db.log_run(name, started, stats["found"], stats["new"], stats["matches"], stats["error"])
    log.info("[%s] gefunden=%d neu=%d treffer=%d", name, stats["found"], stats["new"], stats["matches"])
    return stats


def run_once(cfg: Config, db: DB, notifier: Notifier, only: str | None = None) -> dict[str, dict]:
    results = {}
    for name, scfg in cfg.sources.items():
        if only and name != only:
            continue
        results[name] = run_source(name, scfg, cfg, db, notifier)
    return results


def reprocess(cfg: Config, db: DB) -> int:
    """Wertet alle gespeicherten Inserate neu aus (nach Änderungen an Profilen, Regeln oder
    Suchgebiet). Ohne Netzwerk: Lage kommt aus der Datenbank bzw. dem Geocoding-Cache."""
    area = load_area(db)
    geocoder = Geocoder(db, {**cfg.geocoding, "enabled": False})
    profiles = effective_profiles(cfg, db)
    n = 0
    for row in db.all_for_reprocess():
        raw = RawListing(
            source=row["source"], source_id=row["source_id"], url=row["url"], title=row["title"] or "",
            text=row["raw_text"] or "", structured=row["structured"] or {}, address=row["address"] or "",
            listing_type=row["listing_type"], image_url=row["image_url"],
            lat=row.get("lat"), lon=row.get("lon"), geo_precision=row.get("geo_precision"),
        )
        listing, reasons = evaluate(raw, cfg, geocoder, area, profiles)
        db.upsert(listing, raw.structured, reasons, track_changes=False)
        n += 1
    return n


def in_quiet_hours(cfg: Config) -> bool:
    if not cfg.quiet_hours:
        return False
    start, end = cfg.quiet_hours
    h = datetime.now().hour
    return start <= h < end if start < end else (h >= start or h < end)


class Scheduler(threading.Thread):
    def __init__(self, cfg: Config, db: DB, notifier: Notifier):
        super().__init__(daemon=True, name="scheduler")
        self.cfg, self.db, self.notifier = cfg, db, notifier
        self.trigger = threading.Event()
        self.running = threading.Lock()

    def run_now(self) -> None:
        self.trigger.set()

    def run(self) -> None:
        while True:
            if in_quiet_hours(self.cfg) and not self.trigger.is_set():
                log.info("Ruhezeit – kein Suchlauf")
            else:
                with self.running:
                    run_once(self.cfg, self.db, self.notifier)
            self.trigger.clear()
            # ±25 % Zufall, damit die Abrufe nicht im starren Takt kommen
            wait = self.cfg.interval_minutes * 60 * random.uniform(0.75, 1.25)
            log.info("Nächster Lauf in %.0f Minuten", wait / 60)
            self.trigger.wait(timeout=wait)
            time.sleep(1)
