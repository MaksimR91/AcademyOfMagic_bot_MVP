# routes/debug_tail_route.py
import os, glob
from flask import Blueprint

debug_tail_bp = Blueprint("debug_tail", __name__)

@debug_tail_bp.route("/debug/tail")
def debug_tail():
    import os, glob
    LOG_DIR = "/tmp/logs"
    # ищем самый свежий *.log в каталоге
    pattern = os.path.join(LOG_DIR, "log*")          # ловим и «log», и «log.2025-07-31.log»
    files = sorted(glob.glob(pattern))
    if not files:
        return f"Файл логов не найден (ищу по {pattern})", 404

    latest = files[-1]   # самый свежий
    # читаем последние ±400 строк
    try:
        with open(latest, "r", encoding="utf-8") as f:
            tail = f.readlines()[-400:]
        return "<pre style='font-size:12px'>" + "".join(tail) + "</pre>"
    except Exception as e:
        return f"Не удалось прочитать лог: {e}", 500