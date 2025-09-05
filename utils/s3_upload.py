from utils.env_loader import ensure_env_loaded
ensure_env_loaded()
import os, uuid, boto3
from botocore.config import Config

_AWS_ID     = os.getenv("YANDEX_ACCESS_KEY_ID")
_AWS_SECRET = os.getenv("YANDEX_SECRET_ACCESS_KEY")
_ENDPOINT   = "https://storage.yandexcloud.net"
_REGION     = "ru-central1"
_BUCKET     = "magicacademylogsars"

_s3 = boto3.client(
    "s3",
    region_name=_REGION,
    endpoint_url=_ENDPOINT,
    aws_access_key_id=_AWS_ID,
    aws_secret_access_key=_AWS_SECRET,
    config=Config(connect_timeout=5, read_timeout=10),
)

def upload_image(data: bytes, suffix: str = ".jpg") -> str:
    """
    Сохраняет изображение в папку Photo/… и возвращает публичный URL.
    """
    key = f"Photo/{uuid.uuid4()}{suffix}"
    _s3.put_object(
        Bucket=_BUCKET,
        Key=key,
        Body=data,
        ContentType="image/jpeg",
        ACL="public-read",
    )
    return f"{_ENDPOINT.replace('https://', f'https://{_BUCKET}.')}/{key}"
