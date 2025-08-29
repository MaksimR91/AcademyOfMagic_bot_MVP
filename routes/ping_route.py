from flask import Blueprint
from logger import logger

ping_bp = Blueprint("ping", __name__)

@ping_bp.route("/ping")
def ping():
    logger.info("🔔 Запрос PING")
    return "OK", 200