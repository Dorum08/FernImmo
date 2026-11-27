"""Konfiguration laden (YAML) – Geheimnisse können per Umgebungsvariable kommen."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .matcher import Profile

# Vorgaben für den Kostenrechner (in config.yaml unter "calculator" änderbar)
CALC_DEFAULTS = {
    "grunderwerbsteuer_pct": None,    # None = automatisch nach Bundesland der PLZ (siehe calc.py)
    "notar_grundbuch_pct": 2.0,
    "makler_pct": 3.57,               # Käuferanteil inkl. MwSt.; 0 bei "provisionsfrei"
    "eigenkapital": 60000,
    "eigenkapital_pct": 20,           # für Cashflow in Ergebnislisten (Anteil an den Gesamtkosten)
    "zins_pct": 3.6,
    "tilgung_pct": 2.0,
    "miete_pro_qm": 12.0,             # Vergleichsmiete für die Renditeschätzung – bitte anpassen
    "hausgeld_nicht_umlagefaehig_pct": 35,
}

_ENV = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    """Ersetzt ${VAR} bzw. ${VAR:-default} in Strings rekursiv."""
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "ja", "on")
    return bool(v)


class Config:
    def __init__(self, raw: dict[str, Any]):
        raw = _expand(raw or {})
        self.raw = raw
        self.db_path: str = raw.get("db_path", "data/fernimmo.db")
        self.interval_minutes: float = float(raw.get("interval_minutes", 20))
        self.quiet_hours: list[int] | None = raw.get("quiet_hours")  # z.B. [1, 6]
        f = raw.get("fetch", {})
        self.fetcher: str = f.get("fetcher", "http")
        self.delay: tuple[float, float] = tuple(f.get("delay_seconds", [3, 7]))  # type: ignore[assignment]
        self.user_agent: str | None = f.get("user_agent")
        self.proxy: str | None = f.get("proxy") or None
        self.sources: dict[str, dict] = {
            k: v or {} for k, v in (raw.get("sources") or {}).items()
            if _bool((v or {}).get("enabled", True))
            # ohne Such-URL kein Abruf – außer Kleinanzeigen: dort gilt dann der bundesweite Feed
            and ((v or {}).get("urls") or (v or {}).get("type", k) == "kleinanzeigen")
        }
        self.profiles: list[Profile] = [Profile.from_dict(p) for p in raw.get("profiles", [])]
        n = raw.get("notify", {}) or {}
        for ch in n.values():
            if isinstance(ch, dict) and "enabled" in ch:
                ch["enabled"] = _bool(ch["enabled"])
        self.notify: dict[str, Any] = n
        self.notify_levels: list[str] = n.get("levels", ["voll", "teilweise"])
        self.notify_price_drops: bool = _bool(n.get("price_drops", True))
        self.notify_first_run: bool = _bool(n.get("first_run", False))
        # Warnung, wenn ein Portal so viele Läufe in Folge nichts liefert (0 = aus)
        self.alert_failed_runs: int = int(n.get("alert_failed_runs", 3))
        b = raw.get("backup") or {}
        self.backup_enabled: bool = _bool(b.get("enabled", True))
        self.backup_dir: str = b.get("dir") or str(Path(self.db_path).parent / "backups")
        self.backup_keep: int = int(b.get("keep", 14))
        w = raw.get("web", {})
        self.web_host: str = w.get("host", "0.0.0.0")
        self.web_port: int = int(w.get("port", 8080))
        # Mehrere Nutzer (für Likes/Kommentare): web.users {name: passwort}
        users = dict(w.get("users") or {})
        if w.get("password"):
            users.setdefault(w.get("username", "admin"), w["password"])
        self.web_users: dict[str, str] = {u: str(p) for u, p in users.items() if p}
        # Adresse der Weboberfläche, wie du sie vom Handy aus erreichst (z. B. über WireGuard);
        # für den „Details“-Knopf in Telegram und als Vorgabe in der Android-App
        self.public_url: str | None = (w.get("public_url") or "").rstrip("/") or None
        self.timezone: str = raw.get("timezone", "Europe/Berlin")
        g = raw.get("geocoding") or {}
        if "enabled" in g:
            g["enabled"] = _bool(g["enabled"])
        self.geocoding: dict[str, Any] = g
        u = raw.get("umgebung") or {}
        if "enabled" in u:
            u["enabled"] = _bool(u["enabled"])
        self.umgebung: dict[str, Any] = u      # „In der Nähe“ über OpenStreetMap/Overpass
        self.map_center: list[float] = raw.get("map_center", [51.1634, 10.4477])   # Mitte Deutschlands
        self.map_zoom: int = int(raw.get("map_zoom", 6 if "map_center" not in raw else 11))
        self.calculator: dict[str, Any] = {**CALC_DEFAULTS, **(raw.get("calculator") or {})}

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        with open(path, encoding="utf-8") as fh:
            return cls(yaml.safe_load(fh))
