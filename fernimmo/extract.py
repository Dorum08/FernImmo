"""Merkmalserkennung aus Inseratstexten.

Arbeitet rein regelbasiert auf deutschem Freitext bzw. "Label: Wert"-Zeilen,
wie sie die Portale liefern. Strukturierte Felder (Label: Wert) haben Vorrang
vor Fundstellen im Freitext.
"""
from __future__ import annotations

import re
from typing import Any

NUM = r"(\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?)"
SEP = r"\s*(?:[:\-–]\s*)?(?:ca\.?\s*|circa\s*|rd\.?\s*)?"
EUR = r"\s*(?:€|eur\b|euro\b)"

# Energieträger -> Schlüsselwörter (Reihenfolge ist egal, es werden alle gesammelt)
HEATING_KEYWORDS: dict[str, list[str]] = {
    "fernwaerme": [
        r"fernw(?:ä|ae)rme", r"fernheiz", r"fern-\s*/\s*nahw", r"nah-\s*/\s*fernw",
        r"fern-\s*und\s*nahw", r"nah-\s*und\s*fernw",
    ],
    "nahwaerme": [r"nahw(?:ä|ae)rme"],
    "gas": [r"\berdgas", r"\bgas\b", r"gas-?(?:zentral|etagen|therme|heizung|brennwert)", r"\bflüssiggas"],
    "oel": [r"\b(?:heiz)?(?:ö|oe)l\b", r"(?:ö|oe)l-?(?:heizung|zentralheizung)"],
    "waermepumpe": [r"w(?:ä|ae)rmepumpe", r"luft-?\s*/?\s*wasser", r"erdw(?:ä|ae)rme", r"geotherm"],
    "holz_pellets": [r"pellet", r"\bholz", r"hackschnitzel"],
    "strom": [r"nachtspeicher", r"elektro-?heiz", r"\bstrom\b", r"elektrisch"],
    "bhkw": [r"blockheizkraftwerk", r"\bbhkw\b", r"kraft-?w(?:ä|ae)rme-?kopplung", r"\bkwk\b"],
    "solar": [r"solar"],
}
# Kürzel, die nur in strukturierten Energieträger-Feldern zuverlässig sind
HEATING_ABBREV = {r"\bfw\b": "fernwaerme", r"\bgas\b": "gas"}

HEATING_SYSTEMS = {
    "zentralheizung": r"zentralheizung",
    "etagenheizung": r"etagenheizung|gasetagen|gas-etagen|gastherme",
    "fussbodenheizung": r"fu(?:ß|ss)bodenheizung",
    "ofenheizung": r"ofenheizung",
    "nachtspeicher": r"nachtspeicher",
}

HEATING_LABELS = (
    r"(?:heizungsart|art der heizung|heizung|befeuerung(?:sart)?|"
    r"(?:wesentliche[rn]?\s+)?energietr(?:ä|ae)ger|energiequelle|heizenergie)"
)

FEATURES = {
    "balkon": r"\bbalkon|\bloggia|\bterrasse|dachterrasse",
    "einbaukueche": r"einbauk(?:ü|ue)che|\bebk\b|k(?:ü|ue)che vorhanden|inkl\.? k(?:ü|ue)che",
    "aufzug": r"\baufzug|fahrstuhl|personenaufzug|\blift\b",
    "keller": r"\bkeller(?:abteil|raum)?\b",
    "stellplatz": r"stellplatz|tiefgarage|\bgarage|carport",
    "barrierefrei": r"barrierefrei|barrierearm|stufenlos",
    "moebliert": r"(?<!un)m(?:ö|oe)bliert",
    "garten": r"gartenanteil|gartennutzung|\bgarten\b",
}

EXCLUDE_HINTS = {
    "tausch": r"tauschwohnung|wohnungstausch|\btausche\b|zum tausch|nur tausch",
    "wbs": r"\bwbs\b|wohnberechtigungsschein",
    "befristet": r"\bbefristet|zwischenmiete|untermiete",
    "wg": r"\bwg-?zimmer|\bwg\b|mitbewohner",
    "senioren": r"seniorenwohn|betreutes wohnen|ab 60 jahre",
    # beim Kauf relevant
    "versteigerung": r"zwangsversteigerung|\bversteigerung|versteigerungstermin",
    "erbpacht": r"erbpacht|erbbaurecht|erbbauzins",
    "vermietet": r"(?<!un)vermietet(?:e)?\s+(?:wohnung|einheit|an\b)|derzeit vermietet|aktuell vermietet|"
                 r"ist vermietet|mietverh(?:ä|ae)ltnis besteht|jahresnettokaltmiete|mieteinnahmen",
    "provisionsfrei": r"provisionsfrei|ohne makler|keine k(?:ä|ae)uferprovision|von privat",
    "denkmal": r"denkmalschutz|denkmalgesch(?:ü|ue)tzt|baudenkmal",
}

FLOOR_WORDS = {
    "souterrain": -1, "untergeschoss": -1, "ug": -1,
    "erdgeschoss": 0, "eg": 0, "hochparterre": 0, "parterre": 0,
}


def parse_de_number(s: str | None) -> float | None:
    """'1.234,56' -> 1234.56, '45,5' -> 45.5, '189.000' -> 189000."""
    if not s:
        return None
    s = s.strip().replace(" ", "").replace(" ", "")
    try:
        if "." in s and "," in s:
            return float(s.replace(".", "").replace(",", "."))
        if "," in s:
            return float(s.replace(",", "."))
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):
            return float(s.replace(".", ""))
        return float(s)
    except ValueError:
        return None


def _first(patterns: list[str], text: str, lo: float | None = None, hi: float | None = None,
           flags: int = re.IGNORECASE) -> float | None:
    for pat in patterns:
        for m in re.finditer(pat, text, flags):
            v = parse_de_number(m.group(1))
            if v is None:
                continue
            if lo is not None and v < lo:
                continue
            if hi is not None and v > hi:
                continue
            return v
    return None


def classify_heating(value: str, structured: bool) -> set[str]:
    """Ordnet einen Text Energieträger-Kategorien zu."""
    v = value.lower()
    found: set[str] = set()
    for cat, pats in HEATING_KEYWORDS.items():
        for p in pats:
            for m in re.finditer(p, v):
                # einfache Negation: "keine Fernwärme", "kein Gas"
                before = v[max(0, m.start() - 12):m.start()]
                if re.search(r"\bkein(?:e|en)?\s*$|\bohne\s*$", before):
                    continue
                found.add(cat)
                break
    if structured:
        for p, cat in HEATING_ABBREV.items():
            if re.search(p, v):
                found.add(cat)
    else:
        # Freitext: "Strom"/"Solar"/"Holz" kommen oft in anderem Zusammenhang vor
        found -= {"strom", "solar", "holz_pellets"} if len(v) > 200 else set()
    # Nahwärme nur zusätzlich führen, wenn es nicht Teil von "Nah-/Fernwärme" ist
    return found


def extract(text: str, title: str = "", structured: dict[str, str] | None = None) -> dict[str, Any]:
    """Extrahiert Merkmale.

    text:       gesamter Inseratstext (Beschreibung + Detailfelder)
    title:      Titel des Inserats
    structured: optionale Label->Wert-Paare (z.B. Kleinanzeigen-Detailliste)
    """
    structured = structured or {}
    struct_text = "\n".join(f"{k}: {v}" for k, v in structured.items())
    full = f"{title}\n{struct_text}\n{text}"
    low = full.lower()
    d: dict[str, Any] = {}

    # --- Wohnfläche ---------------------------------------------------------
    d["area"] = _first([
        rf"(?:wohnfl(?:ä|ae)che|wohnfl\.|wfl\.?|nutzfl(?:ä|ae)che|fl(?:ä|ae)che){SEP}{NUM}\s*(?:m²|m2|qm|m\b|quadratmeter)?",
        rf"{NUM}\s*(?:m²|m2|qm|quadratmeter)",
    ], full, lo=8, hi=400)

    # --- Zimmer ------------------------------------------------------------
    d["rooms"] = _first([
        r"(?:zimmeranzahl|anzahl\s+zimmer|zimmer)\s*:\s*(\d{1,2}(?:[.,]5)?)",
        r"(\d{1,2}(?:[.,]5)?)\s*-?\s*(?:zimmer|zi\.?|raum|zkb|zk\b)",
        r"(?:zimmer|zi\.)\s+(\d{1,2}(?:[.,]5)?)\b",
    ], full, lo=0.5, hi=12)
    if d["rooms"] is None and re.search(r"\b(?:1-zi|einzimmer|ein-zimmer|appartement|apartment)\b", low):
        d["rooms"] = 1.0

    # --- Baujahr -----------------------------------------------------------
    year = _first([
        r"(?:baujahr|bj\.?|erbaut|errichtet|baujahr\s*\(?(?:laut|lt\.?)\s*energieausweis\)?)"
        r"\s*(?:[:\-]\s*)?(?:ca\.?\s*|im\s+jahr\s*)?((?:18|19|20)\d{2})\b",
    ], full, lo=1800, hi=2035)
    d["year"] = int(year) if year else None

    # --- Preise ------------------------------------------------------------
    d["rent_cold"] = _first([
        rf"(?:nettokaltmiete|kaltmiete|miete\s*\(kalt\)|miete\s+kalt|grundmiete){SEP}{NUM}{EUR}?",
    ], full, lo=50, hi=20000)
    d["rent_warm"] = _first([
        rf"(?:warmmiete|gesamtmiete|miete\s*\(warm\)|miete\s+warm|bruttomiete){SEP}{NUM}{EUR}?",
    ], full, lo=50, hi=25000)
    d["service_charges"] = _first([
        rf"(?:nebenkosten|betriebskosten|nk-?vorauszahlung|nk){SEP}{NUM}{EUR}",
    ], full, lo=0, hi=3000)
    d["heating_costs"] = _first([
        rf"heizkosten(?:vorauszahlung)?{SEP}{NUM}{EUR}",
    ], full, lo=0, hi=2000)
    d["purchase_price"] = _first([
        rf"kaufpreis{SEP}{NUM}{EUR}?",
    ], full, lo=10000, hi=10_000_000)
    d["hausgeld"] = _first([rf"hausgeld{SEP}{NUM}{EUR}?"], full, lo=10, hi=3000)
    # generischer Preis aus Struktur (z.B. Kleinanzeigen "Preis: 650 €")
    d["price_generic"] = _first([rf"preis{SEP}{NUM}{EUR}"], struct_text or full, lo=50, hi=10_000_000)

    # --- Etage -------------------------------------------------------------
    floor = _first([
        r"(?:etage|geschoss|stockwerk)\s*:\s*(\d{1,2})\b",
        r"(\d{1,2})\s*\.\s*(?:og\b|obergeschoss|etage|stock)",
    ], full, lo=0, hi=40)
    if floor is None:
        m = re.search(r"\b(souterrain|untergeschoss|erdgeschoss|hochparterre|parterre)\b", low)
        if m:
            floor = FLOOR_WORDS[m.group(1)]
    d["floor"] = int(floor) if floor is not None else None
    d["top_floor"] = bool(re.search(r"dachgeschoss|\bdg\b|penthouse", low))

    # --- Energie -----------------------------------------------------------
    m = re.search(r"energieeffizienzklasse\s*[:\-]?\s*(a\+|[a-h])\b", low)
    d["energy_class"] = m.group(1).upper() if m else None
    d["energy_value"] = _first([
        rf"(?:endenergie(?:verbrauch|bedarf)|energieverbrauchskennwert|energiebedarf|energieverbrauch"
        rf"|endenergie){SEP}{NUM}\s*kwh",
    ], full, lo=1, hi=1000)

    # --- Heizung -----------------------------------------------------------
    struct_sources: set[str] = set()
    for k, v in structured.items():
        if re.search(HEATING_LABELS, k, re.IGNORECASE):
            struct_sources |= classify_heating(v, structured=True)
    for m in re.finditer(HEATING_LABELS + r"\s*:\s*([^\n]{1,80})", full, re.IGNORECASE):
        struct_sources |= classify_heating(m.group(1), structured=True)

    free_sources = classify_heating(full, structured=False)
    sources = struct_sources or free_sources
    # Fernwärme-Erwähnung im Freitext ist ein starkes Signal, auch wenn das
    # Strukturfeld nur z.B. "Zentralheizung" sagt.
    if "fernwaerme" in free_sources:
        sources = sources | {"fernwaerme"}
    d["heating_sources"] = sorted(sources)
    d["heating_from"] = "struktur" if struct_sources else ("freitext" if free_sources else None)
    d["heating_systems"] = sorted(k for k, p in HEATING_SYSTEMS.items() if re.search(p, low))
    if "fernwaerme" in sources:
        d["fernwaerme"] = "ja"
    elif struct_sources:
        d["fernwaerme"] = "nein"
    elif free_sources - {"solar", "strom"}:
        d["fernwaerme"] = "eher_nein"
    else:
        d["fernwaerme"] = "unbekannt"

    # --- Lage --------------------------------------------------------------
    m = re.search(r"\b(9[01]\d{3})\b", full)
    d["plz"] = m.group(1) if m else None

    # --- Ausstattung & Hinweise --------------------------------------------
    d["features"] = sorted(k for k, p in FEATURES.items() if re.search(p, low))
    flags = {k for k, p in EXCLUDE_HINTS.items() if re.search(p, low)}
    if re.match(r"\s*(?:suche|gesucht|wir suchen|ich suche)\b", title, re.IGNORECASE):
        flags.add("gesuch")
    d["flags"] = sorted(flags)

    m = re.search(r"(?:verf(?:ü|ue)gbar\s*ab|frei\s*ab|bezugsfrei\s*ab|bezug\s*ab)\s*[:\-]?\s*"
                  r"(sofort|\d{1,2}\.\d{1,2}\.\d{2,4}|\d{1,2}/\d{4}|[a-zäöü]+\s+\d{4})", low)
    d["available_from"] = m.group(1) if m else None

    # Preis pro m²
    price = d["rent_cold"] or d["purchase_price"] or d["price_generic"]
    d["price_per_sqm"] = round(price / d["area"], 2) if price and d["area"] else None
    return d
