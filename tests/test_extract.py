from fernimmo.extract import extract, parse_de_number


def test_parse_de_number():
    assert parse_de_number("1.234,56") == 1234.56
    assert parse_de_number("45,5") == 45.5
    assert parse_de_number("189.000") == 189000
    assert parse_de_number("650") == 650
    assert parse_de_number("") is None


KLEINANZEIGEN_TEXT = """Helle Wohnung in ruhiger Lage, Nähe U-Bahn Gostenhof.
Die Wohnung wird über Fernwärme beheizt, Warmwasser zentral.
Einbauküche kann übernommen werden. Keine WG.
Kaution 3 Kaltmieten."""


def test_kleinanzeigen_structured():
    d = extract(
        KLEINANZEIGEN_TEXT,
        title="Schöne 2-Zimmer-Wohnung mit Balkon in Gostenhof",
        structured={
            "Preis": "720 €",
            "Wohnfläche": "54 m²",
            "Zimmer": "2",
            "Etage": "3",
            "Baujahr": "1965",
            "Warmmiete": "910 €",
            "Nebenkosten": "190 €",
            "Verfügbar ab": "Januar 2027",
            "Ort": "90443 Nürnberg - Gostenhof",
        },
    )
    assert d["area"] == 54
    assert d["rooms"] == 2
    assert d["year"] == 1965
    assert d["floor"] == 3
    assert d["rent_warm"] == 910
    assert d["price_generic"] == 720
    assert d["service_charges"] == 190
    assert d["plz"] == "90443"
    assert d["fernwaerme"] == "ja"
    assert d["heating_from"] == "freitext"
    assert "balkon" in d["features"] and "einbaukueche" in d["features"]
    assert d["price_per_sqm"] == round(720 / 54, 2)
    assert d["available_from"] == "januar 2027"


IS24_LIKE = """Wohnfläche ca.
48,5 m²
Zimmer
1,5
Kaltmiete
590,00 €
Baujahr
1972
Heizungsart
Zentralheizung
Wesentliche Energieträger
Gas
Energieeffizienzklasse: E
Endenergieverbrauch: 145,3 kWh/(m²·a)
Wohnung im 2. OG, Aufzug vorhanden."""


def test_gas_structured_means_no_fernwaerme():
    from fernimmo.sources.base import pair_labels
    d = extract(IS24_LIKE, title="1,5-Zimmer Apartment Nürnberg-Südstadt", structured=pair_labels(IS24_LIKE))
    assert d["area"] == 48.5
    assert d["rooms"] == 1.5
    assert d["rent_cold"] == 590
    assert d["year"] == 1972
    assert d["heating_sources"] == ["gas"]
    assert d["heating_from"] == "struktur"
    assert d["fernwaerme"] == "nein"
    assert d["energy_class"] == "E"
    assert d["energy_value"] == 145.3
    assert d["floor"] == 2
    assert "zentralheizung" in d["heating_systems"]
    assert "aufzug" in d["features"]


def test_fernwaerme_in_struct_and_abbrev():
    d = extract("", title="Wohnung", structured={"Wesentliche Energieträger": "FW", "Zimmer": "1"})
    assert d["fernwaerme"] == "ja"
    d = extract("Befeuerung: Fernwärme\nHeizungsart: Zentralheizung", title="2 Zi")
    assert d["fernwaerme"] == "ja"
    assert d["heating_from"] == "struktur"


def test_nah_fernwaerme_and_negation():
    assert extract("Energieträger: Nah-/Fernwärme")["fernwaerme"] == "ja"
    d = extract("Die Wohnung hat keine Fernwärme, sondern eine Gasetagenheizung.")
    assert d["fernwaerme"] != "ja"


def test_unknown_heating():
    d = extract("Schöne Wohnung, frisch renoviert, 2 Zimmer, 50 m².")
    assert d["fernwaerme"] == "unbekannt"
    assert d["rooms"] == 2


def test_flags():
    d = extract("Biete Tauschwohnung gegen größere Wohnung", title="Tausch: 2 Zimmer")
    assert "tausch" in d["flags"]
    d = extract("", title="Suche 2-Zimmer-Wohnung in Nürnberg")
    assert "gesuch" in d["flags"]
    d = extract("WBS erforderlich")
    assert "wbs" in d["flags"]


def test_purchase():
    d = extract("Kaufpreis: 189.000 €\nHausgeld: 280 €\nWohnfläche: 42 m²\nBaujahr: 1994\nFernwärme")
    assert d["purchase_price"] == 189000
    assert d["hausgeld"] == 280
    assert d["fernwaerme"] == "ja"


def test_erdgeschoss():
    assert extract("Die Wohnung liegt im Erdgeschoss.")["floor"] == 0


def test_kauf_flags():
    d = extract("Die Wohnung ist vermietet, Jahresnettokaltmiete 7.200 €. Erbbaurecht bis 2080.")
    assert "vermietet" in d["flags"] and "erbpacht" in d["flags"]
    d = extract("Ideal als Kapitalanlage oder zur Eigennutzung, bezugsfrei, unvermietet.")
    assert "vermietet" not in d["flags"]
    assert "versteigerung" in extract("Zwangsversteigerung, Termin beim Amtsgericht")["flags"]
    assert "versteigerung" not in extract("Makler GmbH, HRB 123 Amtsgericht Nürnberg")["flags"]


def test_half_rooms_and_words():
    cases = {
        "Schöne 2,5-Zimmer-Wohnung": 2.5, "2.5 Zimmer ETW": 2.5, "2½ Zimmer mit Balkon": 2.5,
        "Gemütliche 3 1/2 Zimmer Wohnung": 3.5, "Zweieinhalb-Zimmer-Wohnung in Top-Lage": 2.5,
        "Eineinhalbzimmerwohnung": 1.5, "Zweizimmerwohnung mit EBK": 2, "Helle Drei-Zimmer-Wohnung": 3,
        "1,5 Zi.-Whg.": 1.5, "Zimmer: 4,5": 4.5, "Zimmeranzahl: 1,5": 1.5,
    }
    for text, expected in cases.items():
        assert extract("", title=text)["rooms"] == expected, text
    assert extract("", structured={"Zimmer": "2,5"})["rooms"] == 2.5


def test_plz_germany():
    assert extract("Lage: 10115 Berlin-Mitte")["plz"] == "10115"
    assert extract("Kaufpreis 25000 €, Wohnung in 80331 München")["plz"] == "80331"   # Preis ist keine PLZ
    assert extract("Objekt-Nr. 99999 in 50667 Köln")["plz"] == "50667"               # 99999 existiert nicht
    assert extract("ohne Ortsangabe")["plz"] is None
