import os, requests, logging
from utils.token_manager import get_token

logger = logging.getLogger(__name__)

# --- адрес Арсения и шаблон, вынесены в env ----------------------
OWNER_WA_ID   = os.getenv("OWNER_WA_ID")                # 7705…
PHONE_ID = os.getenv("PHONE_NUMBER_ID")
API_URL  = f"https://graph.facebook.com/v19.0/{PHONE_ID}/messages"
MAX_LEN  = 4096                                         # лимит WA для text.body

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


def send_document(to: str, media_id: str):
    _post(
        {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "document",
            "document": {"id": media_id},
        },
        "document",
    )


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


def send_owner_resume(full_text: str):
    """
    Шлёт резюме Арсению *компактно*:
      • несколько полей упаковываются в один HSM
        (символ • вместо «\\n» – внутри {{1}} переводы строк запрещены);
      • размер каждого HSM ≤ 1024 симв. – если не влезает, начинаем новый.
    Возвращает list[requests.Response].
    """
    if not OWNER_WA_ID:
        raise RuntimeError("OWNER_WA_ID не задан в переменных окружения")

    MAX_LEN  = 1024
    SEP      = " • "          # разделитель вместо «\n»
    pieces   = [s.strip() for s in full_text.splitlines() if s.strip()]

    # --- упаковываем ------------------------------------------------
    chunks: list[str] = []
    current = ""
    for p in pieces:
        candidate = (current + SEP if current else "") + p
        if len(candidate) > MAX_LEN:
            if current:
                chunks.append(current)   # фиксируем заполненный
            current = p                  # начинаем новый
        else:
            current = candidate
    if current:
        chunks.append(current)

    responses = []
    for idx, chunk in enumerate(chunks, 1):
        payload = {
            "messaging_product": "whatsapp",
            "to": OWNER_WA_ID,
            "type": "template",
            "template": {
                "name": "owner_summary_chunk",     # approved HSM
                "language": {"code": "ru"},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": chunk}
                        ],
                    }
                ],
            },
        }
        try:
            resp = requests.post(API_URL, headers=_headers(), json=payload, timeout=20)
            responses.append(resp)
            if resp.status_code >= 400:
                logger.error(
                    "WA owner send error %s — %s\nresponse=%s\npayload=%s",
                    resp.status_code, resp.reason, resp.text,
                    json.dumps(payload, ensure_ascii=False),
                )
        except Exception as e:
            logger.error("❌ WA owner_resume to %s: %s • chunk=%r", OWNER_WA_ID, e, chunk)
    return responses
