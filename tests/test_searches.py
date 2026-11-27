"""Mehrere benannte Suchen."""
from fastapi.testclient import TestClient

from fernimmo import pipeline
from fernimmo.db import DB
from fernimmo.searches import clean_criteria, ensure_searches, load_searches
from fernimmo.web import create_app

from test_pipeline import FakeFetcher, FakeNotifier, make_cfg, pages


def test_migration_from_config_and_old_settings(tmp_path):
    cfg = make_cfg(tmp_path)
    db = DB(cfg.db_path)
    # Stand der Vorversion: Kriterien/PLZ als Einstellung, Fläche ohne Suche
    db.set_setting("criteria", {"rooms_min": 1.5, "rooms_max": 2.5, "price_max": None, "area_min": None})
    db.set_setting("search_plz", ["904"])
    sq = {"type": "Polygon", "coordinates": [[[11.0, 49.4], [11.1, 49.4], [11.1, 49.5], [11.0, 49.4]]]}
    db.add_area("search", "Alt", sq, "ich")
    ensure_searches(cfg, db)
    (s,) = db.searches()
    assert s["name"] == "fw" and s["plz"] == ["904"]
    assert s["criteria"]["rooms_min"] == 1.5 and s["criteria"]["heating_required"] == ["fernwaerme"]
    assert db.areas("search", search_id=s["id"])[0]["name"] == "Alt"
    ensure_searches(cfg, db)                      # zweiter Aufruf legt nichts doppelt an
    assert len(db.searches()) == 1


def test_clean_criteria():
    c = clean_criteria({"listing_types": ["kauf", "xyz"], "rooms_min": "1,5", "heating": "fern_nah",
                        "exclude_flags": ["erbpacht", "quatsch"], "exclude_keywords": "a, b,,"})
    assert c["listing_types"] == ["kauf"] and c["rooms_min"] == 1.5
    assert c["heating_required"] == ["fernwaerme", "nahwaerme"]
    assert c["exclude_flags"] == ["erbpacht"] and c["exclude_keywords"] == ["a", "b"]
    for bad in ({"listing_types": []}, {"rooms_min": "x"}, {"heating": "kohle"}, {"rooms_min": 3, "rooms_max": 2}):
        try:
            clean_criteria(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_multiple_searches_end_to_end(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, first_run=True)
    fake = FakeFetcher(pages())
    monkeypatch.setattr(pipeline, "make_fetcher", lambda *a, **k: fake)
    db, notifier = DB(cfg.db_path), FakeNotifier()
    c = TestClient(create_app(cfg, db, sync_reprocess=True))
    a = db.searches()[0]["id"]
    c.patch(f"/api/searches/{a}", json={"name": "Nürnberg Fernwärme", "plz": ["904"]})
    # Berlin: passt nicht zu den Nürnberger Inseraten
    b = c.post("/api/searches", json={"name": "Berlin"}).json()["id"]
    c.patch(f"/api/searches/{b}", json={"plz": ["10"], "criteria": {"heating": "egal"}})
    # Familie: 3 Zimmer, Heizung egal, ohne Benachrichtigung
    f = c.post("/api/searches", json={"name": "Familie", "copy_from": a}).json()["id"]
    c.patch(f"/api/searches/{f}", json={"notify": False, "criteria": {"rooms_min": 3, "rooms_max": 3.5,
                                                                       "heating": "egal"}})
    assert db.get_search(f)["plz"] == ["904"]                      # Gebiet mitkopiert

    pipeline.run_once(cfg, db, notifier)
    rows = {r["source_id"]: r for r in db.search(match=None)}
    assert rows["111"]["matched_profiles"] == ["Nürnberg Fernwärme"]
    assert rows["333"]["matched_profiles"] == ["Familie"] and rows["333"]["match_level"] == "voll"
    assert rows["222"]["match_level"] == "none"
    # Meldung nur für die Suche mit Benachrichtigung, mit Suchname
    assert len(notifier.msgs) == 1 and "Suche: Nürnberg Fernwärme" in notifier.msgs[0][1]
    # Filter nach Suche
    assert [r["source_id"] for r in db.search(search_id=f, match=None)] == ["333"]
    assert db.search(search_id=b, match=None) == []
    page = c.get(f"/liste?search={f}&match=").text
    assert "3 Zimmer Familienwohnung" in page and "Helle 2-Zi-ETW" not in page

    # Pausieren nimmt die Treffer der Suche weg
    c.patch(f"/api/searches/{f}", json={"active": False})
    assert db.get(rows["333"]["id"])["match_level"] == "none"
    assert len(load_searches(db)) == 2

    # Löschen entfernt die Flächen der Suche, die letzte Suche bleibt
    sq = {"type": "Polygon", "coordinates": [[[13.3, 52.5], [13.4, 52.5], [13.4, 52.6], [13.3, 52.5]]]}
    c.post("/api/areas", json={"kind": "search", "geometry": sq, "search_id": b})
    assert c.delete(f"/api/searches/{b}").status_code == 200
    assert db.areas("search", search_id=b) == []
    c.delete(f"/api/searches/{f}")
    assert c.delete(f"/api/searches/{a}").status_code == 400
    assert c.patch(f"/api/searches/{a}", json={"name": "  "}).status_code == 400
    assert c.patch("/api/searches/999", json={"name": "x"}).status_code == 400
    assert "Nürnberg Fernwärme" in c.get("/suche").text


def test_market_page_and_rent_estimate(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(pipeline, "make_fetcher", lambda *a, **k: FakeFetcher(pages()))
    db = DB(cfg.db_path)
    pipeline.run_once(cfg, db, FakeNotifier())
    assert db.rent_estimate("90443") is None                     # noch keine Mietinserate
    # drei Mietinserate in 90443 -> Vergleichsmiete aus echten Daten
    from fernimmo.models import Listing
    for i, (rent, area) in enumerate([(600, 50), (700, 55), (800, 60)]):
        db.upsert(Listing(None, "test", f"m{i}", f"u{i}", "Miete", "90443 Nürnberg", "miete",
                          {"rent_cold": rent, "area": area, "plz": "90443"}), {}, [])
    est = db.rent_estimate("90443")
    assert est and est[0] == round(700 / 55, 2) and "3 Mietinseraten" in est[1]
    c = TestClient(create_app(cfg, db))
    row = next(r for r in db.search(match=None) if r["source_id"] == "111")
    det = c.get(f"/listing/{row['id']}").text
    assert "Mietinserate" in det
    t = {r["plz"]: r for r in db.market_table()}["90443"]
    assert t["n_kauf"] == 3 and t["n_miete"] == 3 and t["rendite"]
    page = c.get("/markt?plz=90443").text
    assert "Nach PLZ" in page and "90443" in page
    assert c.get("/markt?plz=abc&art=x").status_code == 200
    assert len(db.price_trend("kauf", "904")) == 1
