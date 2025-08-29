# routes/home_route.py
from flask import Blueprint
from logger import logger

home_bp = Blueprint("home", __name__)

@home_bp.route("/", methods=["GET"])
def home():
    logger.info("🏠 Запрос GET /")
    return "Сервер работает!"