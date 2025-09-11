import os, sys, types, time, pytest
from importlib import import_module

# ─── фикстуры ─────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def base_env(monkeypatch):
    # только окружение; промпты НЕ трогаем
    monkeypatch.setenv("LOCAL_DEV", "1")

@pytest.fixture
def state_store():
    return {}

def install_fake_ask(monkeypatch, mod, answer="нет"):
    # чтобы fallback не ходил в сеть
    monkeypatch.setattr(mod, "ask_openai", lambda prompt: answer, raising=True)

def test_explicit_contact_patterns(monkeypatch):
    mod = __import__("utils.wants_handover_ai", fromlist=["wants_handover_ai"])
    install_fake_ask(monkeypatch, mod, answer="нет")
    msgs = [
        "Свяжитесь со мной, пожалуйста",
        "Передайте Арсению, пусть свяжется",
        "Дайте номер телефона Арсения",
        "Как связаться напрямую с Арсением?",
    ]
    for m in msgs:
        assert mod.wants_handover_ai(m) is True

def test_pricing_patterns(monkeypatch):
    mod = __import__("utils.wants_handover_ai", fromlist=["wants_handover_ai"])
    install_fake_ask(monkeypatch, mod, answer="нет")
    msgs = [
        "Можно скидку?",
        "Дорого. Есть дешевле?",
        "Оплата частями возможна?",
        "Цена — можно снизить стоимость?",
    ]
    for m in msgs:
        assert mod.wants_handover_ai(m) is True

def test_booking_patterns_do_not_handover(monkeypatch):
    mod = __import__("utils.wants_handover_ai", fromlist=["wants_handover_ai"])
    install_fake_ask(monkeypatch, mod, answer="да")  # даже если LLM сказал «да», паттерн booking важнее
    msgs = [
        "Хочу заказать Арсения",
        "Пригласить фокусника Арсения",
        "Забронировать выступление Арсения",
        "Хочу шоу с Арсением",
        "We want to book Arseniy",
        "Need to hire a magician",
    ]
    for m in msgs:
        assert mod.wants_handover_ai(m) is False

def install_state_api(monkeypatch, target_module, store):
    pkg = types.SimpleNamespace()
    sys.modules["state"] = pkg
    state_mod = types.SimpleNamespace(
        get_state=lambda uid: store.setdefault(uid, {}),
        update_state=lambda uid, patch: store.setdefault(uid, {}).update(patch),
        delete_state=lambda uid: store.pop(uid, None),
    )
    sys.modules["state.state"] = state_mod
    for name in ("get_state","update_state"):
        if hasattr(target_module, name):
            monkeypatch.setattr(target_module, name, getattr(state_mod, name), raising=False)

def install_fake_router(monkeypatch, calls):
    fake_router = types.SimpleNamespace(
        route_message=lambda message_text, user_id, force_stage=None, **kw: calls.append(
            {"user_id": user_id, "force_stage": force_stage, "text": message_text}
        )
    )
    monkeypatch.setitem(sys.modules, "router", fake_router)

def patch_llm(monkeypatch, module, answer="ok"):
    # чтобы не ходить наружу
    if hasattr(module, "ask_openai"):
        monkeypatch.setattr(module, "ask_openai", lambda prompt: answer, raising=True)

def patch_schedule(monkeypatch, module, availability="available"):
    # для 3a, чтобы не падал импорт
    sched_ns = types.SimpleNamespace(
        load_schedule_from_s3=lambda: [],
        check_date_availability=lambda d,t,s: availability,
    )
    sys.modules["utils.schedule"] = sched_ns
    monkeypatch.setattr(module, "load_schedule_from_s3", sched_ns.load_schedule_from_s3, raising=False)
    monkeypatch.setattr(module, "check_date_availability", sched_ns.check_date_availability, raising=False)

# ─── тесты: block1 → handover по явному контакту/цене ─────────────
@pytest.mark.parametrize("text", [
    "Свяжитесь со мной",
    "Передай Арсению, пусть свяжется",
    "Можно скидку?",
    "Оплата частями возможна?",
])
def test_block1_triggers_handover(monkeypatch, state_store, text):
    b1 = import_module("blocks.block_01")
    router_calls = []
    install_fake_router(monkeypatch, router_calls)
    install_state_api(monkeypatch, b1, state_store)
    patch_llm(monkeypatch, b1, answer="hi")

    uid = "u_b1"
    state_store[uid] = {"stage": "block1"}
    b1.handle_block1(text, uid, lambda _: None)

    assert any(c["force_stage"] == "block5" for c in router_calls), "Должен быть хендовер в block5"
    st = state_store[uid]
    assert st.get("handover_reason") == "asked_handover"
    assert st.get("scenario_stage_at_handover") == "block1"

def test_block1_booking_is_not_handover(monkeypatch, state_store):
    b1 = import_module("blocks.block_01")
    router_calls = []
    install_fake_router(monkeypatch, router_calls)
    install_state_api(monkeypatch, b1, state_store)
    patch_llm(monkeypatch, b1, answer="ok")

    uid = "u_b1_book"
    state_store[uid] = {"stage": "block1"}
    # «Хочу заказать Арсения» — это НЕ хендовер
    b1.handle_block1("Хочу заказать Арсения", uid, lambda _: None)

    assert not any(c["force_stage"] == "block5" for c in router_calls), "Не должно быть хендовера при заказе"
    # Блок 1 продолжает жить, план дальше в блок2 — это ок, мы не проверяем тут шедулер

# ─── тесты: block3a → handover по явному контакту/цене ───────────
@pytest.mark.parametrize("text", [
    "Дайте контакты Арсения",
    "Как связаться напрямую с ним?",
    "Дорого. Можно дешевле?",
])
def test_block3a_triggers_handover(monkeypatch, state_store, text):
    b3a = import_module("blocks.block_03a")
    router_calls = []
    install_fake_router(monkeypatch, router_calls)
    install_state_api(monkeypatch, b3a, state_store)
    patch_llm(monkeypatch, b3a, answer="{}")   # чтобы хендлер не падал на структурировании
    patch_schedule(monkeypatch, b3a)

    uid = "u_b3a"
    state_store[uid] = {"stage": "block3a"}
    b3a.handle_block3a(text, uid, lambda _: None, client_request_date=time.time())

    assert any(c["force_stage"] == "block5" for c in router_calls), "Должен быть хендовер в block5"
    st = state_store[uid]
    assert st.get("handover_reason") == "asked_handover"
    assert st.get("scenario_stage_at_handover") == "block3"

def test_block3a_booking_is_not_handover(monkeypatch, state_store):
    b3a = import_module("blocks.block_03a")
    router_calls = []
    install_fake_router(monkeypatch, router_calls)
    install_state_api(monkeypatch, b3a, state_store)
    patch_llm(monkeypatch, b3a, answer="{}")
    patch_schedule(monkeypatch, b3a)

    uid = "u_b3a_book"
    state_store[uid] = {"stage": "block3a"}
    b3a.handle_block3a("Хочу пригласить Арсения на шоу", uid, lambda _: None, client_request_date=time.time())

    assert not any(c["force_stage"] == "block5" for c in router_calls)

# ─── block3b: хендовер по контактам/цене; без хендовера для booking ───────
@pytest.mark.parametrize("text", [
    "Свяжитесь со мной по цене",
    "Можно скидку на взрослое шоу?",
])
def test_block3b_triggers_handover(monkeypatch, state_store, text):
    b3b = import_module("blocks.block_03b")
    router_calls = []
    install_fake_router(monkeypatch, router_calls)
    install_state_api(monkeypatch, b3b, state_store)
    patch_llm(monkeypatch, b3b, answer="{}")
    patch_schedule(monkeypatch, b3b)

    uid = "u_b3b"
    state_store[uid] = {"stage": "block3b"}
    # сигнатуры 3b и 3a должны совпадать: (message_text, user_id, send_reply_func, client_request_date)
    b3b.handle_block3b(text, uid, lambda _: None, client_request_date=time.time())

    assert any(c["force_stage"] == "block5" for c in router_calls), "3b: должен быть хендовер в block5"
    st = state_store[uid]
    assert st.get("handover_reason") == "asked_handover"
    assert st.get("scenario_stage_at_handover") == "block3"

def test_block3b_booking_is_not_handover(monkeypatch, state_store):
    b3b = import_module("blocks.block_03b")
    router_calls = []
    install_fake_router(monkeypatch, router_calls)
    install_state_api(monkeypatch, b3b, state_store)
    patch_llm(monkeypatch, b3b, answer="{}")
    patch_schedule(monkeypatch, b3b)
    uid = "u_b3b_book"
    state_store[uid] = {"stage": "block3b"}
    b3b.handle_block3b("Хочу заказать Арсения на юбилей", uid, lambda _: None, client_request_date=time.time())
    assert not any(c["force_stage"] == "block5" for c in router_calls), "3b: booking не должен триггерить хендовер"

# ─── block3c: хендовер по контактам/цене; без хендовера для booking ───────
@pytest.mark.parametrize("text", [
    "Дайте номер Арсения для обсуждения оплаты",
    "Можно подешевле семейное шоу?",
])
def test_block3c_triggers_handover(monkeypatch, state_store, text):
    b3c = import_module("blocks.block_03c")
    router_calls = []
    install_fake_router(monkeypatch, router_calls)
    install_state_api(monkeypatch, b3c, state_store)
    patch_llm(monkeypatch, b3c, answer="{}")
    patch_schedule(monkeypatch, b3c)

    uid = "u_b3c"
    state_store[uid] = {"stage": "block3c"}
    b3c.handle_block3c(text, uid, lambda _: None, client_request_date=time.time())

    assert any(c["force_stage"] == "block5" for c in router_calls), "3c: должен быть хендовер в block5"
    st = state_store[uid]
    assert st.get("handover_reason") == "asked_handover"
    assert st.get("scenario_stage_at_handover") == "block3"
def test_block3c_booking_is_not_handover(monkeypatch, state_store):
    b3c = import_module("blocks.block_03c")
    router_calls = []
    install_fake_router(monkeypatch, router_calls)
    install_state_api(monkeypatch, b3c, state_store)
    patch_llm(monkeypatch, b3c, answer="{}")
    patch_schedule(monkeypatch, b3c)

    uid = "u_b3c_book"
    state_store[uid] = {"stage": "block3c"}
    b3c.handle_block3c("Хотим пригласить Арсения на семейный праздник", uid, lambda _: None, client_request_date=time.time())
    assert not any(c["force_stage"] == "block5" for c in router_calls), "3c: booking не должен триггерить хендовер"

# ─── тесты: block4 → handover по явному контакту/цене до отправки материалов ─
@pytest.mark.parametrize("text", [
    "Дайте номер Арсения",
    "Оплата частями возможна?",
])
def test_block4_triggers_handover(monkeypatch, state_store, text):
    b4 = import_module("blocks.block_04")
    router_calls = []
    install_fake_router(monkeypatch, router_calls)
    install_state_api(monkeypatch, b4, state_store)
    patch_llm(monkeypatch, b4, answer="intro")

    # заглушки для материалов
    monkeypatch.setitem(sys.modules, "utils.materials", types.SimpleNamespace(
        s3=types.SimpleNamespace(get_object=lambda **k: types.SimpleNamespace(Body=types.SimpleNamespace(read=lambda: b'{"videos":{},"kp":{}}'))),
        S3_BUCKET="bucket",
    ))

    uid = "u_b4"
    state_store[uid] = {"stage": "block4"}
    b4.handle_block4(text, uid, lambda _: None, lambda _: None, lambda _: None)

    assert any(c["force_stage"] == "block5" for c in router_calls), "Должен быть хендовер в block5"
    st = state_store[uid]
    assert st.get("handover_reason") == "asked_handover"
    assert st.get("scenario_stage_at_handover") == "block4"


# ─── block3d: «нестандартное шоу» → сразу хендовер Арсению ────────────────
def test_block3d_non_standard_immediate_handover(monkeypatch, state_store):
    b3d = import_module("blocks.block_03d")
    router_calls = []
    install_fake_router(monkeypatch, router_calls)
    install_state_api(monkeypatch, b3d, state_store)
    patch_llm(monkeypatch, b3d, answer="ok")

    uid = "u_b3d"
    state_store[uid] = {"stage": "block3d"}
    # любой нормальный вход в 3d → одно сообщение и сразу handover (reason=non_standard_show)
    b3d.handle_block3d("Хотим нестандартный формат в ТРЦ", uid, lambda _: None, client_request_date=time.time())

    assert any(c["force_stage"] == "block5" for c in router_calls), "3d: сразу хендовер"
    st = state_store[uid]
    assert st.get("handover_reason") == "non_standard_show"
    assert st.get("scenario_stage_at_handover") == "block3"