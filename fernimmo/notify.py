"""Benachrichtigungen: Telegram, ntfy, E-Mail."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any

import httpx

log = logging.getLogger(__name__)

HEATING_NAMES = {
    "fernwaerme": "Fernwärme", "nahwaerme": "Nahwärme", "gas": "Gas", "oel": "Öl",
    "waermepumpe": "Wärmepumpe", "holz_pellets": "Holz/Pellets", "strom": "Strom",
    "bhkw": "BHKW", "solar": "Solar",
}


def fmt_num(v, suffix="", digits=0) -> str:
    if v is None:
        return "?"
    s = f"{v:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return s + suffix


def format_message(l: dict[str, Any], kind: str = "neu", old_price: float | None = None,
                   calc: dict[str, Any] | None = None) -> tuple[str, str]:
    """Liefert (Betreff, Text) für ein Inserat (dict aus der DB)."""
    d = l["data"]
    heating = ", ".join(HEATING_NAMES.get(h, h) for h in d.get("heating_sources", [])) or "unbekannt"
    level = {"voll": "✅ Treffer", "teilweise": "🟡 Möglicher Treffer"}.get(l["match_level"], "Inserat")
    if kind == "preis":
        level = "📉 Preis gesenkt"
    elif l.get("relisted_of"):
        level = "♻️ Wieder eingestellt"
    price_label = "Kaufpreis" if l.get("listing_type") == "kauf" else "Kaltmiete"
    lines = [
        f"{level}: {l['title']}",
        f"{fmt_num(d.get('rooms'), ' Zi.', 1 if (d.get('rooms') or 0) % 1 else 0)} · "
        f"{fmt_num(d.get('area'), ' m²', 0)} · {price_label} {fmt_num(l.get('price'), ' €')}"
        + (f" (vorher {fmt_num(old_price, ' €')})" if old_price else ""),
        f"Heizung: {heating}" + (" (aus Freitext)" if d.get("heating_from") == "freitext" else ""),
    ]
    extra = []
    if d.get("year"):
        extra.append(f"Bj. {d['year']}")
    if d.get("energy_class"):
        extra.append(f"Energieklasse {d['energy_class']}")
    if d.get("floor") is not None:
        extra.append(f"{d['floor']}. OG" if d["floor"] > 0 else "EG")
    if l.get("listing_type") == "kauf" and d.get("hausgeld"):
        extra.append(f"Hausgeld {fmt_num(d['hausgeld'], ' €')}")
    if d.get("price_per_sqm"):
        extra.append(f"{fmt_num(d['price_per_sqm'], ' €/m²', 2)}")
    if extra:
        lines.append(" · ".join(extra))
    m = l.get("market")
    if m:
        lines.append(f"Markt: {m['dev']:+d} % ggü. Median {m['label']} ({fmt_num(m['median'], ' €/m²')}, n={m['n']})")
    if l.get("relisted_of"):
        lines.append(f"Früher als #{l['relisted_of']} inseriert"
                     + (f", erster Preis {fmt_num(l.get('first_price'), ' €')}" if l.get("first_price") else "")
                     + f" – insgesamt {l.get('days_online', 0)} Tage am Markt")
    if calc:
        lines.append(f"≈ {fmt_num(calc['belastung'], ' €')}/Monat (Rate {fmt_num(calc['rate'], ' €')} + Hausgeld), "
                     f"Kaufnebenkosten {fmt_num(calc['nebenkosten'], ' €')}")
    if l.get("address"):
        lines.append(f"📍 {l['address']}")
    if l.get("match_reasons"):
        lines.append("Hinweis: " + "; ".join(l["match_reasons"]))
    if d.get("flags"):
        lines.append("⚠️ " + ", ".join(d["flags"]))
    lines.append(f"FernImmo-ID #{l['id']} · Quelle: {l['source']}")
    lines.append(l["url"])
    return lines[0], "\n".join(lines)


class Notifier:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg or {}

    def send(self, subject: str, body: str, url: str | None = None) -> None:
        sent = False
        for name, fn in (("telegram", self._telegram), ("ntfy", self._ntfy), ("email", self._email)):
            c = self.cfg.get(name) or {}
            if not c.get("enabled"):
                continue
            try:
                fn(c, subject, body, url)
                sent = True
            except Exception as e:  # noqa: BLE001 - ein Kanal darf die anderen nicht stoppen
                log.error("Benachrichtigung über %s fehlgeschlagen: %s", name, e)
        if not sent:
            log.info("Keine Benachrichtigung verschickt (kein Kanal aktiv): %s", subject)

    @staticmethod
    def _telegram(c, subject, body, url):
        r = httpx.post(
            f"https://api.telegram.org/bot{c['bot_token']}/sendMessage",
            json={"chat_id": c["chat_id"], "text": body, "disable_web_page_preview": False},
            timeout=20,
        )
        r.raise_for_status()

    @staticmethod
    def _ntfy(c, subject, body, url):
        headers = {"Title": subject.encode("utf-8").decode("latin-1", "ignore"), "Tags": "house"}
        if url:
            headers["Click"] = url
        if c.get("token"):
            headers["Authorization"] = f"Bearer {c['token']}"
        server = c.get("server", "https://ntfy.sh").rstrip("/")
        r = httpx.post(f"{server}/{c['topic']}", content=body.encode("utf-8"), headers=headers, timeout=20)
        r.raise_for_status()

    @staticmethod
    def _email(c, subject, body, url):
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = c.get("from") or c["username"]
        msg["To"] = c["to"]
        msg.set_content(body)
        port = int(c.get("port", 587))
        if port == 465:
            with smtplib.SMTP_SSL(c["host"], port, timeout=30) as s:
                s.login(c["username"], c["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(c["host"], port, timeout=30) as s:
                s.starttls()
                s.login(c["username"], c["password"])
                s.send_message(msg)
