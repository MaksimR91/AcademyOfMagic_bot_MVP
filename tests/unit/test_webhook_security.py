import hmac, hashlib, json

def _sign(body_bytes: bytes, secret: bytes) -> str:
    return "sha256=" + hmac.new(secret, body_bytes, hashlib.sha256).hexdigest()

def test_verify_token_ok(client):
    resp = client.get("/webhook?hub.mode=subscribe&hub.verify_token=test_verify&hub.challenge=42")
    assert resp.status_code == 200
    assert resp.data == b"42"

def test_verify_token_forbidden(client):
    resp = client.get("/webhook?hub.mode=subscribe&hub.verify_token=WRONG&hub.challenge=42")
    assert resp.status_code == 403

def test_signature_bad_rejected_and_no_dispatch(client, monkeypatch):
    # Подслушаем, что handle_message не дернулся
    called = {"n": 0}
    import routes.webhook_route as wh
    monkeypatch.setattr(wh, "handle_message", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    body = {
        "entry": [{
            "changes": [{
                "value": {
                    "metadata": {"phone_number_id": "123456"},
                    "contacts": [{"wa_id":"71234567890","profile":{"name":"Max"}}],
                    "messages": [{
                        "from":"71234567890","id":"wamid.SAMPLE","timestamp":"1725020000",
                        "type":"text","text":{"body":"привет"}
                    }]
                }
            }]
        }]
    }
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    # Неверная подпись
    resp = client.post("/webhook", data=raw, headers={
        "Content-Type": "application/json",
        "X-Hub-Signature-256": "sha256=deadbeef"
    })
    assert resp.status_code == 403
    assert called["n"] == 0

def test_signature_ok_and_dispatches_to_handler(client, monkeypatch):
    secret = b"shhh"

    # Подслушаем вызов твоего обработчика
    calls = []
    import routes.webhook_route as wh
    monkeypatch.setattr(wh, "handle_message", lambda *args, **kwargs: calls.append((args, kwargs)))

    body = {
        "object": "whatsapp_business_account",
        "entry": [{
            "changes": [{
                "value": {
                    "metadata": {"phone_number_id": "123456"},
                    "contacts": [{"wa_id": "79999999999", "profile": {"name": "Max"}}],
                    "messages": [{
                        "from": "79999999999",
                        "id": "wamid.SAMPLE",
                        "timestamp": "1725020000",
                        "type": "text",
                        "text": {"body": "привет"}
                    }]
                }
            }]
        }]
    }
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    sig = _sign(raw, secret)

    resp = client.post("/webhook", data=raw, headers={
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig
    })
    assert resp.status_code == 200
    assert resp.data == b"ok"

    # Проверяем, что твой handler дернулся с ожидаемыми аргументами
    assert calls, "incoming_message.handle_message не вызван"
    ((message, phone_number_id, bot_display_number, contacts), _kwargs) = calls[0]
    assert phone_number_id == "123456"
    assert isinstance(message, dict) and message.get("type") == "text"
    assert isinstance(contacts, list)
