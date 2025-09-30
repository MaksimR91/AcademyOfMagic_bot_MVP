from utils.env_loader import ensure_env_loaded
ensure_env_loaded()
import os, time, logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.jobstores.memory import MemoryJobStore
from datetime import datetime, timezone
from state.state import get_state
from utils.whatsapp_senders import send_text          # тот же dict‑API
from utils.env_flags import is_local_dev

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

# ---------- JobStore выбор ----------
def _build_jobstores():
    """
    LOCAL_DEV=1 или ACADEMYBOT_TEST=1 → MemoryJobStore.
    PROD (оба 0) → Postgres (Supabase). При ошибке — фоллбек в память.
    """
    if LOCAL_DEV or TEST_MODE:
        log.info("🧠 Jobstore = Memory (LOCAL_DEV/TEST_MODE)")
        return {"default": MemoryJobStore()}
    try:
        # PROD: берём DSN из SUPABASE_DB_URL; если нет — собираем из SUPABASE_URL
        pg_url = os.getenv("SUPABASE_DB_URL")
        if not pg_url:
            raw_supabase = os.getenv("SUPABASE_URL")
            if not raw_supabase:
                raise RuntimeError("neither SUPABASE_DB_URL nor SUPABASE_URL set")
            pg_url = (
                raw_supabase
                .replace("https://", "postgresql+psycopg2://")
                .replace(".supabase.co", ".supabase.co/postgres")
            )
        log.info(f"🔗 reminder_engine PG url → {pg_url.split('@')[-1].split('?')[0]}")
        return {
            "default": SQLAlchemyJobStore(
                url=pg_url,
                engine_options={"connect_args": {"connect_timeout": 5}},
            )
        }
    except Exception as e:
        log.warning(f"⚠️ Postgres недоступен → MemoryJobStore: {e}")
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
        log.info("⏰ reminder_engine started with %s jobstore", next(iter(jobstores)))
    except Exception as e:
        log.exception(f"💥 APScheduler start error: {e}")
        start._started = False

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