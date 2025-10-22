import os
import time
from utils.ask_openai import ask_openai
from utils.wants_handover_ai import wants_handover_ai
from state.state import get_state, update_state
from logger import logger
from utils.whatsapp_senders import send_image, send_text  # ← используем прямые sender’ы для картинки

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

# Новый статический сценарий приветствия:
# 1) короткий текст с эмодзи → 2) КАРТИНКА → 3) бонус с эмодзи
GREETING_TEXT_1 = "Здравствуйте!👋 Я - бот иллюзиониста Арсения. Задам пару вопросов, подберу программу и пришлю цены✨."
GREETING_TEXT_3 = "🎁Бонус: при ответе на все вопросы - на шоу будет особый фокус от Арсения специально для вас!"
# media_id картинки для шага 2 (загружается в WhatsApp Business и попадает в env)
GREETING_IMAGE_ID = os.getenv("GREETING_IMAGE_ID")  # пример: "1234567890123456"

# fallback-текст, если картинка недоступна
GREETING_IMAGE_FALLBACK = "Арсений — профессиональный волшебник, шоу будет незабываемым ✨"



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
        # Отправляем: текст → картинка → бонус. Паузы делаем короче, чтобы не утомлять.
        try:
            send_reply_func(GREETING_TEXT_1)
            time.sleep(1.5)
        except Exception as e:
            logger.warning("[block1] send #1 failed: %s", e)

        # Картинка вместо длинного второго текста; если media_id не задан — шлём короткий fallback
        try:
            if GREETING_IMAGE_ID:
                send_image(user_id, GREETING_IMAGE_ID)
            else:
                logger.warning("[block1] GREETING_IMAGE_ID is not set — sending text fallback")
                send_reply_func(GREETING_IMAGE_FALLBACK)
            time.sleep(1.5)
        except Exception as e:
            logger.warning("[block1] send image/fallback failed: %s", e)

        try:
            send_reply_func(GREETING_TEXT_3)
        except Exception as e:
            logger.warning("[block1] send #3 failed: %s", e)

    # Обновляем состояние
    update_state(user_id, {"stage": "block1", "last_message_ts": time.time()})

    # Запуск таймеров переходов
    from utils.reminder_engine import plan
    proceed_to_block_2(user_id)
    
