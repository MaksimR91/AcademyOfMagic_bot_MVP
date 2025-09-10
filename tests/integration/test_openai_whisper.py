# tests/integration/test_openai_whisper.py
import os
import io
import json
import pytest
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

# ====== конфиг ======
VOICE_FILE = Path("tests/data/voice_short.ogg")   # ≤ 60 сек, нормальное качество
OPENAI_MODEL_WHISPER = os.getenv("OPENAI_MODEL_WHISPER", "gpt-4o-transcribe")  # или "whisper-1"

# S3 (Яндекс) для проверки сохранения
BUCKET = os.getenv("YANDEX_BUCKET", "magicacademylogsars")
S3_ENDPOINT = os.getenv("YANDEX_ENDPOINT", "https://storage.yandexcloud.net")
S3_REGION = os.getenv("YANDEX_REGION", "ru-central1")


# ====== зависимости, что могут отсутствовать в окружении ======
try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None

try:
    import boto3
    from botocore.config import Config as BotoConfig
except Exception:  # pragma: no cover
    boto3 = None
    BotoConfig = None


def _require_openai():
    if not os.getenv("OPENAI_APIKEY"):
        pytest.skip("OPENAI_APIKEY не задан; пропускаю OpenAI Whisper тесты")
    if OpenAI is None:
        pytest.skip("openai SDK не установлен; пропускаю")


def _require_s3():
    if not (boto3 and BotoConfig):
        pytest.skip("boto3 не установлен; пропускаю S3 тесты")
    if not (os.getenv("YANDEX_ACCESS_KEY_ID") and os.getenv("YANDEX_SECRET_ACCESS_KEY")):
        pytest.skip("YANDEX_* не заданы; пропускаю S3 тесты")


def _client_openai():
    return OpenAI(api_key=os.getenv("OPENAI_APIKEY"))


def _client_s3():
    return boto3.client(
        "s3",
        region_name=S3_REGION,
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=os.getenv("YANDEX_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("YANDEX_SECRET_ACCESS_KEY"),
        config=BotoConfig(connect_timeout=5, read_timeout=10),
    )

def test_whisper_transcribes_short_ogg_live():
    """
    ≤60 сек → транскрипция не пустая (ТЗ 3.4).
    """
    _require_openai()
    assert VOICE_FILE.exists(), f"Нет файла {VOICE_FILE} (добавь короткий .ogg в tests/data)"

    client = _client_openai()
    with VOICE_FILE.open("rb") as f:
        # OpenAI audio.transcriptions (универсальный вызов)
        # NB: model: gpt-4o-transcribe (рекомендуемый) или whisper-1
        resp = client.audio.transcriptions.create(
            model=OPENAI_MODEL_WHISPER,
            file=f,     # файл-объект
            # language="ru",  # можно подсказать язык
            # prompt="Аудио — русская речь без музыки",  # при желании
        )

    # SDK может вернуть объект с .text или message.content — у новых SDK .text
    text = getattr(resp, "text", None) or getattr(resp, "text", "")
    assert isinstance(text, str), "Ответ не текст"
    # не пусто и не только пробелы/знаки
    assert text.strip(), f"Пустая транскрипция: {text!r}"
    # подсказка: обычно встречаются кириллические символы
    assert any("а" <= ch.lower() <= "я" or ch == "ё" for ch in text.lower()), \
        f"Похоже, не русский текст: {text!r}"

def test_whisper_bad_audio_or_error():
    """
    Плохое качество / не-аудио → Whisper кидает ошибку или возвращает пустоту.
    По ТЗ в таких случаях бот просит текст. Здесь фиксируем факт ошибки/пустоты.
    """
    _require_openai()

    client = _client_openai()
    bad_bytes = io.BytesIO(b"\x00\x01\x02NOT_OGG_AT_ALL")  # невалидный «ogg»

    error = None
    try:
        client.audio.transcriptions.create(
            model=OPENAI_MODEL_WHISPER,
            file=("noise.ogg", bad_bytes, "audio/ogg"),
        )
    except Exception as e:  # ожидаемо упадёт
        error = e

    # либо ошибка, либо пустая транскрипция (в реале: затем бот просит текст)
    assert error is not None, "Whisper не упал на явном мусоре — странно"

def test_voice_saved_to_s3_paths_ok(monkeypatch):
    """
    Сохранение .ogg + .txt на 30 дней (ТЗ 3.4).
    Проверяем правильный путь и факт записи.
    Формат ключа: s3://magicacademylogsars/voice/{YYYY-MM-DD}/{wamid}.ogg|.txt
    """
    _require_s3()
    s3 = _client_s3()

    # Дата по TZ Asia/Atyrau (UTC+05:00)
    tz = ZoneInfo("Asia/Atyrau")
    today = datetime.now(tz).strftime("%Y-%m-%d")

    # фиктивный wamid (как приходит от Meta)
    wamid = "wamid.TEST_INTEGRATION"

    key_ogg = f"voice/{today}/{wamid}.ogg"
    key_txt = f"voice/{today}/{wamid}.txt"

    # готовим «контент» для проверки
    ogg_bytes = b"OggS" + b"\x00" * 32  # достаточно для PUT (не обязано быть валидным медиа)
    text_payload = "пример транскрипции".encode("utf-8")

    # запись
    s3.put_object(Bucket=BUCKET, Key=key_ogg, Body=ogg_bytes, ContentType="audio/ogg")
    s3.put_object(Bucket=BUCKET, Key=key_txt, Body=text_payload, ContentType="text/plain; charset=utf-8")

    # чтение и проверка
    got_ogg = s3.get_object(Bucket=BUCKET, Key=key_ogg)["Body"].read()
    got_txt = s3.get_object(Bucket=BUCKET, Key=key_txt)["Body"].read()

    assert got_ogg.startswith(b"OggS"), "В S3 не тот .ogg"
    assert got_txt.decode("utf-8") == "пример транскрипции"

    # уборка
    s3.delete_object(Bucket=BUCKET, Key=key_ogg)
    s3.delete_object(Bucket=BUCKET, Key=key_txt)
