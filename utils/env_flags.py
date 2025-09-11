import os
def is_local_dev() -> bool:
    val = str(os.getenv("LOCAL_DEV", "")).strip().lower()
    return val in {"1", "true", "yes"}