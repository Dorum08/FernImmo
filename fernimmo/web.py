"""Weboberfläche (FastAPI): Übersicht mit Likes/Kommentaren und Marktvergleich,
Detailansicht mit Änderungsverlauf und Kostenrechner, Karte, Suchgebiet-Editor,
Export, Laufprotokoll."""
from __future__ import annotations

import csv
import io
import secrets
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Body, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .calc import costs
from .config import Config
from .db import DB
from .geo import PLZ_DIR, Geocoder, circle_polygon, haversine_km, plz_center, plz_state, valid_geometry
from .notify import HEATING_NAMES, fmt_num
from .pipeline import Scheduler, reprocess
from .searches import (DEFAULT_CRITERIA, FLAG_CHOICES, clean_criteria, ensure_searches,
                       heating_choice)

STATUSES = ["neu", "favorit", "kontaktiert", "besichtigung", "ausgeblendet"]
ANON_USER = "ich"
HERE = Path(__file__).parent


def _num(v: str) -> float | None:
    try:
        return float(str(v).replace(",", ".")) if str(v).strip() else None
    except ValueError:
        return None


def _safe_back(back: str, fallback: str = "/") -> str:
    return back if back.startswith("/") and not back.startswith("//") else fallback


@dataclass
class Filters:
    match: str = "treffer"
    fernwaerme: str = ""
    source: str = ""
    status: str = ""
    listing_type: str = ""
    q: str = ""
    sort: str = "first_seen"
    inactive: int = 0
    dupes: int = 0
    liked: int = 0
    rooms_min: str = ""
    rooms_max: str = ""
    price_max: str = ""
    search: str = ""

    def search_args(self) -> dict:
        return dict(match=self.match or None, fernwaerme=self.fernwaerme or None, source=self.source or None,
                    status=self.status or None, listing_type=self.listing_type or None, q=self.q or None,
                    sort=self.sort, active_only=not self.inactive, hide_duplicates=not self.dupes,
                    liked=bool(self.liked), rooms_min=_num(self.rooms_min), rooms_max=_num(self.rooms_max),
                    price_max=_num(self.price_max),
                    search_id=int(self.search) if self.search.isdigit() else None)


class Reprocessor:
    """Neubewertung im Hintergrund (z. B. nach Änderung des Suchgebiets)."""

    def __init__(self, cfg: Config, db: DB, scheduler: Scheduler | None):
        self.cfg, self.db, self.scheduler = cfg, db, scheduler
        self.busy = threading.Event()
        self.again = threading.Event()

    def trigger(self, wait: bool = False) -> None:
        if self.busy.is_set():
            self.again.set()
            return
        self.busy.set()
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        if wait:
            t.join()

    def _run(self) -> None:
        try:
            while True:
                self.again.clear()
                lock = self.scheduler.running if self.scheduler else threading.Lock()
                with lock:
                    reprocess(self.cfg, self.db)
                if not self.again.is_set():
                    break
        finally:
            self.busy.clear()


def create_app(cfg: Config, db: DB, scheduler: Scheduler | None = None, sync_reprocess: bool = False) -> FastAPI:
    app = FastAPI(title="FernImmo", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        """Schutz gegen Cross-Site-Request-Forgery: Ändernde Anfragen (POST/PATCH/DELETE) nur von
        FernImmo selbst. Der Browser schickt das Passwort sonst auch bei Formularen fremder Seiten mit."""
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            src = request.headers.get("origin") or request.headers.get("referer")
            if src:
                from urllib.parse import urlparse
                allowed = {request.headers.get("host", ""), request.headers.get("x-forwarded-host", "")}
                if cfg.public_url:
                    allowed.add(urlparse(cfg.public_url).netloc)
                if urlparse(src).netloc not in allowed:
                    return Response("Anfrage von fremder Seite abgelehnt", status_code=403)
        return await call_next(request)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    security = HTTPBasic(auto_error=bool(cfg.web_users))
    tz = ZoneInfo(cfg.timezone)
    reproc = Reprocessor(cfg, db, scheduler)
    ensure_searches(cfg, db)

    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["num"] = fmt_num
    templates.env.filters["heating"] = lambda srcs: ", ".join(HEATING_NAMES.get(h, h) for h in srcs or [])

    def local_dt(iso: str | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
        if not iso:
            return ""
        return datetime.fromisoformat(iso).astimezone(tz).strftime(fmt)

    templates.env.filters["dt"] = local_dt

    def calc_for(r: dict) -> dict | None:
        if r.get("listing_type") != "kauf":
            return None
        d = r["data"]
        return costs(r.get("price"), d.get("area"), d.get("hausgeld"), d.get("flags"), cfg.calculator,
                     state=plz_state(r.get("plz")), rent_sqm=db.rent_estimate(r.get("plz")))

    templates.env.globals["calc_for"] = calc_for

    def auth(creds: HTTPBasicCredentials | None = Depends(security)) -> str:
        """Liefert den angemeldeten Nutzernamen."""
        if not cfg.web_users:
            return ANON_USER
        if creds:
            pw = cfg.web_users.get(creds.username)
            if pw is not None and secrets.compare_digest(creds.password.encode(), pw.encode()):
                return creds.username
        raise HTTPException(401, "Nicht angemeldet", headers={"WWW-Authenticate": "Basic"})

    def with_distance(rows: list[dict], sort: str) -> list[dict]:
        """Entfernung (Luftlinie) zum nächsten Umkreis-Mittelpunkt; optional danach sortieren."""
        active = {s["id"] for s in db.searches() if s["active"]}
        centers = [a for a in db.areas("search") if a.get("radius_km") is not None and a["search_id"] in active]
        for r in rows:
            r["distance"] = None
            if centers and r.get("lat") is not None:
                best = min(centers, key=lambda a: haversine_km(r["lat"], r["lon"], a["center_lat"], a["center_lon"]))
                r["distance"] = {"km": round(haversine_km(r["lat"], r["lon"], best["center_lat"], best["center_lon"]), 1),
                                 "to": best["name"], "approx": r.get("geo_precision") == "plz"}
        if sort == "distance":
            rows.sort(key=lambda r: (r["distance"] is None, (r["distance"] or {}).get("km", 0)))
        return rows

    def here_of(request: Request) -> str:
        return request.url.path + (f"?{request.url.query}" if request.url.query else "")

    def search_list() -> list[dict]:
        """Suchen mit Kurzinfo für Auswahllisten und die Übersicht."""
        out = []
        for srch in db.searches():
            areas = db.areas("search", search_id=srch["id"])
            srch["n_areas"] = len(areas)
            srch["has_area"] = bool(srch["plz"] or areas)
            out.append(srch)
        return out

    # ------------------------------------------------------------------ Seiten
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, f: Filters = Depends(), user: str = Depends(auth)):
        rows = with_distance(db.search(**f.search_args(), user=user), f.sort)
        return templates.TemplateResponse(request, "index.html", {
            "rows": rows, "stats": db.stats(), "sources": list(cfg.sources), "statuses": STATUSES,
            "f": asdict(f), "runs": db.recent_runs(len(cfg.sources) or 1), "user": user,
            "here": here_of(request), "query": request.url.query, "searches": search_list(),
            "reprocessing": reproc.busy.is_set(),
        })

    @app.get("/listing/{lid}", response_class=HTMLResponse)
    def detail(request: Request, lid: int, user: str = Depends(auth)):
        row = db.get(lid, user)
        if not row:
            raise HTTPException(404)
        return templates.TemplateResponse(request, "detail.html", {
            "l": row, "changes": db.changes(lid), "statuses": STATUSES, "user": user,
            "here": request.url.path, "calc_defaults": cfg.calculator,
            "duplicate": db.get(row["duplicate_of"]) if row["duplicate_of"] else None,
        })

    @app.get("/karte", response_class=HTMLResponse)
    def map_page(request: Request, f: Filters = Depends(), user: str = Depends(auth)):
        return templates.TemplateResponse(request, "map.html", {
            "f": asdict(f), "user": user, "query": request.url.query, "center": cfg.map_center, "zoom": cfg.map_zoom,
            "sources": list(cfg.sources), "statuses": STATUSES, "searches": search_list(),
        })

    @app.get("/gebiet")
    def old_area_page():
        return RedirectResponse("/suche", status_code=301)

    @app.get("/suche", response_class=HTMLResponse)
    def search_page(request: Request, id: int | None = None, user: str = Depends(auth)):
        ensure_searches(cfg, db)
        searches = search_list()
        cur = next((x for x in searches if x["id"] == id), searches[0] if searches else None)
        return templates.TemplateResponse(request, "area.html", {
            "user": user, "center": cfg.map_center, "zoom": cfg.map_zoom,
            "reprocessing": reproc.busy.is_set(), "searches": searches, "cur": cur,
            "heating": heating_choice(cur["criteria"]) if cur else "fernwaerme",
            "flag_choices": FLAG_CHOICES, "room_steps": [None] + [x / 2 for x in range(2, 17)],
        })

    @app.get("/markt", response_class=HTMLResponse)
    def market_page(request: Request, plz: str = "", art: str = "kauf", user: str = Depends(auth)):
        plz = plz if plz.isdigit() and len(plz) <= 5 else ""
        art = art if art in ("kauf", "miete") else "kauf"
        return templates.TemplateResponse(request, "market.html", {
            "user": user, "plz": plz, "art": art, "table": db.market_table(),
            "trend_all": db.price_trend(art), "trend_plz": db.price_trend(art, plz) if plz else [],
        })

    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request, user: str = Depends(auth)):
        return templates.TemplateResponse(request, "runs.html", {
            "runs": db.recent_runs(100), "busy": bool(scheduler and scheduler.running.locked()), "user": user,
        })

    # ------------------------------------------------------- Aktionen (Formulare)
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

    # ------------------------------------------------------------------- Export
    EXPORT_COLS = [
        ("ID", lambda r: r["id"]), ("Titel", lambda r: r["title"]), ("Quelle", lambda r: r["source"]),
        ("Link", lambda r: r["url"]), ("PLZ", lambda r: r["plz"]), ("Adresse", lambda r: r["address"]),
        ("Zimmer", lambda r: r["rooms"]), ("Wohnfläche m²", lambda r: r["area"]),
        ("Kaufpreis €", lambda r: r["price"]),
        ("€/m²", lambda r: round(r["price"] / r["area"]) if r["price"] and r["area"] else None),
        ("Abw. Markt %", lambda r: (r.get("market") or {}).get("dev")),
        ("Hausgeld €", lambda r: r["data"].get("hausgeld")), ("Baujahr", lambda r: r["year"]),
        ("Energieträger", lambda r: ", ".join(HEATING_NAMES.get(h, h) for h in r["data"].get("heating_sources", []))),
        ("Fernwärme", lambda r: r["fernwaerme"]), ("Energieklasse", lambda r: r["data"].get("energy_class")),
        ("Etage", lambda r: r["data"].get("floor")),
        ("Entfernung km", lambda r: (r.get("distance") or {}).get("km")),
        ("Monatl. Belastung €", lambda r: (calc_for(r) or {}).get("belastung")),
        ("Kaufnebenkosten €", lambda r: (calc_for(r) or {}).get("nebenkosten")),
        ("Treffer", lambda r: r["match_level"]), ("Status", lambda r: r["status"]),
        ("Online", lambda r: "ja" if r["active"] else "nein"), ("Tage am Markt", lambda r: r["days_online"]),
        ("Preissenkungen", lambda r: r["price_drops"]), ("Preisänderung %", lambda r: r["price_delta_pct"]),
        ("Wiedereingestellt von", lambda r: r["relisted_of"]), ("Likes", lambda r: ", ".join(r["likes"])),
        ("Kommentare", lambda r: " | ".join(f"{c['user']}: {c['text']}" for c in r["comments"])),
        ("Hinweise", lambda r: ", ".join(r["data"].get("flags", []))),
        ("Erstmals gesehen", lambda r: local_dt(r["first_seen"])),
        ("Zuletzt gesehen", lambda r: local_dt(r["last_seen"])),
    ]

    @app.get("/export.csv")
    def export_csv(f: Filters = Depends(), user: str = Depends(auth)):
        rows = with_distance(db.search(**f.search_args(), user=user, limit=5000), f.sort)
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow([c for c, _ in EXPORT_COLS])
        for r in rows:
            w.writerow(["" if (v := fn(r)) is None else (str(v).replace(".", ",") if isinstance(v, float) else v)
                        for _, fn in EXPORT_COLS])
        # BOM, damit Excel Umlaute richtig erkennt
        return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="fernimmo-{_stamp()}.csv"'})

    @app.get("/export.xlsx")
    def export_xlsx(f: Filters = Depends(), user: str = Depends(auth)):
        from openpyxl import Workbook
        from openpyxl.styles import Font

        rows = with_distance(db.search(**f.search_args(), user=user, limit=5000), f.sort)
        wb = Workbook()
        ws = wb.active
        ws.title = "Inserate"
        ws.append([c for c, _ in EXPORT_COLS])
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for r in rows:
            ws.append([fn(r) for _, fn in EXPORT_COLS])
            link = ws.cell(row=ws.max_row, column=4)
            link.hyperlink, link.style = link.value, "Hyperlink"
        ws.freeze_panes = "C2"
        ws.auto_filter.ref = ws.dimensions
        for col, width in zip("ABCDEFGHIJ", (6, 45, 13, 30, 7, 30, 8, 12, 13, 8)):
            ws.column_dimensions[col].width = width
        out = io.BytesIO()
        wb.save(out)
        return Response(out.getvalue(),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="fernimmo-{_stamp()}.xlsx"'})

    def _stamp() -> str:
        return datetime.now(tz).strftime("%Y-%m-%d_%H%M")

    # ---------------------------------------------------------------------- API
    @app.get("/api/listings")
    def api_listings(f: Filters = Depends(), user: str = Depends(auth)):
        rows = db.search(**f.search_args(), user=user)
        for r in rows:
            r.pop("raw_text", None)
        return JSONResponse(rows)

    @app.get("/api/listings/{lid}/changes")
    def api_changes(lid: int, user: str = Depends(auth)):
        _require(lid)
        return JSONResponse(db.changes(lid))

    @app.get("/api/map")
    def api_map(f: Filters = Depends(), user: str = Depends(auth)):
        rows = db.search(**f.search_args(), user=user, limit=2000)
        return JSONResponse({"points": db.map_points(rows), "without_location": sum(1 for r in rows if r["lat"] is None)})

    @app.get("/api/areas")
    def api_areas(search: int | None = None, user: str = Depends(auth)):
        """Flächen einer Suche (search=ID) bzw. aller aktiven Suchen, dazu die Fernwärme-Gebiete."""
        active = [x for x in db.searches() if x["active"] and (search is None or x["id"] == search)]
        ids = {x["id"] for x in active}
        areas = [a for a in db.areas() if a["kind"] == "fw" or a["search_id"] in ids]
        plz = sorted({p for x in active for p in x["plz"]})
        return JSONResponse({"plz": plz, "areas": areas, "reprocessing": reproc.busy.is_set()})

    def _require_search(sid) -> dict:
        try:
            srch = db.get_search(int(sid))
        except (TypeError, ValueError):
            srch = None
        if not srch:
            raise HTTPException(400, "Unbekannte Suche")
        return srch

    @app.get("/api/searches")
    def api_searches(user: str = Depends(auth)):
        return JSONResponse(search_list())

    @app.post("/api/searches")
    def api_add_search(payload: dict = Body(...), user: str = Depends(auth)):
        name = str(payload.get("name") or "").strip()[:80] or "Neue Suche"
        src = _require_search(payload["copy_from"]) if payload.get("copy_from") else None
        crit = dict(src["criteria"]) if src else dict(DEFAULT_CRITERIA)
        sid = db.add_search(name, crit, list(src["plz"]) if src else [], user)
        if src:
            db.copy_search_areas(src["id"], sid)
        _changed()
        return {"id": sid}

    @app.patch("/api/searches/{sid}")
    def api_update_search(sid: int, payload: dict = Body(...), user: str = Depends(auth)):
        srch = _require_search(sid)
        fields: dict = {}
        if "name" in payload:
            name = str(payload["name"] or "").strip()[:80]
            if not name:
                raise HTTPException(400, "Name fehlt")
            fields["name"] = name
        for k in ("active", "notify"):
            if k in payload:
                fields[k] = 1 if payload[k] else 0
        if "criteria" in payload:
            try:
                fields["criteria"] = clean_criteria(payload["criteria"] or {}, srch["criteria"])
            except ValueError as e:
                raise HTTPException(400, str(e)) from None
        if "plz" in payload:
            # ganze PLZ oder PLZ-Anfänge (1–5 Ziffern); was ein anderer Anfang schon abdeckt, entfällt
            raw = {str(p) for p in payload.get("plz") or [] if str(p).isdigit() and 1 <= len(str(p)) <= 5}
            fields["plz"] = sorted(p for p in raw if not any(p != q and p.startswith(q) for q in raw))
        db.update_search(sid, **fields)
        if set(fields) - {"name"}:
            _changed()
        return db.get_search(sid)

    @app.delete("/api/searches/{sid}")
    def api_delete_search(sid: int, user: str = Depends(auth)):
        _require_search(sid)
        if len(db.searches()) <= 1:
            raise HTTPException(400, "Die letzte Suche kann nicht gelöscht werden")
        db.delete_search(sid)
        _changed()
        return {"ok": True}

    def _changed() -> None:
        reproc.trigger(wait=sync_reprocess)

    @app.post("/api/areas")
    def api_add_area(payload: dict = Body(...), user: str = Depends(auth)):
        kind, geom = payload.get("kind"), payload.get("geometry")
        center, radius = payload.get("center"), payload.get("radius_km")
        if kind not in ("search", "fw"):
            raise HTTPException(400, "Ungültiges Gebiet")
        search_id = _require_search(payload.get("search_id"))["id"] if kind == "search" else None
        if center is not None or radius is not None:
            # Umkreissuche: Mittelpunkt + Radius; das Polygon dient nur der Anzeige/PLZ-Überschneidung
            try:
                lat, lon, radius = float(center[0]), float(center[1]), float(radius)
            except (TypeError, ValueError, IndexError):
                raise HTTPException(400, "Ungültiger Umkreis")
            if not (47 <= lat <= 55.5 and 5 <= lon <= 16 and 0.1 <= radius <= 100):
                raise HTTPException(400, "Ungültiger Umkreis")
            center, geom = (round(lat, 6), round(lon, 6)), circle_polygon(lat, lon, radius)
        elif not valid_geometry(geom):
            raise HTTPException(400, "Ungültiges Gebiet")
        default = f"{radius:g} km Umkreis".replace(".", ",") if radius else ("Suchgebiet" if kind == "search" else "Fernwärme")
        aid = db.add_area(kind, str(payload.get("name") or default), geom, user,
                          center=center, radius_km=radius, search_id=search_id)
        _changed()
        return {"id": aid}

    @app.patch("/api/areas/{aid}")
    def api_rename_area(aid: int, payload: dict = Body(...), user: str = Depends(auth)):
        db.rename_area(aid, str(payload.get("name") or ""))
        return {"ok": True}

    @app.delete("/api/areas/{aid}")
    def api_delete_area(aid: int, user: str = Depends(auth)):
        if not db.delete_area(aid):
            raise HTTPException(404)
        _changed()
        return {"ok": True}

    geocoder = Geocoder(db, cfg.geocoding)

    @app.get("/api/geocode")
    def api_geocode(q: str, user: str = Depends(auth)):
        """Ort für die Umkreissuche: PLZ (offline) oder Adresse/Ort über OpenStreetMap."""
        q = q.strip()[:200]
        if q.isdigit() and len(q) == 5 and plz_center(q):
            lat, lon = plz_center(q)
            return {"lat": lat, "lon": lon, "label": f"PLZ {q} (Mittelpunkt)"}
        city = cfg.geocoding.get("city_hint", "")
        point = (geocoder.geocode(f"{q}, {city}") if city and "," not in q and city.lower() not in q.lower()
                 else None) or geocoder.geocode(q)
        if not point:
            raise HTTPException(404, "Ort nicht gefunden")
        return {"lat": point[0], "lon": point[1], "label": q}

    @app.get("/plz/{name}")
    def plz_file(name: str):
        """PLZ-Gebiete (gzip, vom Browser selbst entpackt): index.json oder 90.geojson usw."""
        import re as _re
        if not _re.fullmatch(r"(index\.json|\d{2}\.geojson)", name):
            raise HTTPException(404)
        path = PLZ_DIR / f"{name}.gz"
        if not path.exists():
            raise HTTPException(404)
        return Response(path.read_bytes(), media_type="application/json",
                        headers={"Content-Encoding": "gzip", "Cache-Control": "public, max-age=86400"})

    @app.get("/health")
    def health():
        return {"ok": True}

    return app
