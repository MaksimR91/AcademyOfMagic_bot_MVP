import os
import subprocess
from tempfile import TemporaryDirectory
from datetime import datetime
import json
import math
import time

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
FFMPEG_MIN_TIMEOUT = 90  # сек
FFMPEG_TIMEOUT_K    = 1.5  # множитель к длительности
MIN_VIDEO_KBPS      = 250  # нижняя граница качества для 360p
AUDIO_KBPS          = 96   # битрейт аудио
BITRATE_SAFETY_K    = 0.94 # запас на контейнер/оверхэнды (~6%)
LADDER              = [(1280,720), (854,480), (640,360)]  # 720p→480p→360p

def _ffprobe_json(path: str) -> dict:
    """Читаем метадату файла через ffprobe -print_format json."""
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        path,
    ]
    out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return json.loads(out.stdout.decode("utf-8"))

def probe_duration_sec(path: str) -> float:
    meta = _ffprobe_json(path)
    dur = meta.get("format", {}).get("duration")
    return float(dur) if dur else 0.0

def probe_resolution(path: str) -> tuple[int,int]:
    meta = _ffprobe_json(path)
    for st in meta.get("streams", []):
        if st.get("codec_type") == "video":
            return int(st.get("width", 0)), int(st.get("height", 0))
    return (0, 0)

def _scale_filter(src_w: int, src_h: int, tw: int, th: int) -> str:
    # сохраняем пропорции, используем -2 для кратности пиксельной сетке
    # подгоняем по меньшей стороне
    if src_w == 0 or src_h == 0:
        return f"scale={tw}:{th}"
    # шире/уже — не важно, подгоняем по высоте
    return f"scale='min({tw},iw)':'-2'"

def encode_bounded(src: str, dst: str, w: int, h: int, v_kbps: int, a_kbps: int, timeout_s: int):
    vf = _scale_filter(*probe_resolution(src), w, h)
    cmd = [
        "ffmpeg", "-y",
        "-i", src,
        "-vf", vf, "-r", "25",
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-level", "4.0",
       "-b:v", f"{v_kbps}k", "-maxrate", f"{v_kbps}k", "-bufsize", f"{2*v_kbps}k",
        "-c:a", "aac", "-b:a", f"{a_kbps}k",
        "-movflags", "+faststart",
        dst,
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=timeout_s)

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
                
                # 0) Если исходник уже <= лимита — просто используем его
                orig_mb = os.path.getsize(local_src) / 1_048_576
                if orig_mb <= MAX_SIZE_MB:
                    try:
                        s3.upload_file(local_src, S3_BUCKET, cmp_key, ExtraArgs={"ContentType": "video/mp4"})
                        logger.info(f"⬆ {cmp_key} (исходник {orig_mb:.1f} МБ) загружен без перекодирования")
                    except Exception as e:
                        logger.error(f"Загрузка исходника как compressed для {src_name} провалилась: {e}")
                    continue

                # 1) Расчёт целевого битрейта под лимит
                dur = max(1.0, probe_duration_sec(local_src))
                total_kbps = int(((TARGET_SIZE_MB * 1024 * 8) / dur) * BITRATE_SAFETY_K)
                video_kbps = max(MIN_VIDEO_KBPS, total_kbps - AUDIO_KBPS)
                timeout_s  = max(FFMPEG_MIN_TIMEOUT, int(dur * FFMPEG_TIMEOUT_K))
                logger.info(f"{src_name}: dur={dur:.2f}s → target total≈{total_kbps} kbps, video≈{video_kbps} kbps, timeout={timeout_s}s")
                # 2) Если для 360p требуется ниже порога качества — пропускаем
                min_required_kbps = MIN_VIDEO_KBPS
                if total_kbps < (min_required_kbps + AUDIO_KBPS):
                    logger.error(f"{src_name}: требуется слишком низкий битрейт ({total_kbps} kbps total) — пропускаем")
                    continue

                # 3) Пробуем по лестнице разрешений
                success = False
                best_size_mb = math.inf
                for (tw, th) in LADDER:
                    try:
                        encode_bounded(local_src, local_cmp, tw, th, video_kbps, AUDIO_KBPS, timeout_s)
                    except subprocess.TimeoutExpired:
                        logger.error(f"ffmpeg {src_name} таймаут {timeout_s}s на {tw}x{th}")
                        continue
                    except subprocess.CalledProcessError as e:
                        logger.error(f"ffmpeg {src_name} упал на {tw}x{th}: {e}")
                        continue

                    size_mb = os.path.getsize(local_cmp) / 1_048_576
                    best_size_mb = min(best_size_mb, size_mb)
                    logger.info(f"{src_name}: {tw}x{th} → {size_mb:.2f} МБ")
                    if size_mb <= MAX_SIZE_MB:
                        success = True
                        break

                if not success:
                    logger.error(f"{src_name}: не удалось уложиться в {MAX_SIZE_MB} МБ (лучшее {best_size_mb:.2f} МБ) — пропуск")
                    continue
                # 4) Успех — загружаем сжатое видео
                try:
                    s3.upload_file(local_cmp, S3_BUCKET, cmp_key, ExtraArgs={"ContentType": "video/mp4"})
                    logger.info(f"⬆ {cmp_key} загружено (<= {MAX_SIZE_MB} МБ)")
                except Exception as e:
                    logger.error(f"Загрузка compressed для {src_name} провалилась: {e}")

            except Exception as e:
                logger.error(f"{src_key}: {e}")
