from utils.env_loader import ensure_env_loaded
ensure_env_loaded()
import os
import requests
from logger import logger
from utils.token_manager import check_token_validity

# Берём токен и chat_id из окружения
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_alert(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("⚠️ TELEGRAM_TOKEN или TELEGRAM_CHAT_ID не заданы")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info("📢 Telegram-уведомление отправлено")
        else:
            logger.warning(f"❌ Ошибка Telegram: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"💥 Исключение при отправке Telegram-сообщения: {e}")

already_alerted = False

def notify_if_token_invalid() -> bool:
    """
    Проверяет валидность WA-токена (utils.token_manager.check_token_validity).
    Если токен невалиден — шлёт алерт в Telegram (один раз за сессию).
    Возвращает True/False по результату проверки.
    """
    global _already_alerted
    ok = check_token_validity()
    if ok:
        logger.info("✅ Токен действителен")
        _already_alerted = False  # сбрасываем, если токен снова стал валиден
        return True
    logger.warning("❌ Токен недействителен")
    if not _already_alerted:
        send_telegram_alert("❗️Токен WhatsApp недействителен. Зайдите в админку и обновите его.")
        _already_alerted = True
    return False