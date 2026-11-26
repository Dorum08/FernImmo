"""Gemeinsame Logik aller Quellen: Suchseiten durchblättern, Exposé-Links finden,
Detailseiten in Text + strukturierte Felder zerlegen."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..fetch import Fetcher, NotFoundError
from ..models import RawListing

log = logging.getLogger(__name__)

# Labels, deren Wert auf Detailseiten oft in der nächsten Zeile steht
KNOWN_LABELS = [
    "Heizungsart", "Heizung", "Befeuerung", "Energieträger", "Wesentliche Energieträger",
    "Wesentlicher Energieträger", "Energiequelle", "Baujahr", "Wohnfläche", "Zimmer", "Etage",
    "Kaltmiete", "Warmmiete", "Nebenkosten", "Heizkosten", "Kaufpreis", "Hausgeld",
    "Energieeffizienzklasse", "Endenergieverbrauch", "Endenergiebedarf", "Verfügbar ab", "Bezugsfrei ab",
    "Objektzustand", "Baujahr laut Energieausweis",
]
_LABEL_RE = re.compile(r"^(" + "|".join(re.escape(l) for l in KNOWN_LABELS) + r")\s*:?$", re.IGNORECASE)


@dataclass
class SearchHit:
    source_id: str
    url: str
    listing_type: str | None


def guess_type(url: str) -> str | None:
    u = url.lower()
    if any(k in u for k in ("kaufen", "kauf", "distributiontypes=buy", "buy", "-kaufen")):
        return "kauf"
    if any(k in u for k in ("mieten", "miete", "distributiontypes=rent", "rent")):
        return "miete"
    return None


def clean_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "header", "footer", "nav", "form"]):
        tag.decompose()
    return soup


def text_of(node) -> str:
    if node is None:
        return ""
    txt = node.get_text("\n", strip=True)
    return re.sub(r"\n{2,}", "\n", txt)


def pair_labels(text: str) -> dict[str, str]:
    """Findet 'Label\\nWert'-Paare bekannter Labels im Fließtext."""
    out: dict[str, str] = {}
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for i, line in enumerate(lines[:-1]):
        m = _LABEL_RE.match(line)
        if m and not _LABEL_RE.match(lines[i + 1]):
            out.setdefault(m.group(1), lines[i + 1][:120])
    return out


def json_ld(html: str) -> list[Any]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            out.append(json.loads(s.string or ""))
        except (TypeError, ValueError):
            continue
    return out


def meta(soup: BeautifulSoup, prop: str) -> str | None:
    tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
    return tag.get("content") if tag and tag.get("content") else None


class Source:
    """Basisklasse. Unterklassen setzen `name`, `link_pattern` und überschreiben bei
    Bedarf `page_url`, `search_hits` und `parse_detail`."""

    name = "base"
    base_url = ""
    link_pattern = r""            # Regex mit Gruppe 1 = ID
    content_selectors: list[str] = []
    exclude_selectors: list[str] = []

    def __init__(self, cfg: dict[str, Any], fetcher: Fetcher):
        self.cfg = cfg
        self.fetcher = fetcher
        self.max_pages = int(cfg.get("max_pages", 3))
        self.max_details = int(cfg.get("max_details_per_run", 40))
        if cfg.get("link_pattern"):
            self.link_pattern = cfg["link_pattern"]
        if cfg.get("content_selector"):
            self.content_selectors = [cfg["content_selector"]]

    # ---------------------------------------------------------------- search
    def search_urls(self) -> Iterable[tuple[str, str | None]]:
        default = self.cfg.get("type_default")
        for entry in self.cfg.get("urls", []):
            if isinstance(entry, dict):
                yield entry["url"], entry.get("type") or default or guess_type(entry["url"])
            else:
                yield entry, default or guess_type(entry)

    def page_url(self, url: str, page: int) -> str | None:
        tpl = self.cfg.get("pagination")
        if page == 1:
            return url
        if tpl:
            return tpl.replace("{url}", url).replace("{page}", str(page))
        return None

    def search_hits(self, html: str, page_url: str, listing_type: str | None) -> list[SearchHit]:
        hits: dict[str, SearchHit] = {}
        for m in re.finditer(r"""href=["']([^"']+)["']""", html):
            href = m.group(1)
            mm = re.search(self.link_pattern, href)
            if mm:
                full = urljoin(page_url, href.split("#")[0])
                hits.setdefault(mm.group(1), SearchHit(mm.group(1), full, listing_type))
        return list(hits.values())

    def search(self) -> list[SearchHit]:
        all_hits: dict[str, SearchHit] = {}
        for url, ltype in self.search_urls():
            for page in range(1, self.max_pages + 1):
                purl = self.page_url(url, page)
                if not purl:
                    break
                try:
                    html = self.fetcher.get(purl)
                except NotFoundError:
                    # Folgeseite existiert nicht -> Ende dieser Suche
                    log.info("[%s] %s nicht gefunden", self.name, purl)
                    break
                hits = self.search_hits(html, purl, ltype)
                new = [h for h in hits if h.source_id not in all_hits]
                log.info("[%s] %s: %d Treffer (%d neu)", self.name, purl, len(hits), len(new))
                for h in new:
                    all_hits[h.source_id] = h
                if not new:
                    break
        return list(all_hits.values())

    # ---------------------------------------------------------------- detail
    def fetch_detail(self, hit: SearchHit) -> RawListing:
        html = self.fetcher.get(hit.url)
        raw = self.parse_detail(html, hit)
        if raw.lat is None:
            from ..geo import coords_from_html
            c = coords_from_html(html)
            if c:
                raw.lat, raw.lon, raw.geo_precision = c[0], c[1], "portal"
        if not raw.listing_type:
            raw.listing_type = hit.listing_type or guess_type(raw.title + " " + raw.text[:500])
        return raw

    def parse_detail(self, html: str, hit: SearchHit) -> RawListing:
        soup = clean_soup(html)
        for sel in self.exclude_selectors:
            for n in soup.select(sel):
                n.decompose()
        nodes = []
        for sel in self.content_selectors:
            nodes += soup.select(sel)
        if not nodes:
            nodes = [soup.find("main") or soup.body or soup]
        text = "\n".join(text_of(n) for n in nodes)
        h1 = soup.find("h1")
        title = (text_of(h1) if h1 else "") or meta(soup, "og:title") or ""
        return RawListing(
            source=self.name, source_id=hit.source_id, url=hit.url, title=title.strip(),
            text=text, structured=pair_labels(text), image_url=meta(soup, "og:image"),
        )
