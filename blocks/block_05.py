from utils.env_loader import ensure_env_loaded
ensure_env_loaded()
import time
from utils.s3_upload import upload_image
import requests, os
from utils.ask_openai import ask_openai
from state.state import get_state, update_state
from utils.wants_handover_ai import wants_handover_ai
from utils.whatsapp_senders import (
    send_owner_resume,      # единственная отправка резюме
    send_image,             # для фото
 )
from logger import logger

GLOBAL_PROMPT = "prompts/global_prompt.txt"
STAGE_PROMPT  = "prompts/block05_prompt.txt"

# ---------------------------------------------------------------------------
def _load(p: str) -> str:
    with open(p, "r", encoding="utf-8") as f:
        return f.read()

# ---------------------------------------------------------------------------
def handle_block5(
    message_text: str,
    user_id: str,
    send_text_func,          # клиенту
    send_owner_text,         # Арсению (текст)
    send_owner_media=None,   # Арсению (медиа), опционально
):
    """
    Универсальный hand-over: формируем расширенное резюме и передаём
    Арсению. Вызывается force_stage='block5' из любого блока.
    """
    if wants_handover_ai(message_text):
        # уже в процессе передачи — игнорируем повторную просьбу
        pass

    st = get_state(user_id) or {}
    # Если не зафиксировали этап для CRM – фиксируем текущий
    if not st.get("scenario_stage_at_handover"):
        update_state(user_id, {"scenario_stage_at_handover": st.get("stage")})
    # --- 1. Отправка резюме Арсению (однократно) ---------------------
    logger.info("[block5] arseni_notified flag: %s", st.get("arseniy_notified"))
    if not st.get("arseniy_notified"):
        reason  = st.get("handover_reason", "")
        comment = _reason_to_comment(reason)
        summary = _build_summary(st, comment)
        # Постоянная подпись для Арсения (всегда одинаковая)
        # формируем две переменные для шаблона
        try:
            # Одним вызовом: сам разрежет и пошлёт несколько template-частей
            wa_resps = send_owner_resume(summary)   # list[requests.Response]
            statuses = [getattr(r, "status_code", "?") for r in wa_resps]
            logger.info("[block5] resume WA-status=%s user=%s", statuses, user_id)
            if any(getattr(r, "status_code", 0) // 100 == 2 for r in wa_resps):
                update_state(user_id, {"arseniy_notified": True})
        except Exception as e:
            logger.error("[block5] failed to send owner summary: %s", e)

        # --- 1a. Фото именинника -------------------------------------
        if st.get("celebrant_photo_id"):
            _forward_and_persist_photo(
                st["celebrant_photo_id"],
                user_id,
                send_image,          # отправляем фото Арсению
            )

    # --- 2. Сообщение клиенту (если ещё не уведомили) ---------------
    if not st.get("client_notified_about_handover"):
        try:
            prompt = (
                _load(GLOBAL_PROMPT) + "\n\n" + _load(STAGE_PROMPT) +
                "\n\nСИТУАЦИЯ: бот передаёт диалог Арсению. Сформируй короткое дружелюбное сообщение: "
                "поблагодари, скажи что Арсений свяжется при необходимости, заверши позитивно."
            )
            txt = ask_openai(prompt).strip()
        except Exception:
            txt = ("Спасибо! Передал информацию Арсению – он посмотрит детали и свяжется с вами при необходимости. "
                   "Хорошего дня!")
        send_text_func(txt)
        update_state(user_id, {
            "client_notified_about_handover": True,
            "last_message_ts": time.time(),
        })

    # --- 3. Переход к block10 (CRM) ---------------------------------
    _goto(user_id, "block6")

# ---------------------------------------------------------------------------
def _pick(snap, st, key, default=""):
    # сначала из снепшота, если есть и непусто; иначе из state
    if snap and str(snap.get(key, "")).strip():
        return snap[key]
    return st.get(key, default)

def _build_summary(st: dict, comment: str) -> str:
    snap = st.get("structured_cache") or {}

    def _yes_no(val):
        if val is True:  return "Да"
        if val is False: return "Нет"
        return ""

    # дата/время: сначала нормализованные (если есть), потом как раньше
    date_iso = _pick(snap, st, "event_date_iso", "")
    time_24  = _pick(snap, st, "event_time_24", "")
    date_time = ""
    if date_iso and time_24:
        date_time = f"{date_iso} {time_24}"
    else:
        # старый способ (чтобы не ломать существующие кейсы)
        dt_raw = _pick(snap, st, "event_date", "")
        tm_raw = _pick(snap, st, "event_time", "")
        date_time = (dt_raw + " " + tm_raw).strip()

    payment_status = ""
    if "payment_valid" in st:
        payment_status = _yes_no(st.get("payment_valid"))
    amount = st.get("payment_amount") or ""

    saw_before = ""
    if "saw_show_before" in st:
        saw_before = _yes_no(st.get("saw_show_before"))

    phone = (
        st.get("normalized_number")
        or st.get("client_phone")
        or ""
    )

    has_photo = "Да" if st.get("celebrant_photo_id") else "Нет"

    children_client = ""
    raw_children = st.get("client_children_attend")
    if isinstance(raw_children, bool):
        children_client = _yes_no(raw_children)
    elif raw_children:
        children_client = str(raw_children)

    lines = [
        "📄 *Резюме для Арсения*",
        f"Этап сценария: {st.get('stage','')}",
        f"Имя клиента: {st.get('client_name','')}",
        f"Телефон клиента: {phone}",
        f"Тип шоу: {st.get('show_type','')}",
        f"Формат мероприятия: {st.get('event_description','')}",
        f"Выбранный пакет: {st.get('package','')}",
        f"Дата, время: {date_time}",
        f"Адрес: {st.get('address','')}",
        f"Имя виновника торжества: {_pick(snap, st, 'celebrant_name')}",
        f"Возраст виновника: {_pick(snap, st, 'celebrant_age')}",
        f"Количество гостей: {_pick(snap, st, 'guests_count')}",
        f"Пол гостей: {st.get('guests_gender','')}",
        f"Внесена ли предоплата: " + (_yes_no(st.get('payment_valid')) if 'payment_valid' in st else ""),
        f"Сумма предоплаты (тенге): {st.get('payment_amount','')}",
        f"Будут ли дети клиента: " + (_yes_no(st.get('client_children_attend')) if isinstance(st.get('client_children_attend'), bool) else str(st.get('client_children_attend') or "")),
        f"Видел(а) шоу раньше?: " + (_yes_no(st.get('saw_show_before')) if 'saw_show_before' in st else ""),
        f"Есть фото именинника: {has_photo}",
    ]

    if st.get("decline_reason"):
        lines.append(f"Причина отказа: {st.get('decline_reason')}")
    if st.get("special_wishes"):
        lines.append(f"Особенности/пожелания: {st.get('special_wishes')}")
    lines.append(f"Комментарий: {comment}")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
def _reason_to_comment(reason: str) -> str:
    mapping = {
        "asked_handover": "Клиент попросил живое общение.",
        "early_date_or_busy": "Срочная дата или слот занят – нужна ручная проверка.",
        "non_standard_show": "Нестандартный формат шоу – нужна консультация.",
        "objection_not_resolved": "Не удалось закрыть возражение.",
        "client_declined": "Клиент отказался от заказа.",
        "payment_invalid": "Не удалось подтвердить оплату / сомнительный чек.",
        "missing_required_fields": "Не удалось собрать обязательные данные.",
        "cannot_resolve_resume": "Не удалось согласовать резюме (нет деталей).",
        "unclear_in_block8": "Непонятный ответ при подтверждении резюме.",
        "confirmed_booking": "Все данные получены – заказ зафиксирован.",
        "no_response_after_7_2": "Молчание после двух напоминаний этапа 7.",
        "no_response_after_8_2": "Молчание после двух напоминаний этапа 8.",
        "reserve_failed": "Не удалось подтвердить слот расписания.",
    }
    return mapping.get(reason, reason or "")
# ---------------------------------------------------------------------------
# ⬇︎ помощник: скачиваем из WhatsApp, кладём в S3, шлём Арсению
def _forward_and_persist_photo(media_id: str, user_id: str, send_owner_media):
    """
    • шлём фото Арсению (image/document)
    • перекладываем в S3 и сохраняем постоянную ссылку в state
    Выполняем ОДИН раз — если уже есть celebrant_photo_url, пропускаем.
    """
    from state.state import get_state, update_state
    st = get_state(user_id) or {}

    # --- 0. отправляем Арсению (может упасть, не критично) -------
    if send_owner_media:
        try:
            send_owner_media(media_id)
        except Exception as e:
            logger.warning(f"[block5] send_owner_media fail: {e}")

    # --- 1. если уже сохранена постоянная ссылка — выход ----------
    if st.get("celebrant_photo_url"):
        return

    # --- 2. запрашиваем временный URL у Meta ----------------------
    token = os.getenv("WHATSAPP_TOKEN") or st.get("wa_token")  # fallback
    try:
        meta = requests.get(
            f"https://graph.facebook.com/v17.0/{media_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        ).json()
        file_url = meta["url"]
        img_resp = requests.get(file_url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
        img_resp.raise_for_status()
    except Exception as e:
        logger.error(f"[block5] cannot fetch media {media_id}: {e}")
        return

    # --- 3. кладём в S3 -------------------------------------------
    try:
        perm_url = upload_image(img_resp.content)
        update_state(user_id, {"celebrant_photo_url": perm_url})
        logger.info(f"[block5] photo uploaded → {perm_url} user={user_id}")
    except Exception as e:
        logger.error(f"[block5] S3 upload failed: {e}")

# ---------------------------------------------------------------------------
def _goto(user_id: str, next_stage: str):
    update_state(user_id, {"stage": next_stage, "last_message_ts": time.time()})
    from router import route_message
    route_message("", user_id, force_stage=next_stage)