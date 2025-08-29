# tests/conftest.py
import pytest
import types
import utils.whatsapp_senders as wa
from dotenv import load_dotenv
load_dotenv(override=True)


@pytest.fixture
def fake_outbox(monkeypatch):
    """
    Подменяем реальную отправку WA-сообщений на сбор в список.
    Ни один HTTP-запрос в тестах не уйдёт.
    Возвращаем список 'sent' — его смотришь в ассертах.
    """
    sent = []

    # --- текст ---
    def _send_text(to: str, body: str):
        sent.append({"type": "text", "to": to, "body": body})
        # имитируем объект Response (если код где-то его ждёт)
        resp = types.SimpleNamespace(status_code=200, ok=True)
        return resp

    # --- медиа (image/document/video) ---
    def _mk_media(kind: str):
        def _send_media(to: str, media_id: str):
            sent.append({"type": kind, "to": to, "media_id": media_id})
            return None
        return _send_media

    # --- резюме владельцу (owner_summary_chunk) ---
    def _send_owner_resume(full_text: str):
        # В реале у тебя идёт чанкинг и список Response.
        # Для тестов достаточно зафиксировать факт отправки + вернуть список "успехов".
        sent.append({"type": "owner_resume", "to": "OWNER", "text": full_text})
        return [types.SimpleNamespace(status_code=200, ok=True)]

    # Подмена публичных API
    monkeypatch.setattr(wa, "send_text", _send_text)
    monkeypatch.setattr(wa, "send_image", _mk_media("image"))
    monkeypatch.setattr(wa, "send_document", _mk_media("document"))
    monkeypatch.setattr(wa, "send_video", _mk_media("video"))
    monkeypatch.setattr(wa, "send_owner_resume", _send_owner_resume)

    return sent
