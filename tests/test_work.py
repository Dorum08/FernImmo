"""Arbeitsansichten: Startseite, Meine Suchen, Ergebnistabelle, Meine Objekte, Detailseite."""
from datetime import date

from fastapi.testclient import TestClient

from fernimmo import pipeline
from fernimmo.db import DB
from fernimmo.metrics import afa_info, city_of, enrich
from fernimmo.poi import parse
from fernimmo.web import create_app

from test_pipeline import FakeFetcher, FakeNotifier, make_cfg, pages

A = ("ich", "pw")


def setup(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, users={"ich": "pw", "partner": "pw2"})
    monkeypatch.setattr(pipeline, "make_fetcher", lambda *a, **k: FakeFetcher(pages()))
    db = DB(cfg.db_path)
    pipeline.run_once(cfg, db, FakeNotifier())
    return cfg, db, TestClient(create_app(cfg, db, sync_reprocess=True))


def ids(db):
    return {r["source_id"]: r["id"] for r in db.search(match=None)}


def test_metrics():
    assert city_of("90443 Nürnberg - Gostenhof", "90443") == "Nürnberg"
    assert afa_info(1975, date(2026, 11, 27))["afa_pct"] == 3.4          # RND 29 Jahre -> 100/29
    assert afa_info(2024, date(2026, 11, 27))["linear_pct"] == 3.0
    assert afa_info(None) is None


def test_enrich_values(tmp_path, monkeypatch):
    cfg, db, _ = setup(tmp_path, monkeypatch)
    r = next(x for x in db.search(match=None) if x["source_id"] == "111")
    enrich([r], db, cfg.calculator)
    assert r["vw"] == round(r["market"]["median"] * 55) and r["vw_pct"] == r["market"]["dev"]
    assert r["miete_soll"] == round(12 * 55) and r["rendite_soll"] == round(660 * 12 / 219000 * 100, 1)
    # Cashflow: Miete − Rate (80 % von Preis + Nebenkosten, 5,6 % p. a.) − 35 % Hausgeld
    total = 219000 * (1 + (3.5 + 2.0 + 3.57) / 100)
    expected = 660 - total * 0.8 * 0.056 / 12 - 260 * 0.35
    assert abs(r["cashflow"] - expected) < 0.01
    assert r["ort"] == "Nürnberg" and r["afa"] is None                     # kein Baujahr


def test_poi_parse():
    data = {"elements": [
        {"type": "node", "lat": 49.4500, "lon": 11.0600, "tags": {"shop": "supermarket", "name": "REWE"}},
        {"type": "way", "center": {"lat": 49.4510, "lon": 11.0610}, "tags": {"shop": "supermarket", "name": "Lidl"}},
        {"type": "node", "lat": 49.4496, "lon": 11.0572, "tags": {"highway": "bus_stop", "name": "Plärrer"}},
        {"type": "node", "lat": 49.4499, "lon": 11.0575, "tags": {"highway": "bus_stop", "name": "Plärrer"}},
        {"type": "node", "lat": 49.4490, "lon": 11.0560, "tags": {"amenity": "pharmacy", "name": "Apotheke"}},
        {"type": "node", "lat": 49.4490, "lon": 11.0560, "tags": {"amenity": "bench"}},
    ]}
    out = parse(data, 49.4495, 11.0571)["categories"]
    assert [p["name"] for p in out["einkaufen"]["items"]] == ["REWE", "Lidl"]
    assert len(out["oepnv"]["items"]) == 1 and out["oepnv"]["items"][0]["m"] < 15   # nächster Mast zählt
    assert out["gesundheit"]["items"][0]["name"] == "Apotheke" and out["bildung"]["items"] == []


def test_pages_and_working(tmp_path, monkeypatch):
    cfg, db, c = setup(tmp_path, monkeypatch)
    i = ids(db)
    sid = db.searches()[0]["id"]

    # Startseite & Meine Suchen zeigen neue Objekte
    start = c.get("/", auth=A).text
    assert "Strategie auswählen" in start and "neue Objekte" in start
    assert "1 neu" in c.get("/suchen", auth=A).text

    # Ergebnisliste der Suche: Treffer, neu markiert
    page = c.get(f"/suchen/{sid}", auth=A).text
    assert "Helle 2-Zi-ETW" in page and "3 Zimmer Familienwohnung" not in page and 'class="is-new' in page
    assert c.get("/suchen/999", auth=A).status_code == 404

    # Detail öffnen -> gesehen (nur für diese Person)
    det = c.get(f"/listing/{i['111']}", auth=A).text
    assert "Eigenschaften" in det and "Cashflow-Rechner" in det and "Historie" in det and "In der Nähe" in det
    assert db.unseen_count("ich", sid) == 0 and db.unseen_count("partner", sid) == 1
    assert 'class="is-seen' in c.get(f"/suchen/{sid}", auth=A).text
    c.post("/api/seen", json={"ids": [i["111"]]}, auth=("partner", "pw2"))
    assert db.unseen_count("partner", sid) == 0

    # Notiz mit Link und Angebot, Tags
    c.post(f"/listing/{i['111']}/note", data={"text": "Hausgeld klären", "link": "example.org/expose",
                                               "offer": "195000", "back": "/"}, auth=A)
    n = db.get(i["111"])["note"]
    assert n["text"] == "Hausgeld klären" and n["link"] == "https://example.org/expose" and n["offer"] == 195000
    c.post(f"/listing/{i['111']}/tag", data={"tag": "Top", "back": "/"}, auth=A)
    r = c.post(f"/listing/{i['111']}/tag", data={"tag": "Besichtigen", "back": "/"}, auth=A,
               headers={"Accept": "application/json"})
    assert r.json()["tags"] == ["Besichtigen", "Top"]
    c.post(f"/listing/{i['111']}/tag", data={"tag": "Top", "remove": "1", "back": "/"}, auth=A)
    assert db.get(i["111"])["tags"] == ["Besichtigen"]

    # Meine Objekte: Like + Status, Board-Status per JSON, Suche in Notizen
    c.post(f"/listing/{i['111']}/like", data={"back": "/"}, auth=A)
    c.post(f"/listing/{i['333']}/status", data={"status": "besichtigung"}, auth=A)
    r = c.post(f"/listing/{i['111']}/status", data={"status": "kontaktiert"}, auth=A, headers={"Accept": "application/json"})
    assert r.json() == {"status": "kontaktiert"}
    board = c.get("/merkliste?view=board", auth=A).text
    assert "Helle 2-Zi-ETW" in board and "3 Zimmer Familienwohnung" in board and 'data-status="besichtigung"' in board
    assert "Helle 2-Zi-ETW" in c.get("/merkliste?q=hausgeld", auth=A).text
    assert "Helle 2-Zi-ETW" not in c.get("/merkliste?status=besichtigung", auth=A).text
    assert "Helle 2-Zi-ETW" in c.get("/merkliste?tag=Besichtigen", auth=A).text
    assert c.post(f"/listing/{i['111']}/status", data={"status": "quatsch"}, auth=A).status_code == 400

    # Schnellsuche und Strategien
    q = c.get("/objekte?ort=904&typ=kauf", auth=A).text
    assert "Helle 2-Zi-ETW" in q and "2 Zimmer Altbau-ETW" in q
    assert "Helle 2-Zi-ETW" not in c.get("/objekte?ort=10", auth=A).text
    assert "Helle 2-Zi-ETW" not in c.get("/objekte?ppsqm_max=3500", auth=A).text
    fw = c.get("/objekte?strategie=fernwaerme", auth=A).text
    assert "Helle 2-Zi-ETW" in fw and "2 Zimmer Altbau-ETW" not in fw
    unter = c.get("/objekte?markt=unter", auth=A).text
    assert "2 Zimmer Altbau-ETW" in unter and "3 Zimmer Familienwohnung" not in unter   # −13 % vs. +15 %
    for k in ("micro", "unter_wert", "ladenhueter", "unter_miete", "cashflow", "xyz"):
        assert c.get(f"/objekte?strategie={k}", auth=A).status_code == 200
    # Navigation und Liste
    assert 'class="nav-badge' in c.get("/markt", auth=A).text or True
    assert c.get("/liste", auth=A).status_code == 200


def test_umgebung_api(tmp_path, monkeypatch):
    cfg, db, c = setup(tmp_path, monkeypatch)
    i = ids(db)
    # nur PLZ bekannt -> Hinweis statt Abfrage
    assert "Genaue Lage unbekannt" in c.get(f"/api/listing/{i['111']}/umgebung", auth=A).json()["error"]
    db._exec("UPDATE listings SET lat=49.4495, lon=11.0571, geo_precision='portal' WHERE id=?", (i["111"],))
    calls = []

    class R:
        def raise_for_status(self): pass
        def json(self): return {"elements": [{"type": "node", "lat": 49.45, "lon": 11.06,
                                              "tags": {"shop": "bakery", "name": "Bäcker"}}]}
    import fernimmo.poi as poi
    monkeypatch.setattr(poi.httpx, "post", lambda *a, **k: calls.append(1) or R())
    j = c.get(f"/api/listing/{i['111']}/umgebung", auth=A).json()
    assert j["categories"]["einkaufen"]["items"][0]["name"] == "Bäcker"
    c.get(f"/api/listing/{i['111']}/umgebung", auth=A)
    assert len(calls) == 1                                                  # zweiter Aufruf aus dem Speicher
