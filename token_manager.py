import os
import requests
from logger import logger

TOKEN_FILE = "token.txt"

def get_token():
    try:
        with open(TOKEN_FILE, "r") as f:
            token = f.read().strip()
        from logger import logger
        logger.info(f"📤 Токен из token.txt (repr): {repr(token)}, длина: {len(token)}")
        return token
    except Exception as e:
        from logger import logger
        logger.warning(f"❌ Ошибка чтения token.txt: {e}")
        return ""


def save_token(token: str):
    with open(TOKEN_FILE, "w") as f:
        f.write(token.strip())

def check_token_validity_and_notify():
    access_token = get_token()
    if not access_token:
        logger.error("❌ Токен отсутствует в token.txt")
        return

    app_id = os.getenv("META_APP_ID")
    app_secret = os.getenv("META_APP_SECRET")
    wa_id = os.getenv("ADMIN_WA_ID")
    phone_number_id = os.getenv("PHONE_NUMBER_ID")

    if not all([app_id, app_secret, wa_id, phone_number_id]):
        logger.error("❌ Не заданы необходимые переменные окружения (META_APP_ID, META_APP_SECRET, ADMIN_WA_ID, PHONE_NUMBER_ID)")
        return

    url = f"https://graph.facebook.com/debug_token"
    params = {
        "input_token": access_token,
        "access_token": f"{app_id}|{app_secret}"
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        logger.info(f"🔍 Ответ Meta при проверке токена: {data}")

        if not data.get("data", {}).get("is_valid"):
            send_whatsapp_alert(phone_number_id, wa_id, "🔒 Ваш токен WhatsApp истёк. Пожалуйста, обновите его через https://academyofmagic-bot.onrender.com/admin/token")
        else:
            logger.info("✅ Токен валиден")

    except Exception as e:
        logger.error(f"❌ Ошибка при проверке валидности токена: {e}")

def send_whatsapp_alert(phone_number_id, to_wa_id, text):
    url = f"https://graph.facebook.com/v15.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {get_token()}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_wa_id,
        "type": "text",
        "text": {"body": text}
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        logger.info(f"📤 Уведомление отправлено Арсению ({to_wa_id}): {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"❌ Ошибка при отправке уведомления: {e}")

