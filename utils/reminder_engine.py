import os, time, logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.jobstores.memory import MemoryJobStore
from state.state import get_state          # тот же dict‑API

if not logging.getLogger().handlers:
    h = logging.StreamHandler()          # stdout → Render console
    h.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    logging.getLogger().addHandler(h)

# ────────────────────  базовый логгер  ──────────────────────
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
log.info("📦 reminder_engine import started")

# ---------- JobStore (Postgres → fallback memory) --------------
try:
    # ── 1) Предпочитаем готовый DSN ---------------------------------
    pg_url = os.getenv("SUPABASE_DB_URL")

    # ── 2) Fallback: строим URL по‑старому из SUPABASE_URL ----------
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

    jobstores = {
        "default": SQLAlchemyJobStore(
            url=pg_url,
            engine_options={"connect_args": {"connect_timeout": 5}},  # чтобы не висеть минутами
        )
    }
except Exception as e:
    log.exception(f"⚠️  SQLAlchemyJobStore init failed, falling back to in-memory store: {e}")
    jobstores = {"default": MemoryJobStore()}

# ---------- APScheduler ----------------------------------------
# Render или prod → SQLAlchemyJobStore  
# Локально → MemoryJobStore (не требует БД)
sched = BackgroundScheduler(jobstores=jobstores, timezone="UTC")
try:
    sched.start()
    log.info("⏰ reminder_engine started with %s jobstore", next(iter(jobstores)))
except Exception as e:
    log.exception(f"💥 APScheduler start error: {e}")

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
    run_at = time.time() + delay_sec

    # при рестарте, если задача уже прошла – не ставим снова
    if run_at <= time.time():
        return

    # remove & add (idempotent)
    try:
        sched.remove_job(job_id)
    except Exception:
        pass
    sched.add_job(
            execute_job,                          # ← уже объект-функция
            "date",
            id=job_id,
            run_date=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(run_at)),
            misfire_grace_time=300,
            args=[user_id, norm_path],             # ← аргументы
         )
    log.info(f"[reminder_engine] scheduled {job_id} in {delay_sec//60} min")

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
from utils.whatsapp_senders import send_text
def _send_func_factory(user_id):
    def _send(body):
        st = get_state(user_id) or {}
        to = st.get("normalized_number", user_id)
        send_text(to, body)
    return _send