# block_03a.py
import time
import re
from utils.ask_openai import ask_openai
from utils.wants_handover_ai import wants_handover_ai
from utils.schedule import load_schedule_from_s3, check_date_availability
from state.state import get_state, update_state
from utils.reminder_engine import plan
from logger import logger
from utils.structured import build_structured_snapshot

# Пути к промптам (оставляем 3a)
GLOBAL_PROMPT_PATH    = "prompts/global_prompt.txt"
STAGE_PROMPT_PATH     = "prompts/block03a_prompt.txt"
STRUCTURE_PROMPT_PATH = "prompts/block03a_data_prompt.txt"
REMINDER_1_PROMPT_PATH= "prompts/block03_reminder_1_prompt.txt"
REMINDER_2_PROMPT_PATH= "prompts/block03_reminder_2_prompt.txt"
AVAILABILITY_PROMPT_PATH = "prompts/block03_availability_prompt.txt"

# Тайминги
DELAY_TO_BLOCK_3_1_HOURS = 4
DELAY_TO_BLOCK_3_2_HOURS = 12
FINAL_TIMEOUT_HOURS      = 4

# ——— УНИФИЦИРОВАННЫЕ КОНСТАНТЫ, КАК В 3C ———
SAFE_KEYS = {
    "event_date", "event_time", "event_location",
    "celebrant_name", "celebrant_gender", "celebrant_age",
    "guests_count", "guests_gender", "guests_age", "no_celebrant"
}
IGNORED_VALUES = {"", "не указано", "не указан", "неизвестно", "прочерк", "-", "n/a"}

KEY_NAMES = {
    "event_date":       "дата мероприятия",
    "event_time":       "время мероприятия",
    "event_location":   "название места проведения",
    "celebrant_name":   "имя ключевого участника",
    "celebrant_gender": "пол ключевого участника",
    "celebrant_age":    "возраст ключевого участника",
    "guests_count":     "количество гостей",
    "guests_gender":    "пол гостей",
    "guests_age":       "возраст гостей",
}

def load_prompt(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
    
def render_prompt(path: str, **kwargs) -> str:
    """
    Рендерим текст промпта с плейсхолдерами {name} через str.format().
    Если в шаблоне нужны фигурные скобки как символы — экранировать {{ }}.
    """
    tmpl = load_prompt(path)
    try:
        return tmpl.format(**kwargs)
    except Exception as e:
        logger.warning(f"[block03a] format error in {path}: {e}")
        return tmpl

def missing_info_keys(state):
    required = [
        'event_date','event_time','event_location',
        'celebrant_name','celebrant_gender','celebrant_age',
        'guests_count','guests_gender','guests_age'
    ]
    return [k for k in required if not state.get(k)]

def upsert_state(user_id, parsed_data: dict):
    state = get_state(user_id) or {}
    out = {}

    def norm(v: str) -> str:
        return (v or "").strip()

    for k, v in (parsed_data or {}).items():
        if k not in SAFE_KEYS:
            continue
        sv = norm(str(v)) if v is not None else ""
        lv = sv.lower()

        # пропускаем мусорные значения
        if lv in IGNORED_VALUES:
            continue

        # --- спец-логика для флага no_celebrant ---
        if k == "no_celebrant":
            yes_values = {"да", "yes", "true", "y", "1"}
            no_values  = {"нет", "no", "false", "n", "0"}

            cur = norm(state.get(k))
            lcur = cur.lower()

            if lv in yes_values:
                # новое "Да" всегда перезаписывает
                out[k] = "Да"
                continue
            if lv in no_values:
                # "нет" пишем только если пусто
                if not cur:
                    out[k] = "нет"
                continue
            # если что-то странное, не трогаем
            continue
        # --- конец спец-логики ---

        # обычные поля: не затираем непустые
        if state.get(k):
            continue
        out[k] = sv

    if out:
        update_state(user_id, out)
    return get_state(user_id)


# ——— Fallback-парсер из старого 3a, если JSON не пришёл ———
def parse_structured_pairs(text: str) -> dict:
    flags = re.IGNORECASE | re.MULTILINE

    patterns = {
        "celebrant_name":       r"Имя\s+(?:ключевого\s+участника|именинника)\s*[-—:]\s*([^\n\r]+)",
        "celebrant_gender":     r"Пол\s+(?:ключевого\s+участника|именинника)\s*[-—:]\s*([^\n\r]+)",
        "celebrant_age":        r"Возраст\s+(?:ключевого\s+участника|именинника)\s*[-—:]\s*([^\n\r]+)",
        "event_date":           r"Дата\s+мероприятия\s*[-—:]\s*([^\n\r]+)",
        "event_time":           r"Время\s+мероприятия\s*[-—:]\s*([^\n\r]+)",
        "event_location_type":  r"(?:Тип\s+места\s+проведения|Классификация\s+места)\s*[-—:]\s*([^\n\r]+)",
        "event_location":       r"(?:Название\s+места\s+проведения|Название\s+места)\s*[-—:]\s*([^\n\r]+)",
        "guests_count":         r"Количество\s+гостей\s*[-—:]\s*([^\n\r]+)",
        "guests_gender":        r"(?:Пол\s+гостей|Пол\s+гостей\s+детского\s+возраста)\s*[-—:]\s*([^\n\r]+)",
        "guests_age":           r"(?:Возраст\s+гостей|Возраст\s+гостей\s+детского\s+возраста)\s*[-—:]\s*([^\n\r]+)",
    }

    def clean(v: str) -> str:
        return v.strip().strip(" .;,")

    result = {}
    for key, pat in patterns.items():
        m = re.search(pat, text, flags)
        if m:
            result[key] = clean(m.group(1))
    return result

def _clean_time(raw_time: str) -> str:
    m = re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", raw_time or "")
    return m.group(0) if m else ""

def _clean_date(raw_date: str) -> str:
    m = re.search(r"\b\d{4}-\d{2}-\d{2}\b", raw_date or "")
    return m.group(0) if m else ""

def handle_block3a(message_text, user_id, send_reply_func, client_request_date=None):
    from router import route_message
    if client_request_date is None:
        client_request_date = time.time()

    if wants_handover_ai(message_text):
        update_state(user_id, {
            "handover_reason": "asked_handover",
            "scenario_stage_at_handover": "block3"
        })
        return route_message(message_text, user_id, force_stage="block5")

    state = get_state(user_id) or {}
    # Любой входящий текст от клиента «гасит» дальнейшие автокасания до явного решения
    update_state(user_id, {"last_sender": "user"})
    prev_info = state.get("event_description", "")
    updated_description = (prev_info + "\n" + (message_text or "")).strip()
    update_state(user_id, {"event_description": updated_description})

    # ——— Промпты (оставляем 3a) ———
    global_prompt = load_prompt(GLOBAL_PROMPT_PATH)
    stage_prompt  = load_prompt(STAGE_PROMPT_PATH)
    struct_prompt = load_prompt(STRUCTURE_PROMPT_PATH)

    # 1) Структурирование как в 3c: сначала пробуем JSON, иначе fallback
    struct_input = struct_prompt + f'\n\nСообщение клиента: "{message_text}"'
    structured_reply = ask_openai(struct_input).strip()
    logger.info("Ответ от OpenAI ДО парсинга:\n%s", structured_reply)

    parsed_data = {}
    try:
        import json
        parsed = json.loads(structured_reply)
        if isinstance(parsed, dict):
            parsed_data = parsed
        else:
            parsed_data = {}
    except Exception:
        parsed_data = parse_structured_pairs(structured_reply)

    state = upsert_state(user_id, parsed_data)
    logger.info("Структурированные данные после апсёрта %s", {k: state.get(k) for k in SAFE_KEYS})

    # для лога — чистая карта входных значений
    parsed_view = {
        k: ("" if v is None else str(v).strip())
        for k, v in (parsed_data or {}).items()
        if v is not None and str(v).strip().lower() not in IGNORED_VALUES
    }
    logger.info("Структурированные данные после очистки %s", parsed_view)

    # snapshot как в 3c
    snap = build_structured_snapshot(state)
    update_state(user_id, {"structured_cache": snap})
    state = get_state(user_id)

    # 2) Уточняем дату/время до ISO/24h (как в 3c)
    from datetime import datetime
    now = datetime.now()
    client_request_date_str = now.strftime("%d %B %Y")
    current_year = now.year

    match_date = None
    match_time = None

    combined_text = f"{prev_info}\n{message_text}".strip()

    # Пытаемся извлечь дату/время всегда, пока не отправлен availability_reply
    if not state.get("availability_reply_sent"):
        date_prompt = f"""
        Сегодня: {client_request_date_str}

        Все сообщения клиента: "{combined_text}"

        Определи, указана ли в сообщениях дата проведения мероприятия.

        Если указан только день и месяц — подставь текущий год: {current_year}.
        Если указан год — используй его.
        Формат: ГГГГ-ММ-ДД. Если даты нет — "нет даты".
        """
        date_reply = ask_openai(date_prompt).strip()
        match_date = date_reply if date_reply.lower() != "нет даты" else None
        logger.info("Дата проведения мероприятия от ИИ %s", match_date)

        time_prompt = f"""
        Все сообщения клиента: "{combined_text}"
        Определи, указано ли в сообщениях время проведения мероприятия.
        Если да — формат ЧЧ:ММ. Иначе — "нет времени".
        """
        time_reply = ask_openai(time_prompt).strip()
        match_time = time_reply if time_reply.lower() != "нет времени" else None
        logger.info("Время проведения мероприятия от ИИ %s", match_time)
    # сохраним нормализованные поля для downstream
    if match_date:
        update_state(user_id, {"event_date_iso": _clean_date(match_date)})
    if match_time:
        update_state(user_id, {"event_time_24": _clean_time(match_time)})

    # пересоберём снепшот
    state = get_state(user_id)
    snap = build_structured_snapshot(state)
    update_state(user_id, {"structured_cache": snap})

    # 3) Мгновенный availability_reply (как в 3c)
    if not state.get("availability_reply_sent"):
        date_iso = _clean_date(match_date) if match_date else None
        time_24  = _clean_time(match_time) if match_time else ""
        if date_iso and time_24:
            schedule = load_schedule_from_s3()
            availability = check_date_availability(date_iso, time_24, schedule)
            logger.info(f"[debug] AVAILABILITY CHECK: {availability} для {date_iso} {time_24}")

            availability_prompt = global_prompt + "\n\n" + render_prompt(
                AVAILABILITY_PROMPT_PATH,
                message_text=message_text,
                date_iso=date_iso,
                time_24=time_24,
                client_request_date=client_request_date_str,
                availability=availability,
            )
            availability_reply = ask_openai(availability_prompt).strip()
            logger.info("availability_reply %s", availability_reply)

            send_reply_func(availability_reply)
            update_state(user_id, {
                "availability_reply_sent": True,
                "summary_and_availability_sent": True,
                "date_decision_flag": availability
            })
             # Если слот свободен — фиксируем его в расписании
            # 🔒 Пытаемся забронировать слот, если он свободен
            if availability == "available":
                try:
                    import utils.schedule as schedule_utils
                    if hasattr(schedule_utils, "reserve_slot"):
                        success = schedule_utils.reserve_slot(date_iso, time_24)
                        logger.info("Результат сохранения слота: %s", success)
                    else:
                        logger.info("reserve_slot отсутствует (вероятно, в тестовой заглушке) — пропускаем бронирование")
                except Exception as e:
                    logger.info("reserve_slot недоступен или упал: %s", e)
            if availability in ("need_handover", "occupied"):
                update_state(user_id, {
                    "handover_reason": "early_date_or_busy",
                    "scenario_stage_at_handover": "block3"
                })
                return route_message("", user_id, force_stage="block5")

    # 4) Доспрашивание недостающих полей (НЕ сбрасываем availability_reply_sent)
    state = get_state(user_id)
    missing_keys = missing_info_keys(state)
    logger.info("Необходимые поля, которые еще не заполнены %s", missing_keys)
    clarification_attempts = int(state.get("clarification_attempts", 0))
    logger.info("clarification_attempts = %s", clarification_attempts)

    if missing_keys and clarification_attempts < 3:
        logger.info(f"Доспрашиваем недостающие поля. Попытка №{clarification_attempts + 1}")
        missing_names = ", ".join(KEY_NAMES.get(k, k) for k in missing_keys)

        prompt = f"""{global_prompt}

{stage_prompt}

Ранее от клиента: {prev_info}

Сегодня: {client_request_date}

Сообщение клиента: "{message_text}"

Важно: НЕ пиши обобщённый ответ, НЕ пиши резюме, НЕ пересказывай всё.
Не нужно благодарить. Просто задай конкретные уточняющие вопросы по: {missing_names}.

Ответ — только список вопросов, без повторений и без summary.
"""
        text_to_client = ask_openai(prompt).strip()
        logger.info("text_to_client %s", text_to_client)

        send_reply_func(text_to_client)
        update_state(user_id, {
            "stage": "block3a",
            "clarification_attempts": clarification_attempts + 1,
            "last_bot_question": text_to_client,
            "summary_sent": False,
            # ВАЖНО: не трогаем availability_reply_sent
        })
        # Не дублируем R1, если он уже стоит (идемпотентность при повторных ответах)
        cur = get_state(user_id) or {}
        if not cur.get("r1_scheduled_b3a"):
            plan(user_id, "blocks.block_03a:send_first_reminder_if_silent", DELAY_TO_BLOCK_3_1_HOURS * 3600)
            update_state(user_id, {"r1_scheduled_b3a": True})
        return

    # 5) Если 3 попытки — решаем, что дальше
    if missing_keys and clarification_attempts >= 3:
        if len(missing_keys) <= 2 and state.get("celebrant_name"):
            logger.info("Мало недостающих данных и есть имя — идём дальше")
            return route_message("", user_id, force_stage="block4")
        else:
            logger.info("Не удалось выяснить нужные данные — передаём Арсению")
            update_state(user_id, {
                "handover_reason": "could_not_collect_info",
                "scenario_stage_at_handover": "block3"
            })
            return route_message("", user_id, force_stage="block5")

    # 6) Фолбек: все данные есть, но availability ещё не отправлен
    state = get_state(user_id)
    if (
        not missing_info_keys(state)
        and not state.get("availability_reply_sent")
        and state.get("event_date")
        and state.get("event_time")
    ):
        logger.info("[fallback] Все данные есть, отправляем availability_reply")
        date_iso = _clean_date(state.get("event_date"))
        time_24  = _clean_time(state.get("event_time"))
        if date_iso and time_24:
            schedule = load_schedule_from_s3()
            availability = check_date_availability(date_iso, time_24, schedule)
            logger.info(f"[fallback] AVAILABILITY CHECK: {availability} для {date_iso} {time_24}")

            availability_prompt = global_prompt + "\n\n" + render_prompt(
                AVAILABILITY_PROMPT_PATH,
                message_text=message_text,
                date_iso=date_iso,
                time_24=time_24,
                client_request_date=client_request_date_str,
                availability=availability,
            )
            availability_reply = ask_openai(availability_prompt).strip()
            logger.info("[fallback] availability_reply %s", availability_reply)

            send_reply_func(availability_reply)
            update_state(user_id, {
                "availability_reply_sent": True,
                "summary_and_availability_sent": True,
                "date_decision_flag": availability
            })
             # 🔒 Пытаемся забронировать слот, если он свободен (fallback)
            if availability == "available":
                try:
                    import utils.schedule as schedule_utils
                    if hasattr(schedule_utils, "reserve_slot"):
                        success = schedule_utils.reserve_slot(date_iso, time_24)
                        logger.info("[fallback] Результат сохранения слота: %s", success)
                    else:
                        logger.info("[fallback] reserve_slot отсутствует (тестовая заглушка) — пропускаем бронирование")
                except Exception as e:
                    logger.info("[fallback] reserve_slot недоступен или упал: %s", e)
            if availability in ("need_handover", "occupied"):
                update_state(user_id, {
                    "handover_reason": "early_date_or_busy",
                    "scenario_stage_at_handover": "block3"
                })
                return route_message("", user_id, force_stage="block5")

    # 7) Переходы по флагу решения даты (в state, не в модульных глобалках)
    state = get_state(user_id)
    flag = state.get("date_decision_flag")
    if not missing_info_keys(state):
        if flag == "available":
            return route_message("", user_id, force_stage="block4")
        elif flag in ("need_handover", "occupied"):
            update_state(user_id, {
                "handover_reason": "early_date_or_busy",
                "scenario_stage_at_handover": "block3"
            })
            return route_message("", user_id, force_stage="block5")

    # 8) Финальные обновления + напоминания
    update_state(user_id, {
        "stage": "block3a",
        "last_message_ts": time.time()
    })
    # не ставим новый R1, если клиент только что ответил, либо R1 уже стоит
    cur = get_state(user_id) or {}
    if cur.get("last_sender") != "user" and not cur.get("r1_scheduled_b3a"):
        plan(user_id, "blocks.block_03a:send_first_reminder_if_silent", DELAY_TO_BLOCK_3_1_HOURS * 3600)
        update_state(user_id, {"r1_scheduled_b3a": True})


def send_first_reminder_if_silent(user_id, send_reply_func):
    state = get_state(user_id)
    if not state or state.get("stage") != "block3a":
        return
    # клиент уже писал после последнего вопроса → не трогаем
    if state.get("last_sender") == "user":
        return
    if state.get("r1_scheduled_b3a"):
        return

    global_prompt   = load_prompt(GLOBAL_PROMPT_PATH)
    reminder_prompt = load_prompt(REMINDER_1_PROMPT_PATH)
    last_q = state.get("last_bot_question", "")
    full_prompt = global_prompt + "\n\n" + reminder_prompt + f'\n\nПоследний вопрос бота: "{last_q}"'

    reply = ask_openai(full_prompt)
    send_reply_func(reply)

    update_state(user_id, {"stage": "block3a", "last_message_ts": time.time()})
    plan(user_id, "blocks.block_03a:send_second_reminder_if_silent", DELAY_TO_BLOCK_3_2_HOURS * 3600)
    update_state(user_id, {"r1_scheduled_b3a": True})


def send_second_reminder_if_silent(user_id, send_reply_func):
    state = get_state(user_id)
    if not state or state.get("stage") != "block3a":
        return
    if state.get("last_sender") == "user":
        return
    if state.get("r2_scheduled_b3a"):
        return

    global_prompt   = load_prompt(GLOBAL_PROMPT_PATH)
    reminder_prompt = load_prompt(REMINDER_2_PROMPT_PATH)
    last_q = state.get("last_bot_question", "")
    full_prompt = global_prompt + "\n\n" + reminder_prompt + f'\n\nПоследний вопрос бота: "{last_q}"'

    reply = ask_openai(full_prompt)
    send_reply_func(reply)

    update_state(user_id, {"stage": "block3a", "last_message_ts": time.time()})
    plan(user_id, "blocks.block_03a:finalize_if_still_silent", FINAL_TIMEOUT_HOURS * 3600)
    update_state(user_id, {"r2_scheduled_b3a": True})
    
    # финальный таймер — ещё 4 ч тишины → block5
def finalize_if_still_silent(user_id, send_reply_func):
    from router import route_message
    state = get_state(user_id)
    if not state or state.get("stage") != "block3a":
        return
    if state.get("fin_scheduled_b3a_done"):
        return
    update_state(user_id, {
        "handover_reason": "no_response_after_3_2",
        "scenario_stage_at_handover": "block3"
    })
    update_state(user_id, {"fin_scheduled_b3a_done": True})
    route_message("", user_id, force_stage="block5")
