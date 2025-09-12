# utils/token_manager.py
from utils.env_loader import ensure_env_loaded
ensure_env_loaded()
import os, time, threading, requests
from logger import logger
from utils.supabase_token import save_token_to_supabase, load_token
from utils.env_flags import is_local_dev

LOCAL_DEV = is_local_dev()
_WHATSAPP_TOKEN: str | None = None



def check_token_validity_raw(token: str) -> bool:
    """Проверка токена без побочных эффектов."""
    if not token:
        return False
    url = f"https://graph.facebook.com/v19.0/me?access_token={token}"
    try:
        r = requests.get(url, timeout=10)
        return r.status_code == 200
    except Exception as e:
        logger.warning(f"⚠️ Ошибка проверки WA токена: {e}")
        return False


def init_token() -> None:
    """Инициализация при старте приложения."""
    global _WHATSAPP_TOKEN
    token = load_token()
    if not token:
        logger.critical("💥 Нет доступного WA токена ни в Supabase, ни в ENV")
        _WHATSAPP_TOKEN = ""
        return

    if check_token_validity_raw(token):
        _WHATSAPP_TOKEN = token
        logger.info(f"🔍 WA токен валиден: {token[:8]}..., len={len(token)}")
    else:
        logger.warning("⚠️ Supabase токен недействителен")
        env_token = os.getenv("WHATSAPP_TOKEN", "")
        if env_token and check_token_validity_raw(env_token):
            _WHATSAPP_TOKEN = env_token
            logger.info("🔑 Переключились на WA токен из ENV (валидный)")
            try:
                save_token_to_supabase(env_token)
                logger.info("☁️ ENV-токен сохранён в Supabase")
            except Exception as e:
                logger.warning(f"⚠️ Не удалось сохранить ENV-токен в Supabase: {e}")
        else:
            _WHATSAPP_TOKEN = ""
            logger.critical("💥 Нет валидного WA токена ни в Supabase, ни в ENV")

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
    """Проверка актуального токена из памяти/инициализации."""
    token = get_token()
    if not token:
        return False
    ok = check_token_validity_raw(token)
    if not ok:
        logger.warning("❌ WA токен из памяти оказался недействительным")
    return ok

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


