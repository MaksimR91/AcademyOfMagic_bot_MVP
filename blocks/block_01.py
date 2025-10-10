import os
import time
from utils.ask_openai import ask_openai
from utils.wants_handover_ai import wants_handover_ai
from state.state import get_state, update_state
from logger import logger

# Пути к промптам
GLOBAL_PROMPT_PATH = "prompts/global_prompt.txt"
STAGE_PROMPT_PATH = "prompts/block01_prompt.txt"

def load_prompt(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def proceed_to_block_2(user_id, send_func=None):
    from router import route_message
    route_message("", user_id, force_stage="block2")

# Флаг: использовать ИИ в приветствии или статический текст
# Можно переключать через переменную окружения USE_AI_GREETING=true/false
USE_AI_GREETING = (os.getenv("USE_AI_GREETING", "false").strip().lower() == "true")

# Статические приветственные сообщения (каждый абзац — отдельное сообщение)
STATIC_GREETING_MESSAGES = [
    "Здравствуйте! Я – бот иллюзиониста Арсения. Моя задача – помочь вам быстро получить информацию о магическом шоу без ожидания ответа Арсения.",
    "Арсений – профессиональный волшебник, шоу будет незабываемым, но нужно ответить на пару вопросов, чтобы всё прошло идеально. После получения ваших ответов я вышлю цены и предложу лучший вариант программы.",
    "И маленький секрет: если вы ответите на все вопросы, Арсений без дополнительной платы покажет на шоу особый бонусный фокус, подготовленный специально для вас!"
]



def handle_block1(message_text, user_id, send_reply_func):
    # Проверка на запрос к Арсению
    need_handover = wants_handover_ai(message_text)
    logger.info("[block1] wants_handover_ai=%s text=%s", need_handover, message_text)
    if need_handover:
        update_state(user_id, {"handover_reason": "asked_handover", "scenario_stage_at_handover": "block1"})
        from router import route_message
        return route_message(message_text, user_id, force_stage="block5")

    if USE_AI_GREETING:
        # Склеиваем промпты и генерируем через ИИ (старое поведение)
        global_prompt = load_prompt(GLOBAL_PROMPT_PATH)
        stage_prompt = load_prompt(STAGE_PROMPT_PATH)
        full_prompt = global_prompt + "\n\n" + stage_prompt + f'\n\nСообщение клиента: "{message_text}"'
        reply = ask_openai(full_prompt)
        send_reply_func(reply)
    else:
        # Отправляем 3 статических сообщения с паузой 3 сек между ними
        for idx, msg in enumerate(STATIC_GREETING_MESSAGES):
            send_reply_func(msg)
            if idx < len(STATIC_GREETING_MESSAGES) - 1:
                try:
                    time.sleep(3)
                except Exception as e:
                    logger.warning("[block1] sleep interrupted: %s", e)

    # Обновляем состояние
    update_state(user_id, {"stage": "block1", "last_message_ts": time.time()})

    # Запуск таймеров переходов
    from utils.reminder_engine import plan
    proceed_to_block_2(user_id)
    
