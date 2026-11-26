"""Fehler aus der Konsistenzprüfung – sollen nicht wiederkommen."""
import pytest

from fernimmo import pipeline
from fernimmo.db import DB
from fernimmo.extract import extract
from fernimmo.fetch import BlockedError, _check_blocked
from fernimmo.geo import AreaFilter, Geocoder
from fernimmo.sources.base import SearchHit

from test_pipeline import URL_GAS, FakeFetcher, FakeNotifier, detail, make_cfg, pages


def test_price_drop_reported_for_liked_listing_without_match(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    fake = FakeFetcher(pages())
    monkeypatch.setattr(pipeline, "make_fetcher", lambda *a, **k: fake)
    db, notifier = DB(cfg.db_path), FakeNotifier()
    pipeline.run_once(cfg, db, notifier)
    gas = next(r for r in db.search(match=None) if r["source_id"] == "222")
    assert gas["match_level"] == "none"
    db.toggle_like(gas["id"], "ich")
    n = len(notifier.msgs)
    fake.pages[URL_GAS] = detail("2 Zimmer Altbau-ETW", "185.000", 2, 60, "Schöner Altbau.", heating="Gasetagenheizung")
    pipeline.run_once(cfg, db, notifier)
    assert len(notifier.msgs) == n + 1 and "Preis gesenkt" in notifier.msgs[-1][1]


def test_recaptcha_on_normal_page_is_not_a_block():
    _check_blocked("u", 200, '<html><script src="https://www.google.com/recaptcha/api.js"></script>Exposé</html>')
    with pytest.raises(BlockedError):
        _check_blocked("u", 200, "<html><title>Ich bin kein Roboter - ImmobilienScout24</title></html>")
    with pytest.raises(BlockedError):
        _check_blocked("u", 403, "")


class _DB:
    def __init__(self):
        self.cache = {}

    def geocache_get(self, q):
        return self.cache.get(q)

    def geocache_put(self, q, p):
        self.cache[q] = p or ()


def test_geocoding_needs_place_and_plausible_result(monkeypatch):
    g = Geocoder(_DB(), {"enabled": True})
    asked = []

    def fake(q):
        asked.append(q)
        return (52.52, 13.40)      # Berlin – egal was gefragt wird
    monkeypatch.setattr(g, "geocode", fake)
    # Straße ohne PLZ/Ort: nicht nachschlagen (mehrdeutig), keine Lage
    assert g.locate("Hauptstraße 5", None, None) == (None, None, None) and asked == []
    # Straße + Nürnberger PLZ, Ergebnis liegt aber in Berlin -> verwerfen, PLZ-Mittelpunkt nehmen
    lat, lon, prec = g.locate("Hauptstraße 5", "90443", None)
    assert prec == "plz" and 49.3 < lat < 49.6 and asked == ["Hauptstraße 5, 90443"]
    # Passendes Ergebnis wird übernommen
    monkeypatch.setattr(g, "geocode", lambda q: (49.4495, 11.0571))
    assert g.locate("Fürther Straße 12", "90443", None)[2] == "adresse"


def test_prefilter_ignores_ambiguous_snippets():
    from fernimmo.searches import DEFAULT_CRITERIA, Search, profile_for
    c = {**DEFAULT_CRITERIA, "rooms_min": 1, "rooms_max": 2, "heating_required": []}
    searches = [Search(1, "s", True, True, c, [], profile_for("s", c), AreaFilter())]
    hit = lambda s: SearchHit("1", "u", "kauf", snippet=s)
    assert pipeline.prefiltered(hit("Schöne ETW 4 Zimmer 98 m² 90443 Nürnberg"), searches)
    # Kachel nennt 2 und 3 Zimmer -> nicht aussortieren
    assert not pipeline.prefiltered(hit("2 Zimmer ETW, im Haus auch 3 Zimmer frei"), searches)
    assert not pipeline.prefiltered(hit("4 Zimmer"), [])     # ohne Suche nie aussortieren
    assert not pipeline.prefiltered(hit(""), searches)


def test_ein_zimmer_in_prose_is_not_room_count():
    assert extract("Schöne Wohnung, ein Zimmer mit Balkon zum Hof.", title="ETW im Altbau")["rooms"] is None
    assert extract("", title="Einzimmerwohnung in Gostenhof")["rooms"] == 1
    assert extract("", title="Ein-Zimmer-Apartment")["rooms"] == 1


def test_source_down_alert_and_recovery(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    fake = FakeFetcher(pages())
    monkeypatch.setattr(pipeline, "make_fetcher", lambda *a, **k: fake)
    db, notifier = DB(cfg.db_path), FakeNotifier()
    pipeline.run_once(cfg, db, notifier)
    n = len(notifier.msgs)
    from test_pipeline import SEARCH_URL
    good = fake.pages[SEARCH_URL]
    fake.pages[SEARCH_URL] = "<html><body>Wartungsarbeiten</body></html>"   # Portal liefert nichts mehr
    for _ in range(3):
        pipeline.run_once(cfg, db, notifier)
    alerts = [m for m in notifier.msgs[n:] if "liefert nichts" in m[0]]
    assert len(alerts) == 1 and "seit 3 Suchläufen" in alerts[0][1]
    pipeline.run_once(cfg, db, notifier)                     # kein zweiter Alarm
    assert len([m for m in notifier.msgs if "liefert nichts" in m[0]]) == 1
    fake.pages[SEARCH_URL] = good
    pipeline.run_once(cfg, db, notifier)
    assert "wieder ok" in notifier.msgs[-1][0]


def test_daily_backup(tmp_path, monkeypatch):
    import sqlite3
    cfg = make_cfg(tmp_path)
    cfg.backup_keep = 2
    monkeypatch.setattr(pipeline, "make_fetcher", lambda *a, **k: FakeFetcher(pages()))
    db = DB(cfg.db_path)
    pipeline.run_once(cfg, db, FakeNotifier())
    bdir = tmp_path / "backups"
    for d in ("2026-01-01", "2026-01-02", "2026-01-03"):
        (bdir / f"fernimmo-{d}.db").parent.mkdir(exist_ok=True)
        (bdir / f"fernimmo-{d}.db").write_text("alt")
    path = pipeline.backup_if_due(cfg, db)
    assert path and pipeline.backup_if_due(cfg, db) is None          # nur einmal pro Tag
    n = sqlite3.connect(path).execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    assert n == 3
    assert len(list(bdir.glob("fernimmo-*.db"))) == 2                 # nur die neuesten behalten


def test_csrf_blocked_from_foreign_site(tmp_path):
    from fastapi.testclient import TestClient

    from fernimmo.web import create_app
    cfg = make_cfg(tmp_path, users={"ich": "pw"})
    cfg.public_url = "http://fernimmo.example:8080"
    c = TestClient(create_app(cfg, DB(cfg.db_path)))
    auth = ("ich", "pw")
    assert c.post("/run", auth=auth, headers={"Origin": "https://boese.example"}, follow_redirects=False).status_code == 403
    assert c.post("/run", auth=auth, headers={"Referer": "https://boese.example/x"}, follow_redirects=False).status_code == 403
    assert c.post("/run", auth=auth, headers={"Origin": "http://testserver"}, follow_redirects=False).status_code == 303
    assert c.post("/run", auth=auth, headers={"Origin": "http://fernimmo.example:8080"}, follow_redirects=False).status_code == 303
    assert c.post("/run", auth=auth, follow_redirects=False).status_code == 303          # ohne Herkunft (z. B. curl)
    assert c.get("/openapi.json").status_code == 404


def test_javascript_links_are_never_stored(tmp_path, monkeypatch):
    from fernimmo.sources.immoscout import ImmoScout24
    page = '<a href="javascript:alert(1)//expose/1234567">x</a><a href="/expose/7654321">y</a>'
    hits = ImmoScout24({}, FakeFetcher({})).search_hits(page, "https://www.immobilienscout24.de/Suche/x", "kauf")
    assert [h.url for h in hits] == ["https://www.immobilienscout24.de/expose/7654321"]
    from fernimmo.models import RawListing
    raw = RawListing("s", "1", "javascript:alert(1)", "T", "", {}, "", "kauf", "javascript:alert(2)")
    cfg = make_cfg(tmp_path)
    try:
        pipeline.evaluate(raw, cfg)
        raise AssertionError("javascript-Link wurde akzeptiert")
    except ValueError:
        pass


def test_initial_searches_created_once_under_concurrency(tmp_path):
    import threading
    from fernimmo.searches import ensure_searches
    cfg = make_cfg(tmp_path)
    db = DB(cfg.db_path)
    ts = [threading.Thread(target=ensure_searches, args=(cfg, db)) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(db.searches()) == 1
