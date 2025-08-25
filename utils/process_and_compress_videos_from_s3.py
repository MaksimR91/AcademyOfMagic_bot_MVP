import os
import subprocess
from tempfile import TemporaryDirectory
from datetime import datetime

from botocore.exceptions import ClientError

from utils.materials import (
    s3, S3_BUCKET,
    list_src_video_keys,
    key_last_modified,
    compressed_key_for,
)
from logger import logger

MAX_SIZE_MB    = 15.9
TARGET_SIZE_MB = 15.0

def compress_video(src: str, dst: str, crf: int):
    cmd = [
        "ffmpeg", "-y", "-i", src,
        "-vcodec", "libx264", "-preset", "slow", "-crf", str(crf),
        dst,
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

def process_and_compress_videos_from_s3():
    src_keys = list_src_video_keys()
    if not src_keys:
        logger.info("Нет исходных видео для обработки.")
        return

    with TemporaryDirectory() as tmp:
        for src_key in src_keys:
            try:
                src_name  = os.path.basename(src_key)
                cmp_key   = compressed_key_for(src_key)
                cmp_name  = os.path.basename(cmp_key)

                if (cmp_mt := key_last_modified(cmp_key)) and key_last_modified(src_key) <= cmp_mt:
                    logger.info(f"✔ {src_name} — сжатая копия актуальна")
                    continue

                local_src = os.path.join(tmp, src_name)
                local_cmp = os.path.join(tmp, cmp_name)

                try:
                    s3.download_file(S3_BUCKET, src_key, local_src)
                except ClientError as e:
                    logger.error(f"Скачивание {src_key}: {e}")
                    continue

                for crf in range(28, 15, -2):
                    try:
                        compress_video(local_src, local_cmp, crf)
                    except Exception as e:
                        logger.error(f"ffmpeg {src_name}: {e}")
                        raise

                    size_mb = os.path.getsize(local_cmp) / 1_048_576
                    if MAX_SIZE_MB >= size_mb >= TARGET_SIZE_MB * 0.85:
                        break
                else:
                    logger.error(f"{src_name} не удалось сжать до < {MAX_SIZE_MB} МБ")
                    continue

                s3.upload_file(local_cmp, S3_BUCKET, cmp_key)
                logger.info(f"⬆ {cmp_key} ({size_mb:.1f} МБ) загружено")

            except Exception as e:
                logger.error(f"{src_key}: {e}")
