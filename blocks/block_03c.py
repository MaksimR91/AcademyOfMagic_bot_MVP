# block_03c.py
import time
import re
from utils.ask_openai import ask_openai
from utils.wants_handover_ai import wants_handover_ai
from utils.schedule import load_schedule_from_s3, check_date_availability
from state.state import get_state, update_state
from utils.reminder_engine import plan
from logger import logger
from utils.structured import build_structured_snapshot

# Пути к промптам
GLOBAL_PROMPT_PATH = "prompts/global_prompt.txt"
STAGE_PROMPT_PATH = "prompts/block03c_prompt.txt"
STRUCTURE_PROMPT_PATH = "prompts/block03c_data_prompt.txt"
REMINDER_1_PROMPT_PATH = "prompts/block03_reminder_1_prompt.txt"
REMINDER_2_PROMPT_PATH = "prompts/block03_reminder_2_prompt.txt"

# Тайминги
DELAY_TO_BLOCK_3_1_HOURS = 4
DELAY_TO_BLOCK_3_2_HOURS = 12
FINAL_TIMEOUT_HOURS     = 4

DATE_DECISION_FLAGS = {}

KEY_NAMES = {
    "celebrant_name": "имя ключевого участника (например, именинника)",
    "celebrant_age": "возраст ключевого участника",
    "celebrant_gender": "пол ключевого участника",
    "event_date": "дата мероприятия",
    "event_time": "время мероприятия",
    "event_location": "название места проведения (например, кафе, ресторан, дом)",
    "guests_count": "количество гостей",
    "children_adult_ratio": "соотношение детей и взрослых",
    "guests_children_gender": "пол гостей-детей",
    "guests_children_age": "возраст гостей-детей",
    "no_celebrant": "нет ключевого участника"
}
SAFE_KEYS = {
    "event_date","event_time","event_location",
    "celebrant_name","celebrant_gender","celebrant_age",
    "guests_count","children_adult_ratio",
    "guests_children_age","guests_children_gender",
    "no_celebrant"
}
IGNORED_VALUES = {"", "не указано", "не указан", "неизвестно", "прочерк", "-", "n/a"}

def to_bool(v):
    s = str(v).strip().lower()
    return s in {"true","yes","да","y","1"}

def upsert_state(user_id, parsed_data: dict):
    state = get_state(user_id) or {}
    out = {}

    for k, v in parsed_data.items():
        if k not in SAFE_KEYS:
            continue

        if k == "no_celebrant":
            nb = to_bool(v)
            # конфликт: уже знаем данные именинника -> принудительно False
            if any(state.get(x) for x in ("celebrant_name","celebrant_gender","celebrant_age")):
                out["no_celebrant"] = False
            else:
                out["no_celebrant"] = nb
            continue

        sv = (str(v).strip() if v is not None else "")
        if sv.lower() in IGNORED_VALUES:
            continue
        if state.get(k):   # уже есть непустое -> не затираем
            continue
        out[k] = sv

    if out:
        update_state(user_id, out)
    return get_state(user_id)

def load_prompt(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def is_true(v):
    return str(v).strip().lower() in {"true","yes","да","y","1"}

def missing_info_keys(state):
    required = [
        'event_date','event_time','event_location',
        'guests_count','children_adult_ratio',
        'guests_children_gender','guests_children_age'
    ]
    if not is_true(state.get("no_celebrant")):
        required += ['celebrant_name','celebrant_gender','celebrant_age']
    return [k for k in required if not state.get(k)]

def handle_block3c(message_text, user_id, send_reply_func, client_request_date=None):
    from router import route_message
    if client_request_date is None:
        client_request_date = time.time()

    if wants_handover_ai(message_text):
        update_state(user_id, {"handover_reason": "asked_handover"})
        return route_message(message_text, user_id, force_stage="block9")

    state = get_state(user_id) or {}
    updated_description = (state.get("event_description", "") + "\n" + message_text).strip()
    update_state(user_id, {"event_description": updated_description})

    prev_info = state.get("event_description", "")

    # Загружаем промпты
    global_prompt = load_prompt(GLOBAL_PROMPT_PATH)
    stage_prompt = load_prompt(STAGE_PROMPT_PATH)
    struct_prompt = load_prompt(STRUCTURE_PROMPT_PATH)

    # 1. Получаем структурированные данные
    struct_input = (
        struct_prompt
        + f"\n\nСообщение клиента: \"{message_text}\""
    )
    import json

    structured_reply = ask_openai(struct_input).strip()
    logger.info("Ответ от OpenAI ДО парсинга:\n%s", structured_reply)

    try:
        parsed_data = json.loads(structured_reply)
    except json.JSONDecodeError:
        logger.warning("Ошибка при разборе JSON-ответа от OpenAI")
        parsed_data = {}

    state = upsert_state(user_id, parsed_data)  # <-- только это
    logger.info("Структурированные данные после апсёрта %s", {k: state.get(k) for k in SAFE_KEYS})

    # раньше ты тут чистил и сразу update_state(...)
    state = upsert_state(user_id, parsed_data)
    parsed_data = {
        key: str(value).strip()
        for key, value in parsed_data.items()
        if value is not None and str(value).strip().lower() not in IGNORED_VALUES
    }
    logger.info("Структурированные данные после очистки %s", parsed_data)
    snap = build_structured_snapshot(state)
    update_state(user_id, {"structured_cache": snap})
    state = get_state(user_id)
    if state.get("no_celebrant") and any(state.get(k) for k in ("celebrant_name", "celebrant_age", "celebrant_gender")):
        logger.info("Удаляем флаг no_celebrant, т.к. появились данные об имениннике")
        update_state(user_id, {"no_celebrant": False})
        state = get_state(user_id)  # обновим
    from datetime import datetime
    now = datetime.now()
    client_request_date_str = now.strftime("%d %B %Y")  # напр. "06 августа 2025"
    current_year = now.year
    match_date = None
    match_time = None
    has_date = state.get("event_date")
    has_time = state.get("event_time")
    combined_text = f"{prev_info}\n{message_text}".strip()
    if has_date and has_time and not state.get("availability_reply_sent"):
        # Уточняем дату и время через OpenAI
        date_prompt = f"""
        Сегодня: {client_request_date_str}

        Все сообщения клиента: "{combined_text}"

        Определи, указана ли в сообщениях дата проведения мероприятия.

        Если в сообщении указан только день и месяц — подставь текущий год: {current_year}.
        Если в сообщении указан полный год — используй его.
        Формат: ГГГГ-ММ-ДД. Если даты нет — напиши "нет даты".
        """
        date_reply = ask_openai(date_prompt).strip()
        match_date = date_reply if date_reply.lower() != "нет даты" else None
        logger.info("Дата проведения мероприятия от ИИ %s", match_date)

        time_prompt = f"""
        Все сообщения клиента: "{combined_text}"
        Определи, указано ли в сообщениях время проведения мероприятия. 
        Если да, напиши его в формате ЧЧ:ММ. Иначе — "нет времени".
        """
        time_reply = ask_openai(time_prompt).strip()
        match_time = time_reply if time_reply.lower() != "нет времени" else None
        logger.info("Время проведения мероприятия от ИИ %s", match_time)
    def clean_time(raw_time: str) -> str:
        match = re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", raw_time)
        return match.group(0) if match else ""
    def clean_date(raw_date: str) -> str:
        match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", raw_date)
        return match.group(0) if match else ""
    if match_date:
        update_state(user_id, {"event_date_iso": clean_date(match_date)})  # 'YYYY-MM-DD'
    if match_time:
        update_state(user_id, {"event_time_24": clean_time(match_time)})   # 'HH:MM'
    # и не забудь пересобрать снепшот после этих апдейтов:
    state = get_state(user_id)
    snap  = build_structured_snapshot(state)
    update_state(user_id, {"structured_cache": snap})

# --- Новый блок: отправка availability_reply сразу, как только есть дата и время ---
    if not state.get("availability_reply_sent"):
        if match_date:
            date = clean_date(match_date)
        else:
            date = None
        time_ = clean_time(match_time) if match_time else ""
        if date and time_:
            schedule = load_schedule_from_s3()
            availability = check_date_availability(date, time_, schedule)
            logger.info(f"[debug] AVAILABILITY CHECK: {availability} для {date} {time_}")
            availability_prompt = (
                global_prompt
                + f"""
                Клиент ранее написал: "{message_text}"
                Дата мероприятия: {date}
                Время мероприятия: {time_}
                Сегодня: {client_request_date}
                СТАТУС: {availability}

                Напиши клиенту:
                - если СТАТУС:available — "дата и время свободны – Арсений сможет выступить".
                - если need_handover/occupied — "Арсений свяжется позже по дате и времени".
                """
            )
            availability_reply = ask_openai(availability_prompt).strip()
            logger.info("availability_reply %s", availability_reply)

            send_reply_func(availability_reply)
            update_state(user_id, {
                "availability_reply_sent": True,
                "summary_and_availability_sent": True,
                "date_decision_flag": availability
            })

            from utils.schedule import reserve_slot
            if availability == "available":
                success = reserve_slot(date, time_)
                logger.info("Результат сохранения слота: %s", success)
                DATE_DECISION_FLAGS[user_id] = "available"
            elif availability in ("need_handover", "occupied"):
                DATE_DECISION_FLAGS[user_id] = "handover"
                update_state(user_id, {
                    "handover_reason": "early_date_or_busy",
                    "scenario_stage_at_handover": "block3"
                })
                return route_message("", user_id, force_stage="block9")
        
    # 2. Проверяем недостающие поля
    state = get_state(user_id)
    missing_keys = missing_info_keys(state)
    logger.info("Необходимые поля, которые еще не заполнены %s", missing_keys)
    clarification_attempts = int(state.get("clarification_attempts", 0))
    logger.info("clarification_attempts = %s", clarification_attempts)

    # Сначала проверяем, нужно ли ещё доспрашивать
    if missing_keys and clarification_attempts < 3:
        logger.info(f"Доспрашиваем недостающие поля. Попытка №{clarification_attempts + 1}")
        missing_names = ", ".join([KEY_NAMES.get(k, k) for k in missing_keys])
        prompt = f"""{global_prompt}

{stage_prompt}

Ранее от клиента: {prev_info}

Сегодня: {client_request_date}

Сообщение клиента: "{message_text}"

Важно: НЕ пиши обобщённый ответ, НЕ пиши резюме, НЕ пересказывай всю информацию. 
Не нужно вежливо благодарить клиента, повторять всё, что он сказал. 

Просто задай конкретные уточняющие вопросы по следующим недостающим пунктам: {missing_names}.

Ответ должен быть только в виде списка вопросов. Никаких повторений, никакого summary.
"""
        text_to_client = ask_openai(prompt).strip()
        logger.info("text_to_client %s", text_to_client)

        send_reply_func(text_to_client)
        update_state(user_id, {
            "stage": "block3c",  # <--- ВАЖНО: фиксация stage
            "clarification_attempts": clarification_attempts + 1,
            "last_bot_question": text_to_client,
            "summary_sent": False
        })
        return

    # Если уже было 2 попытки — решаем, что делать дальше
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
            return route_message("", user_id, force_stage="block9")
        
    # 4. Определяем дату и время, если возможно
    
    if user_id not in DATE_DECISION_FLAGS or not DATE_DECISION_FLAGS[user_id]:
        combined_text = f"{prev_info}\n{message_text}".strip()
        has_date = state.get("event_date")
        has_time = state.get("event_time")
        if has_date and has_time and not state.get("availability_reply_sent"):
            # Уточняем дату и время через OpenAI
            date_prompt = f"""
            Сегодня: {client_request_date_str}

            Все сообщения клиента: "{combined_text}"

            Определи, указана ли в сообщениях дата проведения мероприятия.

            Если в сообщении указан только день и месяц — подставь текущий год: {current_year}.
            Если в сообщении указан полный год — используй его.
            Формат: ГГГГ-ММ-ДД. Если даты нет — напиши "нет даты".
            """
            date_reply = ask_openai(date_prompt).strip()
            match_date = date_reply if date_reply.lower() != "нет даты" else None
            logger.info("Дата проведения мероприятия от ИИ %s", match_date)

            time_prompt = f"""
            Все сообщения клиента: "{combined_text}"
            Определи, указано ли в сообщениях время проведения мероприятия. 
            Если да, напиши его в формате ЧЧ:ММ. Иначе — "нет времени".
            """
            time_reply = ask_openai(time_prompt).strip()
            match_time = time_reply if time_reply.lower() != "нет времени" else None
            logger.info("Время проведения мероприятия от ИИ %s", match_time)

            def clean_time(raw_time: str) -> str:
                match = re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", raw_time)
                return match.group(0) if match else ""

            match_time = clean_time(match_time)

            # Определяем тип места
            pl_text = message_text.lower()
            place_type = None
            if any(w in pl_text for w in ("дом", "квартира", "house", "home")):
                place_type = "home"
            elif "сад" in pl_text:
                place_type = "garden"
            elif any(w in pl_text for w in ("кафе", "ресторан", "cafe", "restaurant")):
                place_type = "cafe"

            # Сохраняем новые данные, если нашли
            extracted = {}
            if place_type:
                extracted["event_location_type"] = place_type
            if match_date:
                extracted["event_date"] = match_date
            if match_time:
                extracted["event_time"] = match_time
            m_guests = re.search(r"\b(\d{1,3})\s+(?:гостей|человек)\b", pl_text)
            if m_guests:
                extracted["guests_count"] = m_guests.group(1)

            if extracted:
                update_state(user_id, extracted)

            # Отправляем availability_reply, если ещё не отправляли
            state = get_state(user_id)
            if not state.get("availability_reply_sent") and match_date and match_time and match_date != "" and match_time != "":
                schedule = load_schedule_from_s3()
                availability = check_date_availability(match_date, match_time, schedule)
                logger.info(f"[debug] Проверка доступности: {availability} для {match_date} {match_time}")
                availability_prompt = (
                    global_prompt
                    + f"""
                    Клиент ранее написал: "{message_text}"
                    Дата мероприятия: {match_date}
                    Время мероприятия: {match_time}
                    Сегодня: {client_request_date}
                    СТАТУС: {availability}

                    Напиши клиенту:
                    - если СТАТУС:available — "дата и время свободны – Арсений сможет выступить".
                    - если need_handover/occupied — "Арсений свяжется позже по дате и времени".
                    """
                )
                availability_reply = ask_openai(availability_prompt).strip()
                logger.info("availability_reply %s", availability_reply)

                send_reply_func(availability_reply)
                update_state(user_id, {
                    "availability_reply_sent": True,
                    "summary_and_availability_sent": True,
                    "date_decision_flag": availability
                })

                # Сохраняем слот в расписание
                from utils.schedule import reserve_slot
                if availability == "available":
                    success = reserve_slot(match_date, match_time)
                    logger.info("Результат сохранения слота: %s", success)
                # Запоминаем, что делать дальше
                if availability == "available":
                    DATE_DECISION_FLAGS[user_id] = "available"
                elif availability in ("need_handover", "occupied"):
                    DATE_DECISION_FLAGS[user_id] = "handover"
                    update_state(user_id, {
                        "handover_reason": "early_date_or_busy",
                        "scenario_stage_at_handover": "block3"
                    })
                    return route_message("", user_id, force_stage="block9")

    # --- 🔁 ДОБАВЬ ЭТО: fallback на случай, если все данные уже есть, но availability_reply ещё не отправлен ---
    state = get_state(user_id)
    if (
        not missing_info_keys(state)
        and not state.get("availability_reply_sent")
        and state.get("event_date")
        and state.get("event_time")
    ):
        logger.info("[fallback] Все данные есть, отправляем availability_reply")

        # Чистим дату/время
        date = clean_date(state["event_date"])
        time_ = clean_time(state["event_time"])

        if date and time_:
            schedule = load_schedule_from_s3()
            availability = check_date_availability(date, time_, schedule)
            logger.info(f"[fallback] AVAILABILITY CHECK: {availability} для {date} {time_}")

            availability_prompt = (
                global_prompt
                + f"""
                Клиент ранее написал: "{message_text}"
                Дата мероприятия: {date}
                Время мероприятия: {time_}
                Сегодня: {client_request_date}
                СТАТУС: {availability}

                Напиши клиенту:
                - если СТАТУС:available — "дата и время свободны – Арсений сможет выступить".
                - если need_handover/occupied — "Арсений свяжется позже по дате и времени".
                """
            )
            availability_reply = ask_openai(availability_prompt).strip()
            logger.info("[fallback] availability_reply %s", availability_reply)

            send_reply_func(availability_reply)
            update_state(user_id, {
                "availability_reply_sent": True,
                "summary_and_availability_sent": True,
                "date_decision_flag": availability
            })

            from utils.schedule import reserve_slot
            if availability == "available":
                success = reserve_slot(date, time_)
                logger.info("[fallback] Результат сохранения слота: %s", success)
                DATE_DECISION_FLAGS[user_id] = "available"
            elif availability in ("need_handover", "occupied"):
                DATE_DECISION_FLAGS[user_id] = "handover"
                update_state(user_id, {
                    "handover_reason": "early_date_or_busy",
                    "scenario_stage_at_handover": "block3"
                })
                return route_message("", user_id, force_stage="block9")

    # Переходы
    state = get_state(user_id)
    if not missing_info_keys(state):
        flag = state.get("date_decision_flag")
        if flag == "available":
            return route_message("", user_id, force_stage="block4")
        elif flag == "handover":
            update_state(user_id, {
                "handover_reason": "early_date_or_busy",
                "scenario_stage_at_handover": "block3"
            })
            return route_message("", user_id, force_stage="block9")

    # Финальные обновления
    update_state(user_id, {
        "stage": "block3c",
        "last_message_ts": time.time()
    })
    plan(user_id, "blocks.block_03c:send_first_reminder_if_silent", DELAY_TO_BLOCK_3_1_HOURS * 3600)

def send_first_reminder_if_silent(user_id, send_reply_func):
    state = get_state(user_id)
    if not state or state.get("stage") != "block3c":
        return  # Клиент уже ответил

    global_prompt   = load_prompt(GLOBAL_PROMPT_PATH)
    reminder_prompt = load_prompt(REMINDER_1_PROMPT_PATH)
    last_q = state.get("last_bot_question", "")
    full_prompt = global_prompt + "\n\n" + reminder_prompt + f'\n\nПоследний вопрос бота: "{last_q}"'

    reply = ask_openai(full_prompt)
    send_reply_func(reply)

    update_state(user_id, {"stage": "block3c", "last_message_ts": time.time()})

    # ставим таймер на второе напоминание
    plan(user_id,
    "blocks.block_03c:send_second_reminder_if_silent",   # <‑‑ путь к функции
    DELAY_TO_BLOCK_3_2_HOURS * 3600)


def send_second_reminder_if_silent(user_id, send_reply_func):
    state = get_state(user_id)
    if not state or state.get("stage") != "block3c":
        return  # Клиент ответил

    global_prompt   = load_prompt(GLOBAL_PROMPT_PATH)
    reminder_prompt = load_prompt(REMINDER_2_PROMPT_PATH)
    last_q = state.get("last_bot_question", "")
    full_prompt = global_prompt + "\n\n" + reminder_prompt + f'\n\nПоследний вопрос бота: "{last_q}"'

    reply = ask_openai(full_prompt)
    send_reply_func(reply)

    update_state(user_id, {"stage": "block3c", "last_message_ts": time.time()})

    # финальный таймер — ещё 4 ч тишины → block9
    def finalize_if_still_silent():
        from router import route_message
        state = get_state(user_id)
        if not state or state.get("stage") != "block3c":
            return
        update_state(user_id, {"handover_reason": "no_response_after_3_2", "scenario_stage_at_handover": "block3"})
        route_message("", user_id, force_stage="block9")

    plan(user_id,
    "blocks.block_03c:finalize_if_still_silent",   # <‑‑ путь к функции
    FINAL_TIMEOUT_HOURS * 3600)
