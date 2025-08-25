# router.py
import os
import inspect
import time
from state.state import get_state, update_state
from logger import logger
from utils.whatsapp_senders import send_text, send_document, send_video, send_image

# ===== блоки ===============================================================
from blocks import (
    block_01, block_02,
    block_03a, block_03b, block_03c, block_03d,
    block_04, block_05,
    block_06a, block_06b,
    block_07, block_08,
    block_09, block_10,
)

# ── читаем список админ‑номеров один раз при импорте ────────────────
ADMIN_NUMBERS = {
    num.strip() for num in os.getenv("ADMIN_NUMBERS", "").split(",") if num.strip()
}

# --- <stage> → (module, handler_name) --------------------------------------
BLOCK_MAP = {
    "block1":  (block_01,  "handle_block1"),
    "block2":  (block_02,  "handle_block2"),  # заглушка, фактический handler будет выбран ниже
    "block3a": (block_03a, "handle_block3a"),
    "block3b": (block_03b, "handle_block3b"),
    "block3c": (block_03c, "handle_block3c"),
    "block3d": (block_03d, "handle_block3d"),
    "block4":  (block_04,  "handle_block4"),
    "block5":  (block_05,  "handle_block5"),
    "block6a": (block_06a, "handle_block6a"),
    "block6b": (block_06b, "handle_block6b"),
    "block7":  (block_07,  "handle_block7"),
    "block8":  (block_08,  "handle_block8"),
    "block9":  (block_09,  "handle_block9"),
    "block10": (block_10,  "handle_block10"),
}

# ---------------------------------------------------------------------------
def route_message(
    message_text: str,
    user_id: str,
    client_name: str | None = None,
    *,
    force_stage: str | None = None,
    message_uid: str | None = None,   # из вебхука
    message_ts: float | None = None,  # epoch seconds из вебхука
):
    """
    · Определяем текущий этап пользователя
    · Делаем идемпотентность/фильтры входящих
    · Готовим callables для WhatsApp
    · Дергаем нужный handler-блок
    """
    # -------- подготовка функций отправки (нужны ПЕРЕД #reset) ------------
    wa_to = (get_state(user_id) or {}).get("normalized_number", user_id)
    send_text_func     = lambda body:     send_text(wa_to, body)
    send_document_func = lambda media_id: send_document(wa_to, media_id)
    send_video_func    = lambda media_id: send_video(wa_to, media_id)

    # ---------- техническая команда "#reset" (только для админа) ----------
    if message_text.strip() == "#reset":
        if user_id in ADMIN_NUMBERS:
            from state.state import delete_state
            delete_state(user_id)

            for mod_name in ("blocks.block_03a", "blocks.block_03b", "blocks.block_03c"):
                try:
                    mod = __import__(mod_name, fromlist=["DATE_DECISION_FLAGS"])
                    getattr(mod, "DATE_DECISION_FLAGS", {}).pop(user_id, None)
                except Exception:
                    pass

            # 🧹 чистим отложенные джобы
            from utils.reminder_engine import sched
            for job in sched.get_jobs():
                if job.id.startswith(f"{user_id}:"):
                    sched.remove_job(job.id)

            send_text(wa_to, "State cleared.")
        else:
            logger.warning("Ignored #reset from non-admin %s", user_id)
            send_text(wa_to, "Команда недоступна.")
        return

    elif message_text.strip() == "#jobs" and user_id in ADMIN_NUMBERS:
        from utils.reminder_engine import sched
        jobs = "\n".join(j.id for j in sched.get_jobs())
        send_text(wa_to, jobs or "нет job-ов")
        return

    # --------- идемпотентность и защита от «старых» входящих -------------
    state = get_state(user_id) or {}

    # нормализуем ts из Meta (может прийти строкой)
    now = time.time()
    try:
        message_ts = float(message_ts) if message_ts is not None else None
    except Exception:
        message_ts = None

    msg_norm = (message_text or "").strip().lower()
    msg_hash = __import__("hashlib").sha1(msg_norm.encode("utf-8")).hexdigest()

    last_uid  = state.get("last_msg_uid")
    last_hash = state.get("last_msg_hash")
    last_seen = state.get("last_msg_ts") or 0.0

    # Лимиты
    DUP_WINDOW_SEC  = int(os.getenv("DUP_WINDOW_SEC", "120"))          # 2 мин — «почти дубликат»
    LATE_DROP_MIN   = int(os.getenv("LATE_DROP_MIN",  "20"))           # 20 мин — «застарелое»
    LATE_DROP_SEC   = LATE_DROP_MIN * 60

    logger.info(
        f"[router] inbox user={user_id} uid={message_uid} ts={message_ts} "
        f"last_uid={last_uid} last_ts={last_seen} hash={msg_hash[:7]}"
    )

    # 3) «застарелое» сообщение (Meta прислала вне очереди и сильно старое)
    if message_ts and last_seen and (message_ts < (last_seen - LATE_DROP_SEC)) and not force_stage:
        lag_sec = int(last_seen - message_ts)
        logger.info(
            f"[router] drop late message user={user_id} lag={lag_sec}s "
            f"(threshold={LATE_DROP_SEC}s)"
        )
        return

    # Зафиксируем текущие метки сразу (даже если блок упадёт — будет прогресс)
    update_state(user_id, {
        "last_msg_uid":  message_uid or last_uid,
        "last_msg_hash": msg_hash,
        "last_msg_ts":   message_ts or now,
    })

    # --------- определим stage и обновим sender ----------------------------
    stage = force_stage or state.get("stage", "block1")

    update_state(user_id, {
        "last_sender": "bot" if force_stage else "user"
    })

    logger.info(f"📍 route_message → user={user_id} stage={stage}")

    # -------- канал для сообщений Арсению ----------------------------------
    OWNER_WA_ID = "787057065073"
    send_owner_text  = lambda body: send_text(OWNER_WA_ID, body)
    def send_owner_media(media_id: str):
        try:
            send_image(OWNER_WA_ID, media_id)
        except Exception as e_img:
            logger.warning(f"[router] send_owner_media image failed ({e_img}); retry as document")
            try:
                send_document(OWNER_WA_ID, media_id)
            except Exception as e_doc:
                logger.error(f"[router] send_owner_media document also failed: {e_doc}")

    # -------- выбираем handler для блока -----------------------------------
    if stage == "block2":
        if force_stage:
            handler = block_02.handle_block2
            logger.info(f"📍 router: запускаем handle_block2 (бот инициирует)")
        else:
            handler = block_02.handle_block2_user_reply
            logger.info(f"📍 router: запускаем handle_block2_user_reply (ответ клиента)")
    else:
        mod, handler_name = BLOCK_MAP.get(stage, BLOCK_MAP["block1"])
        handler = getattr(mod, handler_name)

    # -------- вызов handler -------------------------------------------------
    try:
        if stage == "block4":
            handler(message_text, user_id, send_text_func, send_document_func, send_video_func)
        elif stage == "block9":
            handler(message_text, user_id, send_text_func, send_owner_text, send_owner_media)
        else:
            sig = inspect.signature(handler)
            args = [message_text, user_id, send_text_func]
            if len(sig.parameters) >= 4:
                args.append(time.time())  # client_request_date
            handler(*args)
    except Exception as e:
        logger.exception(f"💥 Ошибка в блоке {stage} для {user_id}: {e}")
        send_text_func("Произошла техническая ошибка, попробуйте позже.")

