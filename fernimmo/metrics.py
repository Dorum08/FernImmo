"""Kennzahlen je Wohnung für Ergebnislisten und Detailseite.

Alle Werte sind Schätzungen aus den Daten, die FernImmo selbst gesammelt hat – keine
Gutachten. Grundlage:
- Vergleichswert: Median-€/m² ähnlicher Inserate derselben PLZ (sonst aller) × Wohnfläche
- Miete: tatsächliche Kaltmiete, wenn vermietet und angegeben, sonst Vergleichsmiete
  (gesammelte Mietinserate der PLZ bzw. calculator.miete_pro_qm)
- Cashflow: Miete − Kreditrate − nicht umlagefähiges Hausgeld, Finanzierung nach
  calculator.eigenkapital_pct / zins_pct / tilgung_pct
- AfA/Restnutzungsdauer: lineare AfA nach Baujahr (§ 7 Abs. 4 EStG) und Restnutzungsdauer
  bei 80 Jahren Gesamtnutzungsdauer (Wohngebäude, ohne Modernisierungen)
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from .calc import grest_pct
from .geo import plz_state

GESAMTNUTZUNGSDAUER = 80


def _actual_rent(r: dict) -> float | None:
    """Tatsächliche Monatskaltmiete bei vermieteten Kaufobjekten (Jahresmieten werden umgerechnet)."""
    d = r["data"]
    rent = d.get("rent_cold")
    if not rent or "vermietet" not in (d.get("flags") or []):
        return None
    if r.get("price") and rent > 3000:          # „Jahresnettokaltmiete 7.200 €“
        rent = rent / 12
    return rent


def city_of(address: str | None, plz: str | None) -> str:
    """„90443 Nürnberg - Gostenhof“ / „Fürther Str. 12, 90443 Nürnberg“ -> „Nürnberg“."""
    a = (address or "").strip()
    m = re.search(r"\b\d{5}\s+([^,\-–(]+)", a)
    if m:
        return m.group(1).strip()
    parts = [x.strip() for x in re.split(r"[,–-]", a) if x.strip() and not re.search(r"\d", x)]
    return parts[0] if parts else ""


def afa_info(year: int | None, today: date | None = None) -> dict | None:
    if not year:
        return None
    today = today or date.today()
    linear = 2.5 if year < 1925 else (3.0 if year >= 2023 else 2.0)
    age = max(0, today.year - year)
    rnd = max(0, GESAMTNUTZUNGSDAUER - age)
    # Bei kurzer Restnutzungsdauer ist per Gutachten eine höhere AfA möglich (100 / RND)
    gutachten = round(100 / rnd, 1) if 0 < rnd < 100 / linear else None
    return {"linear_pct": linear, "restnutzungsdauer": rnd, "alter": age,
            "gutachten_pct": gutachten, "afa_pct": max(linear, gutachten or 0)}


def enrich(rows: list[dict], db, calc_cfg: dict[str, Any]) -> list[dict]:
    """Ergänzt je Zeile: vw (Vergleichswert €), vw_pct, miete, miete_quelle, rendite_ist/soll,
    cashflow, afa. Erwartet Zeilen aus db.search/db.get (mit market)."""
    p = calc_cfg
    ek_pct = float(p.get("eigenkapital_pct", 20))
    zins, tilg = float(p["zins_pct"]), float(p["tilgung_pct"])
    for r in rows:
        d = r["data"]
        price, area = r.get("price"), d.get("area")
        m = r.get("market")
        r["vw"] = round(m["median"] * area) if m and area else None
        r["vw_pct"] = m["dev"] if m else None
        actual = _actual_rent(r) if r.get("listing_type") == "kauf" else None
        est = db.rent_estimate(r.get("plz"))
        est_sqm = est[0] if est else float(p["miete_pro_qm"])
        r["miete_soll"] = round(est_sqm * area) if area else None
        r["miete"] = round(actual) if actual else r["miete_soll"]
        r["miete_quelle"] = "laut Inserat" if actual else (est[1] if est else "Vorgabe")
        r["rendite_ist"] = round(actual * 12 / price * 100, 1) if actual and price else None
        r["rendite_soll"] = round(r["miete_soll"] * 12 / price * 100, 1) if r["miete_soll"] and price else None
        r["cashflow"] = None
        if r.get("listing_type") == "kauf" and price and r["miete"]:
            makler = 0.0 if "provisionsfrei" in (d.get("flags") or []) else p["makler_pct"]
            grest, _ = grest_pct(p, plz_state(r.get("plz")))
            total = price * (1 + (grest + p["notar_grundbuch_pct"] + makler) / 100)
            loan = total * (1 - ek_pct / 100)
            rate = loan * (zins + tilg) / 100 / 12
            hg_nu = (d.get("hausgeld") or 0) * p["hausgeld_nicht_umlagefaehig_pct"] / 100
            r["cashflow"] = round(r["miete"] - rate - hg_nu, 2)
        r["afa"] = afa_info(d.get("year"))
        r["ort"] = city_of(r.get("address"), r.get("plz"))
    return rows


def financing_label(calc_cfg: dict[str, Any]) -> str:
    fmt = lambda v: f"{float(v):g}".replace(".", ",")
    return (f"{fmt(calc_cfg.get('eigenkapital_pct', 20))} % EK · {fmt(calc_cfg['zins_pct'])} % Zins · "
            f"{fmt(calc_cfg['tilgung_pct'])} % Tilgung")


def vw_level(pct: float | None) -> int:
    """0–5 Balken: je günstiger gegenüber dem Vergleichswert, desto mehr (5 = ≥20 % darunter)."""
    if pct is None:
        return 0
    for lvl, limit in ((5, -20), (4, -10), (3, 0), (2, 10), (1, 20)):
        if pct <= limit:
            return lvl
    return 0
