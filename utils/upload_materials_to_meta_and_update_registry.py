import os, json, requests, time, threading
from datetime import datetime, timedelta
from tempfile import TemporaryDirectory
from botocore.exceptions import ClientError

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

def registry_load():
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=REGISTRY_KEY)
        return json.loads(obj["Body"].read())
    except s3.exceptions.NoSuchKey:
        return {"videos": {}, "kp": {}}
    except Exception as e:
        logger.error(f"registry_load: {e}")
        return {"videos": {}, "kp": {}}

def registry_save(reg):
    try:
        s3.put_object(
            Bucket=S3_BUCKET, Key=REGISTRY_KEY,
            Body=json.dumps(reg, indent=2, ensure_ascii=False).encode(),
            ContentType="application/json",
        )
    except Exception as e:
        logger.error(f"registry_save: {e}")

def meta_upload(local: str, mtype: str, wa_token: str):
    try:
        with open(local, "rb") as f:
            resp = requests.post(
            META_URL,
            headers={"Authorization": f"Bearer {wa_token}"},
            files={"file": f},
            data={"messaging_product": "whatsapp", "type": mtype},
            timeout=60,
            )
        resp.raise_for_status()
        return resp.json().get("id")
    except Exception as e:
        logger.error(f"meta_upload {local}: {e}")
        return None

def cat_video(fname):
    n = fname.lower()
    if "garden" in n:       return "child_garden"
    if "home" in n:         return "child_home"
    if "not_home" in n or "cafe" in n: return "child_not_home"
    return "adult"

def cat_kp(fname): return "child" if "child" in fname.lower() else "adult"

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

        # ------------- KP --------------------------
        resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=KP_PREFIX)
        for obj in (resp.get("Contents") or []):
            k = obj["Key"];   fname = os.path.basename(k)
            if k.endswith("/"): continue
            cat = cat_kp(fname)

            prev = reg["kp"].get(cat)
            if prev and prev["filename"] == fname and prev["last_modified"] == obj["LastModified"].isoformat():
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
