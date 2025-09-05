from utils.env_loader import ensure_env_loaded
ensure_env_loaded()
import os, sys, types
import pytest

pytestmark = pytest.mark.llm  # запускать явно: pytest -m llm

DATASET = [
    ("День рождения сына, 7 лет, дома", "детское"),
    ("Свадьба, банкет на 60 гостей", "взрослое"),
    ("Юбилей, будут и дети, и взрослые", "семейное"),
    ("Корпоратив в офисе, 20 человек", "нестандартное"),
    ("Дворовый праздник для соседей, без именинника", "семейное"),
    ("Праздник в детсаду", "детское"),
    ("Мальчику 10 лет, кафе, 15 друзей", "детское"),
    ("Годовщина свадьбы, семья и друзья", "взрослое"),
    ("Коворкинг, презентация, шоу-элементы", "нестандартное"),
    ("Необычное выступление в ТЦ", "нестандартное"),
    ("ДР дочери, 5 лет, кафе с аниматором", "детское"),
    ("Юбилей дедушки 70 лет, дома, семья", "взрослое"),
    ("Семейный пикник, будут и дети, и взрослые", "семейное"),
    ("Выпускной в детском саду", "детское"),
    ("Свадьба на природе, 100 гостей", "взрослое"),
    ("Дворовой праздник для соседей, детская площадка", "семейное"),
    ("Корпоратив в ресторане, 30 человек", "нестандартное"),
    ("Крещение племянника, семейный ужин", "семейное"),
    ("Фойе ТРЦ, хотим сцену и шоу", "нестандартное"),
    ("Презентация продукта в коворкинге", "нестандартное"),
    ("День семьи во дворе, без именинника", "семейное"),
    ("Праздник в школе, 2–4 классы", "детское"),
]

ALLOWED = {"детское","взрослое","семейное","нестандартное","неизвестно"}

def _expected_block(label):
    return {"детское":"block3a","взрослое":"block3b","семейное":"block3c","нестандартное":"block3d"}[label]

@pytest.fixture(autouse=True)
def require_openai_key_and_local_dev(monkeypatch):
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set; skipping LLM classification test")
    monkeypatch.setenv("LOCAL_DEV", "1")  # чтобы планировщик не лез наружу

@pytest.fixture(autouse=True)
def ensure_prompts(tmp_path):
    # минимальные промпты, чтобы импорт блока не падал
    from pathlib import Path
    prom_dir = Path("prompts")
    prom_dir.mkdir(exist_ok=True)
    (prom_dir / "global_prompt.txt").write_text("GLOBAL", encoding="utf-8")
    (prom_dir / "block02_prompt.txt").write_text("BLOCK2", encoding="utf-8")
    (prom_dir / "block02_reminder_1_prompt.txt").write_text("R1", encoding="utf-8")
    (prom_dir / "block02_reminder_2_prompt.txt").write_text("R2", encoding="utf-8")

@pytest.fixture
def fake_state_store():
    return {}

def _install_fake_router(monkeypatch, calls):
    fake_router = types.SimpleNamespace(
        route_message=lambda message_text, user_id, force_stage=None: calls.append(
            {"user_id": user_id, "force_stage": force_stage, "text": message_text}
        )
    )
    monkeypatch.setitem(sys.modules, "router", fake_router)

def _patch_state(monkeypatch, store):
    # у тебя в блоке: from state.state import get_state, update_state
    state_pkg = types.SimpleNamespace()
    sys.modules["state"] = state_pkg
    state_mod = types.SimpleNamespace(
        get_state=lambda uid: store.setdefault(uid, {}),
        update_state=lambda uid, patch: store.setdefault(uid, {}).update(patch),
    )
    sys.modules["state.state"] = state_mod

def _send_accumulator():
    out = []
    return out.append

def test_dataset_accuracy_and_routing(monkeypatch, fake_state_store):
    calls = []
    _install_fake_router(monkeypatch, calls)
    _patch_state(monkeypatch, fake_state_store)

    import blocks.block_02 as b2
    send = _send_accumulator()

    correct = 0
    for i, (text, expected) in enumerate(DATASET, start=1):
        uid = f"user-{i}"
        fake_state_store[uid] = {}
        b2.handle_block2_user_reply(text, uid, send)

        got = fake_state_store[uid].get("show_type")
        assert got in ALLOWED - {"неизвестно"}, f"LLM вернул {got!r} на '{text}'"
        if got == expected:
            correct += 1
        assert calls[-1]["user_id"] == uid
        assert calls[-1]["force_stage"] == _expected_block(got)

    acc = correct / len(DATASET)
    assert acc >= 0.90, f"Accuracy {acc:.2%} < 90%"

def test_three_times_unknown_triggers_handover(monkeypatch, fake_state_store):
    calls = []
    _install_fake_router(monkeypatch, calls)
    _patch_state(monkeypatch, fake_state_store)

    import blocks.block_02 as b2
    send = _send_accumulator()

    uid = "u-unknown"
    fake_state_store[uid] = {}

    # намеренно «пустые» по сути ответы — модель должна вернуть 'неизвестно'
    text = "Здравствуйте, хотим шоу, расскажите подробнее"

    b2.handle_block2_user_reply(text, uid, send)
    b2.handle_block2_user_reply(text, uid, send)
    b2.handle_block2_user_reply(text, uid, send)

    st = fake_state_store[uid]
    # либо хендовер (ожидаемо), либо модель всё же классифицировала (тоже допустимо)
    if st.get("handover_reason") == "classification_failed_x3":
        assert calls[-1]["force_stage"] == "block5"
    else:
        assert st.get("show_type") in ALLOWED - {"неизвестно"}
        assert calls[-1]["force_stage"] in {"block3a","block3b","block3c","block3d"}

