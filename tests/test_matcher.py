from fernimmo.extract import extract
from fernimmo.matcher import Profile, best_match, match

P = Profile(name="test", listing_types=["miete"], rooms_min=1, rooms_max=2, price_max=900,
            heating_required=["fernwaerme"], plz=["904"], exclude_flags=["tausch", "gesuch"])


def m(text, title="", structured=None, ltype="miete"):
    d = extract(text, title, structured)
    return match(P, ltype, d, f"{title}\n{text}")


def test_full_match():
    r = m("Heizungsart: Fernwärme\nKaltmiete: 650 €\nZimmer: 2\n90429 Nürnberg")
    assert r.level == "voll", r.reasons


def test_unknown_heating_is_partial():
    r = m("Kaltmiete: 650 €\nZimmer: 2\n90429 Nürnberg")
    assert r.level == "teilweise"


def test_gas_is_rejected():
    r = m("Heizungsart: Gas\nKaltmiete: 650 €\nZimmer: 2\n90429 Nürnberg")
    assert r.level == "none"


def test_too_many_rooms():
    assert m("Zimmer: 3\nFernwärme\nKaltmiete: 650 €\n90429").level == "none"


def test_too_expensive():
    assert m("Zimmer: 2\nFernwärme\nKaltmiete: 1.050 €\n90429").level == "none"


def test_wrong_type():
    assert m("Zimmer: 2\nFernwärme\n90429", ltype="kauf").level == "none"


def test_wrong_plz():
    assert m("Zimmer: 2\nFernwärme\nKaltmiete: 650 €\n91052 Erlangen").level == "none"


def test_tausch_excluded():
    assert m("Wohnungstausch! Zimmer: 2\nFernwärme\n90429").level == "none"


def test_ignore_unknown_heating():
    p = Profile(name="strict", heating_required=["fernwaerme"], unknown_heating="ignorieren")
    d = extract("Zimmer: 2")
    assert match(p, "miete", d, "").level == "none"


def test_best_match_multiple_profiles():
    p2 = Profile(name="kauf", listing_types=["kauf"])
    d = extract("Heizungsart: Fernwärme\nKaltmiete: 650 €\nZimmer: 2\n90429 Nürnberg")
    level, names, _ = best_match([P, p2], "miete", d, "")
    assert level == "voll" and names == ["test"]
