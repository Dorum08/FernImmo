"""Immowelt.de (AVIV-Plattform, gleiche Technik wie Immonet)."""
from __future__ import annotations

import re

from ..models import RawListing
from .base import SearchHit, Source, clean_soup, meta, pair_labels, text_of


class Immowelt(Source):
    name = "immowelt"
    base_url = "https://www.immowelt.de"
    link_pattern = r"/expose/([A-Za-z0-9\-]{5,})"

    def search_hits(self, html, page_url, listing_type):
        hits = {}
        for m in re.finditer(r"""(?:https://www\.immowelt\.de)?/expose/([A-Za-z0-9\-]{5,})""", html):
            eid = m.group(1)
            hits.setdefault(eid, SearchHit(eid, f"{self.base_url}/expose/{eid}", listing_type))
        return list(hits.values())

    def page_url(self, url: str, page: int) -> str | None:
        if page == 1:
            return url
        url = re.sub(r"[?&]page=\d+", "", url)
        return f"{url}{'&' if '?' in url else '?'}page={page}"

    def parse_detail(self, html: str, hit: SearchHit) -> RawListing:
        soup = clean_soup(html)
        # Abschnitte des Exposés; Empfehlungen/ähnliche Objekte auslassen,
        # sonst landen fremde Flächen/Preise in der Auswertung.
        sections = [
            s for s in soup.select('[data-testid^="aviv.CDP.Sections"]')
            if not re.search(r"similar|recommend|ads|contact", s.get("data-testid", ""), re.I)
        ]
        if not sections:
            main = soup.find("main") or soup.body
            for n in main.select('[data-testid*="imilar"], [data-testid*="ecommend"], aside'):
                n.decompose()
            sections = [main]
        text = "\n".join(text_of(s) for s in sections)
        h1 = soup.find("h1")
        title = (text_of(h1) if h1 else "") or meta(soup, "og:title") or ""
        addr = soup.select_one('[data-testid="aviv.CDP.Sections.Location.Address"]') or soup.select_one(
            '[data-testid*="Address"]')
        return RawListing(
            source=self.name, source_id=hit.source_id, url=hit.url, title=title, text=text,
            structured=pair_labels(text), address=text_of(addr).replace("\n", " "),
            image_url=meta(soup, "og:image"),
        )
