import hmac, hashlib
from flask import Blueprint, request, abort, Response, current_app
from logger import logger
from utils.incoming_message import handle_message, handle_status  # используем реальные обработчики

webhook_bp = Blueprint("webhook", __name__)

def _check_signature(raw: bytes) -> bool:
    """
    HMAC SHA-256 проверка подписи Meta по сырому телу.
    Секрет: META_APP_SECRET (app secret). Заголовок: X-Hub-Signature-256.
    """
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not sig.startswith("sha256="):
        logger.error("VERIFICATION FAILED")
        return False
    secret_str = current_app.config.get("META_APP_SECRET")
    if not secret_str:
        logger.error("VERIFICATION FAILED (no META_APP_SECRET in config)")
        return False
    want = "sha256=" + hmac.new(secret_str.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    ok = hmac.compare_digest(sig, want)
    if not ok:
        logger.error("VERIFICATION FAILED")
    return ok


@webhook_bp.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == 'GET':
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')

        if mode == 'subscribe' and token == current_app.config.get("VERIFY_TOKEN"):
            logger.info("WEBHOOK VERIFIED")
            return Response(challenge or "", mimetype="text/plain")
        else:
            logger.error("VERIFICATION FAILED")
            return abort(403)

    elif request.method == 'POST':
        # 1) Проверка подписи по сырому телу
        raw = request.get_data()
        if not _check_signature(raw):
            return abort(403)

        # 2) Парсинг JSON (тихо, без исключений)
        data = request.get_json(silent=True) or {}
        logger.info("📩 webhook raw json: %s", data)

        if data.get('object') == 'whatsapp_business_account':
            for entry in data.get('entry', []):
                for change in entry.get('changes', []):
                    value = change.get('value', {}) or {}
                    meta = value.get('metadata') or {}
                    phone_id = meta.get('phone_number_id', '')
                    display = meta.get('display_phone_number', '')
                    contacts = value.get('contacts') or []

                    for message in value.get('messages', []):
                        handle_message(message, phone_id, display, contacts)

                    for status in value.get('statuses', []):
                        handle_status(status)

        return Response("ok", mimetype="text/plain")