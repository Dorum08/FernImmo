"""End-to-End mit gefälschten Portalseiten (kein Netzwerk)."""
from fernimmo import pipeline
from fernimmo.config import Config
from fernimmo.db import DB
from fernimmo.fetch import Fetcher
from fernimmo.sources.kleinanzeigen import Kleinanzeigen

SEARCH = """<html><body>
<article class="aditem" data-adid="111" data-href="/s-anzeige/2-zi-fernwaerme/111-203-1234"></article>
<article class="aditem" data-adid="222" data-href="/s-anzeige/2-zi-gas/222-203-1234"></article>
<article class="aditem" data-adid="333" data-href="/s-anzeige/3-zi/333-203-1234"></article>
</body></html>"""


def detail(title, price, rooms, area, desc, heating=None):
    heat = (f'<li class="addetailslist--detail">Heizungsart<span class="addetailslist--detail--value">'
            f'{heating}</span></li>') if heating else ""
    return f"""<html><head><meta property="og:image" content="https://img/x.jpg"></head><body>
<h1 id="viewad-title">{title}</h1><h2 id="viewad-price">{price} €</h2>
<span id="viewad-locality">90443 Nürnberg - Gostenhof</span>
<ul><li class="addetailslist--detail">Wohnfläche<span class="addetailslist--detail--value">{area} m²</span></li>
<li class="addetailslist--detail">Zimmer<span class="addetailslist--detail--value">{rooms}</span></li>{heat}</ul>
<li class="checktag">Balkon</li>
<p id="viewad-description-text">{desc}</p></body></html>"""


PAGES = {
    "https://www.kleinanzeigen.de/s-wohnung-mieten/nuernberg/c203l976": SEARCH,
    "https://www.kleinanzeigen.de/s-anzeige/2-zi-fernwaerme/111-203-1234":
        detail("Helle 2-Zi-Wohnung", 690, 2, 55, "Beheizung über Fernwärme der N-ERGIE."),
    "https://www.kleinanzeigen.de/s-anzeige/2-zi-gas/222-203-1234":
        detail("2 Zimmer Altbau", 650, 2, 60, "Schöner Altbau.", heating="Gasetagenheizung"),
    "https://www.kleinanzeigen.de/s-anzeige/3-zi/333-203-1234":
        detail("3 Zimmer Familienwohnung", 990, 3, 80, "Fernwärme."),
}


class FakeFetcher(Fetcher):
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        return self.pages.get(url, "<html></html>")


class FakeNotifier:
    def __init__(self):
        self.msgs = []

    def send(self, subject, body, url=None):
        self.msgs.append((subject, body))


def make_cfg(tmp_path, first_run=True):
    return Config({
        "db_path": str(tmp_path / "t.db"),
        "sources": {"kleinanzeigen": {"urls": ["https://www.kleinanzeigen.de/s-wohnung-mieten/nuernberg/c203l976"],
                                      "max_pages": 2}},
        "profiles": [{"name": "fw", "listing_types": ["miete"], "rooms_min": 1, "rooms_max": 2,
                      "heating_required": ["fernwaerme"]}],
        "notify": {"first_run": first_run, "levels": ["voll"]},
    })


def test_pagination_url():
    k = Kleinanzeigen({}, FakeFetcher({}))
    assert k.page_url("https://www.kleinanzeigen.de/s-wohnung-mieten/nuernberg/c203l976", 3) == \
        "https://www.kleinanzeigen.de/s-wohnung-mieten/nuernberg/seite:3/c203l976"


def test_run_end_to_end(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    fake = FakeFetcher(PAGES)
    monkeypatch.setattr(pipeline, "make_fetcher", lambda *a, **k: fake)
    db = DB(cfg.db_path)
    notifier = FakeNotifier()

    res = pipeline.run_once(cfg, db, notifier)
    assert res["kleinanzeigen"]["error"] is None
    assert res["kleinanzeigen"]["found"] == 3
    assert res["kleinanzeigen"]["new"] == 3

    rows = {r["source_id"]: r for r in db.search(match=None)}
    assert rows["111"]["match_level"] == "voll"
    assert rows["111"]["fernwaerme"] == "ja"
    assert rows["111"]["listing_type"] == "miete"
    assert rows["222"]["match_level"] == "none"
    assert rows["222"]["fernwaerme"] == "nein"
    assert rows["333"]["match_level"] == "none"
    assert len(notifier.msgs) == 1
    assert "Helle 2-Zi-Wohnung" in notifier.msgs[0][1]

    # Zweiter Lauf: keine erneuten Meldungen, Preissenkung wird erkannt
    PAGES2 = dict(PAGES)
    PAGES2["https://www.kleinanzeigen.de/s-anzeige/2-zi-fernwaerme/111-203-1234"] = detail(
        "Helle 2-Zi-Wohnung", 640, 2, 55, "Beheizung über Fernwärme.")
    fake.pages = PAGES2
    cfg.sources["kleinanzeigen"]["refresh_per_run"] = 10
    pipeline.run_once(cfg, db, notifier)
    assert len(notifier.msgs) == 2
    assert "Preis gesenkt" in notifier.msgs[1][1]
    assert [h["price"] for h in db.price_history(rows["111"]["id"])] == [690, 640]


def test_first_run_summary(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, first_run=False)
    monkeypatch.setattr(pipeline, "make_fetcher", lambda *a, **k: FakeFetcher(PAGES))
    notifier = FakeNotifier()
    pipeline.run_once(cfg, DB(cfg.db_path), notifier)
    assert len(notifier.msgs) == 1 and "erster" in notifier.msgs[0][0].lower()


def test_web(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from fernimmo.web import create_app
    cfg = make_cfg(tmp_path)
    cfg.web_password = "geheim"
    monkeypatch.setattr(pipeline, "make_fetcher", lambda *a, **k: FakeFetcher(PAGES))
    db = DB(cfg.db_path)
    pipeline.run_once(cfg, db, FakeNotifier())
    c = TestClient(create_app(cfg, db))
    assert c.get("/").status_code == 401
    r = c.get("/", auth=("admin", "geheim"))
    assert r.status_code == 200 and "Helle 2-Zi-Wohnung" in r.text and "3 Zimmer" not in r.text
    lid = db.search()[0]["id"]
    assert "Fernwärme" in c.get(f"/listing/{lid}", auth=("admin", "geheim")).text
    r = c.post(f"/listing/{lid}/status", data={"status": "favorit"}, auth=("admin", "geheim"),
               follow_redirects=False)
    assert r.status_code == 303 and db.get(lid)["status"] == "favorit"
    assert c.get("/runs", auth=("admin", "geheim")).status_code == 200
