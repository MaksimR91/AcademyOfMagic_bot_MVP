import os
import time
from utils.ask_openai import ask_openai
from utils.wants_handover_ai import wants_handover_ai
from state.state import get_state, update_state
from logger import logger
from utils.whatsapp_senders import send_image, send_text  # ← используем прямые sender’ы для картинки
from utils.materials import s3, S3_BUCKET
import json

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

STATIC_GREETING_MESSAGES = [
    "Здравствуйте!👋 Я - бот иллюзиониста Арсения. Задам пару вопросов, подберу программу и пришлю цены✨.",
    # Второе сообщение заменяем картинкой (если media_id найден); этот текст — фолбэк.
    "Арсений — профессиональный волшебник, шоу будет незабываемым.",
    "🎁Бонус: при ответе на все вопросы - на шоу будет особый фокус от Арсения специально для вас!"
]


# fallback-текст, если картинка недоступна
GREETING_IMAGE_FALLBACK = "Арсений — профессиональный волшебник, шоу будет незабываемым ✨"
REGISTRY_KEY = "materials/media_registry.json"

def _load_media_registry() -> dict:
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=REGISTRY_KEY)
        reg = json.loads(obj["Body"].read())
        # совместимость со старыми версиями
        if "images" not in reg:
            reg["images"] = {}
        return reg
    except Exception as e:
        logger.warning(f"[block1] media registry load failed: {e}")
        return {"videos": {}, "kp": {}, "images": {}}

def _get_greeting_media_id() -> str | None:
    reg = _load_media_registry()
    img = (reg.get("images") or {}).get("greeting")
    if not img:
        return None
    # простая защита от протухшего media_id (на всякий случай)
    try:
        uploaded = img.get("uploaded_at")
        # если поле есть, считаем, что апдейтер держит его <30 дней
        return img.get("media_id")
    except Exception:
        return img.get("media_id")

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
        # Отправляем 3 части: 1) текст 2) картинка (или фолбэк-текст) 3) текст
        first, fallback_second, third = STATIC_GREETING_MESSAGES
        send_reply_func(first)
        try:
            time.sleep(1.5)
        except Exception:
            pass
        # Пытаемся взять media_id приветственной картинки из реестра
        media_id = _get_greeting_media_id()
        if media_id:
            from whatsapp_senders import send_image
            try:
                # user_id — это WA: 7XXXXXXXXXX (строка). Отправляем картинку.
                send_image(str(user_id), media_id)
            except Exception as e:
                logger.warning(f"[block1] send_image failed, fallback to text: {e}")
                send_reply_func(fallback_second)
        else:
            # если реестр пуст / не успел обновиться — отправляем текст
            send_reply_func(fallback_second)
        try:
            time.sleep(1.5)
        except Exception:
            pass
        send_reply_func(third)

    # Обновляем состояние
    update_state(user_id, {"stage": "block1", "last_message_ts": time.time()})

    # Запуск таймеров переходов
    from utils.reminder_engine import plan
    proceed_to_block_2(user_id)
    
