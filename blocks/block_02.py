import time
from utils.ask_openai import ask_openai
from utils.wants_handover_ai import wants_handover_ai
from utils.reminder_engine import plan
from state.state import update_state
from logger import logger
from importlib import import_module
import os
from utils.reminder_engine import sched  # для отмены джобов

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

def _eff_delay_sec(hours: float) -> float:
    """Учитываем REMINDER_ACCEL, как в 3a."""
    try:
        accel = float(os.getenv("REMINDER_ACCEL", "1.0"))
    except Exception:
        accel = 1.0
    return hours * 3600.0 * accel

def _cancel_block2_jobs(user_id: str):
    """Снимаем все отложенные задачи по block_02 для этого пользователя."""
    try:
        for job in sched.get_jobs():
            # у вас в #reset снимаются все user-джобы по префиксу f"{user_id}:"
            # здесь достаточно быть безопасными и снять всё по пользователю
            if job.id.startswith(f"{user_id}:"):
                sched.remove_job(job.id)
    except Exception as e:
        logger.warning(f"[block02] cancel jobs failed: {e}")

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
     # casefold — надёжнее, чем lower, для кириллицы и смешанных символов
    msg = (text or "").casefold()
    # 1) Детсад → детское
    if any(k in msg for k in [
        "детсад", "детсаду", "в детсаду",
        "садик", "детский сад", "в детском саду",
        "выпускной в саду"
    ]):
        return "детское"
    # 2) Свадьба/жених/невест- → взрослое (учитываем словоформы + явное слово)
    if any(k in msg for k in ["свадьба", "свадьб", "жених", "невест", "роспись", "бракосочетан", "загс", "регистрация брака"]):
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
    # важно: get_state может вернуть None
    state_dict = state.get_state(user_id) or {}
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
        "last_message_ts": time.time(),
        # сбрасываем флаги отправленных повторок на новый цикл
        "r1_sent_b2": False,
        "r2_sent_b2": False,
        # и на всякий случай финальный флаг
        "fin_scheduled_b2_done": False
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
    # критично: снимаем уже запланированные джобы, чтобы они не «стреляли» позже
    _cancel_block2_jobs(user_id)
    # и сразу сбрасываем внутренние флаги расписания/отправок на всякий случай
    state.update_state(user_id, {
        "r1_sent_b2": False,
        "r2_sent_b2": False,
        "r1_scheduled_b2": False,
        "r2_scheduled_b2": False,
        "fin_scheduled_b2_done": False
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
        # Зафиксируем stage до роутинга, чтобы следующие хендлеры не терялись
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
            # клиент ответил — не открываем повторки вручную, повторки управляются по ts
            "cancel_block2_reminders": True
        })
        # пересчитаем по актуальному состоянию, а не по старому снапшоту st
        curr = state.get_state(user_id) or {}
        count = int(curr.get("uninformative_replies", 0)) + 1

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
            "cancel_block2_reminders": True
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
    # ЯВНО фиксируем сцену и дополнительно «страхуемся» от старых таймеров
    state.update_state(user_id, {
        "stage": next_block,
        "cancel_block2_reminders": True,
        "r1_scheduled_b2": False,
        "r2_scheduled_b2": False
    })
    return route_message(message_text, user_id, force_stage=next_block)


def send_first_reminder_if_silent(user_id, send_reply_func):
    state = _state()
    st = state.get_state(user_id)
    if not st:
        logger.info("[block02:R1] skip: no state")
        return
    if st.get("stage") != "block2":
        logger.info("[block02:R1] skip: stage=%s != block2", st.get("stage"))
        return
    if st.get("cancel_block2_reminders"):
        logger.info("[block02:R1] skip: cancel_block2_reminders=True")
        return
    # если клиент отвечал после последнего бот-сообщения — не шлём R1
    last_bot_ts = float(st.get("last_bot_ts") or 0)
    last_user_ts = float(st.get("last_user_ts") or 0)
    now_ts = time.time()
    # защита от «ранних» таймеров (учитываем REMINDER_ACCEL)
    if now_ts - last_bot_ts < _eff_delay_sec(DELAY_TO_BLOCK_2_1_HOURS):
        logger.info("[block02:R1] skip: too early (dt=%.0fs)", now_ts - last_bot_ts)
        return
    if last_user_ts > last_bot_ts:
        logger.info("[block02:R1] skip: last_user_ts > last_bot_ts (%.0f > %.0f)", last_user_ts, last_bot_ts)
        return
    # идемпотентность: если уже ОТПРАВЛЯЛИ R1 — выходим
    if st.get("r1_sent_b2"):
        logger.info("[block02:R1] skip: r1_sent_b2 already True")
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
    logger.info("[block02:R1] sent & scheduled R2")

def send_second_reminder_if_silent(user_id, send_reply_func):
    state = _state()
    st = state.get_state(user_id)
    if not st:
        logger.info("[block02:R2] skip: no state")
        return
    if st.get("stage") != "block2":
        logger.info("[block02:R2] skip: stage=%s != block2", st.get("stage"))
        return  
    if st.get("cancel_block2_reminders"):
        logger.info("[block02:R2] skip: cancel_block2_reminders=True")
        return
    # если клиент отвечал после последнего бот-сообщения — не шлём R2
    last_bot_ts = float(st.get("last_bot_ts") or 0)
    last_user_ts = float(st.get("last_user_ts") or 0)
    now_ts = time.time()
    if now_ts - last_bot_ts < _eff_delay_sec(DELAY_TO_BLOCK_2_2_HOURS):
        logger.info("[block02:R2] skip: too early (dt=%.0fs)", now_ts - last_bot_ts)
        return
    if last_user_ts > last_bot_ts:
        logger.info("[block02:R2] skip: last_user_ts > last_bot_ts (%.0f > %.0f)", last_user_ts, last_bot_ts)
        return
    # идемпотентность: если уже ОТПРАВЛЯЛИ R2 — выходим
    if st.get("r2_sent_b2"):
        logger.info("[block02:R2] skip: r2_sent_b2 already True")
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
    logger.info("[block02:R2] sent & scheduled FINAL")

# Финальный таймер — если клиент не ответит ещё 4 часа, уходим в block5
def finalize_if_still_silent(user_id, send_reply_func):
    state = _state()
    st2 = state.get_state(user_id)
    if not st2 or st2.get("stage") != "block2":
        return 
    if st2.get("cancel_block2_reminders"):
        logger.info("[block02:FIN] skip: cancel_block2_reminders=True")
        return
    # идемпотентность финала
    if st2.get("fin_scheduled_b2_done"):
        return
    # если клиент ответил после последнего бот-сообщения — не хендоверим
    last_bot_ts = float(st2.get("last_bot_ts") or 0)
    last_user_ts = float(st2.get("last_user_ts") or 0)
    now_ts = time.time()
    # защита от «раннего» финала
    if now_ts - last_bot_ts < _eff_delay_sec(FINAL_TIMEOUT_HOURS):
        logger.info("[block02:FIN] skip: too early (dt=%.0fs)", now_ts - last_bot_ts)
        return
    if last_user_ts > last_bot_ts:
        return
    state.update_state(user_id, {
        "handover_reason": "no_response_after_2_2",
        "scenario_stage_at_handover": "block2",
        "fin_scheduled_b2_done": True    })
    from router import route_message
    logger.info("[block02:FIN] handover → block5 (no responses after R2)")
    route_message("", user_id, force_stage="block5")
