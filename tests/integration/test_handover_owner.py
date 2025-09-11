# tests/integration/test_handover_owner.py
import os, sys, types, time
import pytest

# ───────── фикстуры ─────────

@pytest.fixture(autouse=True)
def local_dev_env(monkeypatch, tmp_path):
    # не стартуем APScheduler и не ходим наружу
    monkeypatch.setenv("LOCAL_DEV")
    monkeypatch.setenv("ACADEMYBOT_TEST", "1")
    # простые промпты (нужны для block_05 клиентского текста)
    (tmp_path / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "prompts" / "global_prompt.txt").write_text("GLOBAL", encoding="utf-8")
    (tmp_path / "prompts" / "block05_prompt.txt").write_text("B5", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

@pytest.fixture
def state_store():
    return {}

def install_state_api(monkeypatch, target_module, store):
    pkg = types.SimpleNamespace()
    sys.modules["state"] = pkg
    state_mod = types.SimpleNamespace(
        get_state=lambda uid: store.setdefault(uid, {}),
        update_state=lambda uid, patch: store.setdefault(uid, {}).update(patch),
        delete_state=lambda uid: store.pop(uid, None),
    )
    sys.modules["state.state"] = state_mod
    # подменим импортируемые в модуле функции, если есть прямые ссылки
    for name in ("get_state", "update_state"):
        if hasattr(target_module, name):
            monkeypatch.setattr(target_module, name, getattr(state_mod, name), raising=False)

def install_fake_router(monkeypatch, calls):
    fake_router = types.SimpleNamespace(
        route_message=lambda message_text, user_id, force_stage=None, **kwargs: calls.append(
            {"user_id": user_id, "force_stage": force_stage, "text": message_text}
        )
    )
    monkeypatch.setitem(sys.modules, "router", fake_router)

# ───────── тесты ─────────

def test_owner_resume_sent_once_and_client_notified(monkeypatch, state_store):
    """
    Первый вход в block5:
      • send_owner_resume вызывается 1 раз и успешно (200);
      • клиент получает уведомление;
      • state: arseniy_notified=True, client_notified_about_handover=True;
      • сценарий уходит в block6 (CRM).
    """
    from importlib import import_module
    b5 = import_module("blocks.block_05")

    # захватываем send_owner_resume
    sent = {"calls": 0, "last_summary": ""}
    class _Resp: status_code = 200
    monkeypatch.setattr(
        b5, "send_owner_resume",
        lambda summary: sent.update(calls=sent["calls"]+1, last_summary=summary) or [_Resp()],
        raising=True
    )

    # клиентский send_text
    client_msgs = []
    def send_text_client(body): client_msgs.append(body)

    # роутер (ловим переходы)
    router_calls = []
    install_fake_router(monkeypatch, router_calls)

    # состояние пользователя до хендовера
    uid = "userA"
    install_state_api(monkeypatch, b5, state_store)
    state_store[uid] = {
        "stage": "block4",
        "client_name": "Иван",
        "normalized_number": "+77055550773",
        "show_type": "детское",
        "event_description": "Д/р дома, фокусник",
        "structured_cache": {"event_date_iso": "2025-09-03", "event_time_24": "15:00"},
    }

    # вызов block5
    b5.handle_block5("любое сообщение", uid, send_text_client, send_owner_text=lambda s: None)

    # проверки
    assert sent["calls"] == 1, "Резюме Арсению должно быть отправлено 1 раз"
    assert "Резюме для Арсения" in sent["last_summary"]
    assert "Тип шоу" in sent["last_summary"]
    assert client_msgs, "Клиент должен получить уведомление о хендовере"
    st = state_store[uid]
    assert st.get("arseniy_notified") is True
    assert st.get("client_notified_about_handover") is True
    # переход в block6 зафиксирован
    assert any(c["force_stage"] == "block6" for c in router_calls)

def test_owner_resume_not_resent_on_second_call(monkeypatch, state_store):
    """
    Повторный заход в block5 не дублирует отправку резюме и уведомление клиенту.
    """
    from importlib import import_module
    b5 = import_module("blocks.block_05")

    sent = {"calls": 0}
    monkeypatch.setattr(b5, "send_owner_resume", lambda s: (_ for _ in ()).throw(AssertionError("should not be called")), raising=True)

    router_calls = []
    install_fake_router(monkeypatch, router_calls)

    uid = "userB"
    install_state_api(monkeypatch, b5, state_store)
    state_store[uid] = {
        "stage": "block4",
        "arseniy_notified": True,
        "client_notified_about_handover": True,
        "normalized_number": "+7705***5073",
    }

    # вызов без дублей
    b5.handle_block5("повторная передача", uid, lambda _: None, lambda _: None)
    # только переход в block6 (может повториться)
    assert any(c["force_stage"] == "block6" for c in router_calls)

def test_forward_photo_and_persist_url(monkeypatch, state_store):
    """
    Если в state есть celebrant_photo_id:
      • фото пересылается Арсению (send_image);
      • картинка скачивается по временной ссылке Meta;
      • upload_image возвращает постоянный URL и он сохраняется в state.
    """
    from importlib import import_module
    b5 = import_module("blocks.block_05")

    # 1) send_owner_resume → успешный один раз (чтобы прошли остальные шаги)
    class _Resp: status_code = 200
    monkeypatch.setattr(b5, "send_owner_resume", lambda s: [_Resp()], raising=True)

    # 2) подменим send_image, чтобы не ходить в сеть
    owner_media = {"calls": 0, "last_id": None}
    def fake_send_image(to, media_id):
        owner_media["calls"] += 1
        owner_media["last_id"] = media_id
    monkeypatch.setattr(b5, "send_image", lambda to, media: fake_send_image(to, media), raising=True)

    # 3) requests.get для Meta: первый раз — JSON с url, второй — бинарь
    class _RespJson:
        def __init__(self, data=None, content=None):
            self._data = data
            self.content = content
            self.status_code = 200
        def json(self): return self._data
        def raise_for_status(self): pass

    calls = {"n": 0}
    def fake_get(url, headers=None, timeout=10):
        calls["n"] += 1
        if calls["n"] == 1:
            return _RespJson(data={"url": "https://tmp/meta/image"})
        else:
            return _RespJson(content=b"\x89PNG...")
    monkeypatch.setattr(b5.requests, "get", fake_get, raising=True)

    # 4) upload_image возвращает постоянный URL
    monkeypatch.setitem(sys.modules, "utils.s3_upload", types.SimpleNamespace(upload_image=lambda content: "https://s3/permanent/photo.png"))
    # важно: после monkeypatch выше, импорт внутри модуля уже есть — но _forward_and_persist_photo импортирует upload_image внутри, всё ок.

    router_calls = []
    install_fake_router(monkeypatch, router_calls)

    uid = "userC"
    install_state_api(monkeypatch, b5, state_store)
    state_store[uid] = {
        "stage": "block4",
        "normalized_number": "+7705***5073",
        "celebrant_photo_id": "MEDIA123",
    }

    b5.handle_block5("тут фото", uid, lambda _: None, lambda _: None)

    # пересылка фото Арсению прошла
    assert owner_media["calls"] == 1
    assert owner_media["last_id"] == "MEDIA123"
    # постоянная ссылка сохранена
    assert state_store[uid].get("celebrant_photo_url") == "https://s3/permanent/photo.png"

def test_reason_comment_mapping_in_summary(monkeypatch, state_store):
    """
    В summary должен попасть человеко-читаемый комментарий по handover_reason.
    Например: non_standard_show → «Нестандартный формат шоу – нужна консультация.»
    """
    from importlib import import_module
    b5 = import_module("blocks.block_05")

    captured = {"summary": ""}
    class _Resp: status_code = 200
    def fake_send_owner_resume(summary):
        captured["summary"] = summary
        return [_Resp()]
    monkeypatch.setattr(b5, "send_owner_resume", fake_send_owner_resume, raising=True)

    router_calls = []
    install_fake_router(monkeypatch, router_calls)

    uid = "userD"
    install_state_api(monkeypatch, b5, state_store)
    state_store[uid] = {
        "stage": "block3d",
        "handover_reason": "non_standard_show",
        "normalized_number": "+7705***5073",
    }

    b5.handle_block5("нестандарт", uid, lambda _: None, lambda _: None)

    assert "Нестандартный формат шоу – нужна консультация." in captured["summary"]
    assert any(c["force_stage"] == "block6" for c in router_calls)
