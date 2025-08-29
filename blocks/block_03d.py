import time
from utils.ask_openai import ask_openai
from utils.wants_handover_ai import wants_handover_ai
from state.state import get_state, update_state
from utils.schedule import load_schedule_from_s3  # не нужен, но оставлен для единообразия импорта

# Пути к промптам
GLOBAL_PROMPT_PATH = "prompts/global_prompt.txt"
STAGE_PROMPT_PATH  = "prompts/block03d_prompt.txt"

def load_prompt(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def handle_block3d(message_text: str, user_id: str, send_reply_func, client_request_date: str):
    """
    Нестандартное шоу — сразу передаём общение Арсению после единственного ответа.
    """

    # Если клиент сам просит связаться с Арсением — сразу хенд-овер
    if wants_handover_ai(message_text):
        update_state(user_id, {"handover_reason": "asked_handover", "scenario_stage_at_handover": "block3"})
        from router import route_message
        return route_message(message_text, user_id, force_stage="block5")

    # Готовим промпт
    global_prompt = load_prompt(GLOBAL_PROMPT_PATH)
    stage_prompt  = load_prompt(STAGE_PROMPT_PATH)

    # берём описание события, сохранённое ранее в блоке 2
    prev_info = (get_state(user_id) or {}).get("event_description", "")

    full_prompt = (
        global_prompt
        + "\n\n"
        + stage_prompt
        + f"\n\nОбщая информация от клиента (ранее): {prev_info}"
        + f"\n\nТекущая дата: {client_request_date}."
        + f'\n\nСообщение клиента: "{message_text}"'
    )

    # Генерируем и отправляем ответ
    reply = ask_openai(full_prompt)
    send_reply_func(reply)

    # После единственного сообщения переводим поток в block5 (хенд-овер Арсению)
    update_state(user_id, {"handover_reason": "non_standard_show", "scenario_stage_at_handover": "block3"})
    from router import route_message
    route_message("", user_id, force_stage="block5")