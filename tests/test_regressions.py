"""Fehler aus der Konsistenzprüfung – sollen nicht wiederkommen."""
import pytest

from fernimmo import pipeline
from fernimmo.config import Config
from fernimmo.db import DB
from fernimmo.extract import extract
from fernimmo.fetch import BlockedError, _check_blocked
from fernimmo.geo import AreaFilter, Geocoder
from fernimmo.matcher import Profile
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


def test_prefilter_ignores_ambiguous_snippets(tmp_path):
    cfg = Config({"db_path": str(tmp_path / "x.db"),
                  "profiles": [{"name": "p", "listing_types": ["kauf"], "rooms_min": 1, "rooms_max": 2}]})
    profs, area = cfg.profiles, AreaFilter()
    hit = lambda s: SearchHit("1", "u", "kauf", snippet=s)
    assert pipeline.prefiltered(hit("Schöne ETW 4 Zimmer 98 m² 90443 Nürnberg"), cfg, area, profs)
    # Kachel nennt 2 und 3 Zimmer -> nicht aussortieren
    assert not pipeline.prefiltered(hit("2 Zimmer ETW, im Haus auch 3 Zimmer frei"), cfg, area, profs)
    assert not pipeline.prefiltered(hit("4 Zimmer"), cfg, area, [])     # ohne Profil nie aussortieren
    assert not pipeline.prefiltered(hit(""), cfg, area, profs)


def test_ein_zimmer_in_prose_is_not_room_count():
    assert extract("Schöne Wohnung, ein Zimmer mit Balkon zum Hof.", title="ETW im Altbau")["rooms"] is None
    assert extract("", title="Einzimmerwohnung in Gostenhof")["rooms"] == 1
    assert extract("", title="Ein-Zimmer-Apartment")["rooms"] == 1
