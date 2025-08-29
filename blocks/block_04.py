import json
import time
from utils.materials import s3, S3_BUCKET
from utils.ask_openai import ask_openai
from utils.wants_handover_ai import wants_handover_ai
from state.state import get_state, update_state
from logger import logger

# ---- константы и пути ------------------------------------------------------
GLOBAL_PROMPT_PATH  = "prompts/global_prompt.txt"
STAGE_PROMPT_PATH   = "prompts/block04_prompt.txt"

MEDIA_REGISTRY_KEY = "materials/media_registry.json"   # лежит в Yandex-S3

# ---------------------------------------------------------------------------

def load_prompt(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def load_media_registry() -> dict:
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=MEDIA_REGISTRY_KEY)
        return json.loads(obj["Body"].read())
    except Exception as e:
        logger.error(f"media_registry load error: {e}")
        return {"videos": {}, "kp": {}}
    
def try_send(func, *args, **kwargs):
    """
    Выполнить send_*_func безопасно.
    Возвращает True, если отправка прошла без исключения.
    """
    try:
        func(*args, **kwargs)
        return True
    except Exception as e:
        logger.error(f"[block4] error while sending {func.__name__}: {e}")
        return False

# ---- выбор материалов ------------------------------------------------------

def choose_kp(show_type: str, registry: dict) -> str | None:
    cat = "child" if show_type in ("детское", "семейное") else "adult"
    kp_info = registry.get("kp", {}).get(cat)
    return kp_info["media_id"] if kp_info else None

def choose_video(show_type: str, place_type: str, registry: dict) -> str | None:
    """
    Возвращает media_id подходящего видео или None.
    place_type уже нормализован в блоках 3a-3c:
        "home" | "garden" | "cafe" | None
    """
    if show_type in ("детское", "семейное"):
        if   place_type == "garden": cat = "child_garden"
        elif place_type == "home":   cat = "child_home"
        else:                        cat = "child_not_home"
    else:
        cat = "adult"

    vids = registry.get("videos", {}).get(cat, [])
    if not vids:
        # fallback: пробуем общий "adult" набор, чтобы не остаться без видео
        vids = registry.get("videos", {}).get("adult", [])

    return vids[0]["media_id"] if vids else None

# ---- основной обработчик ---------------------------------------------------

def handle_block4(
    message_text: str,
    user_id: str,
    send_text_func,
    send_document_func,
    send_video_func,
):
    """
    Отправляем КП + видео, даём вежливое финальное сообщение
    и сразу передаём диалог Арсению (block5).
    """

    # --- хенд-овер по запросу клиента ----
    if wants_handover_ai(message_text):
        update_state(user_id, {"handover_reason": "asked_handover", "scenario_stage_at_handover": "block4"})
        from router import route_message
        return route_message(message_text, user_id, force_stage="block5")

    state = get_state(user_id) or {}
    show_type   = state.get("show_type")        # 'детское' / 'семейное' / 'взрослое'
    place_type  = state.get("place_type", "")   # 'дом', 'кафе', 'детский сад' ...
    materials_sent = state.get("materials_sent", False)

    # ========== первый заход: отправляем материалы и завершаем беседу ======
    if not materials_sent:
        registry   = load_media_registry()
        kp_id      = choose_kp(show_type, registry)
        video_id   = choose_video(show_type, place_type, registry)

        if kp_id:
            try_send(send_document_func, kp_id)   # PDF КП
        if video_id:
            try_send(send_video_func, video_id)   # пример шоу

        # Короткое вступление из промпта (можно оставить как есть)
        intro_text = ask_openai(
            load_prompt(GLOBAL_PROMPT_PATH) + "\n\n" + load_prompt(STAGE_PROMPT_PATH)
        )
        if intro_text:
            send_text_func(intro_text)

        # Финальное вежливое сообщение. Без ожидания ответа.
        closing_text = (
            "Спасибо! Материалы отправил. Дальше подключится Арсений — он уточнит детали и предложит лучший вариант. "
            "Хорошего дня!"
        )
        send_text_func(closing_text)

        materials_ts = time.time()
        update_state(user_id, {
        **state,
        "stage": "block4",
        "materials_sent": True,
        "materials_sent_ts": materials_ts,
        "last_message_ts": materials_ts,
        "handover_reason": "materials_sent_auto",
        "scenario_stage_at_handover": "block4",
        })

        # Сразу хендовер
        from router import route_message
        return route_message("", user_id, force_stage="block5")

    # Если по какой-то причине мы всё ещё в block4 и пришёл ответ —
    # просто делаем хендовер (по новой логике этот блок — конечный).
    from router import route_message
    return route_message(message_text, user_id, force_stage="block5")