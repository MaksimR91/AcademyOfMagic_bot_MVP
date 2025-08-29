import time
from utils.ask_openai import ask_openai
from utils.wants_handover_ai import wants_handover_ai
from state.state import get_state, update_state
from utils.reminder_engine import plan
from logger import logger

# Пути к промптам
GLOBAL_PROMPT_PATH = "prompts/global_prompt.txt"
STAGE_PROMPT_PATH = "prompts/block02_prompt.txt"
REMINDER_PROMPT_PATH = "prompts/block02_reminder_1_prompt.txt"
REMINDER_2_PROMPT_PATH = "prompts/block02_reminder_2_prompt.txt"
# Время до повторного касания (4 часа)
DELAY_TO_BLOCK_2_1_HOURS = 4
DELAY_TO_BLOCK_2_2_HOURS = 12
FINAL_TIMEOUT_HOURS = 4

def is_message_informative(text: str) -> bool:
    text = text.lower()
    keywords = [
        "день", "др", "свадьб", "праздн", "мероприят", "вечерин",
        "дет", "взросл", "гост", "челове", "шоу", "детсад", "корпоратив", "трц"
    ]
    has_keywords = any(kw in text for kw in keywords)
    long_enough = len(text.strip().split()) >= 5
    return has_keywords and long_enough

def load_prompt(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

global_prompt = load_prompt(GLOBAL_PROMPT_PATH)
stage_prompt = load_prompt(STAGE_PROMPT_PATH)

def proceed_to_block(stage_name, user_id):
    from router import route_message
    route_message("", user_id, force_stage=stage_name)

def handle_block2(message_text, user_id, send_reply_func):

    state = get_state(user_id)
    # если уже отправляли стартовое сообщение — не дублируем
    if state.get("stage") == "block2" and state.get("block2_intro_sent"):
        return

    # Отправляем стартовое сообщение (только один раз)
    try:
        reply_to_client = ask_openai(global_prompt + "\n\n" + stage_prompt)
    except Exception as e:
        logger.info(f"[error] ❌ Ошибка при ответе клиенту: {e}")
    send_reply_func(reply_to_client)

    update_state(user_id, {
        "stage": "block2",
        "block2_intro_sent": True,
        "last_sender": "bot",
        "last_message_ts": time.time()
    })

    plan(user_id,
         "blocks.block_02:send_first_reminder_if_silent",
         DELAY_TO_BLOCK_2_1_HOURS * 3600)
    return


def handle_block2_user_reply(message_text, user_id, send_reply_func):
    logger.info(f"[debug] 👤 handle_block2_user_reply: user={user_id}, text={message_text}")
    state = get_state(user_id)
    informative = is_message_informative(message_text)

    if not informative:
        count = state.get("uninformative_replies", 0) + 1

        if count > 2:
            update_state(user_id, {
                "handover_reason": "uninformative_x3",
                "scenario_stage_at_handover": "block2"
            })
            from router import route_message
            return route_message("", user_id, force_stage="block5")

        clarification_prompt = global_prompt + "\n\n" + stage_prompt + "\n\n" + \
            "Предоставленной вами информации недостаточно, чтобы понять формат мероприятия. " \
            "Пожалуйста, расскажите подробнее — чей праздник, сколько будет гостей, взрослые это или дети?"

        try:
            clarification_reply = ask_openai(clarification_prompt)
        except Exception as e:
            logger.info(f"[error] ❌ Ошибка при напоминании: {e}")
        send_reply_func(clarification_reply)

        update_state(user_id, {
            "uninformative_replies": count,
            "last_sender": "bot",
            "last_message_ts": time.time()
        })

        plan(user_id,
             "blocks.block_02:send_first_reminder_if_silent",
             DELAY_TO_BLOCK_2_1_HOURS * 3600)
        return

    # Классификация
    classification_prompt = f"""

Клиент описал мероприятие: "{message_text}"

Задача: Вывести РОВНО ОДНО слово-метку из списка:
детское | семейное | взрослое | нестандартное | неизвестно

Приоритет и правила:
1) День рождения:
   - возраст 1–3 → семейное
   - возраст 4–14 → детское
   - возраст 15+ → взрослое
2) Детский сад / садик / выпускной в саду → детское
3) Праздник во дворе:
   - большинство детей → детское
   - примерно поровну детей и взрослых → семейное
4) Свадьба / жених / невеста → взрослое
5) ВСЁ ОСТАЛЬНОЕ → нестандартное
   В частности, фразы вида «иллюзионное шоу», «на сцене/в кафе/в клубе/в ресторане»,
   «для взрослых» БЕЗ явного признака из пунктов 1–4 → строго «нестандартное».
6) Если информации недостаточно (только «здравствуйте», «хочу шоу» и т.п.) → неизвестно

Важно:
- Игнорируй само по себе выражение «для взрослых», если нет явного признака из п.1 (день рождения 15+) или п.4 (свадьба).
- НИКАКИХ пояснений, вопросов, примеров, знаков препинания — выведи только одно слово-метку.
"""
    logger.info(f"[debug] 🤖 классификация: message_text={message_text}")
    try:
        show_type = ask_openai(classification_prompt).strip().lower()
    except Exception as e:
        logger.info(f"[error] ❌ Ошибка при классификации: {e}")
    show_type = show_type.replace(".", "").strip()
    logger.info(f"[debug] 🧠 определён тип шоу: {show_type}")
    
    # Обработка "неизвестно"
    if show_type == "неизвестно":
        count = state.get("uninformative_replies", 0) + 1

        if count > 2:
            update_state(user_id, {
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
        send_reply_func(clarification_reply)

        update_state(user_id, {
            "uninformative_replies": count,
            "last_sender": "bot",
            "last_message_ts": time.time()
        })

        plan(user_id,
             "blocks.block_02:send_first_reminder_if_silent",
             DELAY_TO_BLOCK_2_1_HOURS * 3600)
        return

    # Всё ок — переходим в нужный блок
    update_state(user_id, {
        "show_type": show_type,
        "uninformative_replies": 0,
        "last_sender": "user",
        "last_message_ts": time.time()
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
    return route_message(message_text, user_id, force_stage=next_block)


def send_first_reminder_if_silent(user_id, send_reply_func):
    state = get_state(user_id)
    if not state or state.get("stage") != "block2":
        return  # Клиент уже ответил или сменился блок — ничего не делаем

    global_prompt = load_prompt(GLOBAL_PROMPT_PATH)
    reminder_prompt = load_prompt(REMINDER_PROMPT_PATH)
    full_prompt = global_prompt + "\n\n" + reminder_prompt

    reply = ask_openai(full_prompt)
    send_reply_func(reply)

    update_state(user_id, {"stage": "block2", "last_message_ts": time.time()})

    # Подготовка таймера на второе напоминание через 12 часов (в блок 2.2)
    plan(user_id,
    "blocks.block_02:send_second_reminder_if_silent",   # <‑‑ путь к функции
    DELAY_TO_BLOCK_2_2_HOURS * 3600)
    

def send_second_reminder_if_silent(user_id, send_reply_func):
    state = get_state(user_id)
    if not state or state.get("stage") != "block2":
        return  # Клиент уже ответил — ничего не делаем

    global_prompt = load_prompt(GLOBAL_PROMPT_PATH)
    reminder_prompt = load_prompt(REMINDER_2_PROMPT_PATH)
    full_prompt = global_prompt + "\n\n" + reminder_prompt

    reply = ask_openai(full_prompt)
    send_reply_func(reply)

    update_state(user_id, {"stage": "block2", "last_message_ts": time.time()})

    # Финальный таймер — если клиент не ответит ещё 4 часа, уходим в block5
    def finalize_if_still_silent():
        state = get_state(user_id)
        if not state or state.get("stage") != "block2":
            return  # Ответил — всё ок
        update_state(user_id, {"handover_reason": "no_response_after_2_2", "scenario_stage_at_handover": "block2"})
        from router import route_message
        route_message("", user_id, force_stage="block5")

    plan(user_id,
    "blocks.block_02:finalize_if_still_silent",   # <‑‑ путь к функции
    FINAL_TIMEOUT_HOURS * 3600)
