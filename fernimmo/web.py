"""Weboberfläche (FastAPI): Übersicht mit Likes/Kommentaren, Detailansicht mit
Änderungsverlauf, Laufprotokoll."""
from __future__ import annotations

import secrets
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from .config import Config
from .db import DB
from .notify import HEATING_NAMES, fmt_num
from .pipeline import Scheduler

STATUSES = ["neu", "favorit", "kontaktiert", "besichtigung", "ausgeblendet"]
ANON_USER = "ich"


def _safe_back(back: str, fallback: str = "/") -> str:
    return back if back.startswith("/") and not back.startswith("//") else fallback


def create_app(cfg: Config, db: DB, scheduler: Scheduler | None = None) -> FastAPI:
    app = FastAPI(title="FernImmo", docs_url=None, redoc_url=None)
    security = HTTPBasic(auto_error=bool(cfg.web_users))
    tz = ZoneInfo(cfg.timezone)

    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    templates.env.filters["num"] = fmt_num
    templates.env.filters["heating"] = lambda srcs: ", ".join(HEATING_NAMES.get(h, h) for h in srcs or [])

    def local_dt(iso: str | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
        if not iso:
            return ""
        return datetime.fromisoformat(iso).astimezone(tz).strftime(fmt)

    templates.env.filters["dt"] = local_dt

    def auth(creds: HTTPBasicCredentials | None = Depends(security)) -> str:
        """Liefert den angemeldeten Nutzernamen."""
        if not cfg.web_users:
            return ANON_USER
        if creds:
            pw = cfg.web_users.get(creds.username)
            if pw is not None and secrets.compare_digest(creds.password.encode(), pw.encode()):
                return creds.username
        raise HTTPException(401, "Nicht angemeldet", headers={"WWW-Authenticate": "Basic"})

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, user: str = Depends(auth), match: str = "treffer", fernwaerme: str = "",
              source: str = "", status: str = "", listing_type: str = "", q: str = "", sort: str = "first_seen",
              inactive: int = 0, dupes: int = 0, liked: int = 0):
        rows = db.search(match=match or None, fernwaerme=fernwaerme or None, source=source or None,
                         status=status or None, listing_type=listing_type or None, q=q or None, sort=sort,
                         active_only=not inactive, hide_duplicates=not dupes, liked=bool(liked), user=user)
        here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return templates.TemplateResponse(request, "index.html", {
            "rows": rows, "stats": db.stats(), "sources": list(cfg.sources), "statuses": STATUSES,
            "f": dict(match=match, fernwaerme=fernwaerme, source=source, status=status, listing_type=listing_type,
                      q=q, sort=sort, inactive=inactive, dupes=dupes, liked=liked),
            "runs": db.recent_runs(len(cfg.sources) or 1), "user": user, "here": here,
        })

    @app.get("/listing/{lid}", response_class=HTMLResponse)
    def detail(request: Request, lid: int, user: str = Depends(auth)):
        row = db.get(lid, user)
        if not row:
            raise HTTPException(404)
        return templates.TemplateResponse(request, "detail.html", {
            "l": row, "changes": db.changes(lid), "statuses": STATUSES, "user": user,
            "here": request.url.path,
            "duplicate": db.get(row["duplicate_of"]) if row["duplicate_of"] else None,
        })

    def _require(lid: int) -> None:
        if not db.get(lid):
            raise HTTPException(404)

    @app.post("/listing/{lid}/status")
    def set_status(lid: int, status: str = Form(...), back: str = Form("/"), user: str = Depends(auth)):
        if status not in STATUSES:
            raise HTTPException(400)
        _require(lid)
        db.set_status(lid, status)
        return RedirectResponse(_safe_back(back), status_code=303)

    @app.post("/listing/{lid}/like")
    def like(request: Request, lid: int, back: str = Form("/"), user: str = Depends(auth)):
        _require(lid)
        liked = db.toggle_like(lid, user)
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"liked": liked, "likes": db.get(lid)["likes"]})
        return RedirectResponse(f"{_safe_back(back)}#l{lid}", status_code=303)

    @app.post("/listing/{lid}/comment")
    def comment(lid: int, text: str = Form(...), back: str = Form("/"), user: str = Depends(auth)):
        _require(lid)
        if text.strip():
            db.add_comment(lid, user, text)
        return RedirectResponse(f"{_safe_back(back)}#l{lid}", status_code=303)

    @app.post("/comment/{cid}/delete")
    def delete_comment(cid: int, lid: int = Form(...), back: str = Form("/"), user: str = Depends(auth)):
        db.delete_comment(cid, user)
        return RedirectResponse(f"{_safe_back(back)}#l{lid}", status_code=303)

    @app.post("/run")
    def run_now(user: str = Depends(auth)):
        if scheduler:
            scheduler.run_now()
        return RedirectResponse("/runs", status_code=303)

    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request, user: str = Depends(auth)):
        return templates.TemplateResponse(request, "runs.html", {
            "runs": db.recent_runs(100), "busy": bool(scheduler and scheduler.running.locked()), "user": user,
        })

    @app.get("/api/listings")
    def api_listings(match: str = "treffer", fernwaerme: str = "", user: str = Depends(auth)):
        rows = db.search(match=match or None, fernwaerme=fernwaerme or None, user=user)
        for r in rows:
            r.pop("raw_text", None)
        return JSONResponse(rows)

    @app.get("/api/listings/{lid}/changes")
    def api_changes(lid: int, user: str = Depends(auth)):
        _require(lid)
        return JSONResponse(db.changes(lid))

    @app.get("/health")
    def health():
        return {"ok": True}

    return app
