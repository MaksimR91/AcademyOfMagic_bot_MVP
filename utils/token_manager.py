# utils/token_manager.py
from utils.env_loader import ensure_env_loaded
ensure_env_loaded()
import os, time, threading, requests
from logger import logger
from utils.supabase_token import load_token_from_supabase, save_token_to_supabase
from utils.env_flags import is_local_dev

LOCAL_DEV = is_local_dev()
_WHATSAPP_TOKEN: str | None = None

def init_token() -> None:
    """Инициализация при старте приложения."""
    global _WHATSAPP_TOKEN
    if LOCAL_DEV:
        _WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
        if _WHATSAPP_TOKEN:
            logger.info("🟢 LOCAL_DEV=1: берём WHATSAPP_TOKEN из ENV")
        else:
            logger.critical("💥 LOCAL_DEV=1, но WHATSAPP_TOKEN пуст")
    else:
        try:
            _WHATSAPP_TOKEN = load_token_from_supabase()
            logger.info(f"🔍 WA токен из Supabase: {_WHATSAPP_TOKEN[:8]}..., len={len(_WHATSAPP_TOKEN)}")
        except Exception as e:
            logger.error(f"❌ Supabase токен не получили: {e}")
            _WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
            if _WHATSAPP_TOKEN:
                logger.warning("⚠️ Fallback: WHATSAPP_TOKEN из ENV")
            else:
                logger.critical("💥 Нет WA токена вообще")

def get_token() -> str:
    """Дай актуальный токен (ленивая инициализация)."""
    global _WHATSAPP_TOKEN
    if _WHATSAPP_TOKEN is None:
        init_token()
    return _WHATSAPP_TOKEN or ""

def set_token(new_token: str) -> None:
    """Обновить токен в памяти (после формы/админки)."""
    global _WHATSAPP_TOKEN
    _WHATSAPP_TOKEN = new_token

def save_token(new_token: str) -> bool:
    """Сохранить в Supabase и обновить в памяти (для прода)."""
    ok = save_token_to_supabase(new_token)
    if ok:
        set_token(new_token)
    return ok

def check_token_validity() -> bool:
    token = get_token()
    if not token:
        return False
    url = f"https://graph.facebook.com/v19.0/me?access_token={token}"
    try:
        r = requests.get(url, timeout=10)
        ok = (r.status_code == 200)
        if not ok:
            logger.warning(f"❌ WA токен недействителен: {r.status_code} {r.text[:200]}")
        return ok
    except Exception as e:
        logger.warning(f"⚠️ Ошибка проверки WA токена: {e}")
        return False

def start_token_check_loop(interval_minutes: int = 30):
    def loop():
        while True:
            try:
                # Проверяем и, при необходимости, шлём алерт в TG
                from utils.telegram_alert import notify_if_token_invalid
                notify_if_token_invalid()
            except Exception as e:
                logger.warning(f"⚠️ token_check_loop: {e}")
            time.sleep(interval_minutes * 60)
    threading.Thread(target=loop, daemon=True).start()


