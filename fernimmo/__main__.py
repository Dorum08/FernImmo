"""Kommandozeile:

  python -m fernimmo serve        Weboberfläche + regelmäßige Suche (Normalbetrieb)
  python -m fernimmo run          einen Suchlauf ausführen und beenden (z.B. per cron)
  python -m fernimmo reprocess    gespeicherte Inserate neu auswerten
  python -m fernimmo backup       Datenbank sofort sichern (sonst automatisch täglich)
  python -m fernimmo test-notify  Testnachricht an alle aktiven Kanäle
  python -m fernimmo check URL    eine Detailseite abrufen und die Auswertung anzeigen
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .config import Config
from .db import DB
from .notify import Notifier


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fernimmo")
    ap.add_argument("-c", "--config", default=os.environ.get("FERNIMMO_CONFIG", "config.yaml"))
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    p_run = sub.add_parser("run")
    p_run.add_argument("--source")
    sub.add_parser("reprocess")
    sub.add_parser("backup")
    sub.add_parser("test-notify")
    p_check = sub.add_parser("check")
    p_check.add_argument("url")
    p_check.add_argument("--source", help="Quellenname aus config.yaml (sonst anhand der URL)")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = Config.load(args.config)
    notifier = Notifier(cfg.notify, public_url=cfg.public_url)

    if args.cmd == "test-notify":
        notifier.send("FernImmo Test", "Testnachricht von FernImmo – die Benachrichtigung funktioniert.")
        return 0

    if args.cmd == "check":
        return _check(cfg, args.url, args.source)

    db = DB(cfg.db_path)
    if args.cmd == "run":
        from .pipeline import run_once
        res = run_once(cfg, db, notifier, only=args.source)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 1 if any(r["error"] for r in res.values()) else 0
    if args.cmd == "backup":
        from pathlib import Path
        target = Path(cfg.backup_dir) / f"fernimmo-manuell-{__import__('datetime').datetime.now():%Y-%m-%d_%H%M}.db"
        db.backup(target)
        print(f"Gesichert nach {target}")
        return 0
    if args.cmd == "reprocess":
        from .pipeline import reprocess
        print(f"{reprocess(cfg, db)} Inserate neu ausgewertet")
        return 0
    if args.cmd == "serve":
        import uvicorn

        from .pipeline import Scheduler
        from .web import create_app
        scheduler = Scheduler(cfg, db, notifier)
        scheduler.start()
        tg = cfg.notify.get("telegram") or {}
        if tg.get("enabled") and tg.get("buttons", True):
            from .telegram_bot import TelegramBot
            TelegramBot(tg, db, list(cfg.web_users), cfg.public_url).start()
        uvicorn.run(create_app(cfg, db, scheduler), host=cfg.web_host, port=cfg.web_port, log_level="warning")
        return 0
    return 2


def _check(cfg: Config, url: str, source: str | None) -> int:
    from .fetch import make_fetcher
    from .pipeline import evaluate
    from .sources import build_source
    from .sources.base import SearchHit, guess_type

    if not source:
        from .sources.generic import PORTALS
        domains = {"kleinanzeigen.de": "kleinanzeigen", "immowelt.de": "immowelt",
                   "immobilienscout24.de": "immoscout24",
                   **{p["domain"]: name for name, p in PORTALS.items()}}
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        source = next((name for dom, name in domains.items() if host == dom or host.endswith("." + dom)), None)
    if not source:
        source = "generic"   # lernfähiger Adapter für unbekannte Seiten
    scfg = cfg.sources.get(source, {"type": source})
    fetcher = make_fetcher(scfg.get("fetcher", cfg.fetcher), (0, 0), cfg.user_agent, cfg.proxy)
    try:
        src = build_source(source, scfg, fetcher)
        raw = src.fetch_detail(SearchHit("check", url, guess_type(url)))
    except Exception as e:  # noqa: BLE001 - verständliche Meldung statt Stacktrace
        print(f"Abruf/Auswertung fehlgeschlagen ({source}): {e}", file=sys.stderr)
        return 1
    finally:
        fetcher.close()
    from .searches import ensure_searches, fw_filter, load_searches
    db = DB(cfg.db_path)
    ensure_searches(cfg, db)
    listing, reasons = evaluate(raw, cfg, None, load_searches(db), fw_filter(db))
    print(f"Titel:      {raw.title}\nAdresse:    {raw.address}\nArt:        {raw.listing_type}")
    print(f"Treffer:    {listing.match_level} – Suchen: {', '.join(listing.matched_profiles) or 'keine'} {reasons}")
    print("Strukturiert:", json.dumps(raw.structured, indent=2, ensure_ascii=False))
    print("Merkmale:", json.dumps(listing.data, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
