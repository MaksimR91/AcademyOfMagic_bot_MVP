# logger.py  – минималистично, но стабильно
import os, sys, logging
from logging.handlers import TimedRotatingFileHandler

# --- 1. выбираем тип хендлера -----------------------------------
USE_SIMPLE = sys.platform.startswith("win") or os.getenv("LOCAL_DEV") == "1"

if not USE_SIMPLE:
    try:
        from concurrent_log_handler import ConcurrentTimedRotatingFileHandler as S3TimedRotatingFileHandler
    except ImportError:
        USE_SIMPLE = True

if USE_SIMPLE:
    S3TimedRotatingFileHandler = TimedRotatingFileHandler

# --- 2. настраиваем логирование ---------------------------------
LOG_DIR = os.getenv("LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("bot")
logger.setLevel(logging.INFO)
logger.propagate = False  # ← чтобы root не дублировал

file_handler = S3TimedRotatingFileHandler(
    os.path.join(LOG_DIR, "bot.log"),
    when="midnight",
    backupCount=7,
    encoding="utf-8",
    delay=True,                       # файл откроется при первой записи
)
console_handler = logging.StreamHandler()   # stderr → PowerShell
console_handler.setLevel(logging.DEBUG)
console_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
)
logger.addHandler(console_handler)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
)
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
root.setLevel(logging.INFO)
for h in logger.handlers:
    if h not in root.handlers:
        root.addHandler(h)

guni = logging.getLogger("gunicorn.error")
for h in root.handlers:
    if h not in guni.handlers:
        guni.addHandler(h)

root.info("🔊 Logging ready (pid=%s)", os.getpid())
