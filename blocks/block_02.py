import time
from utils.ask_openai import ask_openai
from utils.wants_handover_ai import wants_handover_ai
from utils.reminder_engine import plan
from state.state import update_state
from logger import logger
from importlib import import_module
import os
from utils.reminder_engine import plan, sched
import re

# Пути к промптам
GLOBAL_PROMPT_PATH = "prompts/global_prompt.txt"
STAGE_PROMPT_PATH = "prompts/block02_prompt.txt"
REMINDER_PROMPT_PATH = "prompts/block02_reminder_1_prompt.txt"
REMINDER_2_PROMPT_PATH = "prompts/block02_reminder_2_prompt.txt"
CLASSIF_PROMPT_PATH = "prompts/block02_classification_prompt.txt"
# Доп. файлы для статичных сообщений (если хочешь хранить тексты вне кода)
INTRO_STATIC_PATH = "prompts/block02_intro_static.txt"
REPROMPT_STATIC_PATH = "prompts/block02_reprompt_static.txt"
REM1_STATIC_PATH = "prompts/block02_reminder_1_static.txt"
REM2_STATIC_PATH = "prompts/block02_reminder_2_static.txt"

# Флаг: использовать ИИ-сообщения или статичные тексты
USE_AI_BLOCK2 = (os.getenv("USE_AI_BLOCK2", "false").strip().lower() == "true")

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

JITTER_SEC = 2.0  # допуск на ранний вызов джобы (сеть/джиттер)

def _replan_seconds(user_id: str, job_fn_qualname: str, seconds: float):
    """Безопасно перепланировать ту же задачу через seconds."""
    if seconds <= 0:
        seconds = 1.0
    try:
        plan(user_id, job_fn_qualname, seconds)
    except Exception as e:
        logger.warning(f"[block02] replan fail {job_fn_qualname}: {e}")

def _cancel_block2_jobs(user_id: str):
    """Снять все джобы этого пользователя (как в #reset), чтобы не «стреляли» позже."""
    try:
        for job in sched.get_jobs():
            if job.id.startswith(f"{user_id}:"):
                sched.remove_job(job.id)
    except Exception as e:
        logger.warning(f"[block02] cancel jobs failed: {e}")

def load_prompt(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
    
def load_static_or_default(path: str, default_text: str) -> str:
    """Пробуем загрузить статичный текст из файла, иначе возвращаем дефолт."""
    try:
        if os.path.exists(path):
            txt = load_prompt(path).strip()
            if txt:
                return txt
    except Exception as e:
        logger.warning(f"[block02] failed to load {path}: {e}")
    return default_text


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

# Статичные тексты по умолчанию (можешь заменить или вынести в файлы выше)
BLOCK2_INTRO_TEXT = load_static_or_default(
    INTRO_STATIC_PATH,
    "Нужно понять формат шоу. Напишите, какое у вас событие: день рождения, свадьба, юбилей, семейный праздник или другое."
)
BLOCK2_REPROMPT_TEXT = load_static_or_default(
    REPROMPT_STATIC_PATH,
    "Не совсем понял. Напишите проще: детское, взрослое, семейное или нестандартное. Если сложно выбрать — опишите событие в одном-двух предложениях."
)
REM1_TEXT_STATIC = load_static_or_default(
    REM1_STATIC_PATH,
    "Напомню про формат шоу. Скажите, у вас детское, взрослое, семейное или нестандартное событие?"
)
REM2_TEXT_STATIC = load_static_or_default(
    REM2_STATIC_PATH,
    "Ещё на связи. Какой формат вашего события: детский, взрослый, семейный или нестандартный?"
)

def proceed_to_block(stage_name, user_id):
    from router import route_message
    route_message("", user_id, force_stage=stage_name)

def _state():
    """Всегда берём актуальный модуль состояния (важно для тестов и прода)."""
    return import_module("state.state")

# --- Простой быстрый парсер полей из свободного текста (без ИИ) ---
_DATE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b")
_TIME = re.compile(r"\b([01]?\d|2[0-3])[:.](\d{2})\b")
_AGE  = re.compile(r"\b(\d{1,2})\s*(год|года|лет)\b", re.IGNORECASE)
_CNT  = re.compile(r"\b(\d{1,3})\s*(гостей|чел|человек)\b", re.IGNORECASE)
_NAME = re.compile(r"(?:для|у|сын|дочь|именинник|именинница)\s+([А-ЯЁA-Z][а-яёa-z\-]+)")
_PLACE_HINTS = ["кафе","бар","ресторан","зал","лофт","школ","сад","трц","тц","дом","клуб"]

def _norm_date(s: str) -> str|None:
    m = _DATE.search(s)
    if not m: return None
    d, mo, y = m.groups()
    y = (("20"+y) if y and len(y)==2 else y) or str(time.gmtime().tm_year)
    try:
        dd = int(d); mm = int(mo); yy = int(y)
        if 1<=dd<=31 and 1<=mm<=12: return f"{yy:04d}-{mm:02d}-{dd:02d}"
    except: pass
    return None

def _norm_time(s: str) -> str|None:
    m = _TIME.search(s); 
    if not m: return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if 0<=hh<=23 and 0<=mm<=59: return f"{hh:02d}:{mm:02d}"
    return None

def _guess_place(s: str) -> str|None:
    low = s.lower()
    for h in _PLACE_HINTS:
        pat = re.compile(rf"{h}\s+([\"«»A-Za-zА-Яа-яЁё0-9\- ]+)")
        m = pat.search(low)
        if m:
            frag = h + " " + m.group(1).strip(" «»\"")
            return frag.strip()
    # кавычки «Салют»
    m2 = re.search(r"[«\"]([A-Za-zА-Яа-яЁё0-9 \-]+)[»\"]", s)
    return m2.group(1).strip() if m2 else None

def _quick_extract_fields(text: str) -> dict:
    if not text: return {}
    out = {}
    if (d := _norm_date(text)): out["event_date"] = d
    if (t := _norm_time(text)): out["event_time"] = t
    if (p := _guess_place(text)): out["event_location"] = p
    if (m := _NAME.search(text)): out["celebrant_name"] = m.group(1)
    if (m := _AGE.search(text)):  out["celebrant_age"] = int(m.group(1))
    if (m := _CNT.search(text)):  out["guests_count"]  = int(m.group(1))
    # грубые подсказки по аудитории
    low = text.lower()
    if "дет" in low: out["guests_age"] = "дети"
    if "взросл" in low: out["guests_age"] = "взрослые"
    return out


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
    if USE_AI_BLOCK2:
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
    else:
        send_reply_func(BLOCK2_INTRO_TEXT)
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
    # важно: убрать уже поставленные джобы, чтобы они не сработали позже
    _cancel_block2_jobs(user_id)
    state.update_state(user_id, {
        "r1_sent_b2": False, "r2_sent_b2": False,
        "r1_scheduled_b2": False, "r2_scheduled_b2": False,
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
        # unified block_03: любые «детское/взрослое/семейное» → block3,
        # «нестандартное» остаётся в block3d
        if rb in {"детское", "взрослое", "семейное"}:
            next_block = "block3"
        else:  # rb == "нестандартное"
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


    # 📥 ДОПОЛНИТЕЛЬНО: забираем поля из ответа клиента в block2
    try:
        extracted = _quick_extract_fields(message_text or "")
        if extracted:
            # не затираем уже заполненные ключи
            cur = state.get_state(user_id) or {}
            safe_update = {k:v for k,v in extracted.items() if not cur.get(k)}
            if safe_update:
                state.update_state(user_id, safe_update)
                logger.info(f"[block2] prefilled from user text: {safe_update}")
        # сохраняем исходный текст для 3 как базовое описание
        if message_text:
            prev = (state.get_state(user_id) or {}).get("event_description","")
            base = (prev + "\n" + message_text).strip() if prev else message_text.strip()
            state.update_state(user_id, {"event_description": base})
    except Exception as e:
        logger.warning(f"[block2] extract failed: {e}")
    
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

        if USE_AI_BLOCK2:
            clarification_prompt = global_prompt + "\n\n" + stage_prompt + "\n\n" + \
                "Предоставленной вами информации было недостаточно. " \
                "Пожалуйста, расскажите о вашем мероприятии подробнее: чей праздник, сколько гостей, взрослые или дети?"
            try:
                clarification_reply = ask_openai(clarification_prompt)
            except Exception as e:
                logger.info(f"[error] ❌ Ошибка при переспросе: {e}")
                clarification_reply = ""
            if clarification_reply:
                send_reply_func(clarification_reply)
        else:
            send_reply_func(BLOCK2_REPROMPT_TEXT)

        # фиксируем последнее бот-сообщение
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

    # unified block_03: «детское/взрослое/семейное» → block3, «нестандартное» → block3d
    if show_type == "нестандартное":
        next_block = "block3d"
    else:
        # любые валидные, кроме «нестандартное», идут в унифицированный block3
        if show_type in {"детское", "взрослое", "семейное"}:
            next_block = "block3"
        else:
            logger.info(f"[warn] ❗Некорректный/пустой тип шоу: {show_type!r}, fallback → 'неизвестно' (переспрашиваем)")
            # оставляем на block2, т.к. выше ветка «неизвестно» уже отработала с репромптом
            return

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
    # идемпотентность: если уже ОТПРАВЛЯЛИ R1 — выходим (должно быть ДО replan)
    if st.get("r1_sent_b2"):
        logger.info("[block02:R1] skip: r1_sent_b2 already True")
        return

    # если клиент отвечал после последнего бот-сообщения — не шлём R1
    last_bot_ts  = float(st.get("last_bot_ts") or 0)
    last_user_ts = float(st.get("last_user_ts") or 0)
    now_ts       = time.time()
    eff_need     = _eff_delay_sec(DELAY_TO_BLOCK_2_1_HOURS)
    dt           = now_ts - last_bot_ts
    # если рано — перепланируем на остаток
    if dt + JITTER_SEC < eff_need:
        remaining = max(eff_need - dt, 1.0)
        logger.info("[block02:R1] too early (dt=%.1fs). replan in %.1fs", dt, remaining)
        _replan_seconds(user_id, "blocks.block_02:send_first_reminder_if_silent", remaining)
        return
    if last_user_ts > last_bot_ts:
        logger.info("[block02:R1] skip: last_user_ts > last_bot_ts (%.0f > %.0f)", last_user_ts, last_bot_ts)
        return

    global_prompt = load_prompt(GLOBAL_PROMPT_PATH)
    reminder_prompt = load_prompt(REMINDER_PROMPT_PATH)
    if USE_AI_BLOCK2:
        full_prompt = global_prompt + "\n\n" + reminder_prompt
        reply = ask_openai(full_prompt)
        send_reply_func(reply)
    else:
        send_reply_func(REM1_TEXT_STATIC)

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
    # идемпотентность: если уже ОТПРАВЛЯЛИ R2 — выходим (должно быть ДО replan)
    if st.get("r2_sent_b2"):
        logger.info("[block02:R2] skip: r2_sent_b2 already True")
        return

    # если клиент отвечал после последнего бот-сообщения — не шлём R2
    last_bot_ts  = float(st.get("last_bot_ts") or 0)
    last_user_ts = float(st.get("last_user_ts") or 0)
    now_ts       = time.time()
    eff_need     = _eff_delay_sec(DELAY_TO_BLOCK_2_2_HOURS)
    dt           = now_ts - last_bot_ts
    if dt + JITTER_SEC < eff_need:
        remaining = max(eff_need - dt, 1.0)
        logger.info("[block02:R2] too early (dt=%.1fs). replan in %.1fs", dt, remaining)
        # Идемпотентность: если уже ставили реплан — больше не плодим задачи
        if st.get("r2_scheduled_b2"):
            logger.info("[block02:R2] skip replan: r2_scheduled_b2 already True")
            return
        _replan_seconds(user_id, "blocks.block_02:send_second_reminder_if_silent", remaining)
        state.update_state(user_id, {"r2_scheduled_b2": True})
        return
    if last_user_ts > last_bot_ts:
        logger.info("[block02:R2] skip: last_user_ts > last_bot_ts (%.0f > %.0f)", last_user_ts, last_bot_ts)
        return

    global_prompt = load_prompt(GLOBAL_PROMPT_PATH)
    reminder_prompt = load_prompt(REMINDER_2_PROMPT_PATH)
    if USE_AI_BLOCK2:
        full_prompt = global_prompt + "\n\n" + reminder_prompt
        reply = ask_openai(full_prompt)
        send_reply_func(reply)
    else:
        send_reply_func(REM2_TEXT_STATIC)

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
    last_bot_ts  = float(st2.get("last_bot_ts") or 0)
    last_user_ts = float(st2.get("last_user_ts") or 0)
    now_ts       = time.time()
    eff_need     = _eff_delay_sec(FINAL_TIMEOUT_HOURS)
    dt           = now_ts - last_bot_ts
    if dt + JITTER_SEC < eff_need:
        # Финал не планирует задачи. Если рано — тихо выходим.
        logger.info("[block02:FIN] too early — no-op")
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
