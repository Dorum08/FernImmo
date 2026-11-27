"""Lernfähiger Adapter für Portale ohne eigenen Code (Ohne-Makler, Immobilo, Immobilien.de,
meinestadt.de, IVD24, RegionalImmobilien24, 1A-Immobilienmarkt und beliebige weitere).

Suche: Gibt es kein festes `link_pattern`, wird auf der Ergebnisseite das häufigste
Link-Muster mit Objektnummer gesucht (z. B. /expose/{id}) – das sind fast immer die
Inserate. Links auf Portale mit eigenem Adapter (Immowelt, Kleinanzeigen, ImmoScout24),
wie sie Meta-Suchen liefern, werden mit genau diesem Adapter ausgewertet.

Detailseite: sichtbarer Text, „Label: Wert“-Paare, schema.org-Daten (JSON-LD) und
Koordinaten. Die eigentliche Merkmalserkennung macht extract.py wie bei allen Quellen.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..models import RawListing
from .base import SearchHit, Source, card_snippet, clean_soup, json_ld, meta, pair_labels, text_of

log = logging.getLogger(__name__)

# Bekannte Portale: Domain und typische Wörter in Exposé-Links (nur als Vorzug bei der
# Mustererkennung – die Erkennung funktioniert auch ohne).
PORTALS: dict[str, dict[str, Any]] = {
    "ohne-makler": {"domain": "ohne-makler.net", "hints": ["immobilie", "expose", "objekt"]},
    "immobilo": {"domain": "immobilo.de", "hints": ["expose", "immobilie", "objekt", "angebot"]},
    "immobilien.de": {"domain": "immobilien.de", "hints": ["expose", "immobilie", "objekt"]},
    "meinestadt": {"domain": "meinestadt.de", "hints": ["expose", "immobilie", "objekt", "angebot"]},
    "ivd24": {"domain": "ivd24immobilien.de", "hints": ["expose", "immobilie", "objekt"]},
    "regionalimmobilien24": {"domain": "regionalimmobilien24.de", "hints": ["expose", "immobilie", "objekt"]},
    "1a-immobilienmarkt": {"domain": "1a-immobilienmarkt.de", "hints": ["expose", "immobilie", "objekt"]},
}

# Domains, für die es einen eigenen Adapter gibt (Weiterleitung von Meta-Suchen)
DELEGATE = {"immowelt.de": "immowelt", "immonet.de": "immowelt", "kleinanzeigen.de": "kleinanzeigen",
            "immobilienscout24.de": "immoscout24"}

_SKIP_EXT = re.compile(r"\.(?:jpe?g|png|gif|webp|svg|pdf|css|js|ico)(?:$|\?)", re.I)
_NAV_WORDS = re.compile(r"(?:login|logout|register|merkliste|impressum|datenschutz|agb|kontakt|newsletter|"
                        r"magazin|ratgeber|news|blog|hilfe|faq|karriere|cookie|seite[:=]|page=|sort=)", re.I)
_ID = re.compile(r"\d{4,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|(?=[a-z0-9]*\d)[a-z0-9]{7,12}(?![a-z-])", re.I)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def shape(url: str) -> str | None:
    """Muster eines Links: Objektnummern und lange Slugs werden zu Platzhaltern."""
    u = urlparse(url)
    if not u.scheme.startswith("http") or _SKIP_EXT.search(u.path):
        return None
    segs, has_id = [], False
    parts = [s for s in u.path.split("/") if s]
    for i, seg in enumerate(parts):
        # Orts-/PLZ-Unterseiten (…/nuernberg:90453/…, …/90427/kaufen/…) sind keine Objektnummern
        if re.fullmatch(r"\D+\b\d{5}\b\D*|\D*\b\d{5}\b\D+", seg) or (re.fullmatch(r"\d{5}", seg) and i < len(parts) - 1):
            segs.append("{plz}")
        elif _ID.search(seg) and re.search(r"\d", seg):
            segs.append("{id}")
            has_id = True
        elif len(seg) > 25 or seg.count("-") >= 3:
            segs.append("{slug}")
        else:
            segs.append(seg.lower())
    if not has_id:
        return None
    return _host(url) + "/" + "/".join(segs)


def listing_id(url: str) -> str:
    """Stabile ID aus dem Link: Domain + längste Ziffernfolge bzw. Kennung im Pfad."""
    u = urlparse(url)
    ids = _ID.findall(u.path)
    best = max(ids, key=len) if ids else u.path.strip("/")
    return f"{_host(url)}:{best}"


class GenericSource(Source):
    name = "generic"

    def __init__(self, cfg: dict[str, Any], fetcher):
        super().__init__(cfg, fetcher)
        preset = PORTALS.get(cfg.get("portal") or cfg.get("type") or "", {})
        self.domain = cfg.get("domain") or preset.get("domain")
        self.hints = cfg.get("hints") or preset.get("hints") or ["expose", "immobilie", "objekt"]
        self._delegates: dict[str, Source] = {}

    # ---------------------------------------------------------------- suche
    def search_hits(self, html: str, page_url: str, listing_type: str | None) -> list[SearchHit]:
        if self.link_pattern:
            hits = super().search_hits(html, page_url, listing_type)
            if hits:
                return hits
            log.info("[%s] link_pattern ohne Treffer – versuche automatische Erkennung", self.name)
        soup = BeautifulSoup(html, "lxml")
        links: dict[str, str] = {}  # url -> shape
        anchors: dict[str, Any] = {}
        own = _host(page_url)
        for a in soup.find_all("a", href=True):
            url = urljoin(page_url, a["href"].split("#")[0])
            host = _host(url)
            if host != own and not host.endswith("." + own) and host not in DELEGATE \
                    and not any(host.endswith(d) for d in DELEGATE) and host != (self.domain or own):
                continue
            if _NAV_WORDS.search(url):
                continue
            sh = shape(url)
            if sh:
                links[url] = sh
                anchors.setdefault(url, a)
        if not links:
            return []
        counts = Counter(links.values())

        def score(item):
            sh, n = item
            bonus = 3 if any(h in sh for h in self.hints) else 0
            bonus += 3 if any(sh.startswith(d) for d in DELEGATE) and own not in sh else 0
            return n + bonus

        best, n = max(counts.items(), key=score)
        if n < 2:
            log.info("[%s] Kein eindeutiges Link-Muster auf %s", self.name, page_url)
            return []
        log.debug("[%s] Link-Muster %s (%d×)", self.name, best, n)
        out: dict[str, SearchHit] = {}
        for url, sh in links.items():
            if sh == best:
                lid = listing_id(url)
                same = lambda h, lid=lid: listing_id(urljoin(page_url, h)) == lid or shape(urljoin(page_url, h)) is None
                out.setdefault(lid, SearchHit(lid, url, listing_type, snippet=card_snippet(anchors[url], same)))
        return list(out.values())

    # --------------------------------------------------------------- detail
    def _delegate(self, url: str) -> Source | None:
        host = _host(url)
        for dom, kind in DELEGATE.items():
            if host == dom or host.endswith("." + dom):
                if kind not in self._delegates:
                    from . import REGISTRY
                    self._delegates[kind] = REGISTRY[kind]({}, self.fetcher)
                return self._delegates[kind]
        return None

    def parse_detail(self, html: str, hit: SearchHit) -> RawListing:
        d = self._delegate(hit.url)
        if d is not None:
            raw = d.parse_detail(html, hit)
            raw.source = self.name
            return raw
        soup = clean_soup(html)
        for n in soup.select('aside, [class*="similar"], [class*="Similar"], [class*="recommend"], '
                             '[id*="similar"], [class*="teaser-list"], [class*="related"]'):
            n.decompose()
        nodes = []
        for sel in self.content_selectors:
            nodes += soup.select(sel)
        if not nodes:
            nodes = [soup.find("main") or soup.find("article") or soup.body or soup]
        text = "\n".join(text_of(n) for n in nodes)
        h1 = soup.find("h1")
        title = (text_of(h1) if h1 else "") or meta(soup, "og:title") or ""
        structured = pair_labels(text)
        address = ""
        for k, v in schema_org(html).items():
            if k == "Adresse":
                address = v
            structured.setdefault(k, v)
        return RawListing(source=self.name, source_id=hit.source_id, url=hit.url, title=title.strip(),
                          text=text, structured=structured, address=address, image_url=meta(soup, "og:image"))


# ---------------------------------------------------------------------------- schema.org
def _walk(o: Any):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _val(v: Any) -> str | None:
    if isinstance(v, dict):
        v = v.get("value") or v.get("name")
    return None if v in (None, "") else str(v)


def schema_org(html: str) -> dict[str, str]:
    """Übersetzt schema.org-Angaben (JSON-LD) in die Labels, die extract.py versteht."""
    out: dict[str, str] = {}
    for doc in json_ld(html):
        for o in _walk(doc):
            if o.get("numberOfRooms") is not None:
                out.setdefault("Zimmer", _val(o["numberOfRooms"]) or "")
            if o.get("floorSize") is not None and _val(o["floorSize"]):
                out.setdefault("Wohnfläche", f"{_val(o['floorSize'])} m²")
            if o.get("yearBuilt"):
                out.setdefault("Baujahr", str(o["yearBuilt"]))
            if o.get("@type") in ("Offer", "AggregateOffer") and o.get("price"):
                out.setdefault("Preis", f"{o['price']} €")
            addr = o.get("address")
            if isinstance(addr, dict) and (addr.get("postalCode") or addr.get("streetAddress")):
                parts = [addr.get("streetAddress"), " ".join(p for p in (addr.get("postalCode"),
                                                                       addr.get("addressLocality")) if p)]
                out.setdefault("Adresse", ", ".join(p for p in parts if p))
    return {k: v for k, v in out.items() if v}
