from fernimmo.calc import costs
from fernimmo.config import CALC_DEFAULTS
from fernimmo.extract import extract
from fernimmo.geo import (AreaFilter, coords_from_html, has_street, overlaps, plz_areas, plz_center,
                          plz_for_point, point_in, valid_geometry)
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


def test_plz_data_nuernberg():
    areas = plz_areas()
    assert len(areas) > 50 and "90443" in areas and "90402" in areas
    lat, lon = 49.4521, 11.0767  # Hauptmarkt
    assert plz_for_point(lat, lon) == "90403"
    c = plz_center("90443")
    assert c and 49.3 < c[0] < 49.6 and 10.9 < c[1] < 11.2
    assert plz_center("12345") is None


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
    c = costs(200000, 50, 250, [], CALC_DEFAULTS)
    assert c["nebenkosten"] == round(200000 * (3.5 + 2.0 + 3.57) / 100)
    assert c["darlehen"] == c["gesamt"] - 60000
    assert c["rate"] == round(c["darlehen"] * 5.6 / 100 / 12)
    assert c["belastung"] == c["rate"] + 250
    assert c["bruttorendite_pct"] == round(50 * 12 * 12 / 200000 * 100, 2)
    assert costs(200000, 50, 0, ["provisionsfrei"], CALC_DEFAULTS)["makler_pct"] == 0
    assert costs(None, 50, 0, [], CALC_DEFAULTS) is None
