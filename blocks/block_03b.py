# block_03b.py
import time
import re
from utils.ask_openai import ask_openai
from utils.wants_handover_ai import wants_handover_ai
from utils.schedule import load_schedule_from_s3, check_date_availability
from state.state import get_state, update_state
from utils.reminder_engine import plan
from logger import logger

# Пути к промптам
GLOBAL_PROMPT_PATH = "prompts/global_prompt.txt"
STAGE_PROMPT_PATH = "prompts/block03b_prompt.txt"
STRUCTURE_PROMPT_PATH = "prompts/block03b_data_prompt.txt"
REMINDER_1_PROMPT_PATH = "prompts/block03_reminder_1_prompt.txt"
REMINDER_2_PROMPT_PATH = "prompts/block03_reminder_2_prompt.txt"
AVAILABILITY_PROMPT_PATH = "prompts/block03_availability_prompt.txt"

# Тайминги
DELAY_TO_BLOCK_3_1_HOURS = 4
DELAY_TO_BLOCK_3_2_HOURS = 12
FINAL_TIMEOUT_HOURS     = 4
DATE_DECISION_FLAGS = {}

KEY_NAMES = {
    "event_format": "формат мероприятия",
    "celebrant_name": "имя ключевого участника (например, жениха или юбиляра)",
    "celebrant_age": "возраст ключевого участника",
    "celebrant_gender": "пол ключевого участника",
    "event_date": "дата мероприятия",
    "event_time": "время мероприятия",
    "event_location": "название места проведения (например, кафе, ресторан, дом)",
    "guests_count": "количество гостей",
    "guests_gender": "пол гостей",
    "guests_age": "возраст гостей",
    "compere_availability": "наличие ведущего"
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
        'event_format',
        'event_date',
        'event_time',
        'event_location',
        'celebrant_name',
        'celebrant_gender',
        'celebrant_age',
        'guests_count',
        'guests_gender',
        'guests_age',
        'compere_availability'
    ]
    missing = [key for key in required if not state.get(key)]
    return missing

def parse_structured_pairs(text):
    result = {}
    patterns = {
        "event_format": r"Формат мероприятия\s*[-—:]\s*(.+)",
        "celebrant_name": r"Имя ключевого участника.*?[-—:]\s*(.+)",
        "celebrant_gender": r"Пол ключевого участника.*?[-—:]\s*(.+)",
        "celebrant_age": r"Возраст ключевого участника.*?[-—:]\s*(.+)",
        "event_date": r"Дата мероприятия\s*[-—:]\s*(.+)",
        "event_time": r"Время мероприятия\s*[-—:]\s*(.+)",
        "event_location_type": r"Тип места проведения\s*[-—:]\s*(.+)",
        "event_location": r"Название места проведения\s*[-—:]\s*(.+)",
        "guests_count": r"Количество гостей\s*[-—:]\s*(.+)",
        "guests_gender": r"Пол гостей\s*[-—:]\s*(.+)",
        "guests_age": r"Возраст гостей\s*[-—:]\s*(.+)",
        "compere_availability": r"(?:наличие ведущего|ведущий)\s*[-—:]\s*(.+)"
    }
    IGNORED_VALUES = {"не указано", "не указан", "неизвестно", "прочерк", "-", "n/a"}
    for key, pattern in patterns.items():
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if val and val.lower() not in IGNORED_VALUES:
                result[key] = val
    return result

def _normalize_compere(val: str) -> str:
    s = val.lower().strip()
    # отрицательные в первую очередь
    neg = {"не будет", "нет", "без ведущего", "отсутствует"}
    pos = {"будет", "да", "есть", "запланирован", "запланирована"}
    if any(x in s for x in neg):
        return "не будет"
    if any(x in s for x in pos):
        return "будет"
    return s  # оставим как есть, если что-то экзотическое

def handle_block3b(message_text, user_id, send_reply_func, client_request_date=None):
    if client_request_date is None:
        client_request_date = time.time()

    if wants_handover_ai(message_text):
        update_state(user_id, {
            "handover_reason": "asked_handover",
            "scenario_stage_at_handover": "block3"
        })
        from router import route_message
        return route_message(message_text, user_id, force_stage="block5")

    state = get_state(user_id) or {}
    # Любой входящий текст от клиента «гасит» дальнейшие автокасания до явного решения
    update_state(user_id, {"last_sender": "user"})
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
        + f"\n\nПредыдущее описание: {prev_info}"
    )
    structured_reply = ask_openai(struct_input).strip()
    parsed_data = parse_structured_pairs(structured_reply)
    # если из структурного ответа пришло поле — нормализуем
    if "compere_availability" in parsed_data:
        parsed_data["compere_availability"] = _normalize_compere(parsed_data["compere_availability"])
    else:
        # fallback по сообщению клиента с приоритетом отрицаний
        msg = message_text.lower()
        if re.search(r"(без\s+ведущ\w+)|(ведущ\w+.*не\s+будет)|(не\s+будет\s+ведущ\w+)", msg):
            parsed_data["compere_availability"] = "не будет"
        elif re.search(r"(с\s+ведущ\w+)|(ведущ\w+.*будет)|(будет\s+ведущ\w+)", msg):
            parsed_data["compere_availability"] = "будет"
    logger.info("Структурированные данные %s", parsed_data)

    if parsed_data:
        update_state(user_id, parsed_data)
        state = get_state(user_id)
    from datetime import datetime

    now = datetime.now()
    client_request_date_str = now.strftime("%d %B %Y")  # напр. "06 августа 2025"
    current_year = now.year
    match_date = None
    match_time = None
    if user_id not in DATE_DECISION_FLAGS or not DATE_DECISION_FLAGS[user_id]:
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
    def clean_time(raw_time: str) -> str:
        match = re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", raw_time)
        return match.group(0) if match else ""
    def clean_date(raw_date: str) -> str:
        match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", raw_date)
        return match.group(0) if match else ""

# --- Новый блок: отправка availability_reply сразу, как только есть дата и время ---
    if not state.get("availability_reply_sent"):
        if match_date:
            date_iso = clean_date(match_date)
        else:
            date_iso = None
        time_24 = clean_time(match_time) if match_time else ""
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
                "summary_and_availability_sent": True
            })

             # 🔒 безопасная бронь слота, без жёсткого импорта
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
            # Запоминаем, что делать дальше
            if availability == "available":
                DATE_DECISION_FLAGS[user_id] = "available"
            elif availability in ("need_handover", "occupied"):
                DATE_DECISION_FLAGS[user_id] = "handover"
                update_state(user_id, {
                    "handover_reason": "early_date_or_busy",
                    "scenario_stage_at_handover": "block3"
                })
                from router import route_message
                return route_message("", user_id, force_stage="block5")
        
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
            "stage": "block3b",  # <--- ВАЖНО: фиксация stage
            "clarification_attempts": clarification_attempts + 1,
            "last_bot_question": text_to_client,
            "summary_sent": False,
            "availability_reply_sent": False,
        })
        # Не дублируем R1, если он уже стоит (идемпотентность при повторных ответах)
        cur = get_state(user_id) or {}
        if not cur.get("r1_scheduled_b3b"):
            plan(user_id, "blocks.block_03b:send_first_reminder_if_silent", DELAY_TO_BLOCK_3_1_HOURS * 3600)
            update_state(user_id, {"r1_scheduled_b3b": True})
        return

    # Если уже было 2 попытки — решаем, что делать дальше
    if missing_keys and clarification_attempts >= 3:
        if len(missing_keys) <= 2 and state.get("celebrant_name"):
            logger.info("Мало недостающих данных и есть имя — идём дальше")
            from router import route_message
            return route_message("", user_id, force_stage="block4")
        else:
            logger.info("Не удалось выяснить нужные данные — передаём Арсению")
            update_state(user_id, {
                "handover_reason": "could_not_collect_info",
                "scenario_stage_at_handover": "block3"
            })
            from router import route_message
            return route_message("", user_id, force_stage="block5")
        
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
                    "summary_and_availability_sent": True
                })

                # Сохраняем слот в расписание (безопасно)
                if availability == "available":
                    try:
                        import utils.schedule as schedule_utils
                        if hasattr(schedule_utils, "reserve_slot"):
                            success = schedule_utils.reserve_slot(match_date, match_time)
                            logger.info("Результат сохранения слота: %s", success)
                        else:
                            logger.info("reserve_slot отсутствует (тестовая заглушка) — пропускаем бронирование")
                    except Exception as e:
                        logger.info("reserve_slot недоступен или упал: %s", e)
                # Запоминаем, что делать дальше
                if availability == "available":
                    DATE_DECISION_FLAGS[user_id] = "available"
                elif availability in ("need_handover", "occupied"):
                    DATE_DECISION_FLAGS[user_id] = "handover"
                    update_state(user_id, {
                        "handover_reason": "early_date_or_busy",
                        "scenario_stage_at_handover": "block3"
                    })
                    from router import route_message
                    return route_message("", user_id, force_stage="block5")

    # Переходы
    state = get_state(user_id)
    if not missing_info_keys(state):
        if DATE_DECISION_FLAGS.get(user_id) == "available":
            from router import route_message
            return route_message("", user_id, force_stage="block4")
        elif DATE_DECISION_FLAGS.get(user_id) == "handover":
            update_state(user_id, {
                "handover_reason": "early_date_or_busy",
                "scenario_stage_at_handover": "block3"
            })
            from router import route_message
            return route_message("", user_id, force_stage="block5")

    # Финальные обновления
    update_state(user_id, {
        "stage": "block3a",
        "last_message_ts": time.time()
    })
    # не ставим новый R1, если клиент только что ответил, либо R1 уже стоит
    cur = get_state(user_id) or {}
    if cur.get("last_sender") != "user" and not cur.get("r1_scheduled_b3b"):
        plan(user_id, "blocks.block_03b:send_first_reminder_if_silent", DELAY_TO_BLOCK_3_1_HOURS * 3600)
        update_state(user_id, {"r1_scheduled_b3b": True})

def send_first_reminder_if_silent(user_id, send_reply_func):
    state = get_state(user_id)
    if not state or state.get("stage") != "block3b":
        return
    # клиент уже писал после последнего вопроса → не трогаем
    if state.get("last_sender") == "user":
        return
    if state.get("r1_scheduled_b3b"):
        return

    global_prompt   = load_prompt(GLOBAL_PROMPT_PATH)
    reminder_prompt = load_prompt(REMINDER_1_PROMPT_PATH)
    last_q = state.get("last_bot_question", "")
    full_prompt = global_prompt + "\n\n" + reminder_prompt + f'\n\nПоследний вопрос бота: "{last_q}"'

    reply = ask_openai(full_prompt)
    send_reply_func(reply)

    update_state(user_id, {"stage": "block3b", "last_message_ts": time.time()})

    # ставим таймер на второе напоминание
    plan(user_id, "blocks.block_03b:send_second_reminder_if_silent", DELAY_TO_BLOCK_3_2_HOURS * 3600)
    update_state(user_id, {"r1_scheduled_b3b": True})
    


def send_second_reminder_if_silent(user_id, send_reply_func):
    state = get_state(user_id)
    if not state or state.get("stage") != "block3b":
        return
    if state.get("last_sender") == "user":
        return
    if state.get("r2_scheduled_b3b"):
        return

    global_prompt   = load_prompt(GLOBAL_PROMPT_PATH)
    reminder_prompt = load_prompt(REMINDER_2_PROMPT_PATH)
    last_q = state.get("last_bot_question", "")
    full_prompt = global_prompt + "\n\n" + reminder_prompt + f'\n\nПоследний вопрос бота: "{last_q}"'

    reply = ask_openai(full_prompt)
    send_reply_func(reply)

    update_state(user_id, {"stage": "block3b", "last_message_ts": time.time()})
    plan(user_id, "blocks.block_03b:finalize_if_still_silent", FINAL_TIMEOUT_HOURS * 3600)
    update_state(user_id, {"r2_scheduled_b3b": True})

    # финальный таймер — ещё 4 ч тишины → block5
def finalize_if_still_silent(user_id, send_reply_func):
    state = get_state(user_id)
    if not state or state.get("stage") != "block3b":
        return
    if state.get("fin_scheduled_b3b_done"):
        return
    update_state(user_id, {"handover_reason": "no_response_after_3_2", "scenario_stage_at_handover": "block3"})
    update_state(user_id, {"fin_scheduled_b3b_done": True})
    from router import route_message
    route_message("", user_id, force_stage="block5")
