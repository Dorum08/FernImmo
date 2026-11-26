"""Telegram-Meldungen mit Knöpfen und deren Auswertung (ohne Netzwerk)."""
from fernimmo import notify, pipeline
from fernimmo.db import DB
from fernimmo.telegram_bot import TelegramBot

from test_pipeline import FakeFetcher, FakeNotifier, make_cfg, pages


class _Resp:
    def __init__(self, status=200, text="ok"):
        self.status_code, self.text = status, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_message_has_buttons_and_falls_back(monkeypatch):
    sent = []

    def fake_post(url, json=None, **kw):
        sent.append(__import__("copy").deepcopy(json))
        # erster Versuch: Telegram lehnt die (private) Details-URL ab
        return _Resp(400, "BUTTON_URL_INVALID") if len(sent) == 1 else _Resp()
    monkeypatch.setattr(notify.httpx, "post", fake_post)
    n = notify.Notifier({"telegram": {"enabled": True, "bot_token": "T", "chat_id": "42"}},
                        public_url="http://10.8.0.1:8080")
    n.send("Betreff", "Text", "https://portal/x", listing_id=7)
    kb = sent[0]["reply_markup"]["inline_keyboard"]
    assert kb[0][0]["callback_data"] == "like:7" and kb[0][1]["callback_data"] == "hide:7"
    assert kb[1][1]["url"] == "http://10.8.0.1:8080/listing/7"
    assert len(sent) == 2 and len(sent[1]["reply_markup"]["inline_keyboard"][1]) == 1   # ohne Details-Knopf


def test_bot_handles_like_and_hide(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(pipeline, "make_fetcher", lambda *a, **k: FakeFetcher(pages()))
    db = DB(cfg.db_path)
    pipeline.run_once(cfg, db, FakeNotifier())
    lid = next(r["id"] for r in db.search(match=None) if r["source_id"] == "111")
    bot = TelegramBot({"bot_token": "T", "chat_id": 42, "users": {111: "anna"}}, db, ["ich"])
    upd = lambda data, chat=42, user=111: {"update_id": 1, "callback_query": {
        "id": "q", "data": data, "from": {"id": user}, "message": {"message_id": 5, "chat": {"id": chat}}}}

    out = bot.handle(upd(f"like:{lid}"))
    assert "anna" in out["answer"]["text"] and db.get(lid)["likes"] == ["anna"]
    assert out["edit"]["reply_markup"]["inline_keyboard"][0][0]["text"] == "♥ Like (1)"
    bot.handle(upd(f"like:{lid}", user=999))                       # unbekannter Nutzer -> erster Web-Nutzer
    assert db.get(lid)["likes"] == ["anna", "ich"]
    out = bot.handle(upd(f"hide:{lid}"))
    assert db.get(lid)["status"] == "ausgeblendet"
    assert out["edit"]["reply_markup"]["inline_keyboard"][0][1]["callback_data"] == f"unhide:{lid}"
    bot.handle(upd(f"unhide:{lid}"))
    assert db.get(lid)["status"] == "neu"
    # fremder Chat und Unsinn werden abgewiesen
    assert bot.handle(upd(f"hide:{lid}", chat=666))["answer"]["text"] == "Nicht erlaubt"
    assert db.get(lid)["status"] == "neu"
    assert bot.handle(upd("quatsch"))["answer"]["text"] == "Unbekannter Knopf"
    assert bot.handle(upd("like:99999"))["answer"]["text"] == "Inserat nicht mehr vorhanden"
    assert bot.handle({"update_id": 2, "message": {}}) is None
