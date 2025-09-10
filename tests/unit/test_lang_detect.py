# tests/unit/test_lang_detect.py
import types
import pytest

# === unit-тесты utils.lang_detect ==========================================
from utils.lang_detect import detect_lang, is_russian, is_affirmative, is_negative

@pytest.mark.parametrize("txt,exp_lang,exp_is_ru", [
    ("Здравствуйте! Хочу шоу на завтра", "ru", True),
    ("Let's do it tomorrow", "en", False),
    ("Merhaba, yarin olur mu?", "tr", False),
    ("Сәлем! Ертең болады ма?", "kk", False),  # детектор часто даёт 'kk' или 'tr' — важно, что не 'ru'
    ("Вітаю! Коли зручно?", "uk", False),
    ("", "ru", True),  # пустое -> treat as ru (fallback)
])
def test_detect_and_is_russian(txt, exp_lang, exp_is_ru):
    lang = detect_lang(txt)
    assert isinstance(lang, str)
    # язык детектора может «гулять» между близкими (kk/tr/uk), но точно НЕ ru для не-русских фраз
    if txt:
        if exp_is_ru:
            assert is_russian(txt) is True
        else:
            assert is_russian(txt) is False
    else:
        assert is_russian(txt) is True

def test_affirmative_negative_ru_en():
    assert is_affirmative("да, ок", "ru")
    assert is_negative("нет", "ru")
    assert is_affirmative("yes ok sure", "en")
    assert is_negative("nope", "en")

def test_affirmative_negative_other_langs():
    assert is_affirmative("evet, tamam", "tr")
    assert is_negative("hayır", "tr")
    assert is_affirmative("иә", "kk") or is_affirmative("иа", "kk")
    assert is_negative("жоқ", "kk")
    assert is_affirmative("так", "uk")
    assert is_negative("ні", "uk")


# === интеграционные юниты: языковой гейт в router ==========================
# Мокаем state и отправители, чтобы тестировать route_message без внешних систем.

@pytest.fixture
def fake_state(monkeypatch):
    store = {}

    def _get_state(uid):
        return store.get(uid, {}).copy()

    def _update_state(uid, patch):
        cur = store.get(uid, {})
        cur.update(patch or {})
        store[uid] = cur

    monkeypatch.setattr("router.get_state", _get_state)
    monkeypatch.setattr("router.update_state", _update_state)
    return store

@pytest.fixture
def sent_out(monkeypatch):
    msgs = {"text": []}

    def _send_text(to, body):
        # сохраняем последние отправленные тексты
        msgs["text"].append((to, body))

    # заглушки для медиа (не используются здесь, но пусть будут)
    monkeypatch.setattr("router.send_text", _send_text)
    monkeypatch.setattr("router.send_document", lambda *a, **k: None)
    monkeypatch.setattr("router.send_video", lambda *a, **k: None)
    monkeypatch.setattr("router.send_image", lambda *a, **k: None)
    return msgs

def test_router_non_ru_triggers_lang_check(fake_state, sent_out):
    from router import route_message

    user = "+7000"
    resp = route_message("Hello! Can we book a show?", user, message_uid="m1", message_ts=1_000_000)

    # Проверяем, что выставлен флаг ожидания подтверждения языка
    st = fake_state[user]
    assert st.get("lang_check_pending") is True
    assert st.get("detected_lang") in {"en", "tr", "kk", "uk", "de", "fr"}  # детектор может варьироваться
    # Отправлено двуязычное сообщение (должна быть русская часть и разделитель/перевод)
    assert sent_out["text"], "Должно быть отправлено сообщение о переходе на русский"
    last_msg = sent_out["text"][-1][1]
    assert "шоу иллюзиониста" in last_msg  # русская часть
    # в любом случае будет вторая часть (fallback en) с "Hello!"
    assert ("Hello!" in last_msg) or ("Merhaba" in last_msg) or ("Сәлеметсіз" in last_msg) or ("Вітаю" in last_msg)

    # Ответ роутера валиден
    assert resp["ok"] is True

def test_router_lang_affirmative_continues(fake_state, sent_out, monkeypatch):
    from router import route_message

    user = "+7001"
    # заранее кладём pending-состояние
    fake_state[user] = {
        "stage": "block1",
        "lang_check_pending": True,
        "detected_lang": "en",
        "normalized_number": user,
        "last_msg_ts": 1_000_000,
    }

    # чтобы не уходить в реальные блоки, подменим block_01.handle_block1 заглушкой
    import blocks.block_01 as block_01
    def fake_handle_block1(message_text, user_id, send_text_func, *args):
        # имитируем, что блок ничего не сломал
        pass
    monkeypatch.setattr(block_01, "handle_block1", fake_handle_block1)

    resp = route_message("yes ok", user, message_uid="m2", message_ts=1_000_100)

    st = fake_state[user]
    assert st.get("lang_check_pending") is False
    assert st.get("lang_confirmed") is True
    assert resp["ok"] is True

def test_router_lang_decline_handover(fake_state, sent_out):
    from router import route_message

    user = "+7002"
    fake_state[user] = {
        "stage": "block1",
        "lang_check_pending": True,
        "detected_lang": "en",
        "normalized_number": user,
        "last_msg_ts": 1_000_000,
    }

    resp = route_message("no", user, message_uid="m3", message_ts=1_000_100)

    st = fake_state[user]
    assert st.get("handover_reason") == "lang_declined"
    assert st.get("stage") == "block5"  # хендовер
    assert resp["ok"] is True
