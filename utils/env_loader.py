from dotenv import load_dotenv, find_dotenv
from pathlib import Path
import os

__ENV_LOADED = False
def ensure_env_loaded():
    """
    Грузит .env один раз на процесс.
    Порядок поиска:
      1) find_dotenv() от текущей CWD
      2) <корень проекта>/.env (относительно этого файла)
    override=False — чтобы ENV из ОС/pytest НЕ перетирались .env.
    """
    global __ENV_LOADED
    if __ENV_LOADED:
         return
    # 1) стандартный поиск
    path = find_dotenv(usecwd=True)
    if not path:
        # 2) путь от корня проекта (../.. относительно этого файла)
        repo_root = Path(__file__).resolve().parents[2]  # utils/ → <repo root>
        candidate = repo_root / ".env"
        if candidate.exists():
            path = str(candidate)
    # Загружаем, даже если path пуст — тогда load_dotenv просто ничего не сделает
    load_dotenv(path or None, override=False)
    # маленькая подсказка в отладке
    os.environ.setdefault("_ENV_DEBUG_PATH", path or "")
    __ENV_LOADED = True