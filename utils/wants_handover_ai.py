import re
from utils.ask_openai import ask_openai

def load_global_prompt():
    with open("prompts/global_prompt.txt", encoding="utf-8") as f:
        return f.read()

# Явный запрос «поговорить напрямую / контакты»
HANDOVER_PATTERNS = [
    r"\bсвяж(ись|итесь|итесь\s+со\s+мной|емся|аться)\b",
    r"\bпусть\b.*\b(он|арсени\w*)\b.*\bсвяжет(ся|с)\b",
    r"\bпередай(те)?\b.*\b(арсени\w*|ему)\b",
    r"\bдайте\b.*\b(телефон|контакты|номер)\b",
    r"\bкак\s+связаться\b",
    r"\bнапрямую\b.*\b(с\s+арсени\w*|с\s+ним)\b",
]

# Торг/оплата
PRICING_PATTERNS = [
    r"\bскидк\w+\b",
    r"\bдорого\b",
    r"\b(дешевле|подешевле)\b",
    r"\bоплат\w+.*\bчаст(ями|ями?)\b",
    r"\b(цена|стоимост\w|бюджет)\b.*\b(снизить|уменьшить|пересмотре?ть)\b",
]

# Заказ/бронирование Арсения (это НЕ хендовер)
BOOKING_PATTERNS = [
    r"\bзаказ(ать|)\b.*\b(арсени\w*)\b",
    r"\bприглас(ить|им)\b.*\b(арсени\w*)\b",
    r"\bзаброниров(ать|ка)\b.*\b(арсени\w*)\b",
    r"\bнанять\b.*\b(арсени\w*|фокусник|иллюзионист)\b",
    r"\bхочу\s+(шоу|выступлени\w)\b.*\b(арсени\w*)\b",
    r"\b(заказ|бронировани\w)\b.*\b(арсени\w*)\b",
    r"\b(book|hire)\b.*\b(arsen\w*)\b",                    # en + Arseniy
    r"\b(book|hire)\b.*\b(magician|illusionist)\b",        # en generic
    r"\b(need|want|would like)\s+to\s+(book|hire)\b.*\b(magician|illusionist)\b",
    r"\bнужен\b.*\b(фокусник|иллюзионист|арсени\w*)\b",
]

def _norm(s: str) -> str:
    s = (s or "").casefold()
    s = s.replace("ё", "е")
    # убираем кавычки/мусор, нормализуем пробелы
    s = re.sub(r"[«»\"'“”]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _match_any(patterns, text):
    return any(re.search(p, text, flags=re.IGNORECASE | re.UNICODE) for p in patterns)

def wants_handover_ai(user_message: str) -> bool:
    text = _norm(user_message)

    # 1) Явный запрос прямого контакта — ХЕНДОВЕР
    if _match_any(HANDOVER_PATTERNS, text):
        # logger.info("[handover] matched HANDOVER_PATTERNS")
        return True

    # 2) Цена/скидка/оплата — ХЕНДОВЕР
    if _match_any(PRICING_PATTERNS, text):
        # logger.info("[handover] matched PRICING_PATTERNS")
        return True

    # 3) Бронирование/заказ Арсения — НЕ хендовер
    if _match_any(BOOKING_PATTERNS, text):
        # logger.info("[handover] matched BOOKING_PATTERNS -> False")
        return False

    # 4) Фолбэк на LLM (консервативный)
    classification_prompt = """

Ты бинарный классификатор. Верни РОВНО одно слово: да или нет.

Скажи «да» ТОЛЬКО если сообщение явно:
• просит связать с Арсением, дать его контакты или передать ему;
ИЛИ
• обсуждает цену/скидку/условия оплаты.

Во всех остальных случаях отвечай «нет», даже если речь о заказе/бронировании шоу, желании узнать детали и т.п.
Если сомневаешься — отвечай «нет».
Не добавляй никаких пояснений, предисловий или извинений.

Примеры:
Текст: "Дайте номер Арсения" -> да
Текст: "Как связаться напрямую?" -> да
Текст: "Можно скидку?" -> да
Текст: "Здравствуйте, хотим шоу, расскажите подробнее" -> нет
Текст: "Хочу пригласить Арсения на свадьбу" -> нет

Текст: "{user_message}"
Ответ:
"""
    try:
        resp = ask_openai(classification_prompt).strip().lower()
    except Exception:
        return False  # безопасный дефолт

    return resp.startswith("да")