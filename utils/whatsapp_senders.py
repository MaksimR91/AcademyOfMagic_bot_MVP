from utils.env_loader import ensure_env_loaded
ensure_env_loaded()
import os, requests, logging, json, re
from utils.token_manager import get_token

logger = logging.getLogger(__name__)

# --- адрес Арсения и шаблон, вынесены в env ----------------------
OWNER_WA_ID   = os.getenv("OWNER_WA_ID")                # 7705…
PHONE_ID = os.getenv("PHONE_NUMBER_ID")
API_URL  = f"https://graph.facebook.com/v19.0/{PHONE_ID}/messages"
MAX_LEN  = 4096                                         # лимит WA для text.body
MISSING_PLACEHOLDER = "—"                              # чем заполняем пустые значения

# ─────────────────────────────────────────────────────────────────
# helper: режем длинный текст
def _chunks(txt: str, size: int = MAX_LEN):
    while txt:
        yield txt[:size]
        txt = txt[size:]

# ─── служебка ────────────────────────────────────────────────────
def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_token()}",
        "Content-Type":  "application/json",
    }


def _post(payload: dict, tag: str) -> None:
    try:
        resp = requests.post(API_URL, json=payload, headers=_headers(), timeout=30)
        resp.raise_for_status()
        logger.info("➡️ WA %s ok → %s", tag, payload["to"])
    except requests.RequestException as e:
        logger.error("❌ WA %s to %s: %s • payload=%s", tag, payload["to"], e, payload)


# ─── публичные функции ──────────────────────────────────────────
def send_text(to: str, body: str):
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body}
    }
    return requests.post(API_URL, headers=_headers(), json=payload, timeout=20)


def send_image(to: str, media_id: str):
    _post(
        {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "image",
            "image": {"id": media_id},
        },
        "image",
    )


def send_document(to: str, media_id: str, filename: str | None = None, caption: str | None = None):
    doc = {"id": media_id}
    if filename:
        doc["filename"] = filename
    if caption:
        doc["caption"] = caption
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "document",
        "document": doc,
    }
    return _post(payload, "WA document")


def send_video(to: str, media_id: str):
    _post(
        {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "video",
            "video": {"id": media_id},
        },
        "video",
    )

def _sanitize_param(value: str) -> str:
    """
    Параметр шаблона не должен содержать \\r\\n\\t и >4 подряд пробелов.
    Сокращаем лишнее и обрезаем крайние пробелы. Ограничим разумной длиной.
    """
    if value is None:
        return MISSING_PLACEHOLDER
    s = str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = re.sub(r" {2,}", " ", s).strip()
    # WhatsApp не документирует строгий лимит параметра, но безопасно держать <1024
    if len(s) > 1000:
        s = s[:1000]
    return s if s else MISSING_PLACEHOLDER

def send_owner_mvp_summary(params: list[str]):
    """
    Отправляет один шаблон summary_owner_mvp_version владельцу (OWNER_WA_ID).
    params — список из 10 значений (в порядке {{1}}..{{10}}). Пустые заполняем "—".
    Возвращает requests.Response.
    """
    if not OWNER_WA_ID:
        raise RuntimeError("OWNER_WA_ID не задан в переменных окружения")
    if not re.fullmatch(r"\d{10,15}", OWNER_WA_ID):
        raise RuntimeError(f"Некорректный OWNER_WA_ID={OWNER_WA_ID!r}")

    # Нормализуем и фиксируем длину ровно 10
    normalized: list[str] = [ _sanitize_param(x) for x in (params or []) ]
    if len(normalized) < 10:
        normalized += [MISSING_PLACEHOLDER] * (10 - len(normalized))
    elif len(normalized) > 10:
        normalized = normalized[:10]

    payload = {
        "messaging_product": "whatsapp",
        "to": OWNER_WA_ID,
        "type": "template",
        "template": {
            "name": "summary_owner_mvp_version",
            "language": { "code": "ru" },
            "components": [
                {
                    "type": "body",
                    "parameters": [ { "type": "text", "text": v } for v in normalized ]
                }
            ]
        }
    }
    try:
        resp = requests.post(API_URL, headers=_headers(), json=payload, timeout=20)
        if resp.status_code >= 400:
            logger.error("WA owner MVP send error %s — %s\nresponse=%s\npayload=%s",
                         resp.status_code, resp.reason, resp.text,
                         json.dumps(payload, ensure_ascii=False))
        else:
            logger.info("➡️ WA template(summary_owner_mvp_version) → %s (%s)",
                        OWNER_WA_ID, resp.status_code)
        return resp
    except Exception as e:
        logger.error("❌ WA owner_mvp_summary to %s: %s", OWNER_WA_ID, e)
        raise