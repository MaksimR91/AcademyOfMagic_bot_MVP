import re
import json
import time
from utils.reminder_engine import plan
from utils.materials import s3, S3_BUCKET
from utils.ask_openai import ask_openai
from utils.wants_handover_ai import wants_handover_ai
from state.state import get_state, update_state, save_if_absent
from logger import logger

# ---- константы и пути ------------------------------------------------------
GLOBAL_PROMPT_PATH  = "prompts/global_prompt.txt"
STAGE_PROMPT_PATH   = "prompts/block04_prompt.txt"
REMINDER_1_PROMPT_PATH = "prompts/block04_reminder_1_prompt.txt"
REMINDER_2_PROMPT_PATH = "prompts/block04_reminder_2_prompt.txt"
FOLLOWUP_PROMPT_PATH = "prompts/block04_followup_prompt.txt"
FOLLOWUP_DELAY_MIN   = 15

MEDIA_REGISTRY_KEY = "materials/media_registry.json"   # лежит в Yandex-S3
DELAY_TO_REMINDER_HOURS = 4
REMINDER_2_DELAY_HOURS = 12   # между 1-м и 2-м касанием
FINAL_TIMEOUT_HOURS    = 4    # после 2-го – в hand-over

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
    Отправляем КП + видео, задаём вопрос, ждём ответ.
    message_text пустое при первом входе из блока 3, непустое – когда клиент отвечает.
    """

    # --- хенд-овер по запросу клиента ----
    if wants_handover_ai(message_text):
        update_state(user_id, {"handover_reason": "asked_handover", "scenario_stage_at_handover": "block4"})
        from router import route_message
        return route_message(message_text, user_id, force_stage="block9")

    state = get_state(user_id) or {}
    show_type   = state.get("show_type")        # 'детское' / 'семейное' / 'взрослое'
    place_type  = state.get("place_type", "")   # 'дом', 'кафе', 'детский сад' ...
    materials_sent = state.get("materials_sent", False)

    # ========== первый заход: отправляем материалы ==========================
    if not materials_sent:
        registry   = load_media_registry()
        kp_id      = choose_kp(show_type, registry)
        video_id   = choose_video(show_type, place_type, registry)

        if kp_id:
            try_send(send_document_func, kp_id)   # PDF КП
        if video_id:
            try_send(send_video_func, video_id)   # пример шоу

        intro_text = ask_openai(
        load_prompt(GLOBAL_PROMPT_PATH) + "\n\n" + load_prompt(STAGE_PROMPT_PATH)
        )
        send_text_func(intro_text)

        materials_ts = time.time()
        follow_up_text = ask_openai(
        load_prompt(GLOBAL_PROMPT_PATH) + "\n\n" + load_prompt(FOLLOWUP_PROMPT_PATH)
        )          # будущий вопрос
        update_state(user_id, {
        **state,
        "stage": "block4",
        "materials_sent": True,
        "materials_sent_ts": materials_ts,   # <- новое
        "follow_up_text": follow_up_text,
        "follow_up_sent": False,
        "last_bot_question": intro_text,
        "last_message_ts": materials_ts,
        })

        # через 15 мин. спросим про пакет
        plan(user_id,
        "blocks.block_04:send_follow_up_if_needed",   # <‑‑ путь к функции
        FOLLOWUP_DELAY_MIN * 60)
        return

    # ========== клиент отвечает после материалов ============================

    # Простая классификация: согласие / возражение
    classification_prompt = (
        "Определите реакцию клиента.\n\n"
        f'Сообщение: "{message_text}"\n\n'
        "Если готов купить — ответьте 'yes'.\n"
        "Если есть вопросы/сомнения/возражения либо что-то иное — 'objection'.\n"

    )
    reaction = ask_openai(classification_prompt).strip().lower()

    # ───────── извлекаем данные из ответа клиента ────────────
    # простейшие регулярки → можно расширять
    extracted = {}
    low = message_text.lower()

    # выбранный пакет
    if "базовый" in low:      extracted["package"] = "базовый"
    elif "восторг" in low: extracted["package"] = "восторг"
    elif "фурор" in low:    extracted["package"] = "фурор"

    # кол-во гостей
    m = re.search(r"\b(\d{1,3})\s*(?:гост[ея]|человек|чел)\b", low)
    if m:
        extracted["guests_count"] = m.group(1)

    # любые другие поля из REQUIRED_FIELDS добавляйте аналогично ↑
    if extracted:
        save_if_absent(user_id, **extracted)

    from router import route_message
    if reaction == "yes":
        return route_message("", user_id, force_stage="block6a")
    # любое другое значение – считаем возражением
    return route_message(message_text, user_id, force_stage="block5")

def send_follow_up_if_needed(user_id, send_text_func):
    st = get_state(user_id)
    if not st or st.get("stage") != "block4" or st.get("follow_up_sent"):
        return

    # клиент что-то написал -> last_message_ts изменился
    if st.get("last_message_ts") != st.get("materials_sent_ts"):
        return

    follow_up = st.get("follow_up_text")
    if not follow_up:
        return

    send_text_func(follow_up)
    update_state(user_id, {
        "follow_up_sent": True,
        "last_bot_question": follow_up,
        "last_message_ts": time.time(),
    })

    plan(user_id,
     "blocks.block_04:send_block4_reminder_if_silent",   # <‑‑ путь к функции
     DELAY_TO_REMINDER_HOURS * 3600)

# ---- напоминание 4.1 -------------------------------------------------------

def send_block4_reminder_if_silent(user_id, send_text_func):
    state = get_state(user_id)
    if not state or state.get("stage") != "block4":
        return

    last_ts = state.get("last_message_ts", 0)
    if time.time() - last_ts < DELAY_TO_REMINDER_HOURS * 3600:
        return  # клиент всё-таки ответил

    global_prompt = load_prompt(GLOBAL_PROMPT_PATH)
    reminder_prompt = load_prompt(REMINDER_1_PROMPT_PATH)
    last_q = state.get("last_bot_question", "")
    full_prompt = (
        global_prompt + "\n\n" + reminder_prompt + f'\n\nПоследний вопрос бота: "{last_q}"'
    )
    text = ask_openai(full_prompt)
    send_text_func(text)

    update_state(user_id, {
        "stage": "block4",
        "last_message_ts": time.time(),
        "reminder1_sent": True             # флажок
    })

    # ------- ставим таймер на второе касание через 12 ч -------
    plan(user_id,
    "blocks.block_04:send_second_reminder_if_silent",   # <‑‑ путь к функции
    REMINDER_2_DELAY_HOURS * 3600)
    
def send_second_reminder_if_silent(user_id, send_text_func):
    st = get_state(user_id)
    if not st or st.get("stage") != "block4" or not st.get("reminder1_sent"):
        return                            # пользователь ответил или мы не там

    # Если клиент всё-таки написал за последние 12 ч – выходим
    if time.time() - st.get("last_message_ts", 0) < REMINDER_2_DELAY_HOURS * 3600:
        return

    global_prompt   = load_prompt(GLOBAL_PROMPT_PATH)
    reminder2_text  = load_prompt(REMINDER_2_PROMPT_PATH)
    last_q          = st.get("last_bot_question", "")
    full_prompt     = f"{global_prompt}\n\n{reminder2_text}\n\nПоследний вопрос бота: \"{last_q}\""

    text = ask_openai(full_prompt)
    send_text_func(text)

    update_state(user_id, {
        "stage": "block4",
        "last_message_ts": time.time(),
        "reminder2_sent": True
    })

    # ------ ставим финальный таймер на 4 ч → block9 -------
    plan(user_id,
    "blocks.block_04:finalize_block4_if_silent",   # <‑‑ путь к функции
    FINAL_TIMEOUT_HOURS * 3600)

def finalize_block4_if_silent(user_id):
    st = get_state(user_id)
    if not st or st.get("stage") != "block4" or not st.get("reminder2_sent"):
        return
    if time.time() - st["last_message_ts"] < FINAL_TIMEOUT_HOURS * 3600:
        return                            # всё-таки ответил
    update_state(user_id, {"handover_reason": "no_response_after_4_2", "scenario_stage_at_handover": "block4"})
    from router import route_message
    route_message("", user_id, force_stage="block9")