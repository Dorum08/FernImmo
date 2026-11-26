"""Telegram-Knöpfe: wertet Klicks auf „Like“ und „Ausblenden“ unter den Meldungen aus.

Der Server steht im Heimnetz und ist von außen nicht erreichbar – deshalb kein Webhook,
sondern Long-Polling (getUpdates): der Dienst fragt Telegram, ob jemand einen Knopf
gedrückt hat. Angenommen werden nur Klicks aus dem konfigurierten Chat.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

import httpx

from .notify import telegram_keyboard

log = logging.getLogger(__name__)


class TelegramBot(threading.Thread):
    def __init__(self, cfg: dict[str, Any], db, web_users: list[str], public_url: str | None = None):
        super().__init__(daemon=True, name="telegram-bot")
        self.cfg, self.db, self.public_url = cfg, db, public_url
        self.api = f"https://api.telegram.org/bot{cfg['bot_token']}"
        self.chat_id = str(cfg["chat_id"])
        # Telegram-Nutzer-ID -> Name in FernImmo (für Likes); sonst der erste Web-Nutzer
        self.users = {str(k): v for k, v in (cfg.get("users") or {}).items()}
        self.default_user = web_users[0] if web_users else "ich"
        self.offset = 0
        self.stop = threading.Event()

    # ------------------------------------------------------------- verarbeiten
    def handle(self, update: dict) -> dict | None:
        """Verarbeitet ein Update; liefert die Antworten an Telegram (für Tests als dict)."""
        cq = update.get("callback_query")
        if not cq:
            return None
        msg = cq.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != self.chat_id:
            return {"answer": {"callback_query_id": cq["id"], "text": "Nicht erlaubt"}}
        try:
            action, lid_s = cq.get("data", "").split(":", 1)
            lid = int(lid_s)
        except ValueError:
            return {"answer": {"callback_query_id": cq["id"], "text": "Unbekannter Knopf"}}
        row = self.db.get(lid)
        if not row:
            return {"answer": {"callback_query_id": cq["id"], "text": "Inserat nicht mehr vorhanden"}}
        user = self.users.get(str((cq.get("from") or {}).get("id")), self.default_user)
        if action == "like":
            liked = self.db.toggle_like(lid, user)
            text = f"♥ gelikt als {user}" if liked else "Like entfernt"
        elif action == "hide":
            self.db.set_status(lid, "ausgeblendet")
            text = "🙈 ausgeblendet"
        elif action == "unhide":
            self.db.set_status(lid, "neu")
            text = "↩ wieder eingeblendet"
        else:
            return {"answer": {"callback_query_id": cq["id"], "text": "Unbekannter Knopf"}}
        row = self.db.get(lid)
        markup = telegram_keyboard(lid, row["url"], likes=len(row["likes"]), hidden=row["status"] == "ausgeblendet",
                                   public_url=self.public_url)
        return {"answer": {"callback_query_id": cq["id"], "text": text},
                "edit": {"chat_id": msg["chat"]["id"], "message_id": msg["message_id"], "reply_markup": markup}}

    # ------------------------------------------------------------------ Schleife
    def run(self) -> None:
        log.info("Telegram-Knöpfe aktiv")
        while not self.stop.is_set():
            try:
                r = httpx.get(f"{self.api}/getUpdates", params={
                    "offset": self.offset, "timeout": 50, "allowed_updates": '["callback_query"]'}, timeout=65)
                r.raise_for_status()
                for upd in r.json().get("result", []):
                    self.offset = upd["update_id"] + 1
                    out = self.handle(upd)
                    if not out:
                        continue
                    httpx.post(f"{self.api}/answerCallbackQuery", json=out["answer"], timeout=20)
                    if "edit" in out:
                        httpx.post(f"{self.api}/editMessageReplyMarkup", json=out["edit"], timeout=20)
            except Exception as e:  # noqa: BLE001 - Netzwerkfehler: kurz warten, weitermachen
                log.warning("Telegram-Abfrage fehlgeschlagen: %s", e)
                self.stop.wait(30)
