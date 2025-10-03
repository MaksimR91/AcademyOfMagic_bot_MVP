from utils.env_loader import ensure_env_loaded
ensure_env_loaded()
from flask import Blueprint, render_template, request, abort
from logger import logger
from utils.token_manager import get_token, set_token, save_token
import os
from utils.env_flags import is_local_dev

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
LOCAL_DEV = is_local_dev()

@admin_bp.route("/token", methods=["GET", "POST"])
def update_token():
    message = None
    if request.method == "POST":
        password = request.form.get("password")
        if password != ADMIN_PASSWORD:
            abort(403)

        token = request.form.get("token", "").strip()
        logger.info(f"📥 Токен из формы (repr): {repr(token)}")
        if token:
            if LOCAL_DEV:
                set_token(token)  # только в памяти
            else:
                save_token(token)  # Postgres + память
        message = "✅ Токен успешно сохранён!"

    return render_template("token.html", message=message)