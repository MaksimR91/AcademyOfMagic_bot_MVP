from utils.env_loader import ensure_env_loaded
ensure_env_loaded()

import time
import requests
import os

from utils.ask_openai import ask_openai
from state.state import get_state, update_state
from utils.wants_handover_ai import wants_handover_ai
from utils.whatsapp_senders import (
    send_owner_mvp_summary,     # НОВАЯ: под summary_owner_mvp_version
    send_image,         # для фото
)
from logger import logger


GLOBAL_PROMPT = "prompts/global_prompt.txt"
STAGE_PROMPT = "prompts/block05_prompt.txt"
OWNER_WA_ID = os.getenv("OWNER_WA_ID")  # в тестах может быть пусто


# ---------------------------------------------------------------------------
def _load(p: str) -> str:
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
def handle_block5(
    message_text: str,
    user_id: str,
    send_text_func,         # клиенту
    send_owner_text,        # Арсению (текст)
    send_owner_media=None,  # Арсению (медиа), опционально
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
    logger.info("[block5] arseniy_notified flag: %s", st.get("arseniy_notified"))
    if not st.get("arseniy_notified"):
        reason = st.get("handover_reason", "")
        comment = _reason_to_comment(reason)
        # Собираем параметры под шаблон summary_owner_mvp_version
        params10 = _build_mvp_params(st, comment)

        # Постоянная подпись для Арсения (всегда одинаковая)
        # формируем две переменные для шаблона
        try:
            # MVP: одно шаблонное сообщение с 10 параметрами
            logger.info("[block5] owner resume (params10): %s", params10)
            resp = send_owner_mvp_summary(params10)
            logger.info("[block5] resume WA-status=[%s] user=%s",
                        getattr(resp, "status_code", "?"), user_id)
            if getattr(resp, "status_code", 0) // 100 == 2:
                update_state(user_id, {"arseniy_notified": True})
        except Exception as e:
            logger.error("[block5] failed to send owner summary: %s", e)

        # --- 1a. Фото именинника -------------------------------------
        if st.get("celebrant_photo_id"):
            _forward_and_persist_photo(
                st["celebrant_photo_id"],
                user_id,
                _send_owner_image,  # враппер с фиксированной сигнатурой
            )

    # --- 2. Сообщение клиенту (если ещё не уведомили) ---------------
    if not st.get("client_notified_about_handover"):
        try:
            prompt = (
                _load(GLOBAL_PROMPT)
                + "\n\n"
                + _load(STAGE_PROMPT)
                + "\n\nСИТУАЦИЯ: бот передаёт диалог Арсению. Сформируй короткое дружелюбное сообщение: "
                "поблагодари, скажи что Арсений свяжется при необходимости, заверши позитивно."
            )
            txt = ask_openai(prompt).strip()
        except Exception:
            txt = (
                "Спасибо! Передал информацию Арсению – он посмотрит детали и свяжется "
                "с вами при необходимости. Хорошего дня!"
            )
        send_text_func(txt)
        update_state(
            user_id,
            {
                "client_notified_about_handover": True,
                "last_message_ts": time.time(),
            },
        )

    # --- 3. Переход к block10 (CRM) ---------------------------------
    _goto(user_id, "block6")


# ---------------------------------------------------------------------------
def _pick(snap, st, key, default=""):
    """
    По умолчанию раньше брали из structured_cache → потом из state.
    Но после апсерта state содержит более актуальные склейки (например, "Жених ... невеста ...").
    Разворачиваем приоритет: сперва state, затем снапшот.
    """
    if str(st.get(key, "")).strip():
        return st.get(key)
    if snap and str(snap.get(key, "")).strip():
        return snap.get(key)
    return default


def _build_summary(st: dict, comment: str) -> str:
    snap = st.get("structured_cache") or {}

    def _is_nonempty(val) -> bool:
        if val is None:
            return False
        if isinstance(val, str):
            return val.strip() != ""
        return True

    def _yes_no(val):
        if val is True:
            return "Да"
        if val is False:
            return "Нет"
        return ""

    # дата/время: сначала нормализованные (если есть), потом как раньше
    date_iso = _pick(snap, st, "event_date_iso", "")
    time_24 = _pick(snap, st, "event_time_24", "")
    date_time = ""
    if date_iso and time_24:
        date_time = f"{date_iso} {time_24}"
    else:
        # старый способ (чтобы не ломать существующие кейсы)
        dt_raw = _pick(snap, st, "event_date", "")
        tm_raw = _pick(snap, st, "event_time", "")
        date_time = (dt_raw + " " + tm_raw).strip()

    phone = st.get("normalized_number") or st.get("client_phone") or ""

    has_photo = "Да" if st.get("celebrant_photo_id") else "Нет"

    celebrant_name = _pick(snap, st, "celebrant_name")
    celebrant_age = _pick(snap, st, "celebrant_age")
    guests_count = _pick(snap, st, "guests_count")
    guests_age_adult = _pick(snap, st, "guests_age")               # для взрослого шоу
    guests_age_kids = _pick(snap, st, "guests_children_age")       # для детского/семейного шоу

    # выбор источника возраста гостей по типу шоу
    show_type_lc = (st.get("show_type") or "").strip().lower()
    guests_age_line = ""
    if show_type_lc.startswith("взрос"):
        guests_age_line = guests_age_adult or ""
    elif show_type_lc.startswith("дет") or show_type_lc.startswith("сем"):
        guests_age_line = guests_age_kids or ""
    else:
        # если тип не распознан — берём что-то одно, что есть
        guests_age_line = guests_age_adult or guests_age_kids or ""

    # логируем ключевые поля до сборки резюме
    try:
        logger.info(
            "[block5] summary fields user=%s date_time='%s' event_location='%s' "
            "celebrant_name='%s' celebrant_age='%s' guests_count='%s'",
            st.get("user_id") or "?",
            date_time,
            (_pick(snap, st, "event_location") or st.get("event_location", "")),
            celebrant_name,
            celebrant_age,
            guests_count,
        )
    except Exception:
        pass

    # лёгкая нормализация кавычек в адресе для резюме
    # Берём именно event_location (может содержать и место, и адрес), приоритет: state → structured_cache
    event_loc = _pick(snap, st, "event_location") or (st.get("event_location") or "")
    event_loc = str(event_loc).replace('"""', '"').replace("''", "'")

    # Нормализованный формат мероприятия (если есть)
    fmt = _pick(snap, st, "event_format") or (st.get("format") or "")

    # Флаги
    no_celebrant = str(st.get("no_celebrant") or "").strip().lower() in {
        "да",
        "yes",
        "true",
        "1",
        "y",
    }

    # Поля с «да/нет»
    payment_status = _yes_no(st.get("payment_valid")) if "payment_valid" in st else ""
    saw_before = _yes_no(st.get("saw_show_before")) if "saw_show_before" in st else ""
    children_client = ""
    if isinstance(st.get("client_children_attend"), bool):
        children_client = _yes_no(st.get("client_children_attend"))
    elif _is_nonempty(st.get("client_children_attend")):
        children_client = str(st.get("client_children_attend"))

    # Компоновщик: добавляет строку только если значение непустое
    lines = []

    def add(label: str, value):
        if _is_nonempty(value):
            lines.append(f"{label}: {value}")

    add("Этап сценария", st.get("scenario_stage_at_handover") or st.get("stage", ""))
    add("Имя клиента", st.get("client_name", ""))
    add("Телефон клиента", phone)
    add("Тип шоу", st.get("show_type", ""))
    add("Формат мероприятия", fmt)
    add("Дата, время", date_time)
    add("Адрес", event_loc)

    # Блок по имениннику — только если он есть
    if not no_celebrant:
        add("Имя виновника торжества", celebrant_name)
        add("Возраст виновника", celebrant_age)

    # Гости
    add("Количество гостей", guests_count)
    add("Возраст гостей", guests_age_line)

    # Оплата (только если что-то известно)
    add("Внесена ли предоплата", payment_status)
    add("Сумма предоплаты (тенге)", st.get("payment_amount", ""))

    # Прочее (только непустые)
    add("Будут ли дети клиента", children_client)
    add("Видел(а) шоу раньше?", saw_before)
    add("Есть фото именинника", has_photo if _is_nonempty(has_photo) else "")
    add("Причина отказа", st.get("decline_reason"))
    add("Особенности/пожелания", st.get("special_wishes"))
    add("Комментарий", comment)

    # Возвращаем финальный текст БЕЗ шапки и с CRLF для надёжных переносов в WhatsApp
    result = "\r\n".join(lines)
    return result

# ─────────────────────────────────────────────────────────────────
# ПОД MVP-Шаблон (summary_owner_mvp_version): собираем 10 параметров {{1}}..{{10}}
def _build_mvp_params(st: dict, comment: str) -> list[str]:
    """
    Порядок строго соответствует шаблону:
      1) Этап сценария
      2) Имя и номер телефона клиента (одной строкой)
      3) Тип шоу
      4) Формат мероприятия
      5) Дата, время
      6) Адрес
      7) Имя виновника торжества
      8) Возраст виновника
      9) Количество гостей
     10) Возраст гостей
    Пустые значения допустимы — отправятся как "—" (обрабатывается в send_owner_mvp_summary).
    """
    snap = st.get("structured_cache") or {}

    def _pick(snap, st, key, default=""):
        if str(st.get(key, "")).strip():
            return st.get(key)
        if snap and str(snap.get(key, "")).strip():
            return snap.get(key)
        return default

    # Дата/время — как в _build_summary
    date_iso = _pick(snap, st, "event_date_iso", "")
    time_24  = _pick(snap, st, "event_time_24", "")
    if date_iso and time_24:
        date_time = f"{date_iso} {time_24}"
    else:
        dt_raw = _pick(snap, st, "event_date", "")
        tm_raw = _pick(snap, st, "event_time", "")
        date_time = (dt_raw + " " + tm_raw).strip()

    event_loc = _pick(snap, st, "event_location") or (st.get("event_location") or "")
    event_loc = str(event_loc).replace('"""', '"').replace("''", "'")

    fmt = _pick(snap, st, "event_format") or (st.get("format") or "")

    phone = st.get("normalized_number") or st.get("client_phone") or ""
    user_name = st.get("client_name", "") or st.get("user_name", "")
    name_phone = f"{user_name} ({phone})".strip() if (user_name or phone) else ""

    celebrant_name = _pick(snap, st, "celebrant_name")
    celebrant_age  = _pick(snap, st, "celebrant_age")
    guests_count   = _pick(snap, st, "guests_count")
    guests_age_adult = _pick(snap, st, "guests_age")
    guests_age_kids  = _pick(snap, st, "guests_children_age")

    show_type_lc = (st.get("show_type") or "").strip().lower()
    if show_type_lc.startswith("взрос"):
        guests_age_line = guests_age_adult or ""
    elif show_type_lc.startswith("дет") or show_type_lc.startswith("сем"):
        guests_age_line = guests_age_kids or ""
    else:
        guests_age_line = guests_age_adult or guests_age_kids or ""

    params10 = [
        (st.get("scenario_stage_at_handover") or st.get("stage", "")) or "",
        name_phone,
        st.get("show_type", "") or "",
        fmt or "",
        date_time or "",
        event_loc or "",
        celebrant_name or "",
        celebrant_age or "",
        guests_count or "",
        guests_age_line or "",
    ]
    return params10


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


# враппер: приводит сигнатуру к send_owner_media(media_id)
def _send_owner_image(media_id: str) -> None:
    """
    Отправляет изображение Арсению, скрывая параметр получателя.
    В тестах OWNER_WA_ID может отсутствовать — используем безопасный дефолт.
    """
    to = OWNER_WA_ID or "OWNER"
    try:
        send_image(to, media_id)
    except Exception as e:
        logger.warning(f"[block5] send_image fail: {e}")


# ⬇︎ помощник: скачиваем из WhatsApp, кладём в S3, шлём Арсению
def _forward_and_persist_photo(media_id: str, user_id: str, send_owner_media):
    """
    • шлём фото Арсению (image/document)
    • перекладываем в S3 и сохраняем постоянную ссылку в state
    Выполняем ОДИН раз — если уже есть celebrant_photo_url, пропускаем.
    """
    from state.state import get_state, update_state

    st = get_state(user_id) or {}

    # --- 0. отправляем Арсению (может упасть, не критично) -------
    if send_owner_media:
        try:
            send_owner_media(media_id)
        except Exception as e:
            logger.warning(f"[block5] send_owner_media fail: {e}")

    # --- 1. если уже сохранена постоянная ссылка — выход ----------
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
        img_resp = requests.get(
            file_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        img_resp.raise_for_status()
    except Exception as e:
        logger.error(f"[block5] cannot fetch media {media_id}: {e}")
        return

    # --- 3. кладём в S3 -------------------------------------------
    try:
        # импорт внутри функции, чтобы моки через sys.modules подхватывались
        from importlib import import_module

        s3 = import_module("utils.s3_upload")
        perm_url = s3.upload_image(img_resp.content)
        update_state(user_id, {"celebrant_photo_url": perm_url})
        logger.info(f"[block5] photo uploaded → {perm_url} user={user_id}")
    except Exception as e:
        logger.error(f"[block5] S3 upload failed: {e}")


# ---------------------------------------------------------------------------
def _goto(user_id: str, next_stage: str):
    update_state(user_id, {"stage": next_stage, "last_message_ts": time.time()})
    from router import route_message

    route_message("", user_id, force_stage=next_stage)
