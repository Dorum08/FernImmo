"""HTTP-Abruf mit höflichen Pausen; optional über einen echten Browser (Playwright)."""
from __future__ import annotations

import logging
import os
import random
import time

import httpx

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Nur eindeutige Sperrseiten-Merkmale – ein normales reCAPTCHA im Kontaktformular zählt nicht
BLOCK_MARKERS = (
    "ich bin kein roboter", "access denied", "zugriff verweigert", "please verify you are a human",
    "unusual traffic", "cf-challenge", "challenge-platform", "px-captcha", "captcha-delivery.com",
    "attention required! | cloudflare", "_incapsula_resource",
)


class BlockedError(RuntimeError):
    """Das Portal hat die Anfrage als Bot erkannt."""


class NotFoundError(RuntimeError):
    """Seite existiert nicht mehr (Inserat gelöscht)."""


class Fetcher:
    def get(self, url: str) -> str:  # pragma: no cover - Interface
        raise NotImplementedError

    def close(self) -> None:
        pass


def _check_blocked(url: str, status: int, html: str) -> None:
    head = html[:20000].lower()
    if status in (401, 403, 405, 429) or (len(html) < 30000 and any(m in head for m in BLOCK_MARKERS)):
        raise BlockedError(f"Blockiert ({status}) bei {url}")


class HttpFetcher(Fetcher):
    def __init__(self, delay: tuple[float, float] = (3.0, 7.0), user_agent: str | None = None,
                 timeout: float = 30.0, proxy: str | None = None):
        self.delay = delay
        self._last = 0.0
        self.client = httpx.Client(
            headers={
                "User-Agent": user_agent or DEFAULT_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
            },
            timeout=timeout,
            follow_redirects=True,
            proxy=proxy,
        )

    def _wait(self) -> None:
        pause = random.uniform(*self.delay)
        elapsed = time.monotonic() - self._last
        if elapsed < pause:
            time.sleep(pause - elapsed)
        self._last = time.monotonic()

    def get(self, url: str) -> str:
        last_exc: Exception | None = None
        for attempt in range(3):
            self._wait()
            try:
                r = self.client.get(url)
                if r.status_code in (404, 410):
                    raise NotFoundError(f"{r.status_code} bei {url}")
                _check_blocked(url, r.status_code, r.text)
                r.raise_for_status()
                return r.text
            except (BlockedError, NotFoundError):
                raise
            except httpx.HTTPError as e:
                last_exc = e
                log.warning("Fehler bei %s (Versuch %d): %s", url, attempt + 1, e)
                time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"Abruf fehlgeschlagen: {url}: {last_exc}")

    def close(self) -> None:
        self.client.close()


class PlaywrightFetcher(Fetcher):
    """Lädt Seiten in einem Headless-Chromium. Hilft bei Portalen, die reine HTTP-Clients
    abweisen. Benötigt `pip install playwright && playwright install chromium`."""

    def __init__(self, delay: tuple[float, float] = (4.0, 9.0), user_agent: str | None = None,
                 headless: bool = True, proxy: str | None = None):
        try:
            from playwright.sync_api import sync_playwright  # lazy import
        except ImportError:
            raise RuntimeError("Browser-Abruf (fetcher: playwright) braucht das Docker-Image "
                               "ghcr.io/dorum08/fernimmo:browser") from None

        self.delay = delay
        self._pw = sync_playwright().start()
        # Im Container: kleiner gemeinsamer Speicher (/dev/shm) und keine Chromium-Sandbox möglich
        launch_args: dict = {"headless": headless, "args": ["--disable-dev-shm-usage", "--no-sandbox"]}
        if os.environ.get("FERNIMMO_CHROMIUM_PATH"):
            launch_args["executable_path"] = os.environ["FERNIMMO_CHROMIUM_PATH"]
        if proxy:
            launch_args["proxy"] = {"server": proxy}
        try:
            self.browser = self._pw.chromium.launch(**launch_args)
        except Exception:
            self._pw.stop()
            raise
        self.context = self.browser.new_context(
            user_agent=user_agent or DEFAULT_UA, locale="de-DE", viewport={"width": 1366, "height": 900},
        )
        self.page = self.context.new_page()

    def get(self, url: str) -> str:
        time.sleep(random.uniform(*self.delay))
        resp = self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:  # noqa: BLE001 - networkidle ist nur ein Bonus
            pass
        html = self.page.content()
        if resp and resp.status in (404, 410):
            raise NotFoundError(f"{resp.status} bei {url}")
        _check_blocked(url, resp.status if resp else 200, html)
        return html

    def close(self) -> None:
        try:
            self.context.close()
            self.browser.close()
        finally:
            self._pw.stop()


def make_fetcher(kind: str, delay: tuple[float, float], user_agent: str | None, proxy: str | None) -> Fetcher:
    if kind == "playwright":
        return PlaywrightFetcher(delay=delay, user_agent=user_agent, proxy=proxy)
    return HttpFetcher(delay=delay, user_agent=user_agent, proxy=proxy)
