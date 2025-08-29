# routes/debug_mem_route.py
import psutil, gc, logging
from flask import Blueprint

debug_mem_bp = Blueprint("debug_mem", __name__)

@debug_mem_bp.route("/debug/mem")
def debug_mem():
    import psutil, gc
    gc.collect()
    mb = psutil.Process().memory_info().rss / 1024 / 1024
    msg = f"🧠 (manual) {mb:.2f} MB"
    logging.getLogger().info(msg)           # файл
    logging.getLogger("gunicorn.error").info(msg)  # консоль
    return f"{mb:.2f} MB", 200