"""Ein Suchlauf: Quellen abfragen, neue Inserate auswerten, speichern, melden."""
from __future__ import annotations

import logging
import re
import random
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from .config import Config
from .db import DB, now
from .extract import extract, normalize_rooms
from .geo import AreaFilter, Geocoder, plz_for_point, plz_state
from .fetch import BlockedError, NotFoundError, make_fetcher
from .searches import Search, ensure_searches, fw_filter, load_searches, match_all
from .models import Listing, RawListing
from .calc import costs
from .notify import Notifier, format_message


def _calc(row: dict, cfg: Config, db: DB | None = None) -> dict | None:
    if row.get("listing_type") != "kauf":
        return None
    d = row["data"]
    return costs(row.get("price"), d.get("area"), d.get("hausgeld"), d.get("flags"), cfg.calculator,
                 state=plz_state(row.get("plz")), rent_sqm=db.rent_estimate(row.get("plz")) if db else None)
from .sources import build_source
from .sources.base import SearchHit

log = logging.getLogger(__name__)


def prefiltered(hit: SearchHit, searches: list[Search]) -> bool:
    """True, wenn schon der Kurztext aus der Ergebnisliste klar gegen alle Suchen spricht
    (z. B. falsche PLZ bei bundesweiter Suche, 4 Zimmer bei Suche nach 1–2). Spart Abrufe."""
    if not hit.snippet or not searches:
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
    return match_all(searches, hit.listing_type, data, hit.snippet).level == "none"


def outside_note(outside: list[str], checked: int, prefiltered: int, new: int) -> str | None:
    """Warnt, wenn eine Suchseite (fast) nur Inserate außerhalb aller Suchgebiete liefert –
    typisch für eine falsche Ortskennung in der URL."""
    if checked >= 5 and len(outside) >= 0.9 * checked:
        common = Counter(p[:3] for p in outside if p != "?").most_common(3)
        where = ", ".join(f"{p}xx" for p, _ in common)
        return (f"{len(outside)} von {checked} neuen Inseraten liegen außerhalb aller Suchgebiete"
                + (f" (v. a. PLZ {where})" if where else "")
                + " – stimmt der Ort in der Suchseiten-URL der config.yaml?")
    if new >= 10 and prefiltered >= 0.9 * new and checked == 0:
        return (f"{prefiltered} von {new} neuen Inseraten schon in der Ergebnisliste aussortiert"
                " – stimmt der Ort in der Suchseiten-URL der config.yaml?")
    return None


def evaluate(raw: RawListing, cfg: Config, geocoder: Geocoder | None = None,
             searches: list[Search] | None = None, fw: AreaFilter | None = None) -> tuple[Listing, list[str]]:
    if not raw.url.lower().startswith(("http://", "https://")):
        raise ValueError(f"Ungültiger Inserats-Link: {raw.url[:80]}")
    if raw.image_url and not raw.image_url.lower().startswith(("http://", "https://")):
        raw.image_url = None
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
    data["fw_zone"] = bool(fw and fw.in_fw_zone(data))
    full_text = f"{raw.title}\n{raw.text}"
    if searches is None:   # ohne Datenbank (z. B. Einzeltest): Profile aus der config.yaml
        searches = [Search(id=0, name=p.name, active=True, notify=True, criteria={}, plz=list(p.plz),
                           profile=p, area=None) for p in cfg.profiles]
    out = match_all(searches, raw.listing_type, data, full_text)
    level, names, reasons = out.level, out.names, out.reasons
    listing = Listing(
        id=None, source=raw.source, source_id=raw.source_id, url=raw.url, title=raw.title,
        address=raw.address, listing_type=raw.listing_type, data=data, match_level=level,
        matched_profiles=names, matched_ids=[i for i in out.ids if i], notify_level=out.notify_level,
        image_url=raw.image_url, raw_text=raw.text,
    )
    return listing, reasons


def run_source(name: str, scfg: dict, cfg: Config, db: DB, notifier: Notifier) -> dict:
    started = now()
    stats = {"found": 0, "new": 0, "matches": 0, "error": None, "note": None}
    outside: list[str] = []     # PLZ neuer Inserate, die in keinem Suchgebiet liegen
    checked = 0
    fetcher = make_fetcher(scfg.get("fetcher", cfg.fetcher), cfg.delay, cfg.user_agent, cfg.proxy)
    geocoder = Geocoder(db, cfg.geocoding)
    searches = load_searches(db)
    fw = fw_filter(db)
    try:
        src = build_source(name, scfg, fetcher)
        known = db.known_ids(src.name)
        first_run = not known
        db.clear_skipped(older_than=(datetime.now(timezone.utc) - timedelta(days=60)).isoformat(timespec="seconds"))
        skipped = db.skipped_ids(src.name)
        # Leere Liste = erster Lauf oder Suche geändert -> Feeds weiter zurück durchsehen
        hits = src.search(known | skipped, backfill=not skipped)
        is_feed = any(f for _, _, f in src.search_entries())
        stats["found"] = len(hits)
        db.touch(src.name, [h.source_id for h in hits if h.source_id in known])
        new_hits = [h for h in hits if h.source_id not in known and h.source_id not in skipped]
        # Quellen nur für Vergleichsmieten werden nie vorab aussortiert (sie treffen keine Kaufsuche)
        todo = [h for h in new_hits if scfg.get("vergleichsmieten") or not prefiltered(h, searches)]
        stats["prefiltered"] = len(new_hits) - len(todo)
        todo_ids = {h.source_id for h in todo}
        db.add_skipped(src.name, [h.source_id for h in new_hits if h.source_id not in todo_ids])
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
            try:
                listing, reasons = evaluate(raw, cfg, geocoder, searches, fw)
            except ValueError as e:
                log.warning("[%s] Inserat übersprungen: %s", name, e)
                continue
            lid, is_new, changes = db.upsert(listing, raw.structured, reasons)
            row = db.get(lid)
            price_drop = next((c for c in changes if c["field"] in ("purchase_price", "rent_cold")
                               and c["direction"] == "down"), None)
            if is_new:
                stats["new"] += 1
                areas = [s.area for s in searches if s.area is not None and s.area.active]
                if areas and not scfg.get("vergleichsmieten"):
                    checked += 1
                    if all(a.check(listing.data) is False for a in areas):
                        outside.append(listing.data.get("plz") or "?")
            if row.get("status") == "ausgeblendet":
                continue
            # Preissenkung: bei gemeldeten Treffern und bei gelikten Wohnungen (auch ohne Treffer)
            if price_drop and cfg.notify_price_drops and (row.get("notified_at") or row["likes"]):
                subject, body = format_message(row, kind="preis", old_price=price_drop["old_price"],
                                               calc=_calc(row, cfg, db))
                notifier.send(subject, body, row["url"], listing_id=lid)
            if listing.match_level == "none":
                continue
            if is_new:
                stats["matches"] += 1
            if is_new and row["duplicate_of"] is None and listing.notify_level in cfg.notify_levels:
                if first_run and not cfg.notify_first_run:
                    first_run_matches.append(row)
                    continue
                subject, body = format_message(row, calc=_calc(row, cfg, db))
                notifier.send(subject, body, row["url"], listing_id=lid)
                db.mark_notified(lid)
        if first_run_matches:
            notifier.send(
                f"FernImmo: erster Lauf {name}",
                f"Erster Suchlauf auf {name}: {len(first_run_matches)} passende Inserate gefunden. "
                "Diese wurden nicht einzeln gemeldet – siehe Weboberfläche. Ab jetzt kommt jedes neue Inserat einzeln.",
            )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(scfg.get("inactive_after_days", 7)))).isoformat(timespec="seconds")
        # Feeds zeigen nur die neuesten Inserate; ältere fehlen dort, sind aber nicht offline.
        # Ob sie noch online sind, klärt dann das regelmäßige Neuladen der Detailseite.
        if not is_feed:
            db.mark_inactive(src.name, cutoff)
        # Bei bundesweiten Feeds liegt fast alles außerhalb – dort ist das gewollt
        stats["note"] = None if is_feed else outside_note(outside, checked, stats["prefiltered"], len(new_hits))
        if is_feed and len(new_hits) >= 20 and not any(extract(h.snippet or "").get("plz") for h in new_hits):
            stats["note"] = ("Ergebnisliste ohne erkennbare PLZ – Vorsortierung nach Suchgebiet nicht möglich, "
                             f"es werden nur die neuesten {src.max_details} Inserate je Lauf geöffnet")
        if stats["note"]:
            log.warning("[%s] %s", name, stats["note"])
    except BlockedError as e:
        stats["error"] = f"{e} – evtl. fetcher: playwright, längere Pausen oder seltener abfragen"
        log.error("[%s] %s", name, stats["error"])
    except Exception as e:  # noqa: BLE001
        stats["error"] = str(e)
        log.exception("[%s] Lauf fehlgeschlagen", name)
    finally:
        fetcher.close()
        db.log_run(name, started, stats["found"], stats["new"], stats["matches"], stats["error"],
                   stats["note"])
    log.info("[%s] gefunden=%d neu=%d treffer=%d", name, stats["found"], stats["new"], stats["matches"])
    return stats


def run_once(cfg: Config, db: DB, notifier: Notifier, only: str | None = None) -> dict[str, dict]:
    ensure_searches(cfg, db)
    results = {}
    for name, scfg in cfg.sources.items():
        if only and name != only:
            continue
        results[name] = run_source(name, scfg, cfg, db, notifier)
        check_source_health(name, cfg, db, notifier)
    return results


def check_source_health(name: str, cfg: Config, db: DB, notifier: Notifier) -> None:
    """Meldet, wenn ein Portal mehrere Läufe in Folge nichts liefert (gesperrt, Layout geändert,
    Such-URL kaputt) – und einmalig, wenn es wieder geht. Sonst merkt man den Ausfall nicht."""
    n = cfg.alert_failed_runs
    if n <= 0:
        return
    runs = db.last_runs(name, n)
    if not runs:
        return
    state = db.get_setting("source_alerts", {})
    bad = lambda r: bool(r["error"]) or not r["found"]
    if len(runs) == n and all(bad(r) for r in runs):
        if state.get(name) != "down":
            reason = runs[0]["error"] or "keine Inserate auf der Suchseite gefunden"
            notifier.send(f"⚠️ FernImmo: {name} liefert nichts",
                          f"⚠️ {name}: seit {n} Suchläufen keine Ergebnisse.\nLetzter Stand: {reason}\n\n"
                          "Mögliche Ursachen: Portal blockiert den Abruf, Seitenaufbau geändert oder Such-URL "
                          "veraltet. Prüfen mit: python -m fernimmo check <Inserat-URL>")
            state[name] = "down"
            db.set_setting("source_alerts", state)
    elif not bad(runs[0]) and state.get(name) == "down":
        notifier.send(f"✅ FernImmo: {name} wieder ok", f"✅ {name} liefert wieder Ergebnisse ({runs[0]['found']}).")
        state.pop(name, None)
        db.set_setting("source_alerts", state)


def backup_if_due(cfg: Config, db: DB) -> str | None:
    """Einmal pro Tag eine Kopie der Datenbank; ältere als `keep` Tage werden gelöscht."""
    if not cfg.backup_enabled:
        return None
    from pathlib import Path
    day = datetime.now().strftime("%Y-%m-%d")
    target = Path(cfg.backup_dir) / f"fernimmo-{day}.db"
    if target.exists():
        return None
    db.backup(target)
    for old in sorted(Path(cfg.backup_dir).glob("fernimmo-*.db"))[:-cfg.backup_keep]:
        old.unlink(missing_ok=True)
    log.info("Datensicherung: %s", target)
    return str(target)


def reprocess(cfg: Config, db: DB) -> int:
    """Wertet alle gespeicherten Inserate neu aus (nach Änderungen an Profilen, Regeln oder
    Suchgebiet). Ohne Netzwerk: Lage kommt aus der Datenbank bzw. dem Geocoding-Cache."""
    ensure_searches(cfg, db)
    geocoder = Geocoder(db, {**cfg.geocoding, "enabled": False})
    searches = load_searches(db)
    fw = fw_filter(db)
    n = 0
    for row in db.all_for_reprocess():
        raw = RawListing(
            source=row["source"], source_id=row["source_id"], url=row["url"], title=row["title"] or "",
            text=row["raw_text"] or "", structured=row["structured"] or {}, address=row["address"] or "",
            listing_type=row["listing_type"], image_url=row["image_url"],
            lat=row.get("lat"), lon=row.get("lon"), geo_precision=row.get("geo_precision"),
        )
        listing, reasons = evaluate(raw, cfg, geocoder, searches, fw)
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
            try:
                backup_if_due(self.cfg, self.db)
            except Exception:  # noqa: BLE001 - Sicherung darf die Suche nicht stoppen
                log.exception("Datensicherung fehlgeschlagen")
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
