"""Kostenrechner für Kaufwohnungen: Kaufnebenkosten, Finanzierung, monatliche
Belastung, Mietrendite. Dieselbe Formel steckt als JavaScript in detail.html
(für die interaktive Variante) – bei Änderungen beide anpassen."""
from __future__ import annotations

from typing import Any

# Grunderwerbsteuer in % je Bundesland (Stand 2025/26 – Sätze ändern sich gelegentlich;
# in config.yaml unter calculator.grunderwerbsteuer_laender überschreibbar)
GRUNDERWERBSTEUER = {
    "BW": 5.0, "BY": 3.5, "BE": 6.0, "BB": 6.5, "HB": 5.5, "HH": 5.5, "HE": 6.0, "MV": 6.0,
    "NI": 5.0, "NW": 6.5, "RP": 5.0, "SL": 6.5, "SN": 5.5, "ST": 5.0, "SH": 6.5, "TH": 5.0,
}
STATE_NAMES = {
    "BW": "Baden-Württemberg", "BY": "Bayern", "BE": "Berlin", "BB": "Brandenburg", "HB": "Bremen",
    "HH": "Hamburg", "HE": "Hessen", "MV": "Mecklenburg-Vorpommern", "NI": "Niedersachsen",
    "NW": "Nordrhein-Westfalen", "RP": "Rheinland-Pfalz", "SL": "Saarland", "SN": "Sachsen",
    "ST": "Sachsen-Anhalt", "SH": "Schleswig-Holstein", "TH": "Thüringen",
}
FALLBACK_GREST = 5.0


def grest_pct(p: dict[str, Any], state: str | None) -> tuple[float, str]:
    """Grunderwerbsteuersatz und Herkunft ('Bayern', 'Vorgabe', 'unbekanntes Land')."""
    if p.get("grunderwerbsteuer_pct") is not None:
        return float(p["grunderwerbsteuer_pct"]), "Vorgabe"
    table = {**GRUNDERWERBSTEUER, **(p.get("grunderwerbsteuer_laender") or {})}
    if state in table:
        return float(table[state]), STATE_NAMES.get(state, state)
    return FALLBACK_GREST, "Bundesland unbekannt"


def annuity_month(loan: float, zins_pct: float, tilgung_pct: float) -> float:
    """Monatsrate einer Annuität mit anfänglicher Tilgung (übliche Bankrechnung)."""
    return max(0.0, loan) * (zins_pct + tilgung_pct) / 100 / 12


def costs(price: float | None, area: float | None, hausgeld: float | None, flags: list[str] | None,
          p: dict[str, Any], rent_per_year: float | None = None, state: str | None = None) -> dict[str, Any] | None:
    if not price:
        return None
    makler = 0.0 if "provisionsfrei" in (flags or []) else p["makler_pct"]
    grest, grest_from = grest_pct(p, state)
    nk_pct = grest + p["notar_grundbuch_pct"] + makler
    nebenkosten = price * nk_pct / 100
    total = price + nebenkosten
    loan = max(0.0, total - p["eigenkapital"])
    rate = annuity_month(loan, p["zins_pct"], p["tilgung_pct"])
    hg = hausgeld or 0.0
    out = dict(
        makler_pct=makler, grunderwerbsteuer_pct=grest, grunderwerbsteuer_von=grest_from, nebenkosten_pct=nk_pct, nebenkosten=round(nebenkosten), gesamt=round(total),
        darlehen=round(loan), rate=round(rate), hausgeld=hg, belastung=round(rate + hg),
    )
    rent = rent_per_year or (area * p["miete_pro_qm"] * 12 if area else None)
    if rent:
        out["miete_jahr"] = round(rent)
        out["miete_geschaetzt"] = rent_per_year is None
        out["bruttorendite_pct"] = round(rent / price * 100, 2)
        out["kaufpreisfaktor"] = round(price / rent, 1)
        not_umlage = hg * 12 * p["hausgeld_nicht_umlagefaehig_pct"] / 100
        out["nettorendite_pct"] = round((rent - not_umlage) / total * 100, 2)
    return out
