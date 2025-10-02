# logger.py  – минималистично, но стабильно
from utils.env_loader import ensure_env_loaded
ensure_env_loaded()
import os, sys, logging
from logging.handlers import TimedRotatingFileHandler
from utils.env_flags import is_local_dev

# --- 0. базовая настройка root → stdout (важно для Render/gunicorn) ----
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,  # ← перезаписываем любые ранние настройки, чтобы точно попасть в stdout
)

# --- 1. выбираем тип хендлера -----------------------------------
USE_SIMPLE = sys.platform.startswith("win") or is_local_dev()

if not USE_SIMPLE:
    try:
        from concurrent_log_handler import ConcurrentTimedRotatingFileHandler as S3TimedRotatingFileHandler
    except ImportError:
        USE_SIMPLE = True

if USE_SIMPLE:
    S3TimedRotatingFileHandler = TimedRotatingFileHandler

# --- 2. настраиваем логирование ---------------------------------
LOG_DIR = os.getenv("LOG_DIR", "logs")
LOG_TO_FILE = os.getenv("LOG_TO_FILE", "").lower() in {"1","true","yes"}
if LOG_TO_FILE:
    os.makedirs(LOG_DIR, exist_ok=True)

# --- создаём logger заранее, до использования ---
logger = logging.getLogger("bot")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
logger.propagate = False  # чтобы root не дублировал

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logger.addHandler(console_handler)

if LOG_TO_FILE:
    file_handler = S3TimedRotatingFileHandler(
        os.path.join(LOG_DIR, "bot.log"),
        when="midnight",
        backupCount=7,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(file_handler)


# --- 3. заглушки для старого кода -------------------------------
class _DummyS3Handler(logging.Handler):
    def emit(self, record): pass

logger_s3   = _DummyS3Handler()
s3_client   = None
BUCKET_NAME = None
logger.addHandler(logger_s3)

# --- 4. дублируем в root и gunicorn -----------------------------
root = logging.getLogger()
root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
for h in logger.handlers:
    if h not in root.handlers:
        root.addHandler(h)

guni = logging.getLogger("gunicorn.error")
for h in root.handlers:
    if h not in guni.handlers:
        guni.addHandler(h)

guni.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

guni_access = logging.getLogger("gunicorn.access")
for h in root.handlers:
    if h not in guni_access.handlers:
        guni_access.addHandler(h)
guni_access.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

root.info("🔊 Logging ready (pid=%s, LOG_TO_FILE=%s, LEVEL=%s)", os.getpid(), LOG_TO_FILE, LOG_LEVEL)
