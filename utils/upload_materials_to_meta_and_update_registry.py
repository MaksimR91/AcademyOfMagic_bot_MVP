from utils.env_loader import ensure_env_loaded
ensure_env_loaded()
import os, json, requests, time, threading
from datetime import datetime, timedelta
from tempfile import TemporaryDirectory
from botocore.exceptions import ClientError
import mimetypes

from utils.materials import (
    s3, S3_BUCKET,
    CMP_PREFIX,
)
from logger import logger
from utils.token_manager import get_token

KP_PREFIX = "materials/KP/"
REGISTRY_KEY = "materials/media_registry.json"
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
META_URL        = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/media"
GREETING_PREFIX = "materials/greeting/"

def registry_load():
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=REGISTRY_KEY)
        reg = json.loads(obj["Body"].read())
        # миграция старых реестров: гарантируем наличие секции images
        if "images" not in reg:
            reg["images"] = {}
        return reg
    except s3.exceptions.NoSuchKey:
        return {"videos": {}, "kp": {}, "images": {}}
    except Exception as e:
        logger.error(f"registry_load: {e}")
        # при любой ошибке возвращаем структуру с images, чтобы не падать в блоках
        return {"videos": {}, "kp": {}, "images": {}}

def registry_save(reg):
    try:
        s3.put_object(
            Bucket=S3_BUCKET, Key=REGISTRY_KEY,
            Body=json.dumps(reg, indent=2, ensure_ascii=False).encode(),
            ContentType="application/json",
        )
    except Exception as e:
        logger.error(f"registry_save: {e}")

def _guess_mime(path: str, mtype: str) -> str:
    # Явно задаём MIME — Graph к этому чувствителен
    # Для document важен корректный application/pdf и т.п.
    # Супер-страховка: pdf всегда application/pdf
    if path.lower().endswith(".pdf"):
        return "application/pdf"
    mime, _ = mimetypes.guess_type(path)
    if mime:
        return mime
    # запасной вариант по типу
    return {
        "video": "video/mp4",
        "document": "application/octet-stream",
        "image": "image/jpeg",
        "audio": "audio/mpeg",
    }.get(mtype, "application/octet-stream")

def meta_upload(local: str, mtype: str, wa_token: str):
    try:
        fname = os.path.basename(local)
        mime = _guess_mime(local, mtype)
        with open(local, "rb") as f:
            files = {"file": (fname, f, mime)}
            data  = {"messaging_product": "whatsapp", "type": mtype}
            resp = requests.post(
                META_URL,
                headers={"Authorization": f"Bearer {wa_token}"},
                files=files,
                data=data,
                timeout=60,
            )
        if not resp.ok:
            # логируем максимум сигнала, чтобы сразу видеть первопричину
            try:
                err_json = resp.json()
            except Exception:
                err_json = resp.text
            logger.error(
                "META /media  %s %s  fname=%s mime=%s type=%s  resp=%s",
                resp.status_code, resp.reason, fname, mime, mtype, err_json
            )
            return None
        return resp.json().get("id")
    except requests.RequestException as e:
        # сетевые/таймауты
        logger.error("META /media request err for %s: %s", local, e, exc_info=True)
        return None
    except Exception as e:
        logger.error("meta_upload %s: %s", local, e, exc_info=True)
        return None

def cat_video(fname: str) -> str:
    """
    Только 2 категории: child | adult.
    Опираемся на имя файла (детские ролики у тебя начинаются с 'Детское_' или содержат 'child').
    """
    n = fname.lower()
    if "детск" in n or "child" in n or "family" in n or "семейн" in n:
        return "child"
    return "adult"

def cat_kp(fname: str) -> str:
    """
    КП единое — всегда 'common'.
    """
    return "common"

def cat_image(fname: str) -> str:
    """
    Изображения сейчас используем для приветствия — 'greeting'.
    При необходимости можно расширить (добавить иконки/постеры и т.п.).
    """
    return "greeting"

def upload_materials_to_meta_and_update_registry(wa_token: str):
    reg  = registry_load()
    date = datetime.utcnow().strftime("%Y-%m-%d")

    with TemporaryDirectory() as tmp:
        # ---------- VIDEO (compressed) ------------
        resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=CMP_PREFIX)
        for obj in (resp.get("Contents") or []):
            k = obj["Key"];   fname = os.path.basename(k)
            if k.endswith("/"): continue
            cat = cat_video(fname)

            # 27-дневная пере-загрузка
            prev = next((v for v in reg["videos"].get(cat, []) if v["filename"] == fname), None)
            if prev and (datetime.strptime(prev["uploaded_at"], "%Y-%m-%d") + timedelta(days=27) > datetime.utcnow()):
                continue

            local = os.path.join(tmp, fname)
            try:   s3.download_file(S3_BUCKET, k, local)
            except ClientError as e:
                logger.error(f"DL {k}: {e}"); continue

            mid = meta_upload(local, "video", wa_token)
            if not mid: continue

            reg.setdefault("videos", {}).setdefault(cat, [])
            reg["videos"][cat] = [v for v in reg["videos"][cat] if v["filename"] != fname]
            reg["videos"][cat].append({
                "filename": fname,
                "media_id": mid,
                "uploaded_at": date,
                "size": obj["Size"],
                "last_modified": obj["LastModified"].isoformat(),
            })
        # ------------- IMAGES (greeting) -------------
        resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=GREETING_PREFIX)
        for obj in (resp.get("Contents") or []):
            k = obj["Key"];   fname = os.path.basename(k)
            if not fname or k.endswith("/"):
                continue
            cat = cat_image(fname)  # 'greeting'

            # пере-загрузка каждые 27 дней
            prev = reg["images"].get(cat)
            need_refresh = True
            if prev:
                try:
                    not_expired = (datetime.strptime(prev["uploaded_at"], "%Y-%m-%d") + timedelta(days=27) > datetime.utcnow())
                except Exception:
                    not_expired = False
                # если не истёк срок и файл не менялся — пропускаем
                if not_expired and prev.get("filename") == fname and prev.get("last_modified") == obj["LastModified"].isoformat():
                    need_refresh = False
            if not need_refresh:
                continue

            local = os.path.join(tmp, fname)
            try:
                s3.download_file(S3_BUCKET, k, local)
            except ClientError as e:
                logger.error(f"DL {k}: {e}")
                continue

            mid = meta_upload(local, "image", wa_token)
            if not mid:
                continue

            reg.setdefault("images", {})[cat] = {
                "filename": fname,
                "media_id": mid,
                "uploaded_at": date,
                "size": obj["Size"],
                "last_modified": obj["LastModified"].isoformat(),
            }

        # ------------- KP --------------------------
        resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=KP_PREFIX)
        for obj in (resp.get("Contents") or []):
            k = obj["Key"];   fname = os.path.basename(k)
            if k.endswith("/"): continue
            cat = cat_kp(fname)

            prev = reg["kp"].get(cat)
            # policy: пере-заливаем КП каждые 27 дней (как и видео), даже если файл не менялся
            need_refresh = True
            if prev:
                try:
                    not_expired = (datetime.strptime(prev["uploaded_at"], "%Y-%m-%d") + timedelta(days=27) > datetime.utcnow())
                except Exception:
                    not_expired = False
                # если не истёк срок и файл не менялся — можно пропустить
                if not_expired and prev.get("filename") == fname and prev.get("last_modified") == obj["LastModified"].isoformat():
                    need_refresh = False
            if not need_refresh:
                continue

            local = os.path.join(tmp, fname)
            try:   s3.download_file(S3_BUCKET, k, local)
            except ClientError as e:
                logger.error(f"DL {k}: {e}"); continue

            mid = meta_upload(local, "document", wa_token)
            if not mid: continue

            reg.setdefault("kp", {})[cat] = {
                "filename": fname,
                "media_id": mid,
                "uploaded_at": date,
                "size": obj["Size"],
                "last_modified": obj["LastModified"].isoformat(),
            }

    registry_save(reg)
    logger.info("✅ media_registry.json обновлён")

def start_media_upload_loop():

    def loop():
        while True:
            token = get_token()                      # всегда самый новый
            try:
                logger.info("⏫ Ежедневная загрузка материалов…")
                upload_materials_to_meta_and_update_registry(token)
            except Exception as e:
                logger.error(f"💥 Ошибка загрузки материалов: {e}")
            time.sleep(86400)
    threading.Thread(target=loop, daemon=True).start()
    logger.info("📅 Цикл ежедневной загрузки материалов запущен")

# ──────────────────────────────────────────────────────────────────────────
# Одноразовый запуск из app.py (gevent.spawn)
# Совместим по сигнатуре: main(force_if_missing=..., skip_upload_existing=...)
def main(force_if_missing: bool = False, skip_upload_existing: bool = True):
    """
    Однократная прогрузка материалов в Meta и пересборка media_registry.json.
    Использует самый свежий WA-токен из get_token().
    """
    try:
        token = get_token()
    except Exception as e:
        logger.error(f"🔒 Не удалось получить токен для одноразовой загрузки: {e}")
        return
    try:
        logger.info("🚀 Одноразовая загрузка материалов в Meta (main)")
        upload_materials_to_meta_and_update_registry(token)
        logger.info("✅ media_registry.json обновлён (main)")
    except Exception as e:
        logger.error(f"💥 main(): ошибка при одноразовой загрузке: {e}")

__all__ = [
    "upload_materials_to_meta_and_update_registry",
    "start_media_upload_loop",
    "main",
]
