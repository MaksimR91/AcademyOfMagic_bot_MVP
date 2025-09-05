from utils.env_loader import ensure_env_loaded
ensure_env_loaded()
import json
import os
import uuid
import pytest
from datetime import datetime
import boto3
from botocore.config import Config

BUCKET = "magicacademylogsars"  # твой бакет

def _require_creds():
    if not (os.getenv("YANDEX_ACCESS_KEY_ID") and os.getenv("YANDEX_SECRET_ACCESS_KEY")):
        pytest.skip("YANDEX_* creds not set; skipping S3 integration")

def _make_client(access_key=None, secret_key=None, endpoint="https://storage.yandexcloud.net"):
    return boto3.client(
        "s3",
        region_name="ru-central1",
        endpoint_url=endpoint,
        aws_access_key_id=access_key or os.getenv("YANDEX_ACCESS_KEY_ID"),
        aws_secret_access_key=secret_key or os.getenv("YANDEX_SECRET_ACCESS_KEY"),
        config=Config(connect_timeout=5, read_timeout=10)
    )

def test_s3_happy_path_load_and_rule(monkeypatch):
    _require_creds()
    from utils import schedule as sched

    # ключ только для теста
    test_key = f"tests/schedule/{uuid.uuid4().hex}.json"

    # подменяем используемый в модуле ключ
    monkeypatch.setattr(sched, "SCHEDULE_KEY", test_key, raising=False)

    # пишем тестовый JSON в S3
    client = _make_client()
    body = json.dumps([{"date":"2025-09-03","time":"12:00"}], ensure_ascii=False).encode("utf-8")
    client.put_object(Bucket=BUCKET, Key=test_key, Body=body, ContentType="application/json")

    try:
        # 1) загрузка должна вернуть список
        slots = sched.load_schedule_from_s3()
        assert isinstance(slots, list)
        assert slots and slots[0]["date"] == "2025-09-03"

        # 2) проверяем правило ±3ч
        assert sched.check_date_availability("2025-09-03","14:59",slots) == "occupied"
        assert sched.check_date_availability("2025-09-03","15:00",slots) == "occupied"
        assert sched.check_date_availability("2025-09-03","15:01",slots) == "available"
    finally:
        # очистка
        client.delete_object(Bucket=BUCKET, Key=test_key)

def test_s3_outage_fallback_need_handover(monkeypatch):
    """
    Имитация падения S3: создаём клиент с фейковыми кредами
    и подсовываем его в модуль schedule → get_object выбросит ошибку.
    """
    _require_creds()
    from utils import schedule as sched
    from utils.schedule import get_availability

    # ключ неважен, всё равно упадём на auth
    test_key = f"tests/schedule/{uuid.uuid4().hex}.json"
    monkeypatch.setattr(sched, "SCHEDULE_KEY", test_key, raising=False)

    # подменяем клиент на «битый»
    bad_client = _make_client(access_key="FAKE", secret_key="FAKE")
    monkeypatch.setattr(sched, "s3_client", bad_client, raising=True)

    # обёртка должна поймать исключение и вернуть need_handover
    assert get_availability("2025-09-05","10:00") == "need_handover"
