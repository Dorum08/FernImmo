"""Bundesweiter Kleinanzeigen-Feed: Vorsortierung nach Suchgebiet, Abbruch bei Bekanntem."""
from fernimmo import pipeline
from fernimmo.config import Config
from fernimmo.db import DB

from test_pipeline import BASE, FakeFetcher, FakeNotifier, detail

FEED = f"{BASE}/s-wohnung-kaufen/anzeige:angebote/c196"
FEED2 = f"{BASE}/s-wohnung-kaufen/anzeige:angebote/seite:2/c196"


def tile(aid, plz, ort):
    return (f'<article class="aditem" data-adid="{aid}" data-href="/s-anzeige/x/{aid}-196-1">'
            f'{plz} {ort} 2 Zi. 55 m² 199.000 € VB</article>')


def site():
    return {
        FEED: "<html><body>" + tile(1, "51147", "Köln") + tile(2, "90443", "Nürnberg") + "</body></html>",
        FEED2: "<html><body>" + tile(3, "20095", "Hamburg") + tile(4, "90402", "Nürnberg") + "</body></html>",
        f"{BASE}/s-anzeige/x/2-196-1": detail("Nbg A", "199.000", 2, 55, "Fernwärme."),
        f"{BASE}/s-anzeige/x/4-196-1": detail("Nbg B", "199.000", 2, 55, "Fernwärme."),
        f"{BASE}/s-anzeige/x/3-196-1": detail("HH", "199.000", 2, 55, "Fernwärme.").replace("90443 Nürnberg", "20095 Hamburg"),
    }


def test_feed(tmp_path, monkeypatch):
    cfg = Config({
        "db_path": str(tmp_path / "t.db"),
        "sources": {"kleinanzeigen": {"max_pages_feed": 3, "max_pages_first_run": 5}},   # keine URLs -> Feed
        "profiles": [{"name": "Nbg", "listing_types": ["kauf"], "plz": ["904"]}],
        "geocoding": {"enabled": False},
    })
    fake = FakeFetcher(site())
    monkeypatch.setattr(pipeline, "make_fetcher", lambda *a, **k: fake)
    db = DB(cfg.db_path)
    res = pipeline.run_once(cfg, db, FakeNotifier())["kleinanzeigen"]
    assert res["error"] is None and res["found"] == 4 and res["prefiltered"] == 2 and res["new"] == 2
    assert not any("/1-196" in u or "/3-196" in u for u in fake.calls)            # Köln/Hamburg nie geladen
    assert {r["title"] for r in db.search(match="treffer")} == {"Nbg A", "Nbg B"}
    assert db.recent_runs(1)[0]["note"] is None                                   # beim Feed keine Ortswarnung

    # Zweiter Lauf: Seite 1 enthält nur Bekanntes/Aussortiertes -> nicht weiterblättern
    fake.calls.clear()
    res = pipeline.run_once(cfg, db, FakeNotifier())["kleinanzeigen"]
    assert fake.calls == [FEED] and res["new"] == 0
    # Feed-Inserate werden nicht nach Tagen offline gesetzt, nur weil sie nicht mehr im Feed stehen
    db._exec("UPDATE listings SET last_seen='2000-01-01T00:00:00+00:00'")
    pipeline.run_once(cfg, db, FakeNotifier())
    assert all(r["active"] for r in db.search(match=None))

    # Suche auf Hamburg erweitert -> Aussortierte werden neu geprüft
    s = db.searches()[0]
    db.update_search(s["id"], plz=["904", "200"])
    fake.calls.clear()
    res = pipeline.run_once(cfg, db, FakeNotifier())["kleinanzeigen"]
    assert FEED2 in fake.calls and res["new"] == 1
    assert "HH" in {r["title"] for r in db.search(match="treffer")}
