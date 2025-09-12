from utils.env_loader import ensure_env_loaded
ensure_env_loaded()
import os, time, requests, threading
from logger import logger
from utils.env_flags import is_local_dev

SUPABASE_URL        = os.getenv("SUPABASE_URL")
SUPABASE_API_KEY    = os.getenv("SUPABASE_API_KEY")
SUPABASE_TABLE_NAME = "tokens"
ENV_FALLBACK_TOKEN  = os.getenv("WHATSAPP_TOKEN")
LOCAL_DEV = is_local_dev()


HEADERS = {
     "apikey": SUPABASE_API_KEY,
     "Authorization": f"Bearer {SUPABASE_API_KEY}",
     "Content-Type": "application/json",
 }

def load_token_from_supabase(retries: int = 3, delay_sec: int = 3) -> str | None:
    if LOCAL_DEV:
        logger.info("LOCAL_DEV=1: skip Supabase load")
        return None
    if not SUPABASE_URL or not SUPABASE_API_KEY:
        logger.error("Supabase env missing: SUPABASE_URL or SUPABASE_API_KEY")
        return None
    url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE_NAME}?select=token&order=updated_at.desc&limit=1"
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if not data:
                logger.warning("No token rows in Supabase")
                return None
            return data[0]["token"]
        except Exception as e:
            last_err = e
            logger.warning(f"[supabase] load_token attempt {attempt}/{retries} failed: {e}")
            time.sleep(delay_sec)
    logger.error(f"[supabase] all load attempts failed: {last_err}")
    return None


def load_token() -> str | None:
    """
    Универсальная загрузка токена:
    1. Пробуем Supabase
    2. Если нет или ошибка — используем ENV (WA_ACCESS_TOKEN)
    """
    token = load_token_from_supabase()
    if token:
        logger.info("🔑 Токен загружен из Supabase")
        return token
    if ENV_FALLBACK_TOKEN:
        logger.warning("⚠️ Используем fallback токен из ENV (WA_ACCESS_TOKEN)")
        return ENV_FALLBACK_TOKEN
    logger.error("❌ Нет доступного токена ни в Supabase, ни в ENV")
    return None

def save_token_to_supabase(token: str) -> bool:
    if LOCAL_DEV:
        logger.info("[supabase] LOCAL_DEV=1: save_token пропущен")
        return True
    if not SUPABASE_URL or not SUPABASE_API_KEY:
        logger.error("[supabase] save_token: missing SUPABASE_URL/API_KEY")
        return False
    url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE_NAME}"
    payload = {"token": token}
    # Можно добавить Prefer, но и так ок:
    r = requests.post(url, json=payload, headers=HEADERS, timeout=10)
    if r.status_code not in (200, 201):
        logger.error(f"[supabase] save_token failed: {r.status_code} {r.text}")
        return False
    return True

def ping_supabase():
    """
    Лёгкий GET, чтобы проект не уснул.
    Берём минимальный селект из таблицы tokens.
    """
    if LOCAL_DEV or (not SUPABASE_URL or not SUPABASE_API_KEY):
        return
    try:
        url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE_NAME}?select=id&limit=1"
        r = requests.get(url, headers=HEADERS, timeout=8)
        logger.info(f"[supabase_ping] status={r.status_code}")
    except Exception as e:
        logger.warning(f"[supabase_ping] fail: {e}")

def start_supabase_ping_loop(interval_hours: int = 12):
    def loop():
        while True:
            try:
                ping_supabase()
            except Exception as e:
                logger.warning(f"⚠️ Supabase ping error: {e}")
            time.sleep(interval_hours * 3600)
    threading.Thread(target=loop, daemon=True).start()