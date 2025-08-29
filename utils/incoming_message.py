import os, time, threading, requests
from openai import OpenAI
from pydub import AudioSegment
from logger import logger
from state.state import save_if_absent, get_state, update_state
from utils.token_manager import get_token
from router import route_message
from utils.outgoing_message import send_text_message

OPENAI_API_KEY = os.getenv("OPENAI_APIKEY")

def handle_message(message, phone_number_id, bot_display_number, contacts):
    from_number = message.get("from")
    meta_msg_id = message.get("id")           # <-- добавили
    meta_ts     = int(message.get("timestamp", time.time()))

    if from_number.endswith(bot_display_number[-9:]):
        logger.info("🔁 Эхо-сообщение от самого себя — пропущено")
        return

    normalized_number = normalize_for_meta(from_number)
    name = contacts[0].get("profile", {}).get("name") if contacts else "друг"

    if message.get("type") == "text":
        text = message.get("text", {}).get("body", "").strip()
        process_text_message(text, normalized_number, phone_number_id, name,
                             meta_message_id=meta_msg_id, meta_ts=meta_ts)

    elif message.get("type") == "audio":
        logger.info("🎤 Аудио передаётся на фон для обработки")
        threading.Thread(
            target=handle_audio_async,
            args=(message, phone_number_id, normalized_number, name),
            daemon=True
        ).start()
    elif message.get("type") in ("image", "document"):
        # MVP: медиа не обрабатываем — ответим пользователю вежливо
        logger.info("🖼 Получено media-сообщение (%s) — в MVP не обрабатываем", message["type"])
        threading.Thread(target=handle_media_async,
                         args=(message, phone_number_id, normalized_number),
                         daemon=True).start()

def handle_audio_async(message, phone_number_id, normalized_number, name):
    try:
        audio_id = message["audio"]["id"]
        logger.info(f"🎿 Обработка голосового файла, media ID: {audio_id}")

        url = f"https://graph.facebook.com/v19.0/{audio_id}"
        headers = {"Authorization": f"Bearer {get_token()}"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        media_url = resp.json().get("url")

        media_resp = requests.get(media_url, headers=headers, timeout=30)
        media_resp.raise_for_status()
        audio_path = "/tmp/audio.ogg"
        with open(audio_path, "wb") as f:
            f.write(media_resp.content)

        audio = AudioSegment.from_file(audio_path)
        duration_sec = len(audio) / 1000
        logger.info(f"⏱️ Длительность аудио: {duration_sec:.1f} секунд")

        if duration_sec > 60:
            logger.warning("⚠️ Аудио превышает 60 секунд")
            send_text_message(phone_number_id, normalized_number,
                              "Пожалуйста, пришлите голосовое сообщение не длиннее 1 минуты.")
            return
        client = OpenAI(api_key=OPENAI_API_KEY)
        with open(audio_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="text"
            )
        logger.info(f"📝 Распознано: {transcript}")
        text = transcript.strip()
        if text:
            message_uid = message.get("id")
            message_ts = int(message.get("timestamp", "0") or 0)
            process_text_message(text, normalized_number, phone_number_id, name, message_uid, message_ts)

    except Exception as e:
        logger.error(f"❌ Ошибка фоновой обработки аудио: {e}")

def handle_media_async(message, phone_number_id, user_id):
    """MVP: медиа не обрабатываем. Идемпотентно фиксируем входящее и отвечаем текстом."""
    media_type = message["type"]
    media_obj  = message[media_type]
    media_id   = media_obj.get("id")

    # UID + ts
    message_uid = message.get("id") or media_id
    try:
        message_ts = int(message.get("timestamp", time.time()))
    except Exception:
        message_ts = int(time.time())

    st = get_state(user_id) or {}
    # жёсткий дедуп
    if st.get("last_incoming_id") == message_uid:
        logger.info(f"[media:MVP] duplicate ignored user={user_id} msg={message_uid}")
        return
    if st.get("last_incoming_ts") and message_ts < st["last_incoming_ts"] - 5:
        logger.info(f"[media:MVP] old message ignored user={user_id} msg={message_uid}")
        return

    # фиксируем последнее входящее
    update_state(user_id, {
        "last_incoming_id": message_uid,
        "last_incoming_ts": message_ts,
        "last_sender": "user"
    })

    # вежливый ответ
    try:
        send_text_message(
            phone_number_id, user_id,
            "Пока принимаю только текст и короткие голосовые. Пришлите текст."
        )
    except Exception as e:
        logger.warning(f"[media:MVP] не удалось отправить ответ: {e}")

def process_text_message(text: str,
                         normalized_number: str,
                         phone_number_id: str,
                         name: str | None,
                         meta_message_id: str | None = None,
                         meta_ts: int | None = None):
    from state.state import get_state, update_state, save_if_absent
    if not text:
        return

    st = get_state(normalized_number) or {}

    # --- ЖЁСТКИЙ дедуп по message_id от Meta
    if meta_message_id and st.get("last_incoming_id") == meta_message_id:
        logger.info(f"[router] drop duplicate by meta_id user={normalized_number} id={meta_message_id}")
        return

    # --- МЯГКИЙ дедуп по одинаковому тексту в очень узком окне (1 сек),
    #     только если предыдущий был тоже "user" и stage не изменился
    now_ts = int(time.time())
    same_text = (text == st.get("last_incoming_text"))
    very_recent = (now_ts - int(st.get("last_incoming_ts", 0)) <= 1)
    same_stage = (st.get("stage") == st.get("last_incoming_stage"))
    if same_text and very_recent and st.get("last_sender") == "user" and same_stage:
        logger.info(f"[router] drop near-duplicate by text window user={normalized_number}")
        return

    # сохраняем информацию о входящем
    update_state(normalized_number, {
        "last_incoming_id": meta_message_id or "",
        "last_incoming_ts": meta_ts or now_ts,
        "last_incoming_text": text,
        "last_incoming_stage": st.get("stage"),
        "last_sender": "user",
    })

    # --- дальше ваш текущий код:
    save_if_absent(normalized_number,
                   normalized_number=normalized_number,
                   raw_number=normalized_number,
                   client_name=name or "")

    try:
        route_message(text, normalized_number, client_name=name)
    except Exception as e:
        logger.exception(f"💥 Ошибка route_message для {normalized_number}: {e}")
        send_text_message(phone_number_id, normalized_number,
                          "Техническая ошибка. Попробуйте позже.")

def normalize_for_meta(number):
    if number.startswith('77'):
        return '787' + number[2:]
    if number.startswith('79'):
        return '789' + number[2:]
    return number

def handle_status(status):
    logger.info("📥 Статус: %s", status)