"""Lernfähiger Adapter für weitere Portale und Vorfilter – mit nachgebauten Seiten."""
from fernimmo import pipeline
from fernimmo.config import Config
from fernimmo.db import DB
from fernimmo.sources import build_source
from fernimmo.sources.base import SearchHit
from fernimmo.sources.generic import listing_id, schema_org, shape

from test_pipeline import FakeFetcher, FakeNotifier

SEARCH_URL = "https://www.ivd24immobilien.de/suche/wohnung-kaufen/nuernberg/"


def card(i, rooms, plz, price):
    return f"""<li class="result">
      <a href="/expose/{i}/schoene-wohnung-in-bester-lage-mit-balkon/"><img src="/img/{i}.jpg"></a>
      <h2><a href="/expose/{i}/schoene-wohnung-in-bester-lage-mit-balkon/">Wohnung {i}</a></h2>
      <p>{rooms} Zimmer · 55 m² · {price} € · {plz} Nürnberg</p>
      <a href="/merkliste/add?id={i}">merken</a>
    </li>"""


SEARCH = f"""<html><body>
<nav><a href="/impressum">Impressum</a><a href="/ratgeber/2024/finanzierung">Ratgeber</a>
<a href="/suche/wohnung-kaufen/nuernberg/?page=2">2</a></nav>
<ul>{card(1234567, 2, "90443", "199.000")}{card(1234568, "1,5", "90429", "149.000")}
{card(1234569, 4, "90443", "399.000")}{card(1234570, 2, "10115", "299.000")}</ul>
<aside><a href="/neubau-2025/">Neubau 2025</a></aside>
</body></html>"""

DETAIL = """<html><head><meta property="og:image" content="https://x/1.jpg">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"RealEstateListing",
 "offers":{"@type":"Offer","price":199000,"priceCurrency":"EUR"},
 "about":{"@type":"Apartment","numberOfRooms":2,"floorSize":{"@type":"QuantitativeValue","value":55,"unitCode":"MTK"},
 "address":{"@type":"PostalAddress","streetAddress":"Fürther Str. 12","postalCode":"90443","addressLocality":"Nürnberg"}}}
</script></head><body><header>Menü</header>
<main><h1>Schöne 2-Zimmer-ETW mit Fernwärme</h1>
<p>Die Wohnung wird über Fernwärme beheizt. Hausgeld: 250 €</p>
<div class="similar-objects"><p>Ähnlich: 5 Zimmer, 140 m², Gasheizung</p></div></main>
</body></html>"""


def test_shape_and_id():
    assert shape("https://www.ivd24immobilien.de/expose/1234567/schoene-wohnung-in-bester-lage/") == \
        "ivd24immobilien.de/expose/{id}/{slug}"
    assert shape("https://www.ivd24immobilien.de/impressum") is None
    assert shape("https://x.de/bild/123456.jpg") is None
    assert listing_id("https://www.ivd24immobilien.de/expose/1234567/abc/") == "ivd24immobilien.de:1234567"
    assert shape("https://www.immowelt.de/expose/2bxyz7k") == "immowelt.de/expose/{id}"


def test_auto_detect_links_and_snippets():
    src = build_source("ivd24", {"urls": [SEARCH_URL]}, FakeFetcher({SEARCH_URL: SEARCH}))
    hits = src.search()
    assert sorted(h.source_id for h in hits) == [f"ivd24immobilien.de:{i}" for i in range(1234567, 1234571)]
    h = next(h for h in hits if h.source_id.endswith("1234568"))
    assert h.url == "https://www.ivd24immobilien.de/expose/1234568/schoene-wohnung-in-bester-lage-mit-balkon/"
    assert "1,5 Zimmer" in h.snippet and "90429" in h.snippet and "1234567" not in h.snippet
    assert h.listing_type == "kauf"


def test_schema_org_and_detail():
    so = schema_org(DETAIL)
    assert so["Zimmer"] == "2" and so["Wohnfläche"] == "55 m²" and so["Preis"] == "199000 €"
    assert so["Adresse"] == "Fürther Str. 12, 90443 Nürnberg"
    src = build_source("ivd24", {}, FakeFetcher({}))
    raw = src.parse_detail(DETAIL, SearchHit("ivd24immobilien.de:1", "https://www.ivd24immobilien.de/expose/1/", "kauf"))
    assert raw.title.startswith("Schöne 2-Zimmer-ETW")
    assert raw.address == "Fürther Str. 12, 90443 Nürnberg"
    assert "Ähnlich" not in raw.text          # Empfehlungsblock entfernt
    from fernimmo.extract import extract
    d = extract(raw.text, raw.title, raw.structured)
    assert d["rooms"] == 2 and d["area"] == 55 and d["fernwaerme"] == "ja" and d["plz"] == "90443"


def test_meta_search_delegates_to_immowelt():
    url = "https://www.meinestadt.de/nuernberg/immobilien/wohnungen?typ=kaufen"
    page = "<html><body>" + "".join(
        f'<div class="card"><a href="https://www.immowelt.de/expose/2b{i}xyz7">Wohnung {i}</a> 2 Zimmer</div>'
        for i in range(10, 14)) + '<a href="https://www.meinestadt.de/nuernberg/kino">Kino</a></body></html>'
    src = build_source("meinestadt", {"urls": [url]}, FakeFetcher({url: page}))
    hits = src.search()
    assert len(hits) == 4 and all("immowelt.de/expose/" in h.url for h in hits)
    assert src._delegate(hits[0].url).name == "immowelt"


def test_all_requested_portals_registered():
    for name in ("ohne-makler", "immobilo", "immobilien.de", "meinestadt", "ivd24",
                 "regionalimmobilien24", "1a-immobilienmarkt"):
        assert build_source(name, {}, FakeFetcher({})).name == name


def test_prefilter_skips_detail_fetches(tmp_path, monkeypatch):
    cfg = Config({
        "db_path": str(tmp_path / "t.db"), "geocoding": {"enabled": False},
        "sources": {"ivd24": {"urls": [SEARCH_URL], "max_pages": 1}},
        "profiles": [{"name": "p", "listing_types": ["kauf"], "rooms_min": 1, "rooms_max": 2,
                      "heating_required": ["fernwaerme"]}],
        "notify": {"first_run": True, "levels": ["voll"]},
    })
    pages = {SEARCH_URL: SEARCH}
    for i in range(1234567, 1234571):
        pages[f"https://www.ivd24immobilien.de/expose/{i}/schoene-wohnung-in-bester-lage-mit-balkon/"] = DETAIL
    fake = FakeFetcher(pages)
    monkeypatch.setattr(pipeline, "make_fetcher", lambda *a, **k: fake)
    db = DB(cfg.db_path)
    db.set_setting("search_plz", ["904"])            # nur Nürnberger PLZ
    res = pipeline.run_once(cfg, db, FakeNotifier())["ivd24"]
    assert res["found"] == 4 and res["prefiltered"] == 2   # 4 Zimmer und Berliner PLZ gar nicht erst geladen
    fetched = [u for u in fake.calls if "/expose/" in u]
    assert sorted(u.split("/")[4] for u in fetched) == ["1234567", "1234568"]


def test_criteria_from_web_apply_to_matching(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from fernimmo.web import create_app
    from test_pipeline import make_cfg, pages as ka_pages
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(pipeline, "make_fetcher", lambda *a, **k: FakeFetcher(ka_pages()))
    db = DB(cfg.db_path)
    pipeline.run_once(cfg, db, FakeNotifier())
    c = TestClient(create_app(cfg, db, sync_reprocess=True))
    three = next(r for r in db.search(match=None) if r["rooms"] == 3)
    assert three["match_level"] == "none"
    # Zimmer 2,5 bis 3,5 -> die 3-Zimmer-Wohnung passt, die 2-Zimmer-Wohnungen nicht mehr
    sid = db.searches()[0]["id"]
    r = c.patch(f"/api/searches/{sid}", json={"criteria": {"rooms_min": 2.5, "rooms_max": 3.5, "price_max": None,
                                                            "area_min": ""}})
    crit = r.json()["criteria"]
    assert (crit["rooms_min"], crit["rooms_max"], crit["price_max"], crit["area_min"]) == (2.5, 3.5, None, None)
    assert db.get(three["id"])["match_level"] == "voll"
    assert all(r["match_level"] == "none" for r in db.search(match=None) if r["rooms"] == 2)
    r = c.patch(f"/api/searches/{sid}", json={"criteria": {"rooms_min": 3, "rooms_max": 2}})
    assert r.status_code == 400 and "größer" in r.json()["detail"]
    page = c.get(f"/suche?id={sid}").text
    assert "Kriterien" in page and 'value="2.5" selected' in page
    # Übersichtsfilter mit halben Zimmern
    assert [r["rooms"] for r in db.search(match=None, rooms_min=2.5)] == [3]
    assert c.get("/liste?match=&rooms_min=1.5&rooms_max=2").status_code == 200
    # PLZ-Anfänge: "904" deckt "90443" ab, Doppelungen fallen weg
    assert c.patch(f"/api/searches/{sid}", json={"plz": ["904", "90443", "1"]}).json()["plz"] == ["1", "904"]
