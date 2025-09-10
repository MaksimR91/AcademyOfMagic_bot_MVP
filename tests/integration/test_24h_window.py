# tests/integration/test_24h_window.py
import time
import types
import pytest
from importlib import import_module

# ───────────────────────── фикстуры окружения ─────────────────────────

@pytest.fixture(autouse=True)
def env_24h(monkeypatch):
    """
    Включаем "24-часовое окно" через переменную окружения LATE_DROP_MIN.
    По умолчанию у тебя 20 минут, здесь выставляем ровно 1440 минут.
    Плюс включаем LOCAL_DEV/ACADEMYBOT_TEST, чтобы не стартовал планировщик.
    """
    monkeypatch.setenv("LATE_DROP_MIN", "1440")
    monkeypatch.setenv("LOCAL_DEV", "1")
    monkeypatch.setenv("ACADEMYBOT_TEST", "1")


@pytest.fixture
def clear_state():
    """
    Акуратная очистка state перед тестом.
    """
    from state.state import user_states
    user_states.clear()
    return user_states


# ───────────────────────── помощники для патча ─────────────────────────

class HandlerSpy:
    """
    Заменяем handle_block2_user_reply на лёгкий spy;
    собираем факты вызова, чтобы удостовериться, что хендлер вызван/не вызван.
    """
    def __init__(self):
        self.called = 0
        self.last_args = None

    def __call__(self, message_text, user_id, send_reply_func):
        self.called += 1
        self.last_args = (message_text, user_id)


@pytest.fixture
def patch_block2_handler(monkeypatch):
    spy = HandlerSpy()
    b2 = import_module("blocks.block_02")
    monkeypatch.setattr(b2, "handle_block2_user_reply", spy, raising=True)
    return spy


def _route(text, uid, ts=None, force_stage=None):
    """
    Упрощённый вызов роутера.
    """
    router = import_module("router")
    return router.route_message(text, uid, message_ts=ts, force_stage=force_stage)


# ───────────────────────── тесты ─────────────────────────

def test_accepts_fresh_message_updates_window(clear_state, patch_block2_handler):
    """
    Сообщение свежее (<24ч): должно пройти в хендлер и переписать last_msg_ts.
    """
    uid = "u_fresh"
    from state.state import update_state, get_state

    # стартуем пользователя на block2, чтобы использовать наш патч-хендлер
    update_state(uid, {"stage": "block2", "last_msg_ts": time.time()})

    t0 = get_state(uid)["last_msg_ts"]
    fresh_ts = t0 + 60  # +1 минута

    _route("привет, это свежий текст", uid, ts=fresh_ts)

    st = get_state(uid)
    assert patch_block2_handler.called == 1, "Хендлер должен быть вызван для свежего сообщения"
    assert abs(st["last_msg_ts"] - fresh_ts) < 1e-3, "Окно активности должно сдвинуться на ts входящего"


def test_drops_older_than_24h_and_does_not_update(clear_state, patch_block2_handler):
    """
    Сообщение старее 24ч: должно быть отброшено ДО вызова хендлера,
    и last_msg_ts не должен измениться.
    """
    uid = "u_old"
    from state.state import update_state, get_state

    # выставляем актуальный last_msg_ts и stage=block2
    now = time.time()
    update_state(uid, {"stage": "block2", "last_msg_ts": now})

    old_ts = now - (24 * 3600 + 60)  # 24ч + 1 мин назад

    _route("старое сообщение", uid, ts=old_ts)

    st = get_state(uid)
    assert patch_block2_handler.called == 0, "Хендлер не должен вызываться для старого сообщения"
    assert abs(st["last_msg_ts"] - now) < 1e-3, "last_msg_ts не должен измениться на старое значение"


def test_new_message_reopens_window_then_old_is_dropped(clear_state, patch_block2_handler):
    """
    Новое сообщение "открывает окно":
      1) старое (24ч+) — дропается (хендлер не вызван, ts неизменен);
      2) новое — проходит, сдвигает last_msg_ts;
      3) после сдвига — снова пробуем старый ts (ещё старее относительно нового last_msg_ts), он дропается.
    """
    uid = "u_reopen"
    from state.state import update_state, get_state

    base = time.time()
    update_state(uid, {"stage": "block2", "last_msg_ts": base})

    too_old_ts = base - (24 * 3600 + 30)  # старше 24ч → дроп
    _route("слишком старое", uid, ts=too_old_ts)
    st = get_state(uid)
    assert patch_block2_handler.called == 0, "Первое сообщение старое — хендлер не вызывается"
    assert abs(st["last_msg_ts"] - base) < 1e-3, "last_msg_ts не должен меняться на старое"

    # Новое сообщение в пределах окна (например, +5 минут)
    fresh_ts = base + 300
    _route("новое, свежее", uid, ts=fresh_ts)
    st = get_state(uid)
    assert patch_block2_handler.called == 1, "Новое свежее сообщение — хендлер должен вызваться"
    assert abs(st["last_msg_ts"] - fresh_ts) < 1e-3, "Окно должно сдвинуться на новое время"

    # Теперь снова попробуем старый ts — теперь он тем более за 24ч от last_seen
    _route("опять старое", uid, ts=too_old_ts)
    st = get_state(uid)
    assert patch_block2_handler.called == 1, "Старое снова дропается — новых вызовов хендлера быть не должно"
    assert abs(st["last_msg_ts"] - fresh_ts) < 1e-3, "last_msg_ts не должен переписаться старым ts"
