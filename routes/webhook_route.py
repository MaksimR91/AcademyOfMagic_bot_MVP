import os
from flask import Blueprint, request, jsonify
from logger import logger
from core_handlers import handle_message, handle_status  # если уже выносил обработчики

webhook_bp = Blueprint("webhook", __name__)
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")

@webhook_bp.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == 'GET':
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')

        if mode == 'subscribe' and token == VERIFY_TOKEN:
            logger.info("WEBHOOK VERIFIED")
            return challenge, 200
        else:
            logger.error("VERIFICATION FAILED")
            return "Verification failed", 403

    elif request.method == 'POST':
        # ➊ Сырой payload, чтобы увидеть реальный user_id и убедиться,
        #    что он совпадает с ADMIN_NUMBERS
        logger.info("📩 webhook raw json: %s", request.get_json())

        data = request.json
        logger.info("Получено сообщение: %s", data)

        if data.get('object') == 'whatsapp_business_account':
            for entry in data.get('entry', []):
                for change in entry.get('changes', []):
                    value = change.get('value', {})

                    for message in value.get('messages', []):
                        handle_message(
                            message,
                            value['metadata']['phone_number_id'],
                            value['metadata']['display_phone_number'],
                            value.get('contacts', [])
                        )

                    for status in value.get('statuses', []):
                        handle_status(status)

        return jsonify({"status": "success"}), 200