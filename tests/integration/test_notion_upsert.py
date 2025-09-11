# tests/integration/test_notion_upsert.py
import os
import types
import pytest
from notion_client import Client
from state import state
from blocks import block_06 as b6

# ───────────────────────── helpers ─────────────────────────
def _install_notion_mock(monkeypatch, *, fail_times=0, capture=None):
    """
    Мокаем Client так, чтобы:
      • pages.create возвращал фейковый page_id после fail_times ошибок;
      • blocks.children.append не падал.
    capture (dict) — собрать переданные properties для ассертов.
    """
    class FakePages:
        def __init__(self): self.calls = 0
        def create(self, *, parent, properties):
            self.calls += 1
            if capture is not None:
                capture["properties"] = properties
            if self.calls <= fail_times:
                from notion_client.errors import APIResponseError
                # имитируем 500
                raise APIResponseError(message="mock 500", request={"path": ""}, response={"status": 500})
            return {"id": "fake_page_id"}

        def update(self, page_id, archived=True):  # для лайв-очистки мок не нужен
            return {"id": page_id, "archived": archived}

    class FakeBlocks:
        class children:
            @staticmethod
            def append(page_id, children):
                return {"ok": True}

    class FakeClient:
        def __init__(self, auth=None):
            self.pages = FakePages()
            self.blocks = FakeBlocks()

    monkeypatch.setattr(b6, "Client", FakeClient, raising=True)
    return FakeClient

# ───────────────────────── fixtures ────────────────────────
@pytest.fixture(autouse=True)
def clear_state():
    state.user_states.clear()
    yield
    state.user_states.clear()

@pytest.fixture(autouse=True)
def local_env(monkeypatch):
    # чтобы APScheduler не стартовал, если где-то планирование
    monkeypatch.setenv("LOCAL_DEV", "1")

# ─────────────────── быстрые тесты (моки) ──────────────────
def _seed_minimal_state(user_id: str):
    state.set_state(user_id, {
        "normalized_number": "+77051230000",
        "client_name": "Тест Клиент",
        "show_type": "детское",
        "event_description": "д/р",
        "package": "Базовый",
        "event_date_iso": "2030-01-01",
        "event_time_24": "12:00",
        "scenario_stage_at_handover": "block3a",
        "handover_reason": "asked_handover",
        "structured_cache": {},  # опционально
    })

def test_build_properties_and_success(monkeypatch):
    """Проверяем корректную сборку props и успешный апсерта в Notion."""
    capture = {}
    _install_notion_mock(monkeypatch, capture=capture)
    # подкидываем валидные env, чтобы handle_block6 не отвалился
    monkeypatch.setenv("NOTION_API_KEY", "x")
    monkeypatch.setenv("NOTION_CRM_DATABASE_ID", "dbid")

    uid = "u_ok"
    _seed_minimal_state(uid)
    b6.handle_block6("", uid, lambda _: None)

    st = state.get_state(uid)
    assert st.get("notion_exported") is True
    assert st.get("notion_page_id") == "fake_page_id"

    props = capture["properties"]
    assert "Name" in props and props["Name"]["title"]
    assert props["ЭТАП"]["status"]["name"] in {"Получение информации", "Ручная обработка заказа", "Отправка материалов", "Сбор информации", "CRM", "Приветствие"}
    assert props["ТИП МЕРОПРИЯТИЯ"]["multi_select"][0]["name"] == "детское"
    assert props["Когда"]["date"]["start"].startswith("2030-01-01T12:00")

def test_retry_on_fail_then_success(monkeypatch):
    """Имитация 1 неудачи API → повторная попытка успешна, счётчик ретраев увеличен."""
    capture = {}
    _install_notion_mock(monkeypatch, fail_times=1, capture=capture)
    monkeypatch.setenv("NOTION_API_KEY", "x")
    monkeypatch.setenv("NOTION_CRM_DATABASE_ID", "dbid")

    uid = "u_retry"
    _seed_minimal_state(uid)
    # первый вызов внутри handle_block6 упадёт, _handle_export_failure выставит retry_count=1
    b6.handle_block6("", uid, lambda _: None)
    st = state.get_state(uid)
    assert st.get("notion_export_error") is True
    assert st.get("notion_retry_count") == 1

    # имитируем «повторный заход» (как бы APScheduler зашёл)
    b6.handle_block6("", uid, lambda _: None)
    st = state.get_state(uid)
    assert st.get("notion_exported") is True
    assert st.get("notion_export_error") is False
    assert st.get("notion_page_id") == "fake_page_id"

def test_implicit_refusal_sets_status(monkeypatch):
    """Причины молчаливого отказа должны помечать статус 'Отказ (молчание клиента)'."""
    capture = {}
    _install_notion_mock(monkeypatch, capture=capture)
    monkeypatch.setenv("NOTION_API_KEY", "x")
    monkeypatch.setenv("NOTION_CRM_DATABASE_ID", "dbid")

    uid = "u_refuse"
    _seed_minimal_state(uid)
    state.update_state(uid, {"handover_reason": "no_response_after_3_2"})
    b6.handle_block6("", uid, lambda _: None)

    props = capture["properties"]
    assert props["ЭТАП"]["status"]["name"] == "Отказ (молчание клиента)"

# ─────────────── лайв-смоук в реальный Notion ──────────────
REQUIRED_ENVS = ("NOTION_API_KEY", "NOTION_CRM_DATABASE_ID")
_live_skip_reason = None
if os.getenv("SKIP_LIVE_NOTION", "1") != "0":
    _live_skip_reason = "SKIP_LIVE_NOTION!=0 (по умолчанию отключено)"
else:
    for k in REQUIRED_ENVS:
        if not os.getenv(k):
            _live_skip_reason = f"{k} not set"

@pytest.mark.skipif(not os.getenv("NOTION_API_KEY"), reason="no live Notion API key")
def test_live_notion_smoke_and_cleanup(monkeypatch):
    """
    Реальный Notion:
      • создаём запись (через реальный Client);
      • убеждаемся, что появилась;
      • архивируем (cleanup).
    """
    # НЕ мокаем Client — используем реальный
    uid = "live_notion_u1"
    _seed_minimal_state(uid)

    # на всякий случай очистим флаги экспорта
    state.update_state(uid, {"notion_exported": False, "notion_page_id": None, "notion_export_error": False})

    b6.handle_block6("", uid, lambda _: None)

    st = state.get_state(uid)
    assert st.get("notion_exported") is True, "ожидали успешный экспорт"
    page_id = st.get("notion_page_id"); assert page_id

    notion = Client(auth=os.environ["NOTION_API_KEY"])
    page = notion.pages.retrieve(page_id=page_id)
    assert page["id"] == page_id
    props = page["properties"]
    assert "Name" in props and props["Name"]["title"], "должно быть название"
    assert "ЭТАП" in props and props["ЭТАП"]["status"]["name"], "должен быть статус"

    # cleanup: архивируем страницу
    notion.pages.update(page_id, archived=True)
