# routes/debug_upload_log_route.py
from flask import Blueprint
from logger import logger

debug_upload_log_bp = Blueprint("debug_upload_log", __name__)

@debug_upload_log_bp.route("/debug/upload-log")
def manual_log_upload():
    from logger import upload_to_s3_manual
    upload_to_s3_manual()
    return "Загрузка выполнена (если файл был)", 200