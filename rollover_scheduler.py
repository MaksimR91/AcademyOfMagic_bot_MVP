from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers import SchedulerAlreadyRunningError
from datetime import datetime, timedelta
import os
from logger import logger, S3TimedRotatingFileHandler, logger_s3, s3_client, BUCKET_NAME, LOG_DIR

scheduler = BackgroundScheduler()

def manual_rollover():
    for handler in logger.handlers:
        if isinstance(handler, S3TimedRotatingFileHandler):
            logger.info("🌀 Ручной вызов doRollover()")
            handler.doRollover()
            break
    else:
        logger.warning("❗ Хендлер S3TimedRotatingFileHandler не найден")

def start_rollover_scheduler():
    scheduler.add_job(manual_rollover, "cron", hour=0, minute=5)
    try:
        scheduler.start()
    except SchedulerAlreadyRunningError:
        # Планировщик уже запущен – игнорируем вторую попытку
        pass
    logger.info("🕒 Планировщик ротации и загрузки запущен")

def upload_to_s3_yesterday():
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    local_path = os.path.join(LOG_DIR, f"log.{yesterday}.log")
    s3_key = f"logs/log.{yesterday}.log"

    logger_s3.info("🚀 Отложенная загрузка в S3 (авто)")
    if not os.path.exists(local_path):
        logger_s3.warning("❗ Лог-файл отсутствует, нечего загружать")
        return

    try:
        with open(local_path, 'r', encoding='utf-8') as f:
            content = f.read()
            logger_s3.info(f"📄 Содержимое файла перед загрузкой (авто):\n{content}")
    except Exception as e:
        logger_s3.warning(f"❌ Не удалось прочитать файл: {e}")

    try:
        s3_client.upload_file(local_path, BUCKET_NAME, s3_key)
        logger_s3.info(f"✅ Авто-загрузка успешна: {s3_key}")

        try:
            s3_client.head_object(Bucket=BUCKET_NAME, Key=s3_key)
            logger_s3.info("🔍 HEAD: файл появился в бакете (авто)")
        except s3_client.exceptions.ClientError as e:
            logger_s3.warning(f"❗ HEAD (авто): файл не найден. Ошибка: {e}")

    except Exception as e:
        logger_s3.exception("💥 Ошибка при авто-загрузке в S3")

def schedule_s3_upload():
    job_id = f"s3_upload_{datetime.now().strftime('%H%M%S')}"
    scheduler.add_job(
        upload_to_s3_yesterday,
        trigger='date',
        run_date=datetime.now() + timedelta(seconds=60),
        id=job_id,
        replace_existing=False
    )
    logger.info(f"⏳ Планирование авто-загрузки через минуту: job_id = {job_id}")

