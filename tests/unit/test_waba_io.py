# tests/integration/test_waba_io.py
import os
import time
import pathlib
import pytest
import requests

"""
Интеграционные тесты WhatsApp Cloud API (Graph).
Требуются ENV:
  WHATSAPP_TOKEN      — токен доступа (страница/приложение Meta)
  PHONE_NUMBER_ID        — ID номера (например: 123456789012345)
  TEST_WA_RECIPIENT      — номер получателя в международном формате, без '+', напр. 7705XXXXXXX
Опционально (для медиа по ссылке):
  MEDIA_IMAGE_LINK       — публичная https-ссылка на small jpg/png
  MEDIA_PDF_LINK         — публичная https-ссылка на small pdf
  MEDIA_VIDEO_LINK       — публичная https-ссылка на small mp4 (<16 МБ)
Опционально (для загрузки и отправки по media_id):
  MEDIA_IMAGE_PATH       — локальный путь к небольшому jpg/png
  MEDIA_PDF_PATH         — локальный путь к небольшому pdf
  MEDIA_VIDEO_PATH       — локальный путь к небольшому mp4 (<16 МБ)
  GRAPH_API_VERSION      — по умолчанию v20.0
"""

API_VER = os.getenv("GRAPH_API_VERSION", "v20.0")
BASE_URL = f"https://graph.facebook.com/{API_VER}"

REQUIRED = ("WHATSAPP_TOKEN", "PHONE_NUMBER_ID", "TEST_WA_RECIPIENT")


def _require_env():
    missing = [k for k in REQUIRED if not os.getenv(k)]
    if missing:
        pytest.skip(f"Missing ENV for WABA live test: {', '.join(missing)}")


def _headers():
    return {
        "Authorization": f"Bearer {os.getenv('WHATSAPP_TOKEN')}",
        "Content-Type": "application/json",
    }


def _url_messages():
    return f"{BASE_URL}/{os.getenv('PHONE_NUMBER_ID')}/messages"


def _assert_ok(resp: requests.Response):
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
    data = resp.json()
    # у сообщений должен быть id
    if isinstance(data, dict) and "messages" in data:
        assert data["messages"] and "id" in data["messages"][0]
    return data


def test_send_text_message_live():
    _require_env()
    payload = {
        "messaging_product": "whatsapp",
        "to": os.getenv("TEST_WA_RECIPIENT"),
        "type": "text",
        "text": {"body": "Тест: текст из интеграционного теста ✅"},
    }
    resp = requests.post(_url_messages(), headers=_headers(), json=payload, timeout=30)
    data = _assert_ok(resp)
    # небольшая пауза, чтобы Meta не зарубила частые вызовы
    time.sleep(1.0)
    assert data["messages"][0]["id"]


@pytest.mark.parametrize(
    "env_name,media_type,payload_key",
    [
        ("MEDIA_IMAGE_LINK", "image", "image"),
        ("MEDIA_PDF_LINK", "document", "document"),
        ("MEDIA_VIDEO_LINK", "video", "video"),
    ],
)
def test_send_media_by_link_live(env_name, media_type, payload_key):
    _require_env()
    link = os.getenv(env_name)
    if not link:
        pytest.skip(f"{env_name} not set")
    payload = {
        "messaging_product": "whatsapp",
        "to": os.getenv("TEST_WA_RECIPIENT"),
        "type": media_type,
        payload_key: {"link": link},
    }
    # Для документов полезно указать filename
    if media_type == "document" and link.endswith(".pdf"):
        payload[payload_key]["filename"] = "offer.pdf"
    resp = requests.post(_url_messages(), headers=_headers(), json=payload, timeout=60)
    data = _assert_ok(resp)
    time.sleep(1.0)
    assert data["messages"][0]["id"]


@pytest.mark.parametrize(
    "env_path,form_type,mime_hint",
    [
        ("MEDIA_IMAGE_PATH", "image", "image/jpeg"),
        ("MEDIA_PDF_PATH", "document", "application/pdf"),
        ("MEDIA_VIDEO_PATH", "video", "video/mp4"),
    ],
)
def test_upload_media_and_send_by_id_live(env_path, form_type, mime_hint):
    _require_env()
    p = os.getenv(env_path)
    if not p:
        pytest.skip(f"{env_path} not set")
    file_path = pathlib.Path(p)
    if not file_path.exists():
        pytest.skip(f"{env_path} file not found: {file_path}")

    # 1) Upload media → media_id
    upload_url = f"{BASE_URL}/{os.getenv('PHONE_NUMBER_ID')}/media"
    files = {
        "file": (file_path.name, open(file_path, "rb"), mime_hint),
        "messaging_product": (None, "whatsapp"),
    }
    resp_up = requests.post(
        upload_url,
        headers={"Authorization": f"Bearer {os.getenv('WHATSAPP_TOKEN')}"},
        files=files,
        timeout=120,
    )
    assert resp_up.status_code == 200, f"Upload failed {resp_up.status_code}: {resp_up.text}"
    media_id = resp_up.json().get("id")
    assert media_id, f"No media id in upload response: {resp_up.text}"
    time.sleep(1.0)

    # 2) Send by media_id
    payload = {
        "messaging_product": "whatsapp",
        "to": os.getenv("TEST_WA_RECIPIENT"),
        "type": form_type,
        form_type: {"id": media_id},
    }
    # Для документа можно указать имя файла
    if form_type == "document" and file_path.suffix.lower() == ".pdf":
        payload["document"]["filename"] = file_path.name

    resp_send = requests.post(_url_messages(), headers=_headers(), json=payload, timeout=60)
    data = _assert_ok(resp_send)
    time.sleep(1.0)
    assert data["messages"][0]["id"]

    # 3) (необязательно) удалить медиа (если нужно чистить — раскомментируй)
    # del_url = f"{BASE_URL}/{media_id}"
    # requests.delete(del_url, headers={"Authorization": f"Bearer {os.getenv('WHATSAPP_TOKEN')}"}, timeout=30)


def test_rate_limit_shape_is_reasonable_live():
    """
    Небольшая проверка «формы» ошибок при частых вызовах (не всегда триггернется).
    Если Meta вернёт 429, у ответа будет понятный JSON.
    """
    _require_env()
    payload = {
        "messaging_product": "whatsapp",
        "to": os.getenv("TEST_WA_RECIPIENT"),
        "type": "text",
        "text": {"body": "Rate-limit probe"},
    }
    # сделаем несколько быстрых вызовов подряд
    errs = 0
    for _ in range(4):
        r = requests.post(_url_messages(), headers=_headers(), json=payload, timeout=10)
        if r.status_code != 200:
            errs += 1
            # у Graph ошибки — JSON с "error"
            try:
                j = r.json()
                assert "error" in j
            except Exception:
                pytest.fail(f"Non-200 without JSON error: {r.status_code} {r.text}")
        time.sleep(0.3)
    assert errs >= 0  # тест не требует обязательного 429; это «мягкая» проверка формы ответа
