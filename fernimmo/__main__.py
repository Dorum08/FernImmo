"""Kommandozeile:

  python -m fernimmo serve        Weboberfläche + regelmäßige Suche (Normalbetrieb)
  python -m fernimmo run          einen Suchlauf ausführen und beenden (z.B. per cron)
  python -m fernimmo reprocess    gespeicherte Inserate neu auswerten
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
    notifier = Notifier(cfg.notify)

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
        uvicorn.run(create_app(cfg, db, scheduler), host=cfg.web_host, port=cfg.web_port, log_level="warning")
        return 0
    return 2


def _check(cfg: Config, url: str, source: str | None) -> int:
    from .fetch import make_fetcher
    from .pipeline import evaluate
    from .sources import build_source
    from .sources.base import SearchHit, guess_type

    if not source:
        domains = {"kleinanzeigen.de": "kleinanzeigen", "immowelt.de": "immowelt",
                   "immobilienscout24.de": "immoscout24"}
        source = next((name for dom, name in domains.items() if dom in url), None)
    if not source:
        print("Quelle nicht erkannt, bitte --source angeben", file=sys.stderr)
        return 2
    scfg = cfg.sources.get(source, {"type": source})
    fetcher = make_fetcher(scfg.get("fetcher", cfg.fetcher), (0, 0), cfg.user_agent, cfg.proxy)
    try:
        src = build_source(source, scfg, fetcher)
        raw = src.fetch_detail(SearchHit("check", url, guess_type(url)))
    finally:
        fetcher.close()
    listing, reasons = evaluate(raw, cfg)
    print(f"Titel:      {raw.title}\nAdresse:    {raw.address}\nArt:        {raw.listing_type}")
    print(f"Treffer:    {listing.match_level} {listing.matched_profiles} {reasons}")
    print("Strukturiert:", json.dumps(raw.structured, indent=2, ensure_ascii=False))
    print("Merkmale:", json.dumps(listing.data, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
