import json
import time
from utils.materials import s3, S3_BUCKET
from utils.wants_handover_ai import wants_handover_ai
from state.state import get_state, update_state
from logger import logger

# ---- константы ------------------------------------------------------
MEDIA_REGISTRY_KEY = "materials/media_registry.json"   # лежит в Yandex-S3
# ---------------------------------------------------------------------------

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
    """
    КП единое для всех типов — берём kp.common.
    """
    kp_info = (registry.get("kp") or {}).get("common")
    return kp_info.get("media_id") if kp_info else None

def choose_video(show_type: str, registry: dict) -> str | None:
    """
    Возвращает media_id подходящего видео или None.
    Деление по месту убрано: только child/adult.
    """
    cat = "child" if show_type in ("детское", "семейное") else "adult"
    videos = (registry.get("videos") or {})
    vids = videos.get(cat, [])
    if not vids:
        # на всякий случай фолбэк
        vids = videos.get("adult", []) or videos.get("child", [])
    return (vids[0] or {}).get("media_id") if vids else None

def infer_show_type_from_stage(state: dict) -> str | None:
    """
    Фолбэк, если show_type пуст: определяем по исходному блоку.
    Если блок не сохранён — вернём None.
    """
    # пробуем явные следы
    from_stage = state.get("prev_stage") or state.get("last_stage") or state.get("stage_before")
    # иногда в state хранится только текущая стадия, попробуем history, если есть
    if not from_stage:
        from_stage = state.get("scenario_stage_at_handover")  # вдруг пришли в 4 откуда-то

    if not from_stage:
        return None
    if "block3a" in from_stage:
        return "детское"
    if "block3b" in from_stage:
        return "взрослое"
    if "block3c" in from_stage:
        return "семейное"
    if "block3d" in from_stage:
        return "нестандартное"
    return None

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
    show_type   = (state.get("show_type") or "").strip().lower()   # 'детское' / 'семейное' / 'взрослое' / 'нестандартное'
    materials_sent = state.get("materials_sent", False)
    logger.info(f"[block4] вход: show_type={show_type!r}, snapshot={ {k: state.get(k) for k in ('stage','show_type','prev_stage','last_stage')} }")

    # ========== первый заход: отправляем материалы и завершаем беседу ======
    if not materials_sent:
        # 1) show_type обязателен; если его нет — пытаемся вывести из блоков 3x
        if not show_type:
            inferred = infer_show_type_from_stage(state)
            if inferred:
                show_type = inferred
                update_state(user_id, {"show_type": show_type})
                logger.info(f"[block4] show_type был пуст → взяли из from_stage: {show_type}")

        # 2) Если всё ещё непонятно — один короткий вопрос и выходим (без ИИ)
        if not show_type:
            question = "Чтобы прислать материалы, уточните формат: детское, семейное, взрослое или нестандартное?"
            try_send(send_text_func, question)
            update_state(user_id, {"stage": "block4_wait_type", "last_message_ts": time.time()})
            return  # ждём ответ клиента

        registry   = load_media_registry()
        kp_id      = choose_kp(show_type, registry)
        video_id   = choose_video(show_type, registry)

        if not kp_id and not video_id:
            logger.warning(f"[block4] Не нашли материалов в реестре для show_type={show_type} (kp.common / videos.{ 'child' if show_type in ('детское','семейное') else 'adult'})")
        if kp_id:
            try_send(send_document_func, kp_id)   # PDF КП
        if video_id:
            try_send(send_video_func, video_id)   # пример шоу

        # Финальное вежливое сообщение (без ИИ).
        try_send(
            send_text_func,
            "Спасибо! Материалы отправил. Дальше подключится Арсений — он уточнит детали и предложит лучший вариант. ✨"
        )

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