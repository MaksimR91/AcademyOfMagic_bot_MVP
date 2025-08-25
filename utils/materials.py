import os
import os.path as op
import boto3
from datetime import datetime
from botocore.config import Config
from botocore.exceptions import ClientError
from logger import logger

# === S3 / Yandex Object Storage ============================================
AWS_ACCESS_KEY_ID     = os.getenv("YANDEX_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("YANDEX_SECRET_ACCESS_KEY")
ENDPOINT_URL          = "https://storage.yandexcloud.net"
REGION_NAME           = "ru-central1"

s3_cfg = Config(connect_timeout=5, read_timeout=10)
s3     = boto3.client(
    "s3",
    region_name=REGION_NAME,
    endpoint_url=ENDPOINT_URL,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    config=s3_cfg,
)

# === prefixes ==============================================================
S3_BUCKET   = "magicacademylogsars"
SRC_PREFIX  = "materials/video/"
CMP_PREFIX  = "materials/video/compressed/"

# === helpers ===============================================================

def key_last_modified(key: str):
    try:
        meta = s3.head_object(Bucket=S3_BUCKET, Key=key)
        return meta["LastModified"]
    except ClientError:
        return None


def compressed_key_for(src_key: str) -> str:
    """ materials/video/clip.mov → materials/video/compressed/clip_comp.mov """
    fname = op.basename(src_key)
    base, ext = op.splitext(fname)
    return f"{CMP_PREFIX}{base}_comp{ext}"


def list_src_video_keys():
    """
    Все объекты *кроме* compressed/, папок.
    Тип расширения не фильтруем — если файл не видео, ffmpeg упадёт, мы залогируем.
    """
    keys = []
    try:
        resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=SRC_PREFIX)
        for obj in resp.get("Contents", []):
            k = obj["Key"]
            if k.endswith("/") or CMP_PREFIX in k:
                continue
            keys.append(k)
    except ClientError as e:
        logger.error(f"list_src_video_keys: {e}")
    return keys
