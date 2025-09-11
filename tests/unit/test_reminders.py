import os, sys, types
import pytest
from importlib import import_module
from pathlib import Path

# ---------- автофикстуры ----------
@pytest.fixture(autouse=True)
def local_dev_env(monkeypatch):
    # планировщик не ходит наружу
    monkeypatch.setenv("LOCAL_DEV")

@pytest.fixture(autouse=True)
def ensure_prompts():
    """
    НИЧЕГО не создаём и не перезаписываем.
    Если какого-то промпта нет — падаем, чтобы не маскировать проблемы.
    """
    required = [
        "prompts/global_prompt.txt",
        "prompts/block02_prompt.txt",
        "prompts/block02_reminder_1_prompt.txt",
        "prompts/block02_reminder_2_prompt.txt",
        # общие 3x
        "prompts/block03_reminder_1_prompt.txt",
        "prompts/block03_reminder_2_prompt.txt",
        # 3a
        "prompts/block03a_prompt.txt",
        "prompts/block03a_data_prompt.txt",
        # 3b
        "prompts/block03b_prompt.txt",
        "prompts/block03b_data_prompt.txt",
        # 3c
        "prompts/block03c_prompt.txt",
        "prompts/block03c_data_prompt.txt",
    ]
    missing = [p for p in required if not Path(p).exists()]
    assert not missing, f"Отсутствуют обязательные промпты: {missing}. Не перезаписываем заглушками."

@pytest.fixture
def state_store():
    return {}

# ---------- helpers ----------
def install_state_api(monkeypatch, target_module, store):
    # у модулей импорт: from state.state import get_state, update_state
    pkg = types.SimpleNamespace()
    sys.modules["state"] = pkg
    state_mod = types.SimpleNamespace(
        get_state=lambda uid: store.setdefault(uid, {}),
        update_state=lambda uid, patch: store.setdefault(uid, {}).update(patch),
    )
    sys.modules["state.state"] = state_mod
    monkeypatch.setattr(target_module, "get_state", state_mod.get_state, raising=False)
    monkeypatch.setattr(target_module, "update_state", state_mod.update_state, raising=False)

def install_fake_router(monkeypatch, calls):
    # фиксируем, что ушли в block5 при хендовере
    fake_router = types.SimpleNamespace(
        route_message=lambda message_text, user_id, force_stage=None: calls.append(
            {"user_id": user_id, "force_stage": force_stage, "text": message_text}
        )
    )
    monkeypatch.setitem(sys.modules, "router", fake_router)

class PlanSpy:
    def __init__(self, monkeypatch):
        import utils.reminder_engine as re
        self.calls = []
        self._orig = re.plan
        def wrapper(user_id, task_ref, delay_seconds):
            self.calls.append((user_id, task_ref, delay_seconds))
            return self._orig(user_id, task_ref, delay_seconds)
        monkeypatch.setattr(re, "plan", wrapper, raising=True)
        self.wrapper = wrapper
    def patch_into_module(self, monkeypatch, mod):
        # в модулях импорт по имени: from utils.reminder_engine import plan
        monkeypatch.setattr(mod, "plan", self.wrapper, raising=False)

def patch_llm(monkeypatch, mod, text="ok"):
    # здесь не тестируем LLM — нужен ответ "для вида"
    monkeypatch.setattr(mod, "ask_openai", lambda prompt: text, raising=True)

def patch_aux_for_block3x(monkeypatch, mod, availability="available"):
    # не просим ручной хендовер
    monkeypatch.setattr(mod, "wants_handover_ai", lambda t: False, raising=True)
    # snapshot
    structured_ns = types.SimpleNamespace(build_structured_snapshot=lambda st: dict(st))
    sys.modules["utils.structured"] = structured_ns
    monkeypatch.setattr(mod, "build_structured_snapshot", structured_ns.build_structured_snapshot, raising=False)
    # расписание
    sched_ns = types.SimpleNamespace(
        load_schedule_from_s3=lambda: [],
        check_date_availability=lambda d, t, s: availability,
    )
    sys.modules["utils.schedule"] = sched_ns
    monkeypatch.setattr(mod, "load_schedule_from_s3", sched_ns.load_schedule_from_s3, raising=False)
    monkeypatch.setattr(mod, "check_date_availability", sched_ns.check_date_availability, raising=False)

def ask_for_busy_flow(prompt: str):
    p = prompt.lower()
    if "формат: гггг-мм-дд" in p or "format: yyyy-mm-dd" in p or "формат: гggg-мм-дд" in p:
        return "2025-09-03"
    if "формат чч:мм" in p or "hh:mm" in p:
        return "15:00"
    return "{}"  # структурирование пустое — но это ок

# конфиги блоков: (module_path, handler_fn, stage_name, ns_for_task_refs)
BLOCKS = [
    ("blocks.block_03a", "handle_block3a", "block3a", "blocks.block_03a"),
    ("blocks.block_03b", "handle_block3b", "block3b", "blocks.block_03b"),
    ("blocks.block_03c", "handle_block3c", "block3c", "blocks.block_03c"),
]

# ================== ТЕСТЫ ДЛЯ BLOCK 2 ==================

def test_block2_clarification_schedules_r1_for_uninformative(monkeypatch, state_store):
    from importlib import import_module
    plan_spy = PlanSpy(monkeypatch)
    b2 = import_module("blocks.block_02")
    plan_spy.patch_into_module(monkeypatch, b2)

    calls = []
    install_fake_router(monkeypatch, calls)
    install_state_api(monkeypatch, b2, state_store)
    patch_llm(monkeypatch, b2, text="clarify")

    uid = "u_uninf"
    state_store[uid] = {}
    # Текст короткий/без ключей → not informative
    b2.handle_block2_user_reply("хочу фокусника", uid, lambda x: None)

    # Должен встать таймер R1 на 4 часа
    assert any(
        c[0] == uid and c[1] == "blocks.block_02:send_first_reminder_if_silent" and c[2] == 4*3600
        for c in plan_spy.calls
    ), "После переспрашивания (uninformative) должен ставиться R1=4h"

def test_block2_clarification_schedules_r1_for_unknown(monkeypatch, state_store):
    from importlib import import_module
    plan_spy = PlanSpy(monkeypatch)
    b2 = import_module("blocks.block_02")
    plan_spy.patch_into_module(monkeypatch, b2)

    calls = []
    install_fake_router(monkeypatch, calls)
    install_state_api(monkeypatch, b2, state_store)
    # Сделаем informative, но классификация 'неизвестно'
    def llm_return_unknown(prompt: str):
        return "неизвестно"
    monkeypatch.setattr(b2, "ask_openai", llm_return_unknown, raising=True)

    uid = "u_unknown"
    state_store[uid] = {}
    txt = "Праздник будет, подробности уточню позже"
    b2.handle_block2_user_reply(txt, uid, lambda x: None)

    assert any(
        c[0] == uid and c[1] == "blocks.block_02:send_first_reminder_if_silent" and c[2] == 4*3600
        for c in plan_spy.calls
    ), "После переспрашивания (неизвестно) должен ставиться R1=4h"

def test_block2_handover_stops_reminders_after_third_try(monkeypatch, state_store):
    from importlib import import_module
    plan_spy = PlanSpy(monkeypatch)
    b2 = import_module("blocks.block_02")
    plan_spy.patch_into_module(monkeypatch, b2)

    calls = []
    install_fake_router(monkeypatch, calls)
    install_state_api(monkeypatch, b2, state_store)
    # Всегда 'неизвестно' → на 3-й раз хендовер
    monkeypatch.setattr(b2, "ask_openai", lambda p: "неизвестно", raising=True)

    uid = "u3"
    state_store[uid] = {}
    before = len(plan_spy.calls)
    b2.handle_block2_user_reply("хочу фокусника", uid, lambda x: None)  # 1
    b2.handle_block2_user_reply("хочу фокусника", uid, lambda x: None)  # 2
    # 3-я попытка → хендовер
    b2.handle_block2_user_reply("хочу фокусника", uid, lambda x: None)  # 3
    after = len(plan_spy.calls)

    # проверяем, что был роут в block5 и новых таймеров не добавили
    assert calls[-1]["force_stage"] == "block5"
    assert after == before + 2, "На 3-й попытке не должно ставиться ещё напоминаний"

# ================== ТЕСТЫ (общая логика для 3a/3b/3c) ==================

@pytest.mark.parametrize("mod_path,handle_name,stage,ns", BLOCKS)
def test_clarification_should_schedule_r1_4h(monkeypatch, state_store, mod_path, handle_name, stage, ns):
    """
    После доспрашивания (нехватает данных) должен ставиться R1 через 4 часа.
    Если тест красный — добавь plan(..., 4*3600) в ветке доспрашивания перед return.
    """
    plan_spy = PlanSpy(monkeypatch)
    mod = import_module(mod_path)
    plan_spy.patch_into_module(monkeypatch, mod)

    calls = []
    install_fake_router(monkeypatch, calls)
    install_state_api(monkeypatch, mod, state_store)
    patch_aux_for_block3x(monkeypatch, mod, availability="available")

    # отдадим "пустой JSON", чтобы точно были missing_keys ⇒ перейдём в ветку доспрашивания
    patch_llm(monkeypatch, mod, text="{}")

    uid = f"{stage}_clarify"
    state_store[uid] = {}
    getattr(mod, handle_name)("нужны уточнения", uid, lambda x: None)

    assert any(
        c[0] == uid and c[1] == f"{ns}:send_first_reminder_if_silent" and c[2] == 4*3600
        for c in plan_spy.calls
    ), f"После доспрашивания в {stage} должен ставиться R1=4h"

@pytest.mark.parametrize("mod_path,handle_name,stage,ns", BLOCKS)
def test_reminder_chain_r1_r2_finalize(monkeypatch, state_store, mod_path, handle_name, stage, ns):
    """
    R1 → R2 (через 12ч) → finalize (через 4ч).
    """
    plan_spy = PlanSpy(monkeypatch)
    mod = import_module(mod_path)
    plan_spy.patch_into_module(monkeypatch, mod)

    calls = []
    install_fake_router(monkeypatch, calls)
    install_state_api(monkeypatch, mod, state_store)
    patch_llm(monkeypatch, mod, text="rem")
    patch_aux_for_block3x(monkeypatch, mod, availability="available")

    uid = f"{stage}_chain"
    state_store[uid] = {"stage": stage, "last_bot_question": "?"}

    before = len(plan_spy.calls)
    mod.send_first_reminder_if_silent(uid, lambda x: None)
    assert len(plan_spy.calls) == before + 1
    _, task_ref, delay = plan_spy.calls[-1]
    assert task_ref == f"{ns}:send_second_reminder_if_silent"
    assert delay == 12 * 3600

    before = len(plan_spy.calls)
    mod.send_second_reminder_if_silent(uid, lambda x: None)
    assert len(plan_spy.calls) == before + 1
    _, task_ref, delay = plan_spy.calls[-1]
    assert task_ref == f"{ns}:finalize_if_still_silent"
    assert delay == 4 * 3600

@pytest.mark.parametrize("mod_path,handle_name,stage,ns", BLOCKS)
def test_reminder_skips_when_stage_changed(monkeypatch, state_store, mod_path, handle_name, stage, ns):
    """
    Если клиент уже НЕ на этом этапе — R1 ничего не планирует.
    """
    plan_spy = PlanSpy(monkeypatch)
    mod = import_module(mod_path)
    plan_spy.patch_into_module(monkeypatch, mod)

    calls = []
    install_fake_router(monkeypatch, calls)
    install_state_api(monkeypatch, mod, state_store)
    patch_llm(monkeypatch, mod, text="rem")
    patch_aux_for_block3x(monkeypatch, mod, availability="available")

    uid = f"{stage}_other"
    # другой этап
    state_store[uid] = {"stage": "block2"}
    before = len(plan_spy.calls)
    mod.send_first_reminder_if_silent(uid, lambda x: None)
    assert len(plan_spy.calls) == before, f"На другом этапе не должны ставиться напоминания {stage}"

@pytest.mark.parametrize("mod_path,handle_name,stage,ns", BLOCKS)
@pytest.mark.parametrize("busy_flag", ["occupied", "need_handover"])
def test_early_busy_handover_has_no_new_timer(monkeypatch, state_store, mod_path, handle_name, stage, ns, busy_flag):
    """
    Если расписание: occupied/need_handover — сразу хендовер в block5, без новых таймеров.
    """
    plan_spy = PlanSpy(monkeypatch)
    mod = import_module(mod_path)
    plan_spy.patch_into_module(monkeypatch, mod)

    calls = []
    install_fake_router(monkeypatch, calls)
    install_state_api(monkeypatch, mod, state_store)

    # schedule → занято/хендовер
    patch_aux_for_block3x(monkeypatch, mod, availability=busy_flag)
    # ask_openai для дат/времени отдаёт валидные значения
    monkeypatch.setattr(mod, "ask_openai", ask_for_busy_flow, raising=True)

    uid = f"{stage}_{busy_flag}"
    state_store[uid] = {}
    before = len(plan_spy.calls)
    getattr(mod, handle_name)("дата 3 сентября, 15:00", uid, lambda x: None)
    after = len(plan_spy.calls)

    assert calls and calls[-1]["force_stage"] == "block5"
    assert after == before, f"При '{busy_flag}' в {stage} не должны ставиться напоминания"


# ================== ДОБАВЛЕНО: «любой ответ отменяет напоминания» ==================
def test_block2_any_user_reply_cancels_timers(monkeypatch, state_store):
    """
    ТЗ: любой ответ клиента отменяет напоминания.
    Сценарий:
      1) ставим R1 (через 4ч) в block2;
      2) имитируем ответ клиента → переход в block3a (rule-based 'детское');
      3) пробуем вызвать R1/R2 обработчики — новых планирований быть не должно.
    """
    from importlib import import_module
    plan_spy = PlanSpy(monkeypatch)
    b2 = import_module("blocks.block_02")
    plan_spy.patch_into_module(monkeypatch, b2)

    calls = []
    install_fake_router(monkeypatch, calls)
    install_state_api(monkeypatch, b2, state_store)

    uid = "u_cancel_any_reply"
    # выставим старт блока 2
    state_store[uid] = {"stage": "block2"}

    # Ставим R1 напрямую (как будто бот уже доспрашивал)
    before = len(plan_spy.calls)
    b2.send_first_reminder_if_silent(uid, lambda x: None)
    after = len(plan_spy.calls)
    assert after == before + 1
    assert plan_spy.calls[-1][1].endswith("blocks.block_02:send_second_reminder_if_silent")

    # Любой ответ клиента → быстрый rule-based переход в детское
    # (слово "детсад" триггерит _rule_based_label → 'детское' → block3a)
    b2.handle_block2_user_reply("У нас выпускной в детсаде", uid, lambda x: None)

    # Повторный вызов обработчиков напоминаний теперь не должен ничего планировать
    before = len(plan_spy.calls)
    b2.send_first_reminder_if_silent(uid, lambda x: None)
    b2.send_second_reminder_if_silent(uid, lambda x: None)
    after = len(plan_spy.calls)
    assert after == before, "После ответа клиента таймеры не должны планироваться"

# ================== ДОБАВЛЕНО: финальный хендовер после R2 (+4ч) ==================
def test_block3a_finalize_routes_to_handover(monkeypatch, state_store):
    """
    После r2 и ещё 4 часа тишины должен быть хендовер (block5) с флагом причины.
    Проверяем сам finalize в 3a.
    """
    from importlib import import_module
    plan_spy = PlanSpy(monkeypatch)  # не обязателен здесь, но пусть будет для единообразия
    mod = import_module("blocks.block_03a")
    plan_spy.patch_into_module(monkeypatch, mod)

    calls = []
    install_fake_router(monkeypatch, calls)
    install_state_api(monkeypatch, mod, state_store)
    patch_llm(monkeypatch, mod, text="rem")
    patch_aux_for_block3x(monkeypatch, mod, availability="available")

    uid = "u3a_finalize"
    state_store[uid] = {"stage": "block3a", "last_bot_question": "?"}

    # имитируем, что уже отправили второе напоминание и прошло 4 часа тишины
    mod.finalize_if_still_silent(uid, lambda x: None)

    # проверяем переход в block5 и причину
    assert calls and calls[-1]["force_stage"] == "block5"
    st = state_store[uid]
    assert st.get("handover_reason") == "no_response_after_3_2"
    assert st.get("scenario_stage_at_handover") == "block3"

def test_block2_finalize_routes_to_handover(monkeypatch, state_store):
    """
    Аналогичный тест для block2: ожидаем хендовер после finalize.
    """
    from importlib import import_module
    b2 = import_module("blocks.block_02")

    calls = []
    install_fake_router(monkeypatch, calls)
    install_state_api(monkeypatch, b2, state_store)

    uid = "u2_finalize"
    state_store[uid] = {"stage": "block2"}

    b2.finalize_if_still_silent(uid, lambda x: None)
    assert calls and calls[-1]["force_stage"] == "block5"
    st = state_store[uid]
    assert st.get("handover_reason") == "no_response_after_2_2"
    assert st.get("scenario_stage_at_handover") == "block2"

# ================== ДОБАВЛЕНО: отмена напоминаний для 3a/3b/3c при ответе ==================
@pytest.mark.parametrize("mod_path,handle_name,stage,ns", BLOCKS)
def test_block3x_any_user_reply_cancels_timers(monkeypatch, state_store, mod_path, handle_name, stage, ns):
    """
    Клиент ответил — остаёмся в своём основном блоке (stage не меняется на block5),
    и новые напоминания не планируются.
    """
    plan_spy = PlanSpy(monkeypatch)
    mod = import_module(mod_path)
    plan_spy.patch_into_module(monkeypatch, mod)

    calls = []
    install_fake_router(monkeypatch, calls)
    install_state_api(monkeypatch, mod, state_store)
    patch_aux_for_block3x(monkeypatch, mod, availability="available")
    # пусть LLM вернёт что-то осмысленное, не пустой JSON
    patch_llm(monkeypatch, mod, text="Спасибо, вот ответы на вопросы")

    uid = f"u_{stage}_cancel"
    state_store[uid] = {"stage": stage, "last_bot_question": "?"}

    # поставим R1
    mod.send_first_reminder_if_silent(uid, lambda x: None)
    planned_before = len(plan_spy.calls)

    # пришёл ответ клиента — обрабатываем основной handler
    getattr(mod, handle_name)("отвечаю: дата 2025-09-03, время 15:00", uid, lambda x: None)

    # проверим, что stage остался своим и не ушли в block5
    assert not calls or calls[-1]["force_stage"] != "block5", "На ответ клиента не должен срабатывать хендовер"
    # повторные вызовы R1/R2 не планируют новых задач
    mod.send_first_reminder_if_silent(uid, lambda x: None)
    mod.send_second_reminder_if_silent(uid, lambda x: None)
    assert len(plan_spy.calls) == planned_before, "После ответа клиента новые напоминания не ставятся"

# ================== ДОБАВЛЕНО: идемпотентность R1/R2/Final для всех блоков ==================
@pytest.mark.parametrize("mod_path,stage,ns", [
    ("blocks.block_02","block2","blocks.block_02"),
   ("blocks.block_03a","block3a","blocks.block_03a"),
   ("blocks.block_03b","block3b","blocks.block_03b"),
   ("blocks.block_03c","block3c","blocks.block_03c"),
])
def test_reminders_idempotent(monkeypatch, state_store, mod_path, stage, ns):
    """
    Повторные вызовы R1/R2/Final не должны плодить новые задачи.
    """
    plan_spy = PlanSpy(monkeypatch)
    mod = import_module(mod_path)
    plan_spy.patch_into_module(monkeypatch, mod)

    calls = []
    install_fake_router(monkeypatch, calls)
    install_state_api(monkeypatch, mod, state_store)
    patch_llm(monkeypatch, mod, text="rem")
    if stage != "block2":
        patch_aux_for_block3x(monkeypatch, mod, availability="available")

    uid = f"u_{stage}_idem"
    state_store[uid] = {"stage": stage, "last_bot_question": "?"}

    # R1 дважды
    before = len(plan_spy.calls)
    mod.send_first_reminder_if_silent(uid, lambda x: None)
    mod.send_first_reminder_if_silent(uid, lambda x: None)
    after = len(plan_spy.calls)
    assert after == before + 1, "R1 должен ставиться один раз"

    # R2 дважды
    before = len(plan_spy.calls)
    mod.send_second_reminder_if_silent(uid, lambda x: None)
    mod.send_second_reminder_if_silent(uid, lambda x: None)
    after = len(plan_spy.calls)
    assert after == before + 1, "R2 должен ставиться один раз"

    # Final дважды (если есть finalize)
    if hasattr(mod, "finalize_if_still_silent"):
        before = len(plan_spy.calls)
        # финализатор не планирует таймеры, но должен быть идемпотентным по side-effects
        mod.finalize_if_still_silent(uid, lambda x: None)
        mod.finalize_if_still_silent(uid, lambda x: None)
        after = len(plan_spy.calls)
        assert after == before, "Final не должен планировать новые задачи повторно"