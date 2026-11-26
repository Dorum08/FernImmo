from __future__ import annotations

from typing import Any

from ..fetch import Fetcher
from .base import Source
from .generic import PORTALS, GenericSource
from .immoscout import ImmoScout24
from .immowelt import Immowelt
from .kleinanzeigen import Kleinanzeigen

REGISTRY: dict[str, type[Source]] = {
    "kleinanzeigen": Kleinanzeigen,
    "immowelt": Immowelt,
    "immoscout24": ImmoScout24,
    "generic": GenericSource,
    **{name: GenericSource for name in PORTALS},
}


def build_source(name: str, cfg: dict[str, Any], fetcher: Fetcher) -> Source:
    kind = cfg.get("type", name)
    cls = REGISTRY.get(kind)
    if cls is None:
        raise ValueError(f"Unbekannter Quellentyp: {kind} (bekannt: {', '.join(sorted(REGISTRY))})")
    src = cls({**cfg, "portal": kind} if cls is GenericSource else cfg, fetcher)
    # Eigener Name je Konfigurationseintrag (z. B. zweiter Kleinanzeigen-Eintrag für Vergleichsmieten)
    src.name = name
    return src
