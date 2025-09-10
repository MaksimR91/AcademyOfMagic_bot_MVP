# tests/integration/test_voice_flow.py
import io
import json
import pytest
from types import SimpleNamespace

# Точка входа (у тебя так)
import utils.incoming_message as inc

@pytest.fixture
def outbox(monkeypatch):
    sent = []
    from utils import outgoing_message as om

    def _send_text_message(phone_number_id, to, body):
        sent.append({"to": to, "body": body})
        return SimpleNamespace(status_code=200, ok=True)

    monkeypatch.setattr(om, "send_text_message", _send_text_message, raising=True)
    return sent

def _mk_audio_webhook(*, wamid="wamid.GS1", audio_id="MEDIA_AUDIO_ID", ts="1725020000"):
    return {
        "id": wamid,
        "timestamp": ts,
        "type": "audio",
        "audio": {"id": audio_id, "mime_type": "audio/ogg"},
        "from": "79991112233",
    }

# --- моки Graph (получение media URL и скачивание файла) ---
class GraphMock:
    def __init__(self, monkeypatch, *, bytes_payload: bytes):
        import requests
        def _get(url, headers=None, timeout=10):
            if url.endswith("/MEDIA_AUDIO_ID"):
                return SimpleNamespace(
                    status_code=200,
                    ok=True,
                    json=lambda: {"url": "https://graph.example/media.bin"},
                    raise_for_status=lambda: None,
                )
            if url == "https://graph.example/media.bin":
                return SimpleNamespace(
                    status_code=200,
                    ok=True,
                    content=bytes_payload,
                    raise_for_status=lambda: None,
                )
            raise AssertionError(f"unexpected GET {url}")
        monkeypatch.setattr(requests, "get", _get, raising=True)

# --- мок pydub: длительность ---
def patch_duration(monkeypatch, seconds: float):
    from pydub import AudioSegment
    class _Seg:
        def __init__(self): pass
        def __len__(self): return int(seconds * 1000)
    monkeypatch.setattr(AudioSegment, "from_file", lambda *a, **k: _Seg(), raising=True)

# --- моки OpenAI и S3 сохранения ---
def patch_whisper_ok(monkeypatch, text="пример транскрипции"):
    from openai import OpenAI
    class _Cli:
        class audio:
            class transcriptions:
                @staticmethod
                def create(model, file, response_format="text"):
                    return SimpleNamespace(text=text)
    monkeypatch.setattr(inc, "OpenAI", lambda api_key=None: _Cli(), raising=True)

def patch_whisper_fail(monkeypatch):
    from openai import OpenAI
    class _Cli:
        class audio:
            class transcriptions:
                @staticmethod
                def create(*a, **k):
                    raise RuntimeError("decode failed")
    monkeypatch.setattr(inc, "OpenAI", lambda api_key=None: _Cli(), raising=True)

def patch_s3_spy(monkeypatch):
    calls = []
    # перехватываем внутренний saver
    def _save_voice_to_s3(raw_bytes, transcript_text, wamid):
        calls.append({"bytes": raw_bytes, "text": transcript_text, "wamid": wamid})
    monkeypatch.setattr(inc, "_save_voice_to_s3", _save_voice_to_s3, raising=True)
    return calls

# ============ ТЕСТЫ ============

def test_voice_over_60s_asks_to_shorten(monkeypatch, outbox):
    # Graph вернёт байты .ogg
    GraphMock(monkeypatch, bytes_payload=b"OggS" + b"\x00"*64)
    patch_duration(monkeypatch, seconds=75)  # > 60
    patch_whisper_ok(monkeypatch, text="не должно вызываться")  # защитный
    # вход
    msg = _mk_audio_webhook()
    inc.handle_audio_async(msg, phone_number_id="PNID", normalized_number="78999999999", name="Max")
    assert outbox, "Нет ответа пользователю"
    assert "минут" in outbox[-1]["body"].lower() or "короче" in outbox[-1]["body"].lower()

def test_voice_bad_audio_asks_text(monkeypatch, outbox):
    GraphMock(monkeypatch, bytes_payload=b"NOT_OGG")
    patch_duration(monkeypatch, seconds=20)  # норм длительность
    patch_whisper_fail(monkeypatch)          # Whisper падает
    msg = _mk_audio_webhook()
    inc.handle_audio_async(msg, phone_number_id="PNID", normalized_number="78999999999", name="Max")
    assert outbox, "Нет ответа пользователю при ошибке Whisper"
    low = outbox[-1]["body"].lower()
    assert "текст" in low or "напишите" in low

def test_voice_ok_transcribed_and_saved(monkeypatch, outbox):
    payload = b"OggS" + b"\x00"*64
    GraphMock(monkeypatch, bytes_payload=payload)
    patch_duration(monkeypatch, seconds=15)
    patch_whisper_ok(monkeypatch, text="пример транскрипции")
    s3_calls = patch_s3_spy(monkeypatch)

    msg = _mk_audio_webhook(wamid="wamid.TEST_OK")
    inc.handle_audio_async(msg, phone_number_id="PNID", normalized_number="78999999999", name="Max")

    # был ли вызван saver с нужным wamid?
    assert s3_calls and s3_calls[-1]["wamid"] == "wamid.TEST_OK"
    assert s3_calls[-1]["bytes"].startswith(b"OggS")
    assert "пример транскрипции" in s3_calls[-1]["text"]
