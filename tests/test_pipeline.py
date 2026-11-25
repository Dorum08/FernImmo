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

    def send(self, subject, body, url=None):
        self.msgs.append((subject, body))


def make_cfg(tmp_path, first_run=True, users=None):
    return Config({
        "db_path": str(tmp_path / "t.db"),
        "sources": {"kleinanzeigen": {"urls": [SEARCH_URL], "max_pages": 2, "refresh_hours": 0}},
        "profiles": [{"name": "fw", "listing_types": ["kauf"], "rooms_min": 1, "rooms_max": 2,
                      "heating_required": ["fernwaerme"]}],
        "notify": {"first_run": first_run, "levels": ["voll"]},
        "web": {"users": users or {}},
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
    lid = db.search()[0]["id"]

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
    fake.pages[SEARCH_URL] = SEARCH.replace(f'data-adid="111"', 'data-adid="999"')
    pipeline.run_once(cfg, db, notifier)
    assert db.get(lid)["active"] == 0
    assert db.changes(lid)[0]["field"] == "offline"


def test_reprocess_does_not_log_changes(env):
    cfg, db, notifier, _ = env
    pipeline.run_once(cfg, db, notifier)
    lid = db.search()[0]["id"]
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
    lid = db.search()[0]["id"]
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
    r = c.get("/", auth=A)
    assert r.status_code == 200 and "Helle 2-Zi-ETW" in r.text and "3 Zimmer" not in r.text
    lid = db.search()[0]["id"]
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
    page = c.get("/", auth=A).text
    assert "Lage top, &lt;b&gt;Preis&lt;/b&gt; verhandeln" in page   # HTML wird escaped
    assert "1 Kommentar" in page
    cid = db.get(lid)["comments"][0]["id"]
    c.post(f"/comment/{cid}/delete", data={"lid": lid, "back": "/"}, auth=A)   # Anna darf Bens nicht löschen
    assert len(db.get(lid)["comments"]) == 1

    det = c.get(f"/listing/{lid}", auth=A).text
    assert "Änderungsverlauf" in det and "erstmals gefunden" in det and "Fernwärme" in det
    r = c.post(f"/listing/{lid}/status", data={"status": "favorit"}, auth=A, follow_redirects=False)
    assert r.status_code == 303 and db.get(lid)["status"] == "favorit"
    assert c.get("/runs", auth=A).status_code == 200
    assert c.get(f"/api/listings/{lid}/changes", auth=A).json()[0]["field"] == "created"
    assert c.post("/listing/99999/like", data={"back": "/"}, auth=A).status_code == 404
