# tests/integration/test_materials_end2end.py
import os
import json
import subprocess
from pathlib import Path
from io import BytesIO

import pytest
import boto3
from botocore.config import Config

# ==== ENV / S3 клиент (как в коде) ==========================================
ENDPOINT_URL = "https://storage.yandexcloud.net"
REGION_NAME  = "ru-central1"
S3_BUCKET    = "magicacademylogsars"

AWS_ACCESS_KEY_ID     = os.getenv("YANDEX_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("YANDEX_SECRET_ACCESS_KEY")

s3 = boto3.client(
    "s3",
    region_name=REGION_NAME,
    endpoint_url=ENDPOINT_URL,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    config=Config(connect_timeout=5, read_timeout=10),
)

# ==== хелперы ==============================================================

def _ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return True
    except Exception:
        return False

def _put_bytes(key: str, data: bytes, content_type: str):
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=data, ContentType=content_type)

def _upload_file(key: str, path: Path, content_type: str):
    s3.upload_file(str(path), S3_BUCKET, key, ExtraArgs={"ContentType": content_type})

# ==== сам тест ===============================================================

@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg не установлен")
def test_materials_end2end(monkeypatch, tmp_path):
    """
    E2E: S3 → ffmpeg → Meta registry → WABA.
    Ожидаем:
      - ffmpeg сжал исходник в compressed/
      - upload_materials_to_meta_and_update_registry записал media_id в реестр
      - в WABA ушли: текст (ИИ) + документ (PDF) + видео
    """

    # ---------- Arrange: файлы в S3 ----------
    # PDF КП
    pdf_key  = "materials/KP/offer.pdf"   # cat_kp → 'adult'
    _put_bytes(pdf_key, b"%PDF-1.4\n% tiny pdf\n", "application/pdf")

    # маленькое тест-видео (2 сек) → кладём в materials/video/
    src_mp4 = tmp_path / "garden_demo.mp4"
    # делаем короткое видео (быстро собирается)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25",
        "-t", "2",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-crf", "28", "-preset", "veryfast",
        str(src_mp4),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # имя содержит 'garden' → cat_video → 'child_garden'
    orig_key = "materials/video/garden_demo.mp4"
    _upload_file(orig_key, src_mp4, "video/mp4")

    # ---------- ffmpeg: сжатие исходников ----------
    from utils.process_and_compress_videos_from_s3 import process_and_compress_videos_from_s3
    process_and_compress_videos_from_s3()

    # убедимся, что compressed появился
    from utils.materials import compressed_key_for, CMP_PREFIX
    cmp_key = compressed_key_for(orig_key)
    head = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=CMP_PREFIX)
    keys = {o["Key"] for o in (head.get("Contents") or [])}
    assert cmp_key in keys, "Ожидали сжатое видео в compressed/"

    # ---------- Meta: обновление реестра (мокаем upload) ----------
    import utils.upload_materials_to_meta_and_update_registry as uploader

    # вернём детерминированные media_id, без реального сети
    monkeypatch.setattr(uploader, "meta_upload",
                        lambda local, mtype, wa_token: f"MID_{mtype.upper()}_FAKE_1")

    # запускаем обновление реестра (использует compressed/ и KP/)
    uploader.upload_materials_to_meta_and_update_registry(wa_token="DUMMY")

    # читаем реестр
    reg_obj = s3.get_object(Bucket=S3_BUCKET, Key="materials/media_registry.json")
    reg = json.loads(reg_obj["Body"].read().decode("utf-8"))

    # в реестре есть:
    # - kp['adult'].media_id == MID_DOCUMENT_FAKE_1
    # - videos['child_garden'][...] media_id == MID_VIDEO_FAKE_1
    assert reg["kp"]["adult"]["media_id"] == "MID_DOCUMENT_FAKE_1"
    vg = reg["videos"]["child_garden"]
    assert isinstance(vg, list) and len(vg) >= 1
    assert vg[-1]["media_id"] == "MID_VIDEO_FAKE_1"

    # ---------- WABA: перехват вызовов отправки ----------
    import utils.whatsapp_senders as wa

    sent = {"text": [], "document": [], "video": []}

    # send_text использует requests.post напрямую — подменим его локально
    class _Resp:
        status_code = 200
        reason = "OK"
        text = "ok"
        def raise_for_status(self): pass

    def fake_requests_post(url, headers=None, json=None, timeout=20, **kw):
        if json and json.get("type") == "text":
            sent["text"].append(json["text"]["body"])
        return _Resp()

    # _post вызывается send_document / send_video — перехватим и распарсим
    def fake__post(payload: dict, tag: str):
        if payload.get("type") == "document":
            sent["document"].append(payload["document"]["id"])
        if payload.get("type") == "video":
            sent["video"].append(payload["video"]["id"])

    monkeypatch.setattr(wa.requests, "post", fake_requests_post)
    monkeypatch.setattr(wa, "_post", fake__post)

    # ---------- ИИ-текст ----------
    import utils.ask_openai as ask
    monkeypatch.setattr(ask, "ask_openai", lambda *a, **k: "Короткая выжимка по материалам ✨")

    # ---------- Отправка клиенту ----------
    # Текст
    to = os.getenv("TEST_WA_RECIPIENT", "77050000000")
    wa.send_text(to, ask.ask_openai("сделай подпись"))
    # Документ (КП adult)
    wa.send_document(to, reg["kp"]["adult"]["media_id"])
    # Видео (child_garden)
    wa.send_video(to, vg[-1]["media_id"])

    # ---------- Проверки ----------
    assert len(sent["text"]) == 1, "Должен уйти 1 текст от ИИ"
    assert "выжимка" in sent["text"][0]
    assert sent["document"] == ["MID_DOCUMENT_FAKE_1"], "Должен уйти 1 документ (media_id из реестра)"
    assert sent["video"] == ["MID_VIDEO_FAKE_1"], "Должно уйти 1 видео (media_id из реестра)"
