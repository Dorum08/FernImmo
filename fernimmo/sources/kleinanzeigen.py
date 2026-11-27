"""Kleinanzeigen.de – Wohnungen mieten/kaufen."""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..fetch import NotFoundError
from ..models import RawListing
from .base import SearchHit, Source, card_snippet, clean_soup, is_http, meta, text_of


class Kleinanzeigen(Source):
    name = "kleinanzeigen"
    base_url = "https://www.kleinanzeigen.de"
    link_pattern = r"/s-anzeige/[^\"'?#]+/(\d+)-\d+-\d+"

    def default_urls(self) -> list:
        # Bundesweit, neueste zuerst: deckt jede Suche ab, egal wo ihr Gebiet liegt.
        # Die PLZ in der Ergebniskachel sortiert alles außerhalb der Suchgebiete vorab aus.
        return [{"url": f"{self.base_url}/s-wohnung-kaufen/anzeige:angebote/c196", "type": "kauf", "feed": True}]

    def page_url(self, url: str, page: int) -> str | None:
        if page == 1:
            return url
        # .../s-wohnung-mieten/nuernberg/c203l6810 -> .../s-wohnung-mieten/nuernberg/seite:2/c203l6810
        url = re.sub(r"/seite:\d+", "", url)
        head, _, last = url.rstrip("/").rpartition("/")
        return f"{head}/seite:{page}/{last}"

    def search_hits(self, html: str, page_url: str, listing_type):
        soup = BeautifulSoup(html, "lxml")
        hits: dict[str, SearchHit] = {}
        for art in soup.select("article.aditem[data-adid]"):
            # Top-/Partneranzeigen von außerhalb überspringen
            href = art.get("data-href") or (art.select_one("a[href]") or {}).get("href")
            if not href:
                continue
            aid = art["data-adid"]
            if not is_http(urljoin(self.base_url, href)):
                continue
            # Ort (PLZ) zuerst, damit er nie hinter einem langen Beschreibungsanriss abgeschnitten wird
            loc = art.select_one(".aditem-main--top--left, [class*='aditem-main--top--left'], [class*='location']")
            text = art.get_text(" ", strip=True)
            snippet = (f"{loc.get_text(' ', strip=True)} · " if loc else "") + text
            hits.setdefault(aid, SearchHit(aid, urljoin(self.base_url, href), listing_type, snippet=snippet[:1000]))
        if hits:
            return list(hits.values())
        # Layout geändert? Inserats-Links suchen und den Text ihrer Kachel als Kurzinfo nehmen,
        # damit die Vorsortierung nach PLZ weiter funktioniert.
        for a in soup.find_all("a", href=True):
            m = re.search(self.link_pattern, a["href"])
            if not m:
                continue
            url = urljoin(self.base_url, a["href"].split("#")[0])
            if is_http(url):
                aid = m.group(1)
                same = lambda h, aid=aid: f"/{aid}-" in h or "/s-anzeige/" not in h
                hits.setdefault(aid, SearchHit(aid, url, listing_type, snippet=card_snippet(a, same)))
        return list(hits.values())

    def parse_detail(self, html: str, hit: SearchHit) -> RawListing:
        soup = clean_soup(html)
        if not soup.select_one("#viewad-description-text"):
            # Nur als gelöscht werten, wenn die Seite das ausdrücklich sagt – sonst könnte
            # eine Layoutänderung alle Inserate fälschlich "offline" schalten.
            if re.search(r"nicht mehr verfügbar|wurde gelöscht|nicht mehr aktiv|deaktiviert", html, re.I):
                raise NotFoundError(f"Anzeige {hit.source_id} gelöscht oder deaktiviert")
            return super().parse_detail(html, hit)
        title = text_of(soup.select_one("#viewad-title")) or meta(soup, "og:title") or ""
        price = text_of(soup.select_one("#viewad-price"))
        desc = text_of(soup.select_one("#viewad-description-text"))
        address = " ".join(
            t for t in (text_of(soup.select_one("#street-address")), text_of(soup.select_one("#viewad-locality"))) if t
        )
        structured: dict[str, str] = {}
        if price:
            structured["Preis"] = price
        for li in soup.select("li.addetailslist--detail"):
            val_node = li.select_one(".addetailslist--detail--value")
            val = text_of(val_node)
            if val_node:
                val_node.extract()
            label = text_of(li).strip(" :")
            if label:
                structured[label] = val
        tags = [text_of(t) for t in soup.select("li.checktag")]
        if tags:
            structured["Ausstattung"] = ", ".join(tags)
        if address:
            structured["Ort"] = address
        img = soup.select_one("#viewad-image")
        title = re.sub(r"^\s*(?:Reserviert|Gelöscht)\s*•\s*", "", title)
        return RawListing(
            source=self.name, source_id=hit.source_id, url=hit.url, title=title.strip(), text=desc,
            structured=structured, address=address,
            image_url=(img.get("src") if img else None) or meta(soup, "og:image"),
        )
