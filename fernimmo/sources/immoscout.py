"""ImmobilienScout24. Achtung: IS24 setzt starken Bot-Schutz ein. Mit dem reinen
HTTP-Abruf wird man meist geblockt; `fetcher: playwright` hilft manchmal."""
from __future__ import annotations

import re

from ..models import RawListing
from .base import SearchHit, Source, clean_soup, meta, pair_labels, text_of

# is24qa-Klassen -> Label (seit Jahren stabile Test-Hooks auf den Exposés)
QA_FIELDS = {
    "kaltmiete": "Kaltmiete", "warmmiete": "Warmmiete", "nebenkosten": "Nebenkosten",
    "heizkosten": "Heizkosten", "kaufpreis": "Kaufpreis", "hausgeld": "Hausgeld",
    "wohnflaeche-ca": "Wohnfläche", "wohnflaeche-main": "Wohnfläche", "zi": "Zimmer", "zi-main": "Zimmer",
    "etage": "Etage", "baujahr": "Baujahr", "heizungsart": "Heizungsart",
    "wesentliche-energietraeger": "Wesentliche Energieträger", "energietraeger": "Energieträger",
    "energieeffizienzklasse": "Energieeffizienzklasse", "endenergiebedarf": "Endenergiebedarf",
    "endenergieverbrauch": "Endenergieverbrauch", "bezugsfrei": "Verfügbar ab",
    "objektzustand": "Objektzustand",
}


class ImmoScout24(Source):
    name = "immoscout24"
    base_url = "https://www.immobilienscout24.de"
    link_pattern = r"/expose/(\d{6,})"

    def page_url(self, url: str, page: int) -> str | None:
        if page == 1:
            return url
        url = re.sub(r"[?&]pagenumber=\d+", "", url)
        return f"{url}{'&' if '?' in url else '?'}pagenumber={page}"

    def parse_detail(self, html: str, hit: SearchHit) -> RawListing:
        soup = clean_soup(html)
        structured: dict[str, str] = {}
        for cls, label in QA_FIELDS.items():
            node = soup.select_one(f".is24qa-{cls}")
            if node and text_of(node):
                structured.setdefault(label, text_of(node).replace("\n", " "))
        parts = [text_of(soup.select_one(f".is24qa-{c}")) for c in (
            "objektbeschreibung", "ausstattung", "lage", "sonstiges")]
        text = "\n".join(p for p in parts if p)
        if not text:
            main = soup.select_one("#is24-content") or soup.find("main") or soup.body
            text = text_of(main)
            structured = {**pair_labels(text), **structured}
        title = text_of(soup.select_one("#expose-title")) or meta(soup, "og:title") or ""
        address = text_of(soup.select_one(".address-block")).replace("\n", " ")
        return RawListing(
            source=self.name, source_id=hit.source_id, url=hit.url, title=title, text=text,
            structured=structured, address=address, image_url=meta(soup, "og:image"),
        )
