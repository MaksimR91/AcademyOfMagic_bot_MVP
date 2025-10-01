import time
from utils.ask_openai import ask_openai
from utils.wants_handover_ai import wants_handover_ai
from utils.reminder_engine import plan
from state.state import update_state
from logger import logger
from importlib import import_module
import os

# Пути к промптам
GLOBAL_PROMPT_PATH = "prompts/global_prompt.txt"
STAGE_PROMPT_PATH = "prompts/block02_prompt.txt"
REMINDER_PROMPT_PATH = "prompts/block02_reminder_1_prompt.txt"
REMINDER_2_PROMPT_PATH = "prompts/block02_reminder_2_prompt.txt"
CLASSIF_PROMPT_PATH = "prompts/block02_classification_prompt.txt"
# Время до повторного касания (4 часа)
DELAY_TO_BLOCK_2_1_HOURS = 4
DELAY_TO_BLOCK_2_2_HOURS = 12
FINAL_TIMEOUT_HOURS = 4

def load_prompt(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def render_prompt(path: str, **kwargs) -> str:
    """
    Рендерим текст промпта с плейсхолдерами через str.format().
    Внутренние фигурные скобки в промпте должны быть экранированы как {{ }}.
    """
    tmpl = load_prompt(path)
    try:
        return tmpl.format(**kwargs)
    except Exception as e:
        logger.warning(f"[block02] format error in {path}: {e}")
        return tmpl  # лучше вернуть сырой, чем упасть
    
global_prompt = load_prompt(GLOBAL_PROMPT_PATH)
stage_prompt = load_prompt(STAGE_PROMPT_PATH)

def proceed_to_block(stage_name, user_id):
    from router import route_message
    route_message("", user_id, force_stage=stage_name)

def _state():
    """Всегда берём актуальный модуль состояния (важно для тестов и прода)."""
    return import_module("state.state")

def _rule_based_label(text: str) -> str | None:
    """
    Позитивные шорткаты: если явно распознали — сразу возвращаем метку.
    Иначе None → дальше решает ИИ.
    """
    msg = text.lower()
    # 1) Детсад → детское
    if any(k in msg for k in [
        "детсад", "детсаду", "в детсаду",
        "садик", "детский сад", "в детском саду",
        "выпускной в саду"
    ]):
        return "детское"
    # 2) Свадьба/жених/невест- → взрослое (учитываем словоформы)
    if any(k in msg for k in ["свадьб", "жених", "невест"]):
        return "взрослое"
    # 3) Явно семейные маркеры
    if any(k in msg for k in ["семейн", "крещени"]):
        return "семейное"
    # 4) Корпоратив / коворкинг / презентация → НEСТАНДАРТНОЕ (см. актуальный DATASET)
    #    Важно: НЕ триггерим на "ресторан"/"банкет" сами по себе, чтобы не ломать юбилеи и свадьбы.
    if any(k in msg for k in ["корпоратив", "коворкинг", "презентац"]):
        return "нестандартное"
    # 5) ТРЦ/ТЦ/сцена/фойе → нестандартное
    if any(k in msg for k in ["трц", "тц", "сцена", "фойе"]):
         return "нестандартное"
    return None

def handle_block2(message_text, user_id, send_reply_func):

    state = _state()
    state_dict = state.get_state(user_id)
    # если уже отправляли стартовое сообщение — не дублируем
    if state_dict.get("stage") == "block2" and state_dict.get("block2_intro_sent"):
        return

    # Отправляем стартовое сообщение (только один раз)
    reply_to_client = ""
    try:
        reply_to_client = ask_openai(global_prompt + "\n\n" + stage_prompt)
    except Exception as e:
        logger.info(f"[error] ❌ Ошибка при ответе клиенту: {e}")
    if reply_to_client:
        send_reply_func(reply_to_client)
        # важно: помечаем, что последнее сообщение — от бота
        state.update_state(user_id, {
            "last_sender": "bot",
            "last_bot_ts": time.time()
        })

    state.update_state(user_id, {
        "stage": "block2",
        "block2_intro_sent": True,
        "last_sender": "bot",
        "last_message_ts": time.time()
    })

    plan(user_id,
         "blocks.block_02:send_first_reminder_if_silent",
         DELAY_TO_BLOCK_2_1_HOURS * 3600)
    # помечаем, что R1 уже запланирован (идемпотентность)
    state.update_state(user_id, {"r1_scheduled_b2": True})
    return


def handle_block2_user_reply(message_text, user_id, send_reply_func):
    logger.info(f"[debug] 👤 handle_block2_user_reply: user={user_id}, text={message_text}")
    state = _state()
    st = state.get_state(user_id) or {}
    # На любой входящий ответ пользователя сразу помечаем, что это ответ
    # и рубим повторные касания в блоке 2.
    now_ts = time.time()
    state.update_state(user_id, {
        "last_sender": "user",
        "last_message_ts": now_ts,
        "last_user_ts": now_ts,
        "cancel_block2_reminders": True
    })
    # 🔁 Хендовер по явной просьбе клиента (теперь проверяем здесь, а не в handle_block2)
    if wants_handover_ai(message_text):
        update_state(user_id, {
            "handover_reason": "asked_handover",
            "scenario_stage_at_handover": "block2"
        })
        from router import route_message
        return route_message(message_text, user_id, force_stage="block5")
    # (0) Позитивные шорткаты: если уверенно узнали тип — сразу маршрутизируем.
    rb = _rule_based_label(message_text)
    if rb:
        ts = time.time()
        state.update_state(user_id, {
            "show_type": rb,
            "uninformative_replies": 0,
            "last_sender": "user",
            "last_message_ts": ts,
            "last_user_ts": ts,
            "cancel_block2_reminders": True
        })
        if rb == "детское":
            next_block = "block3a"
        elif rb == "взрослое":
            next_block = "block3b"
        elif rb == "семейное":
            next_block = "block3c"
        else:
            next_block = "block3d"
        from router import route_message
        # В тестах фейковый роутер не меняет stage — зафиксируем сами
        state.update_state(user_id, {"stage": next_block})
        return route_message(message_text, user_id, force_stage=next_block)

    # Классификация
    classification_prompt = render_prompt(CLASSIF_PROMPT_PATH, message_text=message_text)
    try:
        resp = ask_openai(classification_prompt)
        show_type = (resp or "").strip().lower()
    except Exception as e:
        logger.info(f"[error] ❌ Ошибка при классификации: {e}")
        show_type = "неизвестно"

    # Нормализуем ПЕРЕД проверкой allowed
    for junk in (".", "!", "?", ":", ";", "—", "–"):
        show_type = show_type.replace(junk, "")
    show_type = show_type.strip()

    allowed = {"детское", "семейное", "взрослое", "нестандартное", "неизвестно"}
    if not show_type or show_type not in allowed:
        logger.info(f"[warn] ⚠️ Некорректный ответ модели: {show_type!r}, fallback → 'неизвестно'")
        show_type = "неизвестно"
    logger.info(f"[debug] 🧠 определён тип шоу: {show_type}")
    
    # Обработка "неизвестно"
    if show_type == "неизвестно":
        # всегда фиксируем в state текущий show_type
        state.update_state(user_id, {
            "show_type": "неизвестно",
            "last_sender": "user",
            "last_message_ts": time.time(),
            "last_user_ts": time.time(),
            "cancel_block2_reminders": False
        })
        count = st.get("uninformative_replies", 0) + 1

        if count > 2:
            state.update_state(user_id, {
                "handover_reason": "classification_failed_x3",
                "scenario_stage_at_handover": "block2"
            })
            from router import route_message
            return route_message("", user_id, force_stage="block5")

        clarification_prompt = global_prompt + "\n\n" + stage_prompt + "\n\n" + \
            "Предоставленной вами информации было недостаточно. " \
            "Пожалуйста, расскажите о вашем мероприятии подробнее: чей праздник, сколько гостей, взрослые или дети?"

        try:
            clarification_reply = ask_openai(clarification_prompt)
        except Exception as e:
            logger.info(f"[error] ❌ Ошибка при напоминании 2: {e}")
            clarification_reply = ""
        if clarification_reply:
            send_reply_func(clarification_reply)
            # фиксируем последнее бот-сообщение, как в 3a
            state.update_state(user_id, {
                "last_sender": "bot",
                "last_bot_ts": time.time()
            })

        state.update_state(user_id, {
            "show_type": "неизвестно",
            "uninformative_replies": count,
            "last_sender": "bot",
            "last_message_ts": time.time(),
            "cancel_block2_reminders": False
        })

        # R1 ставим только если ещё не ставили ранее
        if not (state.get_state(user_id) or {}).get("r1_scheduled_b2"):
            plan(user_id,
                 "blocks.block_02:send_first_reminder_if_silent",
                 DELAY_TO_BLOCK_2_1_HOURS * 3600)
            state.update_state(user_id, {"r1_scheduled_b2": True})
        return

    # Всё ок — переходим в нужный блок (пишем только через update_state)
    ts = time.time()
    state.update_state(user_id, {
        "show_type": show_type,
        "uninformative_replies": 0,
        "last_sender": "user",
        "last_message_ts": ts,
        "last_user_ts": ts,
        "cancel_block2_reminders": True
    })

    if show_type == "детское":
        next_block = "block3a"
    elif show_type == "взрослое":
        next_block = "block3b"
    elif show_type == "семейное":
        next_block = "block3c"
    elif show_type == "нестандартное":
        next_block = "block3d"
    else:
        logger.info(f"[warn] ❗Неожиданный тип шоу: {show_type}, fallback → block5")
        next_block = "block5"  # fallback на всякий случай

    from router import route_message
    # ЯВНО фиксируем сцену перед маршрутизацией (как и в rule-based ветке)
    state.update_state(user_id, {"stage": next_block})
    return route_message(message_text, user_id, force_stage=next_block)


def send_first_reminder_if_silent(user_id, send_reply_func):
    state = _state()
    st = state.get_state(user_id)
    # Глобальный рубильник
    if st and st.get("cancel_block2_reminders"):
        return
    if not st or st.get("stage") != "block2":
        return  # Клиент уже ответил или сменился блок — ничего не делаем
    # если клиент отвечал после последнего бот-сообщения — не шлём R1
    last_bot_ts = float(st.get("last_bot_ts") or 0)
    last_user_ts = float(st.get("last_user_ts") or 0)
    if st.get("last_sender") == "user" or (last_user_ts > last_bot_ts):
        return
    # идемпотентность: если уже ставили R1 — выходим
    if st.get("r1_scheduled_b2"):
        return

    global_prompt = load_prompt(GLOBAL_PROMPT_PATH)
    reminder_prompt = load_prompt(REMINDER_PROMPT_PATH)
    full_prompt = global_prompt + "\n\n" + reminder_prompt

    reply = ask_openai(full_prompt)
    send_reply_func(reply)

    now_ts = time.time()
    state.update_state(user_id, {
        "stage": "block2",
        "last_message_ts": now_ts,
        "last_sender": "bot",
        "last_bot_ts": now_ts,
        "r1_sent_b2": True
    })

    # Подготовка таймера на второе напоминание через 12 часов (в блок 2.2)
    plan(user_id, "blocks.block_02:send_second_reminder_if_silent", DELAY_TO_BLOCK_2_2_HOURS * 3600)
    state.update_state(user_id, {"r1_scheduled_b2": True})
    

def send_second_reminder_if_silent(user_id, send_reply_func):
    state = _state()
    st = state.get_state(user_id)
    if st and st.get("cancel_block2_reminders"):
        return
    if not st or st.get("stage") != "block2":
        return  # Клиент уже ответил — ничего не делаем
    # если клиент отвечал после последнего бот-сообщения — не шлём R2
    last_bot_ts = float(st.get("last_bot_ts") or 0)
    last_user_ts = float(st.get("last_user_ts") or 0)
    if st.get("last_sender") == "user" or (last_user_ts > last_bot_ts):
        return
    if st.get("r2_scheduled_b2"):
        return

    global_prompt = load_prompt(GLOBAL_PROMPT_PATH)
    reminder_prompt = load_prompt(REMINDER_2_PROMPT_PATH)
    full_prompt = global_prompt + "\n\n" + reminder_prompt

    reply = ask_openai(full_prompt)
    send_reply_func(reply)

    now_ts = time.time()
    state.update_state(user_id, {
        "stage": "block2",
        "last_message_ts": now_ts,
        "last_sender": "bot",
        "last_bot_ts": now_ts,
        "r2_sent_b2": True
    })
    plan(user_id, "blocks.block_02:finalize_if_still_silent", FINAL_TIMEOUT_HOURS * 3600)
    state.update_state(user_id, {"r2_scheduled_b2": True})

# Финальный таймер — если клиент не ответит ещё 4 часа, уходим в block5
def finalize_if_still_silent(user_id, send_reply_func):
    state = _state()
    st2 = state.get_state(user_id)
    if st2 and st2.get("cancel_block2_reminders"):
        return
    if not st2 or st2.get("stage") != "block2":
        return  # Ответил — всё ок
    # идемпотентность финала
    if st2.get("fin_scheduled_b2_done"):
        return
    # если клиент ответил после последнего бот-сообщения — не хендоверим
    last_bot_ts = float(st2.get("last_bot_ts") or 0)
    last_user_ts = float(st2.get("last_user_ts") or 0)
    if last_user_ts > last_bot_ts:
        return
    state.update_state(user_id, {
        "handover_reason": "no_response_after_2_2",
        "scenario_stage_at_handover": "block2",
        "fin_scheduled_b2_done": True    })
    from router import route_message
    route_message("", user_id, force_stage="block5")
