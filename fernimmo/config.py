"""Konfiguration laden (YAML) – Geheimnisse können per Umgebungsvariable kommen."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .matcher import Profile

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
            k: v for k, v in (raw.get("sources") or {}).items() if v and _bool(v.get("enabled", True))
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
        w = raw.get("web", {})
        self.web_host: str = w.get("host", "0.0.0.0")
        self.web_port: int = int(w.get("port", 8080))
        # Mehrere Nutzer (für Likes/Kommentare): web.users {name: passwort}
        users = dict(w.get("users") or {})
        if w.get("password"):
            users.setdefault(w.get("username", "admin"), w["password"])
        self.web_users: dict[str, str] = {u: str(p) for u, p in users.items() if p}
        self.timezone: str = raw.get("timezone", "Europe/Berlin")

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        with open(path, encoding="utf-8") as fh:
            return cls(yaml.safe_load(fh))
