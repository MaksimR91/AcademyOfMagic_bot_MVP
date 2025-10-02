from utils.env_loader import ensure_env_loaded
ensure_env_loaded()
import os, time, logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.jobstores.memory import MemoryJobStore
from sqlalchemy import create_engine
from datetime import datetime, timezone
from state.state import get_state
from utils.whatsapp_senders import send_text          # тот же dict‑API
from utils.env_flags import is_local_dev

# опционально (если дадим REDIS_URL — будет самый надёжный стор)
try:
    from apscheduler.jobstores.redis import RedisJobStore  # type: ignore
except Exception:  # Redis не обязателен
    RedisJobStore = None  # type: ignore

if not logging.getLogger().handlers:
    h = logging.StreamHandler()          # stdout → Render console
    h.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    logging.getLogger().addHandler(h)

# ────────────────────  базовый логгер  ──────────────────────
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
log.info("📦 reminder_engine import started")
LOCAL_DEV = is_local_dev()
# Приводим к булю: только 1/true/yes включают тестовый режим.
TEST_MODE = str(os.getenv("ACADEMYBOT_TEST", "")).strip().lower() in {"1", "true", "yes"}
# Ускоритель напоминаний для тестов/стендов (1.0 — без ускорения)
try:
    ACCEL = float(os.getenv("REMINDER_ACCEL", "1.0"))
except Exception:
    ACCEL = 1.0
log.info(f"⚑ flags: TEST_MODE={TEST_MODE}, LOCAL_DEV={LOCAL_DEV}, REMINDER_ACCEL={ACCEL}")
AUTOSTART = os.getenv("REMINDER_AUTOSTART", "1").lower() in {"1","true","yes"}

def _safe_dsn(dsn: str) -> str:
    """Для логов: скрываем пароль, оставляем хост/БД."""
    try:
        tail = dsn.split("@", 1)[-1]
        return tail.split("?", 1)[0]
    except Exception:
        return "<dsn>"


def _mk_engine(url: str):
    """Единый фабричный метод движка с безопасными опциями."""
    return create_engine(
        url,
        pool_pre_ping=True,                 # отваливающиеся коннекты чинит прозрачно
        pool_recycle=60,                    # ребалансируем соединения почаще
        pool_size=5, max_overflow=5,
        connect_args={"connect_timeout": 5} # не висим навсегда
    )

def _build_jobstores():
    """
    Порядок:
      1) LOCAL_DEV/TEST → Memory
      2) REDIS_URL → RedisJobStore
      3) PG_POOLED_URL → SQLAlchemyJobStore (PgBouncer/pooled)
      4) NEON_DATABASE_URL (или PG_DIRECT_URL) → SQLAlchemyJobStore (прямой Postgres)
      5) fallback → Memory
    """
    # 1) локальный/тестовый стенд → сразу память
    if LOCAL_DEV or TEST_MODE:
        log.info("🧠 Jobstore = Memory (LOCAL_DEV/TEST_MODE)")
        return {"default": MemoryJobStore()}

    # 2) Redis (самый стабильный стор для задач, если есть URL)
    redis_url = os.getenv("REDIS_URL", "").strip()
    if RedisJobStore and redis_url:
        try:
            js = RedisJobStore(url=redis_url, jobs_key="aps:jobs", run_times_key="aps:runs")  # type: ignore
            log.info("🔗 reminder_engine REDIS url → %s", redis_url.split("@")[-1])
            return {"default": js}
        except Exception as e:
            log.error("💥 RedisJobStore init failed: %s", e, exc_info=True)

    # 3) Пулер (PgBouncer): используем PG_POOLED_URL
    pooled = os.getenv("PG_POOLED_URL", "").strip()
    if pooled:
        try:
            eng = _mk_engine(pooled)
            with eng.connect() as _:
                pass  # быстрая проверка коннекта
            log.info("🔗 reminder_engine PG (pooler) → %s", _safe_dsn(pooled))
            return {"default": SQLAlchemyJobStore(engine=eng)}
        except Exception as e:
            log.error("💥 PG pooler unreachable: %s", e, exc_info=True)

    # 4) Прямой Postgres (минует PgBouncer): сначала NEON_DATABASE_URL, затем PG_DIRECT_URL
    direct = (
        os.getenv("NEON_DATABASE_URL", "").strip()
        or os.getenv("PG_DIRECT_URL", "").strip()
    )
    if direct:
        try:
            eng = _mk_engine(direct)
            with eng.connect() as _:
                pass
            log.info("🔗 reminder_engine PG (direct) → %s", _safe_dsn(direct))
            return {"default": SQLAlchemyJobStore(engine=eng)}
        except Exception as e:
            log.error("💥 PG direct unreachable: %s", e, exc_info=True)

    # 5) fallback
    log.warning("⚠️ No stable DB for jobstore → MemoryJobStore fallback")
    return {"default": MemoryJobStore()}

# ---------- APScheduler старт ----------
jobstores = _build_jobstores()
sched = BackgroundScheduler(jobstores=jobstores, timezone="UTC")

# ВНИМАНИЕ: не стартуем шедулер при импорте.
# Входная точка вызывает start().
def start():
    """Запустить планировщик. Всегда стартуем (и в TEST/LOCAL_DEV — с Memory)."""
    if getattr(start, "_started", False):
        return
    try:
        sched.start()
        start._started = True
        js_name = type(sched._jobstores["default"]).__name__ if "default" in sched._jobstores else "<unknown>"
        log.info("⏰ reminder_engine started with %s jobstore", js_name)
    except Exception as e:
        log.exception(f"💥 APScheduler start error: {e}")
        start._started = False


# автозапуск по умолчанию, чтобы не забыть вызвать start() в app.py
if AUTOSTART:
    try:
        start()
    except Exception as _e:
        log.exception("💥 reminder_engine autostart failed: %s", _e)

# ---------- универсальный планировщик ---------------------------
#  accepted func_path formats
#  • "package.module.func"
#  • "package.module:func"       ← остаётся совместимо с прод-кодом
def plan(user_id: str, func_ref, delay_sec: int) -> None:
    """
    Зарегистрировать одноразовую задачу.
    • func_path  – строкой "blocks.block02.send_first_reminder_if_silent"
    • delay_sec  – через сколько секунд вызвать
    При повторном вызове с тем же ключом старая задача перезаписывается.
    """
    # принимаем ИЛИ строку, ИЛИ саму функцию
    if callable(func_ref):
        norm_path = f"{func_ref.__module__}.{func_ref.__name__}"
    else:
        norm_path = func_ref.replace(":", ".", 1)
    job_id    = f"{user_id}:{norm_path}"
    # ускоряем при REMINDER_ACCEL<1 (для интеграционных тестов)
    try:
        delay_sec = max(1, int(delay_sec * ACCEL))
    except Exception:
        pass
    run_at_ts = time.time() + delay_sec

    # при рестарте, если задача уже прошла – не ставим снова
    if run_at_ts <= time.time():
        return

    # remove & add (idempotent)
    try:
        sched.remove_job(job_id)
    except Exception:
        pass
     # run_date — явный UTC datetime, чтобы не путаться с таймзоной
    run_dt = datetime.fromtimestamp(run_at_ts, tz=timezone.utc)
    sched.add_job(
        execute_job,
        "date",
        id=job_id,
        run_date=run_dt,
        misfire_grace_time=300,
        args=[user_id, norm_path],
    )
    # дружелюбный лог в секундах и минутах
    log.info(f"[reminder_engine] scheduled {job_id} in {delay_sec}s (~{delay_sec/60:.1f} min)")

# ---------- точка входа, которую увидит APScheduler -------------
def execute_job(user_id: str, func_path: str):
    """
    Унифицированный launcher, чтобы избежать проблем сериализации.
    Сигнатура строго (user_id, func_path) – оба строки.
    """
    func_path = func_path.replace(":", ".", 1)     # поддержка «:»
    mod_name, func_name = func_path.rsplit(".", 1)
    mod = __import__(mod_name, fromlist=[func_name])
    func = getattr(mod, func_name)
    try:
        func(user_id, _send_func_factory(user_id))
    except TypeError:
        func(user_id)
    except Exception as e:
        log.error(f"[reminder_engine] job {user_id}:{func_path} error: {e}")

# ---------- лёгкая обёртка для send_text ------------------------
def _send_func_factory(user_id):
    def _send(body):
        st = get_state(user_id) or {}
        to = st.get("normalized_number", user_id)
        send_text(to, body)
    return _send