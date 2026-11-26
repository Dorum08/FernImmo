"""Kostenrechner für Kaufwohnungen: Kaufnebenkosten, Finanzierung, monatliche
Belastung, Mietrendite. Dieselbe Formel steckt als JavaScript in detail.html
(für die interaktive Variante) – bei Änderungen beide anpassen."""
from __future__ import annotations

from typing import Any


def annuity_month(loan: float, zins_pct: float, tilgung_pct: float) -> float:
    """Monatsrate einer Annuität mit anfänglicher Tilgung (übliche Bankrechnung)."""
    return max(0.0, loan) * (zins_pct + tilgung_pct) / 100 / 12


def costs(price: float | None, area: float | None, hausgeld: float | None, flags: list[str] | None,
          p: dict[str, Any], rent_per_year: float | None = None) -> dict[str, Any] | None:
    if not price:
        return None
    makler = 0.0 if "provisionsfrei" in (flags or []) else p["makler_pct"]
    nk_pct = p["grunderwerbsteuer_pct"] + p["notar_grundbuch_pct"] + makler
    nebenkosten = price * nk_pct / 100
    total = price + nebenkosten
    loan = max(0.0, total - p["eigenkapital"])
    rate = annuity_month(loan, p["zins_pct"], p["tilgung_pct"])
    hg = hausgeld or 0.0
    out = dict(
        makler_pct=makler, nebenkosten_pct=nk_pct, nebenkosten=round(nebenkosten), gesamt=round(total),
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
