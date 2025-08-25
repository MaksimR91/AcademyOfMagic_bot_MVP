import os, time, requests
from logger import logger

SUPABASE_URL        = os.getenv("SUPABASE_URL")
SUPABASE_API_KEY    = os.getenv("SUPABASE_API_KEY")
SUPABASE_TABLE_NAME = "tokens"

HEADERS = {
     "apikey": SUPABASE_API_KEY,
     "Authorization": f"Bearer {SUPABASE_API_KEY}",
     "Content-Type": "application/json",
 }

def load_token_from_supabase(retries: int = 3, delay_sec: int = 3) -> str:
    url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE_NAME}?select=token&order=updated_at.desc&limit=1"
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if not data:
                raise RuntimeError("No token rows in Supabase")
            return data[0]["token"]
        except Exception as e:
            last_err = e
            logger.warning(f"[supabase] load_token attempt {attempt}/{retries} failed: {e}")
            time.sleep(delay_sec)
    raise last_err

def save_token_to_supabase(token: str) -> bool:
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
    if not SUPABASE_URL or not SUPABASE_API_KEY:
        return
    try:
        url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE_NAME}?select=id&limit=1"
        r = requests.get(url, headers=HEADERS, timeout=8)
        logger.info(f"[supabase_ping] status={r.status_code}")
    except Exception as e:
        logger.warning(f"[supabase_ping] fail: {e}")