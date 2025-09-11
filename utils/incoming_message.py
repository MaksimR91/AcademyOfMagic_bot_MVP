from utils.env_loader import ensure_env_loaded
ensure_env_loaded()
import os, time, threading, requests
from openai import OpenAI
from logger import logger
from state.state import save_if_absent, get_state, update_state
from utils.token_manager import get_token
from router import route_message
import utils.outgoing_message as outgoing
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    import boto3
    from botocore.config import Config as BotoConfig
except Exception:
    boto3 = None
    BotoConfig = None

OPENAI_API_KEY = os.getenv("OPENAI_APIKEY")
S3_BUCKET = os.getenv("YANDEX_BUCKET", "magicacademylogsars")
S3_ENDPOINT = os.getenv("YANDEX_ENDPOINT", "https://storage.yandexcloud.net")
S3_REGION = os.getenv("YANDEX_REGION", "ru-central1")

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
    from pydub import AudioSegment
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
        # локальный путь
        audio_path = "/tmp/audio.ogg"
        with open(audio_path, "wb") as f:
            f.write(media_resp.content)

        audio = AudioSegment.from_file(audio_path)
        duration_sec = len(audio) / 1000
        logger.info(f"⏱️ Длительность аудио: {duration_sec:.1f} секунд")

        if duration_sec > 60:
            logger.warning("⚠️ Аудио превышает 60 секунд")
            outgoing.send_text_message(
                phone_number_id,
                normalized_number,
                "Голосовое длиннее 60 сек. Пришлите короче (до минуты) или напишите текст."
            )
            return
        client = OpenAI(api_key=OPENAI_API_KEY)
        with open(audio_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="text"
            )
        # у SDK .text уже строка; при response_format="text" resp — тоже строка
        text = getattr(transcript, "text", transcript)
        logger.info(f"📝 Распознано: {text}")
        text = (text or "").strip()

        # сохраняем .ogg + .txt в S3 (если настроено)
        try:
            _save_voice_to_s3(
                raw_bytes=media_resp.content,
                transcript_text=text,
                wamid=message.get("id") or audio_id
            )
        except Exception as e:
            logger.warning(f"⚠️ Не удалось сохранить голосовое в S3: {e}")
        if text:
            message_uid = message.get("id")
            message_ts = int(message.get("timestamp", "0") or 0)
            process_text_message(text, normalized_number, phone_number_id, name, message_uid, message_ts)

    except Exception as e:
        logger.error(f"❌ Ошибка фоновой обработки аудио: {e}")
        # По ТЗ: плохое качество → попросить текст
        try:
            outgoing.send_text_message(
                phone_number_id,
                normalized_number,
                "Не получилось распознать голосовое. Напишите текст или пришлите короткое сообщение (до 60 сек)."
            )
        except Exception as ee:
            logger.warning(f"Не удалось отправить уведомление пользователю после ошибки: {ee}")

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
        outgoing.send_text_message(
            phone_number_id,
            user_id,
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
        outgoing.send_text_message(
            phone_number_id,
            normalized_number,
            "Техническая ошибка. Попробуйте позже."
        )

def normalize_for_meta(number):
    if number.startswith('77'):
        return '787' + number[2:]
    if number.startswith('79'):
        return '789' + number[2:]
    return number

def handle_status(status):
    logger.info("📥 Статус: %s", status)

def _save_voice_to_s3(raw_bytes: bytes, transcript_text: str, wamid: str) -> None:
    """
    Кладём .ogg и .txt на 30 дней: s3://magicacademylogsars/voice/{YYYY-MM-DD}/{wamid}.ogg|.txt
    Дата — по Asia/Atyrau.
    """
    if not boto3 or not os.getenv("YANDEX_ACCESS_KEY_ID") or not os.getenv("YANDEX_SECRET_ACCESS_KEY"):
        logger.info("S3 не настроен (YANDEX_* отсутствуют) — пропускаем сохранение голосового")
        return
    tz = ZoneInfo("Asia/Atyrau")
    day = datetime.now(tz).strftime("%Y-%m-%d")
    key_ogg = f"voice/{day}/{wamid}.ogg"
    key_txt = f"voice/{day}/{wamid}.txt"

    s3 = boto3.client(
        "s3",
        region_name=S3_REGION,
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=os.getenv("YANDEX_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("YANDEX_SECRET_ACCESS_KEY"),
        config=BotoConfig(connect_timeout=5, read_timeout=10),
    )
    s3.put_object(Bucket=S3_BUCKET, Key=key_ogg, Body=raw_bytes, ContentType="audio/ogg")
    s3.put_object(Bucket=S3_BUCKET, Key=key_txt, Body=(transcript_text or "").encode("utf-8"),
                  ContentType="text/plain; charset=utf-8")
    logger.info("💾 Сохранено голосовое в S3: %s, %s", key_ogg, key_txt)