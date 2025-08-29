import os, time, gc, threading, psutil, logging
from logger import logger

def cleanup_temp_files():
    tmp_path = "/tmp"
    if os.path.exists(tmp_path):
        for fname in os.listdir(tmp_path):
            if fname.endswith(('.wav', '.mp3', '.ogg')):
                try:
                    os.remove(os.path.join(tmp_path, fname))
                    logger.info(f"🥹 Удален временный файл: {fname}")
                except Exception as e:
                    logger.warning(f"❌ Ошибка удаления файла {fname}: {e}")
    for fname in os.listdir("tmp"):
        if fname.startswith("app_start_") and fname.endswith(".log"):
            try:
                os.remove(os.path.join("tmp", fname))
            except Exception as e:
                logger.warning(f"❌ Ошибка удаления старого лога {fname}: {e}")

def start_memory_cleanup_loop():
    guni = logging.getLogger("gunicorn.error")
    def loop():
        while True:
            time.sleep(600)
            gc.collect()
            mb = psutil.Process().memory_info().rss / 1024 / 1024
            msg = f"🧠 Использованная память {mb:.2f} MB"
            # Пишем ТОЛЬКО в один логгер (gunicorn.error) — попадёт в консоль Render.
            # В файл запись придёт через propagate root? Нет. Поэтому дублируем в root вручную.
            logging.getLogger().info(msg)   # в файл
            guni.info(msg)                  # в консоль
    threading.Thread(target=loop, daemon=True).start()

def log_memory_usage():
    process = psutil.Process()
    mem_mb = process.memory_info().rss / 1024 / 1024
    logger.info(f"Используемая память: {mem_mb:.2f} MB")