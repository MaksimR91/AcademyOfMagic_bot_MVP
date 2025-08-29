# blocks/block_06.py
import os
import time
from utils.reminder_engine import plan
from notion_client import Client, APIResponseError
from state.state import get_state, update_state
from logger import logger

"""
Block 6: Финальный экспорт данных в Notion CRM.

ЛОГИКА:
  • Берём всё накопленное в state к моменту handover (после block5).
  • Определяем «достигнутый этап» (scenario_stage_at_handover -> человеко-читабельное имя) и пишем его в поле ЭТАП.
  • Поле ОТКАЗ заполняем только при реальном отказе / потере лида.
  • Причину handover (handover_reason) помещаем в поле 'дополнение' как пояснение + прочие детали.
  • Пытаемся создать запись в Notion. При ошибке — до MAX_RETRY_COUNT повторных попыток каждые RETRY_DELAY_SECONDS.
  • Клиенту НИЧЕГО не отправляем.
"""

# ───────────────── Настройки ретраев ────────────────────────────────────────
RETRY_DELAY_SECONDS = 600          # 10 минут между попытками
MAX_RETRY_COUNT     = 5

# ───────────────── Карта технических стадий → бизнес-названий ───────────────
SCENARIO_STAGE_MAP = {
    "block1":  "Приветствие",
    "block2":  "Сбор информации",
    "block3a": "Получение информации",
    "block3b": "Получение информации",
    "block3c": "Получение информации",
    "block3d": "Получение информации",
    "block4":  "Отправка материалов",
    "block5":  "Ручная обработка заказа",
    "block6": "CRM",
}

# ───────────────── Причины handover (для комментариев / отказов) ────────────
HANDOVER_REASON_HUMAN = {
    "asked_handover":          "Клиент попросил живое общение.",
    "early_date_or_busy":      "Срочная дата или слот занят.",
    "non_standard_show":       "Нестандартный формат шоу.",
    "objection_not_resolved":  "Не удалось закрыть возражение.",
    "client_declined":         "Клиент отказался от заказа.",
    "payment_invalid":         "Сомнительный или неподтверждённый платёж.",
    "missing_required_fields": "Не удалось собрать обязательные поля.",
    "cannot_resolve_resume":   "Не удалось согласовать резюме.",
    "unclear_in_block8":       "Непонятный ответ при подтверждении резюме.",
    "confirmed_booking":       "Бронь подтверждена (все данные собраны).",
    "no_response_after_2_2":   "Молчание после напоминаний этапа 2.",
    "no_response_after_3_2":   "Молчание после напоминаний этапа 3.",
    "no_response_after_4_2":   "Молчание после напоминаний этапа 4.",
    "no_response_after_5_2":   "Молчание после напоминаний этапа 5.",
    "no_response_after_7_2":   "Молчание после напоминаний этапа 7.",
    "no_response_after_8_2":   "Молчание после напоминаний этапа 8.",
    "reserve_failed":          "Не удалось подтвердить слот расписания.",
}

# ───────────────── Причины, трактуемые как реальный отказ / потеря лида ─────
IMPLICIT_REFUSAL_REASONS = {
    "client_declined",
    "no_response_after_2_2",
    "no_response_after_3_2",
    "no_response_after_4_2",
    "no_response_after_5_2",
    "no_response_after_7_2",
    "no_response_after_8_2",
}

SILENT_REFUSAL_REASONS = {
    "no_response_after_2_2",
    "no_response_after_3_2",
    "no_response_after_4_2",
    "no_response_after_5_2",
    "no_response_after_7_2",
    "no_response_after_8_2",
}

# ----------------------------------------------------------------------------
def _handover_comment(reason: str | None) -> str:
    """Текстовое описание причины для поля 'дополнение'."""
    if not reason:
        return ""
    return HANDOVER_REASON_HUMAN.get(reason, "")

def _combine_date_time(date_str: str | None, time_str: str | None) -> str | None:
    """
    Собираем ISO start. Если только дата — отдаём дату.
    Если есть время в формате HH:MM — дополняем ":00".
    """
    if not date_str and not time_str:
        return None
    if date_str and time_str:
        t = time_str
        if len(t) == 5:  # HH:MM
            t += ":00"
        return f"{date_str}T{t}"
    return date_str or None

# ----------------------------------------------------------------------------
def _build_notion_properties(st: dict) -> dict:
    """
    Формирует словарь properties для создания страницы в Notion.
    Пустые значения пропускаем.
    """
    props = {}

    phone       = st.get("normalized_number") or st.get("raw_number") or ""
    client_name = st.get("client_name") or ""
    name_combined = (phone + " " + client_name).strip() or phone or client_name or "Клиент"

    # --- Name (title) -------------------------------------------------------
    props["Name"] = {"title": [{"text": {"content": name_combined[:200]}}]}

    # --- с кем переговоры ---------------------------------------------------
    props["с кем переговоры"] = {
        "rich_text": [{"text": {"content": name_combined[:200]}}]
    }

    # --- ЭТАП (status) ------------------------------------------------------
    scenario_stage_code = st.get("scenario_stage_at_handover") or st.get("stage")
    stage_human = SCENARIO_STAGE_MAP.get(scenario_stage_code, "Не указано")
    # если это неявный отказ – фиксируем его в ЭТАП
    if st.get("handover_reason") in SILENT_REFUSAL_REASONS:
        props["ЭТАП"] = {"status": {"name": "Отказ (молчание клиента)"}}
    else:
        props["ЭТАП"] = {"status": {"name": stage_human}}

    # --- ОТКАЗ (multi_select) ----------------------------------------------
    reason = st.get("handover_reason")
    decline_text = st.get("decline_reason")
    if decline_text:
        props["ОТКАЗ"] = {"multi_select": [{"name": decline_text[:80]}]}
    elif reason in IMPLICIT_REFUSAL_REASONS:
        # Для неявных отказов используем человеко‑читабельную формулировку причины handover
        human_reason = HANDOVER_REASON_HUMAN.get(reason, "Отказ")
        if human_reason:
            props["ОТКАЗ"] = {"multi_select": [{"name": human_reason[:80]}]}
    # --- Для кого праздник --------------------------------------------------
    celebrant_name = st.get("celebrant_name") or ""
    celebrant_age  = st.get("celebrant_age") or ""
    celebrant_line = ""
    if celebrant_name and celebrant_age:
        celebrant_line = f"{celebrant_name}, {celebrant_age} лет"
    elif celebrant_name:
        celebrant_line = celebrant_name
    elif celebrant_age:
        celebrant_line = f"{celebrant_age} лет"
    if celebrant_line:
        props["Для кого праздник"] = {
            "rich_text": [{"text": {"content": celebrant_line[:200]}}]
        }

    # --- Программа (пакет) --------------------------------------------------
    package = st.get("package")
    if package:
        props["Программа"] = {"multi_select": [{"name": package[:60]}]}

   # --- Когда (дата/время) -------------------------------------------------
    # 1) сначала берём нормализованные поля, которые кладёт block3c
    snap = st.get("structured_cache") or {}
    date_iso = (
        st.get("event_date_iso")
        or snap.get("event_date_iso")
    )

    # время: берём 24ч формат, либо что есть (попробуем почистить)
    time_24 = st.get("event_time_24") or snap.get("event_time_24") or st.get("event_time")

    # легкая нормализация времени до HH:MM
    if time_24:
        import re
        m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", str(time_24))
        time_24 = f"{m.group(1)}:{m.group(2)}" if m else None

    dt_iso = _combine_date_time(date_iso, time_24)
    if dt_iso:
        props["Когда"] = {"date": {"start": dt_iso}}

    # --- адрес --------------------------------------------------------------
    address = st.get("address")
    if address:
        props["адрес"] = {"rich_text": [{"text": {"content": address[:400]}}]}

    # --- ФОРМАТ -------------------------------------------------------------
    # Используем event_description как формат
    event_format = st.get("event_description") or st.get("format")
    if event_format:
        props["ФОРМАТ"] = {"multi_select": [{"name": event_format[:80]}]}

    # --- ТИП МЕРОПРИЯТИЯ (из show_type) -------------------------------------
    show_type = st.get("show_type")
    if show_type:
        props["ТИП МЕРОПРИЯТИЯ"] = {"multi_select": [{"name": show_type[:80]}]}

    # --- предоплата ---------------------------------------------------------
    payment_valid = st.get("payment_valid")
    amount = st.get("payment_amount") or ""
    prepay_line = ""
    if payment_valid is True:
        prepay_line = "Да"
    elif payment_valid is False:
        prepay_line = "Нет"
    if amount:
        prepay_line = (prepay_line + ", " if prepay_line else "") + str(amount)
    if prepay_line:
        props["предоплата"] = {"rich_text": [{"text": {"content": prepay_line[:120]}}]}

    # --- дополнение ---------------------------------------------------------
    extra_parts = []
    comment = _handover_comment(reason)
    if comment:
        extra_parts.append(comment)
    if st.get("celebrant_photo_id"):
        extra_parts.append("Фото именинника получено.")
    if st.get("special_wishes"):
        extra_parts.append(f"Пожелания: {st['special_wishes']}")
    # Можно добавить любые служебные флаги при необходимости
    if extra_parts:
        props["дополнение"] = {
            "rich_text": [{"text": {"content": "\n".join(extra_parts)[:1900]}}]
        }

    return props

# ----------------------------------------------------------------------------
def retry_export(user_id: str):
    """Внешняя функция — нужна APScheduler для импорта."""
    from router import route_message
    route_message("", user_id, force_stage="block6")
    logger.info(f"[block6] scheduled retry in {RETRY_DELAY_SECONDS}s user={user_id}")
    
def _schedule_retry(user_id: str):
    plan(user_id, "blocks.block_6:retry_export", RETRY_DELAY_SECONDS)


# ----------------------------------------------------------------------------
def handle_block6(message_text: str, user_id: str, send_text_func):
    """
    Финальный этап: выгрузка данных в Notion CRM.
    Никаких сообщений клиенту не отправляем.
    """
    st = get_state(user_id) or {}

    # Уже выгружено — ничего не делаем
    if st.get("notion_exported"):
        logger.info(f"[block6] already exported user={user_id} page={st.get('notion_page_id')}")
        return

    notion_key = os.getenv("NOTION_API_KEY")
    db_id      = os.getenv("NOTION_CRM_DATABASE_ID")
    if not notion_key or not db_id:
        logger.error("[block6] NOTION_API_KEY or NOTION_CRM_DATABASE_ID missing")
        update_state(user_id, {"notion_export_error": True})
        return  # ретраи бессмысленны без ключей

    props = _build_notion_properties(st)
    if "Name" not in props:
        logger.error("[block6] No 'Name' property built — abort export")
        update_state(user_id, {"notion_export_error": True})
        return

    retry_count = st.get("notion_retry_count", 0)
    client = Client(auth=notion_key)

    try:
        resp = client.pages.create(parent={"database_id": db_id}, properties=props)
        page_id = resp["id"]

        # ­‑‑‑ прикрепляем фото именинника (если есть постоянная ссылка) ----
        if st.get("celebrant_photo_url"):
            try:
                client.blocks.children.append(
                    page_id,
                    children=[
                        {
                            "object": "block",
                            "type": "image",
                            "image": {
                                "type": "external",
                                "external": {"url": st["celebrant_photo_url"]},
                            },
                        }
                    ],
                )
                logger.info(f"[block6] image block appended user={user_id}")
            except Exception as e:
                logger.warning(f"[block6] cannot append image block: {e}")
        update_state(user_id, {
            "notion_exported": True,
            "notion_page_id": page_id,
            "notion_export_error": False,
            "last_message_ts": time.time()
        })
        logger.info(f"[block6] Notion export SUCCESS user={user_id} page={page_id}")
    except APIResponseError as e:
        logger.error(f"[block6] Notion API error user={user_id}: {e}")
        _handle_export_failure(user_id, retry_count)
    except Exception as e:
        logger.error(f"[block6] Unexpected export error user={user_id}: {e}")
        _handle_export_failure(user_id, retry_count)

# ----------------------------------------------------------------------------
def _handle_export_failure(user_id: str, retry_count: int):
    """
    Обработка неудачной попытки экспорта.
    """
    if retry_count + 1 >= MAX_RETRY_COUNT:
        logger.error(f"[block6] Max retries reached ({MAX_RETRY_COUNT}) user={user_id} — giving up")
        update_state(user_id, {
            "notion_export_error": True,
            "notion_retry_count": retry_count + 1,
            "last_message_ts": time.time()
        })
        return

    update_state(user_id, {
        "notion_export_error": True,
        "notion_retry_count": retry_count + 1,
        "last_message_ts": time.time()
    })
    logger.info(f"[block6] will retry (attempt {retry_count + 1}) user={user_id}")
    _schedule_retry(user_id)