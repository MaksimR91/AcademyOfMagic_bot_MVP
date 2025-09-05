from utils.env_loader import ensure_env_loaded
ensure_env_loaded()
import os, logging

CRITICAL_VARS = [
    "VERIFY_TOKEN", "PHONE_NUMBER_ID",
    "META_APP_ID", "META_APP_SECRET",
    "SUPABASE_URL", "SUPABASE_API_KEY",
]

WARNING_VARS = [
    "OPENAI_APIKEY", "YANDEX_ACCESS_KEY_ID", "YANDEX_SECRET_ACCESS_KEY",
    "ADMIN_PASSWORD", "GCP_VISION_KEY_JSON",
    "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID",
    "WHATSAPP_TOKEN", "ADMIN_WA_ID",
    "NOTION_API_KEY", "NOTION_CRM_DATABASE_ID",
]

def _missing(keys):              # helper
    return [k for k in keys if not os.getenv(k)]

def check_env():
    # 👇 выводим путь загруженного .env для отладки
    print("ENV loaded from:", os.getenv("_ENV_DEBUG_PATH"))

    miss_crit = _missing(CRITICAL_VARS)
    miss_warn = _missing(WARNING_VARS)

    if miss_crit:
        logging.critical("🚨 Missing ENV (critical): %s", ", ".join(miss_crit))
    if miss_warn:
        logging.warning("⚠️ Missing ENV (warning): %s", ", ".join(miss_warn))

if __name__ == "__main__":
    check_env()