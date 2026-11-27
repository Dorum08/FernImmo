"""End-to-End mit gefälschten Portalseiten (kein Netzwerk)."""
import pytest

from fernimmo import pipeline
from fernimmo.config import Config
from fernimmo.db import DB
from fernimmo.fetch import Fetcher, NotFoundError
from fernimmo.sources.kleinanzeigen import Kleinanzeigen

BASE = "https://www.kleinanzeigen.de"
SEARCH_URL = f"{BASE}/s-wohnung-kaufen/nuernberg/c196l976"
URL_FW = f"{BASE}/s-anzeige/2-zi-fernwaerme/111-196-1234"
URL_GAS = f"{BASE}/s-anzeige/2-zi-gas/222-196-1234"
URL_3ZI = f"{BASE}/s-anzeige/3-zi/333-196-1234"

SEARCH = f"""<html><body>
<article class="aditem" data-adid="111" data-href="{URL_FW[len(BASE):]}"></article>
<article class="aditem" data-adid="222" data-href="{URL_GAS[len(BASE):]}"></article>
<article class="aditem" data-adid="333" data-href="{URL_3ZI[len(BASE):]}"></article>
</body></html>"""


def detail(title, price, rooms, area, desc, heating=None, hausgeld=None):
    heat = (f'<li class="addetailslist--detail">Heizungsart<span class="addetailslist--detail--value">'
            f'{heating}</span></li>') if heating else ""
    hg = (f'<li class="addetailslist--detail">Hausgeld<span class="addetailslist--detail--value">'
          f'{hausgeld} €</span></li>') if hausgeld else ""
    return f"""<html><head><meta property="og:image" content="https://img/x.jpg"></head><body>
<h1 id="viewad-title">{title}</h1><h2 id="viewad-price">{price} € VB</h2>
<span id="viewad-locality">90443 Nürnberg - Gostenhof</span>
<ul><li class="addetailslist--detail">Wohnfläche<span class="addetailslist--detail--value">{area} m²</span></li>
<li class="addetailslist--detail">Zimmer<span class="addetailslist--detail--value">{rooms}</span></li>{heat}{hg}</ul>
<li class="checktag">Balkon</li>
<p id="viewad-description-text">{desc}</p></body></html>"""


def pages():
    return {
        SEARCH_URL: SEARCH,
        URL_FW: detail("Helle 2-Zi-ETW", "219.000", 2, 55, "Beheizung über Fernwärme der N-ERGIE.", hausgeld=260),
        URL_GAS: detail("2 Zimmer Altbau-ETW", "199.000", 2, 60, "Schöner Altbau.", heating="Gasetagenheizung"),
        URL_3ZI: detail("3 Zimmer Familienwohnung", "349.000", 3, 80, "Fernwärme."),
    }


class FakeFetcher(Fetcher):
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        page = self.pages.get(url)
        if page is None:
            raise NotFoundError(f"404 bei {url}")
        return page


class FakeNotifier:
    def __init__(self):
        self.msgs = []

    def send(self, subject, body, url=None, listing_id=None):
        self.msgs.append((subject, body))


def make_cfg(tmp_path, first_run=True, users=None):
    return Config({
        "db_path": str(tmp_path / "t.db"),
        "sources": {"kleinanzeigen": {"urls": [SEARCH_URL], "max_pages": 2, "refresh_hours": 0}},
        "profiles": [{"name": "fw", "listing_types": ["kauf"], "rooms_min": 1, "rooms_max": 2,
                      "heating_required": ["fernwaerme"]}],
        "notify": {"first_run": first_run, "levels": ["voll"]},
        "web": {"users": users or {}},
        "geocoding": {"enabled": False},
    })


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    fake = FakeFetcher(pages())
    monkeypatch.setattr(pipeline, "make_fetcher", lambda *a, **k: fake)
    return cfg, DB(cfg.db_path), FakeNotifier(), fake


def test_pagination_url():
    k = Kleinanzeigen({}, FakeFetcher({}))
    assert k.page_url(SEARCH_URL, 3) == f"{BASE}/s-wohnung-kaufen/nuernberg/seite:3/c196l976"


def test_run_end_to_end(env):
    cfg, db, notifier, fake = env
    res = pipeline.run_once(cfg, db, notifier)
    assert res["kleinanzeigen"]["error"] is None
    assert res["kleinanzeigen"]["found"] == 3
    assert res["kleinanzeigen"]["new"] == 3

    rows = {r["source_id"]: r for r in db.search(match=None)}
    fw = rows["111"]
    assert fw["match_level"] == "voll"
    assert fw["fernwaerme"] == "ja"
    assert fw["listing_type"] == "kauf"
    assert fw["price"] == 219000
    assert fw["data"]["purchase_price"] == 219000
    assert fw["data"]["hausgeld"] == 260
    assert rows["222"]["match_level"] == "none"
    assert rows["222"]["fernwaerme"] == "nein"
    assert rows["333"]["match_level"] == "none"
    assert len(notifier.msgs) == 1
    assert "Helle 2-Zi-ETW" in notifier.msgs[0][1]
    assert f"#{fw['id']}" in notifier.msgs[0][1]
    # Erster Eintrag im Verlauf
    ch = db.changes(fw["id"])
    assert len(ch) == 1 and ch[0]["field"] == "created" and "219.000" in ch[0]["new"]


def test_change_history_and_price_drop(env):
    cfg, db, notifier, fake = env
    pipeline.run_once(cfg, db, notifier)
    lid = next(r["id"] for r in db.search(match=None) if r["source_id"] == "111")

    # Anbieter senkt den Preis, ändert Hausgeld und Beschreibung
    fake.pages[URL_FW] = detail("Helle 2-Zi-ETW", "205.000", 2, 55,
                                "Beheizung über Fernwärme der N-ERGIE. Neu: Fenster 2025 getauscht.", hausgeld=275)
    pipeline.run_once(cfg, db, notifier)

    changes = db.changes(lid)
    by_field = {c["field"]: c for c in changes}
    assert by_field["purchase_price"]["old"] == "219.000 €"
    assert by_field["purchase_price"]["new"] == "205.000 €"
    assert by_field["purchase_price"]["direction"] == "down"
    assert by_field["hausgeld"]["direction"] == "up"
    assert "Fenster 2025" in by_field["raw_text"]["new"]
    assert all(c["ts"] for c in changes)
    assert "Preis gesenkt" in notifier.msgs[-1][1]
    assert [h["price"] for h in db.price_history(lid)] == [219000, 205000]

    # Unverändert neu laden -> keine neuen Einträge
    n = len(db.changes(lid))
    pipeline.run_once(cfg, db, notifier)
    assert len(db.changes(lid)) == n

    # Inserat wird gelöscht -> offline im Verlauf
    del fake.pages[URL_FW]
    fake.pages[SEARCH_URL] = SEARCH.replace('data-adid="111"', 'data-adid="999"')
    pipeline.run_once(cfg, db, notifier)
    assert db.get(lid)["active"] == 0
    assert db.changes(lid)[0]["field"] == "offline"


def test_reprocess_does_not_log_changes(env):
    cfg, db, notifier, _ = env
    pipeline.run_once(cfg, db, notifier)
    lid = next(r["id"] for r in db.search(match=None) if r["source_id"] == "111")
    n = len(db.changes(lid))
    pipeline.reprocess(cfg, db)
    assert len(db.changes(lid)) == n


def test_first_run_summary(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, first_run=False)
    monkeypatch.setattr(pipeline, "make_fetcher", lambda *a, **k: FakeFetcher(pages()))
    notifier = FakeNotifier()
    pipeline.run_once(cfg, DB(cfg.db_path), notifier)
    assert len(notifier.msgs) == 1 and "erster" in notifier.msgs[0][0].lower()


def test_likes_and_comments(env):
    cfg, db, notifier, _ = env
    pipeline.run_once(cfg, db, notifier)
    lid = next(r["id"] for r in db.search(match=None) if r["source_id"] == "111")
    assert db.toggle_like(lid, "anna") is True
    assert db.toggle_like(lid, "ben") is True
    assert db.toggle_like(lid, "ben") is False
    cid = db.add_comment(lid, "anna", "Besichtigung anfragen!")
    row = db.get(lid, "anna")
    assert row["likes"] == ["anna"] and row["liked"] is True
    assert row["comments"][0]["text"] == "Besichtigung anfragen!"
    assert db.delete_comment(cid, "ben") is False      # fremden Kommentar nicht löschbar
    assert db.delete_comment(cid, "anna") is True
    assert db.search(liked=True)[0]["id"] == lid
    assert db.search(q=f"#{lid}", match="voll")[0]["id"] == lid


def test_web(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from fernimmo.web import create_app
    cfg = make_cfg(tmp_path, users={"anna": "geheim", "ben": "auch-geheim", "leer": ""})
    monkeypatch.setattr(pipeline, "make_fetcher", lambda *a, **k: FakeFetcher(pages()))
    db = DB(cfg.db_path)
    pipeline.run_once(cfg, db, FakeNotifier())
    c = TestClient(create_app(cfg, db))
    A, B = ("anna", "geheim"), ("ben", "auch-geheim")

    assert c.get("/").status_code == 401
    assert c.get("/", auth=("leer", "")).status_code == 401
    assert c.get("/", auth=("anna", "falsch")).status_code == 401
    r = c.get("/liste", auth=A)
    assert r.status_code == 200 and "Helle 2-Zi-ETW" in r.text and "3 Zimmer" not in r.text
    lid = next(r["id"] for r in db.search(match=None) if r["source_id"] == "111")
    assert f"#{lid}" in r.text

    # Like per Formular und per JSON
    r = c.post(f"/listing/{lid}/like", data={"back": "/?match=voll"}, auth=A, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/?match=voll#l{lid}"
    r = c.post(f"/listing/{lid}/like", data={"back": "/"}, auth=B, headers={"Accept": "application/json"})
    assert r.json() == {"liked": True, "likes": ["anna", "ben"]}
    # offene Weiterleitung verhindern
    r = c.post(f"/listing/{lid}/like", data={"back": "//evil.example"}, auth=B, follow_redirects=False)
    assert r.headers["location"].startswith("/#")

    # Kommentar auf der Übersicht sichtbar, mit Autor
    c.post(f"/listing/{lid}/comment", data={"text": "Lage top, <b>Preis</b> verhandeln", "back": "/"}, auth=B)
    page = c.get("/liste", auth=A).text
    assert "Lage top, &lt;b&gt;Preis&lt;/b&gt; verhandeln" in page   # HTML wird escaped
    assert "1 Kommentar" in page
    cid = db.get(lid)["comments"][0]["id"]
    c.post(f"/comment/{cid}/delete", data={"lid": lid, "back": "/"}, auth=A)   # Anna darf Bens nicht löschen
    assert len(db.get(lid)["comments"]) == 1

    det = c.get(f"/listing/{lid}", auth=A).text
    assert "Historie" in det and "erstmals gefunden" in det and "Fernwärme" in det
    r = c.post(f"/listing/{lid}/status", data={"status": "favorit"}, auth=A, follow_redirects=False)
    assert r.status_code == 303 and db.get(lid)["status"] == "favorit"
    assert c.get("/runs", auth=A).status_code == 200
    assert c.get(f"/api/listings/{lid}/changes", auth=A).json()[0]["field"] == "created"
    assert c.post("/listing/99999/like", data={"back": "/"}, auth=A).status_code == 404


def test_relisting_detected(env):
    cfg, db, notifier, fake = env
    pipeline.run_once(cfg, db, notifier)
    old_id = next(r["id"] for r in db.search(match=None) if r["source_id"] == "111")
    # Inserat verschwindet ...
    del fake.pages[URL_FW]
    fake.pages[SEARCH_URL] = SEARCH.replace('data-adid="111"', 'data-adid="999"')
    pipeline.run_once(cfg, db, notifier)
    assert db.get(old_id)["active"] == 0
    # ... und taucht unter neuer Anzeigen-ID günstiger wieder auf
    url_new = f"{BASE}/s-anzeige/2-zi-fernwaerme-neu/555-196-1234"
    fake.pages[url_new] = detail("2-Zi-ETW mit Fernwärme – jetzt günstiger", "199.000", 2, 55,
                                 "Fernwärme, frisch gestrichen.", hausgeld=260)
    fake.pages[SEARCH_URL] += f'<article class="aditem" data-adid="555" data-href="{url_new[len(BASE):]}"></article>'
    n = len(notifier.msgs)
    pipeline.run_once(cfg, db, notifier)
    new = next(r for r in db.search() if r["source_id"] == "555")
    assert new["relisted_of"] == old_id
    assert new["predecessors"] == [old_id]
    assert new["first_price"] == 219000 and new["price_drops"] == 1
    assert new["price_delta_pct"] == round((199000 / 219000 - 1) * 100, 1)
    fields = [c["field"] for c in db.changes(new["id"])]
    assert "relisted" in fields and "created" in fields and "offline" in fields   # inkl. Vorgänger
    assert any(c["field"] == "relisted_as" for c in db.changes(old_id, with_predecessors=False))
    assert "Wieder eingestellt" in notifier.msgs[n][1] and f"#{old_id}" in notifier.msgs[n][1]


def test_market_compare(env):
    cfg, db, notifier, _ = env
    pipeline.run_once(cfg, db, notifier)
    stats = db.market_stats()
    med, n = stats[("kauf", "90443")]
    assert n == 3
    assert round(med) == round(219000 / 55)          # Median von 3982, 3317, 4363
    fw = next(r for r in db.search() if r["source_id"] == "111")
    assert fw["market"]["dev"] == 0 and fw["market"]["label"] == "PLZ 90443"


def test_area_api_map_and_export(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from openpyxl import load_workbook
    import io

    from fernimmo.web import create_app
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(pipeline, "make_fetcher", lambda *a, **k: FakeFetcher(pages()))
    db = DB(cfg.db_path)
    pipeline.run_once(cfg, db, FakeNotifier())
    c = TestClient(create_app(cfg, db, sync_reprocess=True))
    lid = next(r["id"] for r in db.search(match=None) if r["source_id"] == "111")
    assert db.get(lid)["match_level"] == "voll"
    assert db.get(lid)["geo_precision"] == "plz" and db.get(lid)["plz"] == "90443"

    # Suchgebiet ohne 90443 -> kein Treffer mehr
    sid = db.searches()[0]["id"]
    r = c.patch(f"/api/searches/{sid}", json={"plz": ["90402", "90403", "kaputt"]})
    assert r.json()["plz"] == ["90402", "90403"]
    assert db.get(lid)["match_level"] == "none"
    # Freihand-Fläche, die 90443 (Gostenhof) überdeckt -> Lage unklar, aber möglicher Treffer
    square = {"type": "Polygon", "coordinates": [[[11.04, 49.44], [11.07, 49.44], [11.07, 49.46], [11.04, 49.46], [11.04, 49.44]]]}
    aid = c.post("/api/areas", json={"kind": "search", "name": "Gostenhof", "geometry": square,
                                     "search_id": sid}).json()["id"]
    assert db.get(lid)["match_level"] == "teilweise"
    assert "Lage unklar" in " ".join(db.get(lid)["match_reasons"])
    assert c.post("/api/areas", json={"kind": "search", "geometry": {"type": "Point"}, "search_id": sid}).status_code == 400
    assert c.post("/api/areas", json={"kind": "search", "geometry": square}).status_code == 400   # ohne Suche
    assert c.post("/api/areas", json={"kind": "xyz", "geometry": square}).status_code == 400
    areas = c.get(f"/api/areas?search={sid}").json()
    assert areas["plz"] == ["90402", "90403"] and areas["areas"][0]["name"] == "Gostenhof"
    c.patch(f"/api/areas/{aid}", json={"name": "Gostenhof/Bärenschanze"})
    assert db.areas()[0]["name"] == "Gostenhof/Bärenschanze"
    # PLZ wieder aufnehmen -> Volltreffer
    c.patch(f"/api/searches/{sid}", json={"plz": ["90443"]})
    assert db.get(lid)["match_level"] == "voll"
    assert c.delete(f"/api/areas/{aid}").status_code == 200
    assert c.delete(f"/api/areas/{aid}").status_code == 404

    # Seiten
    for url in ("/", "/karte", "/gebiet", "/suche", f"/listing/{lid}", "/static/leaflet/leaflet.js"):
        assert c.get(url).status_code == 200, url
    r = c.get("/plz/90.geojson")
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip"
    assert any(f["properties"]["plz"] == "90443" for f in r.json()["features"])
    assert "90443" in c.get("/plz/index.json").json()["plz"]
    assert c.get("/plz/../config.geojson").status_code == 404
    assert c.get("/plz/ab.geojson").status_code == 404
    det = c.get(f"/listing/{lid}").text
    assert "Cashflow-Rechner" in det and "Bewertung" in det and "minimap" in det
    pts = c.get("/api/map?match=").json()
    assert len(pts["points"]) == 3 and pts["points"][0]["precision"] == "plz"

    # Export
    csv_text = c.get("/export.csv?match=").content.decode("utf-8-sig")
    head, *lines = csv_text.strip().splitlines()
    assert head.startswith("ID;Titel;Quelle") and len(lines) == 3
    assert "219000" in csv_text
    wb = load_workbook(io.BytesIO(c.get("/export.xlsx?match=voll").content))
    ws = wb.active
    assert ws.cell(1, 1).value == "ID" and ws.max_row == 2
    assert ws.cell(2, 9).value == 219000 and ws.cell(2, 4).hyperlink is not None


def test_radius_search_web(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from fernimmo.web import create_app
    cfg = make_cfg(tmp_path)
    P = pages()
    # Wohnung 111 mit exakten Koordinaten (Gostenhof), 222 in der Südstadt
    P[URL_FW] = P[URL_FW].replace("<head>", '<head><meta property="og:latitude" content="49.4495">'
                                  '<meta property="og:longitude" content="11.0571">')
    monkeypatch.setattr(pipeline, "make_fetcher", lambda *a, **k: FakeFetcher(P))
    db = DB(cfg.db_path)
    pipeline.run_once(cfg, db, FakeNotifier())
    c = TestClient(create_app(cfg, db, sync_reprocess=True))
    fw = next(r for r in db.search(match=None) if r["source_id"] == "111")

    # Ort per PLZ suchen (offline aus den mitgelieferten PLZ-Daten)
    g = c.get("/api/geocode", params={"q": "90403"}).json()
    assert 49.44 < g["lat"] < 49.46 and "90403" in g["label"]

    # 1 km um den Plärrer: Gostenhof-Wohnung (ca. 0,6 km) liegt drin
    r = c.post("/api/areas", json={"kind": "search", "name": "Plärrer", "center": [49.4486, 11.0631], "radius_km": 1,
                                   "search_id": db.searches()[0]["id"]})
    assert r.status_code == 200
    area = db.areas("search")[0]
    assert area["radius_km"] == 1 and area["center_lat"] == 49.4486
    assert db.get(fw["id"])["match_level"] == "voll"
    page = c.get("/liste?sort=distance").text
    assert "km bis Plärrer" in page
    # Kleinerer Radius: 0,3 km -> außerhalb
    c.delete(f"/api/areas/{area['id']}")
    c.post("/api/areas", json={"kind": "search", "center": [49.4486, 11.0631], "radius_km": 0.3, "search_id": db.searches()[0]["id"]})
    assert db.get(fw["id"])["match_level"] == "none"
    assert db.areas("search")[0]["name"] == "0,3 km Umkreis"
    # Ungültige Eingaben
    assert c.post("/api/areas", json={"kind": "search", "center": [0, 0], "radius_km": 1, "search_id": db.searches()[0]["id"]}).status_code == 400
    assert c.post("/api/areas", json={"kind": "search", "center": [49.4, 11.0], "radius_km": 500, "search_id": db.searches()[0]["id"]}).status_code == 400
    assert c.post("/api/areas", json={"kind": "search", "center": "x", "radius_km": 1, "search_id": db.searches()[0]["id"]}).status_code == 400
    # Export enthält Entfernung
    assert "Entfernung km" in c.get("/export.csv?match=").content.decode("utf-8-sig").splitlines()[0]
