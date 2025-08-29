import requests
from logger import logger
from utils.token_manager import get_token

API_URL = "https://graph.facebook.com/v19.0/{phone_number_id}/messages"

def send_text_message(phone_number_id, to, text):
    url = API_URL.format(phone_number_id=phone_number_id)
    headers = {"Authorization": f"Bearer {get_token()}", 
               "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    response = requests.post(url, headers=headers, json=payload)
    resp_text = response.text[:500] + "..." if len(response.text) > 500 else response.text
    logger.info(f"➡️ WhatsApp {to}, статус: {response.status_code}, ответ: {resp_text}")