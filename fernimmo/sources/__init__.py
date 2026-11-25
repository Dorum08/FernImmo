from __future__ import annotations

from typing import Any

from ..fetch import Fetcher
from .base import Source
from .immoscout import ImmoScout24
from .immowelt import Immowelt
from .kleinanzeigen import Kleinanzeigen

REGISTRY: dict[str, type[Source]] = {
    "kleinanzeigen": Kleinanzeigen,
    "immowelt": Immowelt,
    "immoscout24": ImmoScout24,
    "generic": Source,
}


def build_source(name: str, cfg: dict[str, Any], fetcher: Fetcher) -> Source:
    kind = cfg.get("type", name)
    cls = REGISTRY.get(kind)
    if cls is None:
        raise ValueError(f"Unbekannter Quellentyp: {kind}")
    src = cls(cfg, fetcher)
    if kind == "generic":
        if not cfg.get("link_pattern"):
            raise ValueError(f"Quelle {name}: 'link_pattern' fehlt")
        src.name = name
    return src
