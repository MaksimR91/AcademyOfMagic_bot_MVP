from utils.env_loader import ensure_env_loaded
ensure_env_loaded()
import gevent.monkey
gevent.monkey.patch_all(subprocess=True, ssl=True)
from dotenv import load_dotenv
load_dotenv()
# ----- ENV sanity check --------------------------------------------------
from utils.env_check import check_env
check_env()                       # только логируем, не падаем
import logging
logging.getLogger().info("💬 logger test — root INFO visible?")
import os
import gc
import psutil
import time
import threading
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, abort
from logger import logger
from rollover_scheduler import start_rollover_scheduler
import requests
from openai import OpenAI
from utils.upload_materials_to_meta_and_update_registry import start_media_upload_loop
import json, tempfile, textwrap
from router import route_message
from state.state import save_if_absent, get_state, update_state
from utils.token_manager import init_token, get_token, set_token, save_token, start_token_check_loop
from utils.telegram_alert import notify_if_token_invalid
from utils.outgoing_message import send_text_message
from utils.incoming_message import handle_message, handle_status
from utils.cleanup import cleanup_temp_files, start_memory_cleanup_loop, log_memory_usage
from utils.env_flags import is_local_dev
from utils import reminder_engine  # ⬅ импортируем модуль, чтобы иметь start()

logger.info("💬 logger test — должен появиться в консоли Render")

# приводим к булю: "1", "true", "yes" → True
LOCAL_DEV = is_local_dev()
# флаг «запустить фоновые задачи один раз»
_startup_once = threading.Event()

# ======= ЛОКАЛЬНЫЙ ЛОГГЕР ДЛЯ ПЕРВОГО ЭТАПА ЗАПУСКА ========
os.makedirs("tmp", exist_ok=True)
logger.info("🟢 app.py импортирован")

# ─── Глушим «болтливые» библиотеки ──────────────────────────────────────────────
NOISY_LOGGERS = ("botocore", "boto3", "urllib3", "s3transfer", "apscheduler")
for _name in NOISY_LOGGERS:
    _lg = logging.getLogger(_name)
    _lg.setLevel(logging.WARNING)   # или ERROR, если совсем тишина нужна
    _lg.propagate = False

# Дополнительно для boto3 можно:
try:
    import boto3
    boto3.set_stream_logger("", logging.WARNING)
except Exception:
    pass

from routes.admin_routes import admin_bp
from routes.debug_tail_route import debug_tail_bp
from routes.home_route import home_bp
from routes.debug_upload_log_route import debug_upload_log_bp
from routes.ping_route import ping_bp
from routes.webhook_route import webhook_bp
from routes.debug_mem_route import debug_mem_bp

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
openai_api_key = os.getenv("OPENAI_APIKEY")
META_APP_ID = os.getenv("META_APP_ID")
META_APP_SECRET = os.getenv("META_APP_SECRET")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

client = OpenAI(api_key=openai_api_key)
logger.info(f"🔐 OpenAI API key начинается на: {openai_api_key[:5]}..., длина: {len(openai_api_key)}")

init_token()  # учтёт LOCAL_DEV

# ─────────────────────────────────────────────────────────────
def _bootstrap_background():
    """
    Всё тяжёлое — только в фоне, чтобы не блокировать ответ на $PORT.
    """
    # ⏰ Планировщик напоминаний (APScheduler)
    try:
        reminder_engine.start()  # идемпотентный старт; в LOCAL/TEST он сам себя пропустит
    except Exception as e:
        logger.warning(f"⚠️ Не удалось запустить reminder_engine: {e}")
    # Планировщик ротации логов
    try:
        start_rollover_scheduler()
    except Exception as e:
        logger.warning(f"⚠️ Не удалось запустить rollover scheduler: {e}")

    # Проверка/автообновление токена
    try:
        start_token_check_loop()
    except Exception as e:
        logger.warning(f"⚠️ Не удалось запустить token_check_loop: {e}")

    # Разовая проверка токена с алертом
    try:
        notify_if_token_invalid()
    except Exception as e:
        logger.warning(f"⚠️ notify_if_token_invalid() упала: {e}")

    # Ежедневная загрузка материалов
    try:
        start_media_upload_loop()
    except Exception as e:
        logger.warning(f"⚠️ Не удалось запустить media_upload_loop: {e}")

    # Разовая очистка и фоновый контроль памяти
    try:
        cleanup_temp_files()
    except Exception as e:
        logger.warning(f"⚠️ Не удалось выполнить очистку временных файлов: {e}")
    try:
        start_memory_cleanup_loop()
    except Exception as e:
        logger.warning(f"⚠️ Не удалось запустить memory_cleanup_loop: {e}")


# ─────────────────────────────────────────────────────────────
# ФАБРИКА ПРИЛОЖЕНИЯ
def create_app():
    """Создаёт и настраивает Flask-приложение. Без тяжёлых блокировок."""
    app = Flask(__name__)

    # Логгер Flask → root/gunicorn
    flask_log = app.logger
    flask_log.setLevel(logging.INFO)
    flask_log.handlers.clear()
    flask_log.propagate = True

    # Blueprint админки
    app.register_blueprint(admin_bp)
    app.register_blueprint(debug_tail_bp)
    app.register_blueprint(home_bp)
    app.register_blueprint(debug_upload_log_bp)
    app.register_blueprint(ping_bp)
    app.register_blueprint(webhook_bp)
    app.register_blueprint(debug_mem_bp)

    # Быстрый health — Render сразу увидит, что сервис жив
    @app.get("/health")
    def health():
        return "ok", 200

    # Старт фона — уже после первого запроса (не блокирует импорт/инициализацию)
      # Старт фона при ПЕРВОМ входящем запросе (замена before_first_request в Flask 3.1)
    @app.before_request
    def _kick_bg():
        if not _startup_once.is_set():
            _startup_once.set()
            threading.Thread(target=_bootstrap_background, daemon=True).start()


    # Конфиг для вебхука (юнит-тесты переопределяют эти ключи у app.config)
    app.config.update(
        VERIFY_TOKEN=VERIFY_TOKEN,
        META_APP_SECRET=META_APP_SECRET,
    )

    return app

# Создаём экземпляр через фабрику, чтобы декораторы ниже получили готовый app
app = create_app()
    

if __name__ == '__main__':
    logger.debug("🚀 Запуск Flask-приложения через __main__")
    try:
        logger.info("📡 Старт сервера Flask...")
        app.run(host='0.0.0.0', port=5000)
    except Exception as e:
        logger.exception("💥 Ошибка при запуске Flask-приложения")
