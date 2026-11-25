"""Weboberfläche (FastAPI): Übersicht, Filter, Detailansicht, Laufprotokoll."""
from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from .config import Config
from .db import DB
from .notify import HEATING_NAMES, fmt_num
from .pipeline import Scheduler

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["num"] = fmt_num
TEMPLATES.env.filters["heating"] = lambda srcs: ", ".join(HEATING_NAMES.get(h, h) for h in srcs or [])
STATUSES = ["neu", "favorit", "kontaktiert", "besichtigung", "ausgeblendet"]


def create_app(cfg: Config, db: DB, scheduler: Scheduler | None = None) -> FastAPI:
    app = FastAPI(title="FernImmo", docs_url=None, redoc_url=None)
    security = HTTPBasic(auto_error=bool(cfg.web_password))

    def auth(creds: HTTPBasicCredentials | None = Depends(security)) -> None:
        if not cfg.web_password:
            return
        ok = creds and secrets.compare_digest(creds.username, cfg.web_user) and secrets.compare_digest(
            creds.password, cfg.web_password)
        if not ok:
            raise HTTPException(401, "Nicht angemeldet", headers={"WWW-Authenticate": "Basic"})

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def index(request: Request, match: str = "treffer", fernwaerme: str = "", source: str = "",
              status: str = "", listing_type: str = "", q: str = "", sort: str = "first_seen",
              inactive: int = 0, dupes: int = 0):
        rows = db.search(match=match or None, fernwaerme=fernwaerme or None, source=source or None,
                         status=status or None, listing_type=listing_type or None, q=q or None, sort=sort,
                         active_only=not inactive, hide_duplicates=not dupes)
        return TEMPLATES.TemplateResponse(request, "index.html", {
            "rows": rows, "stats": db.stats(), "sources": list(cfg.sources), "statuses": STATUSES,
            "f": dict(match=match, fernwaerme=fernwaerme, source=source, status=status,
                      listing_type=listing_type, q=q, sort=sort, inactive=inactive, dupes=dupes),
            "runs": db.recent_runs(len(cfg.sources) or 1),
        })

    @app.get("/listing/{lid}", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def detail(request: Request, lid: int):
        row = db.get(lid)
        if not row:
            raise HTTPException(404)
        return TEMPLATES.TemplateResponse(request, "detail.html", {
            "l": row, "history": db.price_history(lid), "statuses": STATUSES,
            "duplicate": db.get(row["duplicate_of"]) if row["duplicate_of"] else None,
        })

    @app.post("/listing/{lid}/status", dependencies=[Depends(auth)])
    def set_status(lid: int, status: str = Form(...), back: str = Form("/")):
        if status not in STATUSES:
            raise HTTPException(400)
        db.set_status(lid, status)
        return RedirectResponse(back if back.startswith("/") else "/", status_code=303)

    @app.post("/run", dependencies=[Depends(auth)])
    def run_now():
        if scheduler:
            scheduler.run_now()
        return RedirectResponse("/runs", status_code=303)

    @app.get("/runs", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def runs(request: Request):
        return TEMPLATES.TemplateResponse(request, "runs.html", {
            "runs": db.recent_runs(100), "busy": bool(scheduler and scheduler.running.locked()),
        })

    @app.get("/api/listings", dependencies=[Depends(auth)])
    def api_listings(match: str = "treffer", fernwaerme: str = ""):
        rows = db.search(match=match or None, fernwaerme=fernwaerme or None)
        for r in rows:
            r.pop("raw_text", None)
        return JSONResponse(rows)

    @app.get("/health")
    def health():
        return {"ok": True}

    return app
