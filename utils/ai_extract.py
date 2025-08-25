import json, re
from utils.ask_openai import ask_openai
from logger import logger
from utils.constants import REQUIRED_FIELDS

def ai_extract_fields(message: str, state_snapshot: dict) -> dict:
    prompt = (
        "Ты — JSON‑парсер. Извлеки известные поля, если они встречаются "
        "в сообщении клиента. Отвечай ТОЛЬКО валидным JSON‑объектом без "
        "дополнительного текста.\n\n"
        "Если клиент явно отказался раскрывать информацию — добавь ключ "
       '"refused_fields" со списком полей, по которым отказ.\n'
        f"Текущее состояние:\n```json\n{json.dumps(state_snapshot, ensure_ascii=False, indent=2)}\n```\n"
        f"Сообщение клиента: \"{message}\"\n\n"
        "Поле‑справка:\n"
        "- event_date (YYYY-MM-DD)\n"
        "- event_time (HH:MM)\n"
        "- address\n"
        "- place_type (home/garden/cafe)\n"
        "- guests_count (int)\n"
        "- children_at_party (true/false)\n"
        "- package (базовый/восторг/фурор)\n"
        "- saw_show_before (true/false)\n"
        "- has_photo (true/false)\n"
        "- special_wishes (string)"
    )
    try:
        raw = ask_openai(prompt)
        data = json.loads(raw)
        refused = data.pop("refused_fields", [])
        result = {k: data[k] for k in data if k in REQUIRED_FIELDS}
        return result, refused
    except Exception as e:
        logger.error(f"[ai_extract] fail: {e}")
        return {}, []