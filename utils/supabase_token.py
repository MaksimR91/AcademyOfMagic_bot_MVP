from utils.env_loader import ensure_env_loaded
ensure_env_loaded()
import os, time
from logger import logger
from utils.env_flags import is_local_dev
from sqlalchemy import create_engine, text

ENV_FALLBACK_TOKEN  = os.getenv("WHATSAPP_TOKEN")
LOCAL_DEV = is_local_dev()

# ✅ источники для Postgres (Neon/др.)
# Приоритет: PG_POOLED_URL → NEON_DATABASE_URL → PG_DIRECT_URL
PG_POOLED_URL      = (os.getenv("PG_POOLED_URL") or "").strip()
NEON_DATABASE_URL  = (os.getenv("NEON_DATABASE_URL") or "").strip()
PG_DIRECT_URL      = (os.getenv("PG_DIRECT_URL") or "").strip()
PG_URL = PG_POOLED_URL or NEON_DATABASE_URL or PG_DIRECT_URL

def _mk_engine(url: str):
    """Единый фабричный метод движка с безопасными опциями."""
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=60,
        pool_size=3, max_overflow=3,
        connect_args={"connect_timeout": 5}
    )

def _load_token_from_postgres() -> str | None:
    """
    Основной путь: читаем последний токен из таблицы public.tokens в Postgres (Neon/PG).
    """
    if LOCAL_DEV:
        logger.info("[token] LOCAL_DEV=1: пропускаю Postgres load")
        return None
    if not PG_URL:
        logger.info("[token] PG_URL не задан → пропускаю Postgres load")
        return None
    try:
        eng = _mk_engine(PG_URL)
        with eng.connect() as conn:
            # Берём самую свежую запись по updated_at
            row = conn.execute(
                text("select token from public.tokens order by updated_at desc limit 1")
            ).first()
            if not row:
                logger.warning("[token] В Postgres таблица tokens пустая")
                return None
            token = row[0]
            if token:
                logger.info("🔑 Токен загружен из Postgres")
                return token
            logger.warning("[token] В Postgres последняя строка без токена")
    except Exception as e:
        logger.error(f"[token] Ошибка чтения из Postgres: {e}")
    return None

def load_token() -> str | None:
    """
    Универсальная загрузка токена:
    1) Пробуем Postgres (Neon/PG_URL)
    2) Если нет или ошибка — используем ENV (WHATSAPP_TOKEN)
    """
    token = _load_token_from_postgres()
    if token:
        logger.info("🔑 Токен загружен из Postgres")
        return token
    if ENV_FALLBACK_TOKEN:
        logger.warning("⚠️ Используем fallback токен из ENV (WA_ACCESS_TOKEN)")
        return ENV_FALLBACK_TOKEN
    logger.error("❌ Нет доступного токена ни в Postgres, ни в ENV")
    return None

def _save_token_to_postgres(token: str) -> bool:
    """
    Сохраняем токен в Postgres (вставляем новую строку).
    Таблица public.tokens с колонками (id, token, created_at, updated_at) как обсуждали.
    """
    if LOCAL_DEV:
        logger.info("[token] LOCAL_DEV=1: save_token_to_postgres пропущен")
        return True
    if not PG_URL:
        logger.info("[token] PG_URL не задан → пропускаю Postgres save")
        return False
    try:
        eng = _mk_engine(PG_URL)
        with eng.begin() as conn:
            conn.execute(
                text("insert into public.tokens (token) values (:t)"),
                {"t": token},
            )
        logger.info("💾 Токен сохранён в Postgres")
        return True
    except Exception as e:
        logger.error(f"[token] Ошибка сохранения в Postgres: {e}")
        return False

def save_token(token: str) -> bool:
    """
    Единая точка сохранения: пишем в Postgres (Neon/PG_URL)
    """
    ok_pg = _save_token_to_postgres(token)
    return bool(ok_pg)
