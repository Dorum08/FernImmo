from fernimmo.calc import costs
from fernimmo.config import CALC_DEFAULTS
from fernimmo.extract import extract
from fernimmo.geo import (AreaFilter, coords_from_html, has_street, overlaps, plz_center, plz_exists,
                          plz_feature, plz_for_point, plz_index, plz_state, point_in, valid_geometry)
from fernimmo.matcher import Profile, match

SQUARE = {"type": "Polygon", "coordinates": [[[11.0, 49.4], [11.2, 49.4], [11.2, 49.5], [11.0, 49.5], [11.0, 49.4]]]}
WITH_HOLE = {"type": "Polygon", "coordinates": [
    SQUARE["coordinates"][0], [[11.05, 49.42], [11.1, 49.42], [11.1, 49.47], [11.05, 49.47], [11.05, 49.42]]]}


def test_point_in_polygon():
    assert point_in(49.45, 11.15, SQUARE)
    assert not point_in(49.45, 11.3, SQUARE)
    assert not point_in(49.45, 11.07, WITH_HOLE)        # im Loch
    assert point_in(49.45, 11.15, WITH_HOLE)
    multi = {"type": "MultiPolygon", "coordinates": [SQUARE["coordinates"]]}
    assert point_in(49.45, 11.15, multi)


def test_overlaps():
    other = {"type": "Polygon", "coordinates": [[[11.15, 49.45], [11.3, 49.45], [11.3, 49.6], [11.15, 49.6], [11.15, 49.45]]]}
    far = {"type": "Polygon", "coordinates": [[[12.0, 49.0], [12.1, 49.0], [12.1, 49.1], [12.0, 49.0]]]}
    # Kreuz: keine Ecke liegt im anderen, die Kanten schneiden sich aber
    thin_h = {"type": "Polygon", "coordinates": [[[10.9, 49.44], [11.3, 49.44], [11.3, 49.46], [10.9, 49.46], [10.9, 49.44]]]}
    thin_v = {"type": "Polygon", "coordinates": [[[11.09, 49.3], [11.11, 49.3], [11.11, 49.6], [11.09, 49.6], [11.09, 49.3]]]}
    assert overlaps(SQUARE, other)
    assert not overlaps(SQUARE, far)
    assert overlaps(thin_h, thin_v)


def test_plz_data_germany():
    idx = plz_index()["plz"]
    assert len(idx) > 8000
    assert plz_for_point(49.4521, 11.0767) == "90403"      # Nürnberg, Hauptmarkt
    assert plz_for_point(52.5163, 13.3777) == "10117"      # Berlin, Brandenburger Tor
    assert plz_for_point(53.5413, 9.9842) == "20457"       # Hamburg, Elbphilharmonie
    assert plz_for_point(48.1374, 11.5755) == "80331"      # München, Marienplatz
    c = plz_center("90443")
    assert c and 49.3 < c[0] < 49.6 and 10.9 < c[1] < 11.2
    assert plz_feature("90443")["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert plz_center("00000") is None and not plz_exists("00000")
    assert [plz_state(p) for p in ("90403", "10115", "20095", "01067", "50667", "27568", "28195", "66111")] == \
        ["BY", "BE", "HH", "SN", "NW", "HB", "HB", "SL"]


def test_area_plz_prefix():
    area = AreaFilter(plz=["904", "10115"])
    assert area.check({"plz": "90443"}) is True
    assert area.check({"plz": "10115"}) is True
    assert area.check({"plz": "10117"}) is False


def test_coords_from_html():
    assert coords_from_html('<meta property="og:latitude" content="49.45"><meta property="og:longitude" content="11.08">') == (49.45, 11.08)
    ld = '<script type="application/ld+json">{"geo":{"@type":"GeoCoordinates","latitude":49.44,"longitude":11.06}}</script>'
    assert coords_from_html(ld) == (49.44, 11.06)
    assert coords_from_html('{"lat": 49.4123, "lng": 11.0456}') == (49.4123, 11.0456)
    assert coords_from_html('{"lat": 12.0, "lng": 50.0}') is None   # außerhalb Deutschlands
    assert coords_from_html("<html></html>") is None


def test_has_street():
    assert has_street("Fürther Straße 12, 90429 Nürnberg")
    assert not has_street("90443 Nürnberg - Gostenhof")


def test_valid_geometry():
    assert valid_geometry(SQUARE)
    assert not valid_geometry({"type": "Polygon", "coordinates": [[[11, 49], [11.1, 49]]]})
    assert not valid_geometry({"type": "Point", "coordinates": [11, 49]})
    assert not valid_geometry("kaputt")


def test_area_filter():
    area = AreaFilter(plz=["90443"], polygons=[SQUARE])
    assert area.check({"plz": "90443"}) is True
    assert area.check({"plz": "90000", "lat": 49.45, "lon": 11.15, "geo_precision": "adresse"}) is True
    assert area.check({"plz": "90000", "lat": 49.45, "lon": 11.5, "geo_precision": "portal"}) is False
    # Nur PLZ bekannt, PLZ-Gebiet überschneidet die Fläche -> unklar
    assert area.check({"plz": "90403", "lat": 49.45, "lon": 11.07, "geo_precision": "plz"}) is None
    # PLZ weit weg (Erlangen) -> außerhalb
    assert area.check({"plz": "91052", "geo_precision": "plz"}) is False
    assert AreaFilter(plz=["90443"]).check({"plz": "90429"}) is False
    assert AreaFilter(polygons=[SQUARE]).check({}) is None


def test_match_with_area():
    p = Profile(name="t", listing_types=["kauf"], plz=["999"])     # profile.plz wird durch Gebiet ersetzt
    area = AreaFilter(plz=["90443"])
    d = extract("Kaufpreis: 200.000 €\nZimmer: 2\nFernwärme")
    d.update(plz="90443")
    assert match(p, "kauf", d, "", area).level == "voll"
    d["plz"] = "90429"
    r = match(p, "kauf", d, "", area)
    assert r.level == "none" and "Suchgebiet" in r.reasons[0]


def test_fw_zone():
    area = AreaFilter(fw_zones=[SQUARE])
    assert area.in_fw_zone({"lat": 49.45, "lon": 11.15, "geo_precision": "adresse"})
    assert not area.in_fw_zone({"lat": 49.45, "lon": 11.15, "geo_precision": "plz"})


def test_costs():
    c = costs(200000, 50, 250, [], CALC_DEFAULTS, state="BY")
    assert c["nebenkosten"] == round(200000 * (3.5 + 2.0 + 3.57) / 100)
    assert c["grunderwerbsteuer_von"] == "Bayern"
    assert costs(200000, 50, 0, [], CALC_DEFAULTS, state="NW")["grunderwerbsteuer_pct"] == 6.5
    assert costs(200000, 50, 0, [], CALC_DEFAULTS, state=None)["grunderwerbsteuer_von"] == "Bundesland unbekannt"
    assert costs(200000, 50, 0, [], {**CALC_DEFAULTS, "grunderwerbsteuer_pct": 4})["grunderwerbsteuer_pct"] == 4
    assert costs(200000, 50, 0, [], {**CALC_DEFAULTS, "grunderwerbsteuer_laender": {"BY": 4.5}},
                 state="BY")["grunderwerbsteuer_pct"] == 4.5
    assert c["darlehen"] == c["gesamt"] - 60000
    assert c["rate"] == round(c["darlehen"] * 5.6 / 100 / 12)
    assert c["belastung"] == c["rate"] + 250
    assert c["bruttorendite_pct"] == round(50 * 12 * 12 / 200000 * 100, 2)
    assert costs(200000, 50, 0, ["provisionsfrei"], CALC_DEFAULTS, state="BY")["makler_pct"] == 0
    assert costs(None, 50, 0, [], CALC_DEFAULTS) is None


def test_haversine_and_circle():
    from fernimmo.geo import circle_polygon, haversine_km
    # Hauptmarkt -> Plärrer ca. 1,2 km
    assert 1.0 < haversine_km(49.4521, 11.0767, 49.4486, 11.0631) < 1.3
    circ = circle_polygon(49.45, 11.08, 2.0)
    assert len(circ["coordinates"][0]) == 65 and valid_geometry(circ)
    for lon, lat in circ["coordinates"][0][:-1]:
        assert abs(haversine_km(49.45, 11.08, lat, lon) - 2.0) < 0.02


def test_area_filter_circle():
    area = AreaFilter(circles=[(49.4486, 11.0631, 1.5)])   # 1,5 km um den Plärrer
    near = {"lat": 49.4521, "lon": 11.0767, "geo_precision": "adresse"}   # Hauptmarkt, ~1,2 km
    far = {"lat": 49.4380, "lon": 11.0850, "geo_precision": "portal"}     # Südstadt, ~2 km
    assert area.check(near) is True
    assert area.check(far) is False
    # Nur PLZ bekannt: 90443 (Gostenhof) schneidet den Kreis -> unklar; Erlangen -> außerhalb
    assert area.check({"plz": "90443", "geo_precision": "plz"}) is None
    assert area.check({"plz": "91052", "geo_precision": "plz"}) is False
