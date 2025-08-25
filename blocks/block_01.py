import os
import time
from utils.ask_openai import ask_openai
from utils.wants_handover_ai import wants_handover_ai
from state.state import get_state, update_state
from logger import logger

# Пути к промптам
GLOBAL_PROMPT_PATH = "prompts/global_prompt.txt"
STAGE_PROMPT_PATH = "prompts/block01_prompt.txt"

# Время до автоматического перехода
DELAY_TO_BLOCK_2_SECONDS = 15


def load_prompt(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def proceed_to_block_2(user_id, send_func=None):
    from router import route_message
    route_message("", user_id, force_stage="block2")


def handle_block1(message_text, user_id, send_reply_func):
    # Проверка на запрос к Арсению
    need_handover = wants_handover_ai(message_text)
    logger.info("[block1] wants_handover_ai=%s text=%s", need_handover, message_text)
    if need_handover:
        update_state(user_id, {"handover_reason": "asked_handover", "scenario_stage_at_handover": "block1"})
        from router import route_message
        return route_message(message_text, user_id, force_stage="block9")

    # Склеиваем промпты
    global_prompt = load_prompt(GLOBAL_PROMPT_PATH)
    stage_prompt = load_prompt(STAGE_PROMPT_PATH)
    full_prompt = global_prompt + "\n\n" + stage_prompt + f'\n\nСообщение клиента: "{message_text}"'

    # Генерация ответа
    reply = ask_openai(full_prompt)

    # Отправка ответа
    send_reply_func(reply)

    # Обновляем состояние
    update_state(user_id, {"stage": "block1", "last_message_ts": time.time()})

    # Запуск таймеров переходов
    from utils.reminder_engine import plan
    plan(user_id,
    "blocks.block_01:proceed_to_block_2",   # <‑‑ путь к функции
    DELAY_TO_BLOCK_2_SECONDS)
    
