# block_03.py
import os
import re
import time
import json
from typing import Optional, Any
from importlib import import_module
from logger import logger
import dateparser
from datetime import datetime
import pytz
from utils.ask_openai import ask_openai
from utils.wants_handover_ai import wants_handover_ai
from utils.reminder_engine import plan
from utils.schedule import load_schedule_from_s3, check_date_availability
from state.state import get_state, update_state
from utils.structured import build_structured_snapshot

# ──────────────────────────────────────────────────────────────────────────────
# Конфиг по типам шоу
# event_type берём из state["show_type"] ∈ {"детское","взрослое","семейное"}
# ──────────────────────────────────────────────────────────────────────────────

PROMPTS = {
    "global": "prompts/global_prompt.txt",
    "availability": "prompts/block03_availability_prompt.txt",
    "reminder_1": "prompts/block03_reminder_1_prompt.txt",
    "reminder_2": "prompts/block03_reminder_2_prompt.txt",
    # типо-специфичные:
    "stage_by_type": {
        "детское":   "prompts/block03a_prompt.txt",
        "взрослое":  "prompts/block03b_prompt.txt",
        "семейное":  "prompts/block03c_prompt.txt",
    },
    "struct_by_type": {
        "детское":   "prompts/block03a_data_prompt.txt",
        "взрослое":  "prompts/block03b_data_prompt.txt",
        "семейное":  "prompts/block03c_data_prompt.txt",
    },
}

# Поля-синонимы для отображения в коротких доспрашивающих вопросах
KEY_NAMES = {
    "event_format":           "формат мероприятия",
    "event_date":             "дата",
    "event_time":             "время",
    "event_location":         "место проведения",
    "celebrant_name":         "имя ключевого участника",
    "celebrant_gender":       "пол ключевого участника",
    "celebrant_age":          "возраст ключевого участника",
    "guests_count":           "количество гостей",
    "guests_gender":          "пол гостей",
    "guests_age":             "возраст гостей",
    "guests_children_gender": "пол детей",
    "guests_children_age":    "возраст детей",
    "children_adult_ratio":   "соотношение детей и взрослых",
    "compere_availability":   "наличие ведущего",
    "no_celebrant":           "нет ключевого участника",
}

# Все допустимые ключи (union 3a/3b/3c)
SAFE_KEYS = {
    "event_format",
    "event_date", "event_time", "event_location",
    "celebrant_name", "celebrant_gender", "celebrant_age",
    "guests_count", "guests_gender", "guests_age",
    "guests_children_gender", "guests_children_age",
    "children_adult_ratio",
    "compere_availability",
    "no_celebrant",
    # унификации даты/времени:
    "event_date_iso", "event_time_24"
}
IGNORED_VALUES = {"", "не указано", "не указан", "неизвестно", "прочерк", "-", "n/a"}
# ── доп. «пустые» значения для safe-upsert -----------------------------------
SENTINELS = {"не указано", "неизвестно", "unknown", "", None, "-", "n/a"}

def _clean_value(v):
    if isinstance(v, str) and v.strip().lower() in SENTINELS:
        return None
    return v

# Требуемые поля по типу (минимум для расчёта цены и программы)
REQUIRED_BY_TYPE = {
    "детское": [
        "event_date","event_time","event_location",
        "celebrant_name","celebrant_gender","celebrant_age",
        "guests_count","guests_gender","guests_age"
    ],
    "взрослое": [
        "event_format","event_date","event_time",
        "event_location","celebrant_name","celebrant_gender",
        "celebrant_age","guests_count","guests_gender",
        "guests_age","compere_availability"
    ],
    "семейное": [
        "event_date","event_time","event_location",
        "guests_count","children_adult_ratio",
        "guests_children_gender","guests_children_age"
        # блок по имениннику добавляется динамически, если no_celebrant != "Да"
        # см. missing_info_keys(...)
    ],
}

# Тайминги
DELAY_TO_BLOCK_3_1_HOURS = 4
DELAY_TO_BLOCK_3_2_HOURS = 12
FINAL_TIMEOUT_HOURS      = 4
_EPSILON_SEC = 1.0  # допуск на джиттер

# Единые ключи для напоминаний блока 3
REM_KEYS = {
    "r1_sent": "r1_sent_b3",
    "r2_sent": "r2_sent_b3",
    "r1_scheduled": "r1_scheduled_b3",
    "r2_scheduled": "r2_scheduled_b3",
    "fin_done": "fin_scheduled_b3_done",
}

def _is_service_msg(text: str) -> bool:
    return (text or "").strip().startswith("#")

def _eff_delay_sec(hours: float) -> float:
    try:
        accel = float(os.getenv("REMINDER_ACCEL", "1.0"))
    except Exception:
        accel = 1.0
    return hours * 3600.0 * accel

def _state_mod():
    return import_module("state.state")

def load_prompt(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def render_prompt(path: str, **kwargs) -> str:
    tmpl = load_prompt(path)
    try:
        return tmpl.format(**kwargs)
    except Exception as e:
        logger.warning(f"[block03] format error in {path}: {e}")
        return tmpl

def _true(v) -> bool:
    return str(v).strip().lower() in {"true","yes","да","y","1","истина"}

# ──────────────────────────────────────────────────────────────────────────────
# Лёгкий парсер полей прямо из пользовательского текста (без ИИ)
# ──────────────────────────────────────────────────────────────────────────────
_DATE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b")
_TIME = re.compile(r"\b([01]?\d|2[0-3])[:.](\d{2})\b")
_AGE  = re.compile(r"\b(\d{1,2})\s*(год|года|лет)\b", re.IGNORECASE)
_CNT  = re.compile(r"\b(\d{1,3})\s*(гостей|гост[яе]?|чел(?:овек[а]?)*|человек|детей|участник(?:ов)?)\b", re.IGNORECASE)
_NAME = re.compile(r"(?:для|у|сын|дочь|именинник|именинница)\s+([А-ЯЁA-Z][а-яёa-z\-]+)")
# Расширенный поиск имени (именинник/сын/дочь/юбиляр + самостоятельное имя)
NAME_HINTS = r"(?:именинник|именинница|сын|дочь|ребёнок|ребенок|юбиляр|дочк[аи])"
RE_NAME = re.compile(rf"{NAME_HINTS}[^A-Za-zА-Яа-яЁё]*([A-ZА-ЯЁ][a-zа-яё]+)", re.IGNORECASE)
RE_STANDALONE_NAME = re.compile(r'(^|[,\s])([A-ZА-ЯЁ][a-zа-яё]{2,})($|[,\s])')
# Место (с кавычками и без)
RE_LOCATION_QUOTED = re.compile(r'(?:кафе|ресторан|ТРЦ|бар|клуб|школа|сад|дет(?:ский)? сад|дом|квартира)\s*[«"](.*?)[»"]', re.IGNORECASE)
RE_LOCATION_GENERIC = re.compile(r'\b(дом|квартира|дет(?:ский)? сад|школа|ресторан|кафе|ТРЦ|бар|клуб)\b', re.IGNORECASE)
# Важно: здесь извлекаем место в «оригинальном» регистре (а не lower),
# чтобы не терять «Парус» → «Парус»
_PLACE_HINTS = ["кафе","бар","ресторан","зал","лофт","школ","сад","трц","тц","дом","клуб"]

def _norm_date_local(s: str) -> str|None:
    m = _DATE.search(s)
    if not m: return None
    d, mo, y = m.groups()
    y = (("20"+y) if y and len(y)==2 else y)
    from datetime import datetime
    if not y:
        y = str(datetime.now().year)
    try:
        dd = int(d); mm = int(mo); yy = int(y)
        if 1<=dd<=31 and 1<=mm<=12: return f"{yy:04d}-{mm:02d}-{dd:02d}"
    except: 
        return None

def _norm_time_local(s: str) -> str|None:
    m = _TIME.search(s)
    if not m: return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if 0<=hh<=23 and 0<=mm<=59: return f"{hh:02d}:{mm:02d}"
    return None

def _guess_place_local(s: str) -> str|None:
    low = s.lower()
    for h in _PLACE_HINTS:
        # ищем в lower, но возвращаем кусок из оригинальной строки
        idx = low.find(h)
        if idx >= 0:
            # возьмём хвост после хинта и попробуем вытянуть «"Парус"» или слово
            tail = s[idx+len(h):]
            m_q = re.search(r'[«"]\s*([A-Za-zА-Яа-яЁё0-9 \-]+)\s*[»"]', tail)
            if m_q:
                return f'{s[idx:idx+len(h)]} "{m_q.group(1).strip()}"'
            # fallback — одно-два слова после хинта
            m_w = re.search(r"\s+([A-Za-zА-Яа-яЁё0-9\-]+(?:\s+[A-Za-zА-Яа-яЁё0-9\-]+)?)", tail)
            if m_w:
                return f'{s[idx:idx+len(h)]} {m_w.group(1).strip()}'
            return s[idx:idx+len(h)]
    return None

def _quick_extract_fields_from_user_text(text: str) -> dict:
    if not text: return {}
    out = {}
    # имя
    m = RE_NAME.search(text)
    if not m:
        m = RE_STANDALONE_NAME.search(text)
        if m:
            out["celebrant_name"] = m.group(2).strip().title()
    else:
        out["celebrant_name"] = m.group(1).strip().title()
    # гости
    m = re.search(r'(?:гостей|человек)\s*(?:≈|~|=|:)?\s*(\d{1,3})', text, re.IGNORECASE)
    if m:
        out["guests_count"] = int(m.group(1))
    # дата/время (простые формы)
    if (d := _norm_date_local(text)): out["event_date"] = d
    if (t := _norm_time_local(text)): out["event_time"] = t
    # место
    m = RE_LOCATION_QUOTED.search(text)
    if m:
        title = m.group(1).strip().replace('«','"').replace('»','"')
        kind = re.search(r'(кафе|ресторан|ТРЦ|бар|клуб|школа|сад|дет(?:ский)? сад|дом|квартира)', text, re.IGNORECASE)
        prefix = kind.group(1).lower() if kind else "место"
        out["event_location"] = f'{prefix} "{title}"'
    else:
        if (p := _guess_place_local(text)):
            out["event_location"] = p
        else:
            m = RE_LOCATION_GENERIC.search(text)
            if m: out["event_location"] = m.group(0).lower()
    # возраст/аудитория
    if (m := _AGE.search(text)):  out["celebrant_age"] = int(m.group(1))
    if (m := _CNT.search(text)):  out["guests_count"]  = out.get("guests_count") or int(m.group(1))
    # грубые подсказки по аудитории
    low = text.lower()
    if "дет" in low: out["guests_age"] = "дети"
    if "взросл" in low: out["guests_age"] = "взрослые"
    return out

# ── безопасный upsert: НЕ затираем уже заполненное и игнорируем «пустые» ----
def upsert_state_safe(user_id: str, parsed: dict) -> dict:
    st = get_state(user_id) or {}
    changed = {}
    for k, v in (parsed or {}).items():
        if k not in SAFE_KEYS: 
            continue
        v = _clean_value(v)
        if v is None:
            continue
        if (st.get(k) in (None, "",) or st.get(k) in SENTINELS):
            changed[k] = v
    if changed:
        update_state(user_id, changed)
    return get_state(user_id)

# ──────────────────────────────────────────────────────────────────────────────
# Мягкое вступление перед вопросами
# ──────────────────────────────────────────────────────────────────────────────
def _build_soft_preface(event_type: str, st: dict) -> str:
    name = (st or {}).get("celebrant_name")
    if event_type == "детское":
        base = "Чтобы Арсений подготовил детское шоу"
        if name:
            base += f" для {name}"
    elif event_type == "семейное":
        base = "Чтобы Арсений учёл семейный формат вашего праздника"
    else:
        base = "Чтобы Арсений подготовил программу под ваш формат"
    return f"{base}, ответьте, пожалуйста, на пару вопросов:"

# ──────────────────────────────────────────────────────────────────────────────
# Dynamic required по типу + no_celebrant (для семейного)
# ──────────────────────────────────────────────────────────────────────────────
def missing_info_keys(state):
    event_type = (state.get("show_type") or "").strip().lower()
    required = list(REQUIRED_BY_TYPE.get(event_type, []))

    # Семейное: если НЕТ именинника — блок полей именинника не спрашиваем.
    if event_type == "семейное":
        if not _true(state.get("no_celebrant")):
            required += ["celebrant_name","celebrant_gender","celebrant_age"]

    # Фильтруем по факту незаполненности
    return [k for k in required if not state.get(k)]

# ── список недостающих ТОЛЬКО реально пустых (для детерминированных вопросов)
def compute_missing(state: dict) -> list[str]:
    event_type = (state.get("show_type") or "").strip().lower()
    required = list(REQUIRED_BY_TYPE.get(event_type, []))
    missing = []
    for k in required:
        # учитываем нормализованные аналоги
        if k == "event_date":
            v = state.get("event_date") or state.get("event_date_iso")
        elif k == "event_time":
            v = state.get("event_time") or state.get("event_time_24")
        else:
            v = state.get(k)
        if v is None or (isinstance(v, str) and v.strip() == ""):
            missing.append(k)
    # семейное: если явно «нет именинника» — не требуем имя
    if event_type == "семейное" and str(state.get("no_celebrant","")).strip().lower() in {"да","yes","true","y","1"}:
        if "celebrant_name" in missing:
            missing.remove("celebrant_name")
        if "celebrant_gender" in missing:
            missing.remove("celebrant_gender")
        if "celebrant_age" in missing:
            missing.remove("celebrant_age")
    return missing

# ── детерминированные вопросы вместо LLM-шаблонов ---------------------------
QUESTION_LABELS = {
    "event_date": "Дата мероприятия?",
    "event_time": "Время мероприятия?",
    "event_location": "Где будет проходить мероприятие (дом/детсад/школа/ресторан)?",
    "celebrant_name": "Как зовут именинника?",
    "celebrant_gender": "Какой пол именинника?",
    "celebrant_age": "Сколько лет имениннику?",
    "guests_count": "Сколько гостей ожидается?",
    "guests_gender": "Какой пол у гостей детского возраста?",
    "guests_age": "Какой возраст у гостей детского возраста?",
    "compere_availability": "Нужен ли ведущий?",
    "children_adult_ratio": "Какое соотношение детей и взрослых?",
}

def build_questions(state: dict, missing_keys: list[str]) -> str:
    prefix = "Чтобы Арсений подготовил программу, ответьте, пожалуйста, на несколько вопросов:\n"
    bullets = [f"- {QUESTION_LABELS[k]}" for k in missing_keys if k in QUESTION_LABELS]
    return prefix + "\n".join(bullets)

# ──────────────────────────────────────────────────────────────────────────────
# upsert с нормализациями и «не затирать непустое»
# ──────────────────────────────────────────────────────────────────────────────
def upsert_state(user_id: str, parsed: dict):
    st = get_state(user_id) or {}
    out = {}

    def norm(v):
        return ("" if v is None else str(v)).strip()

    for k, v in (parsed or {}).items():
        if k not in SAFE_KEYS:
            continue
        sv = norm(v)
        if sv.lower() in IGNORED_VALUES:
            continue

        # no_celebrant: строковые «Да/нет» → "Да"/"нет"
        if k == "no_celebrant":
            if _true(sv):
                out[k] = "Да"
            elif not st.get(k):  # «нет» ставим только если поля не было
                out[k] = "нет"
            continue

        # compere_availability (ведущий) — нормализация
        if k == "compere_availability":
            s = sv.lower()
            neg = {"не будет", "нет", "без ведущего", "отсутствует"}
            pos = {"будет", "да", "есть", "запланирован", "запланирована"}
            if any(x in s for x in neg):
                sv = "не будет"
            elif any(x in s for x in pos):
                sv = "будет"

        # не затираем непустые значения в state
        if st.get(k):
            continue
        out[k] = sv

    if out:
        update_state(user_id, out)
    return get_state(user_id)

# ──────────────────────────────────────────────────────────────────────────────
# Fallback-парсер (regex union), если модель выдала не-JSON
# ──────────────────────────────────────────────────────────────────────────────
def parse_structured_pairs(text: str) -> dict:
    flags = re.IGNORECASE | re.MULTILINE
    def grab(pat):
        m = re.search(pat, text, flags)
        return m.group(1).strip() if m else None

    res = {}

    # Общее
    mapping = {
        "event_format":           r"Формат\s+мероприятия\s*[-—:]\s*([^\n\r]+)",
        "celebrant_name":         r"Имя\s+(?:ключевого\s+участника|именинника)[\s\S]*?[-—:]\s*([^\n\r]+)",
        "celebrant_gender":       r"Пол\s+(?:ключевого\s+участника|именинника)[\s\S]*?[-—:]\s*([^\n\r]+)",
        "celebrant_age":          r"Возраст\s+(?:ключевого\s+участника|именинника)[\s\S]*?[-—:]\s*([^\n\r]+)",
        "event_date":             r"Дата\s+мероприятия\s*[-—:]\s*([^\n\r]+)",
        "event_time":             r"Время\s+мероприятия\s*[-—:]\s*([^\n\r]+)",
        "event_location":         r"(?:(?:Название\s+)?места\s+проведения|Название\s+места|локац(?:ия|ии)|адрес)\s*[-—:]\s*([^\n\r]+)",
        "guests_count":           r"Количество\s+гостей\s*[-—:]\s*([^\n\r]+)",
        "guests_gender":          r"Пол\s+гостей\s*[-—:]\s*([^\n\r]+)",
        "guests_age":             r"Возраст\s+гостей\s*[-—:]\s*([^\n\r]+)",
        "guests_children_gender": r"Пол\s+дет[ей]\s*[-—:]\s*([^\n\r]+)",
        "guests_children_age":    r"Возраст\s+дет[ей]\s*[-—:]\s*([^\n\r]+)",
        "children_adult_ratio":   r"соотношение\s+детей\s+и\s+взрослых\s*[-—:]\s*([^\n\r]+)",
        "compere_availability":   r"(?:наличие\s+ведущего|ведущий)\s*[-—:]\s*([^\n\r]+)",
    }
    for k, pat in mapping.items():
        v = grab(pat)
        if v and v.lower() not in IGNORED_VALUES:
            res[k] = v

    # no_celebrant
    if re.search(r"(нет\s+ключевого\s+участника|нет\s+именинника|никто\s+конкретный|без\s+главного\s+героя)", text, flags):
        res["no_celebrant"] = "Да"
    return res

# ──────────────────────────────────────────────────────────────────────────────
# Нормализация даты/времени
# ──────────────────────────────────────────────────────────────────────────────
def _clean_time(raw_time: str) -> str:
    s = (raw_time or "").strip().replace(".", ":").replace(" ", "")
    m = re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", s)
    return m.group(0) if m else ""

def _clean_date(raw_date: str) -> str:
    s = (raw_date or "").strip().replace("/", "-").replace(".", "-")
    m = re.search(r"\b\d{4}-\d{2}-\d{2}\b", s)
    return m.group(0) if m else ""

# ── нормализация места (приводим к виду: тип "Название") --------------------
def normalize_location(state: dict):
    loc = state.get("event_location")
    if not loc:
        return
    loc2 = str(loc)
    if '«' in loc2 or '»' in loc2:
        loc2 = loc2.replace('«','"').replace('»','"')
    else:
        m = re.match(r'(кафе|ресторан|трц|бар|клуб|школа|сад|детский сад|дом|квартира)\s+(.+)', loc2, re.IGNORECASE)
        if m:
            loc2 = f'{m.group(1).lower()} "{m.group(2).strip()}"'
    if loc2 != loc:
        state["event_location"] = loc2

# ── нормализация даты/времени через dateparser (RU, будущее) ----------------
_TZ = pytz.timezone(os.getenv("LOCAL_TZ", "Europe/Moscow"))

def normalize_datetime(state: dict):
    date_txt = state.get("event_date")
    time_txt = state.get("event_time")
    if not date_txt and not time_txt:
        return
    settings = {
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": datetime.now(_TZ),
        "LANGUAGE_DETECTION_CONFIDENCE_THRESHOLD": 0.0,
        "DATE_ORDER": "DMY",
        "STRICT_PARSING": False,
        "SKIP_TOKENS": ["в"],
    }
    dt = None
    if date_txt and time_txt:
        dt = dateparser.parse(f"{date_txt} {time_txt}", languages=["ru"], settings=settings)
    elif date_txt:
        dt = dateparser.parse(f"{date_txt}", languages=["ru"], settings=settings)
    if dt:
        if dt.tzinfo is None:
            dt = _TZ.localize(dt)
        else:
            dt = dt.astimezone(_TZ)
        state["event_date_iso"] = dt.date().isoformat()
        state["event_time_24"] = dt.strftime("%H:%M")
        if not state.get("event_time"):
            state["event_time"] = state["event_time_24"]

# ──────────────────────────────────────────────────────────────────────────────
# Основной хендлер блока 3 (унифицированный)
# ──────────────────────────────────────────────────────────────────────────────
def handle_block3(message_text, user_id, send_reply_func, client_request_date=None):
    from router import route_message
    if client_request_date is None:
        client_request_date = time.time()

    if _is_service_msg(message_text):
        logger.info("[block03] сервисная команда — игнор")
        return

    # Хендовер по явной просьбе
    if wants_handover_ai(message_text):
        update_state(user_id, {
            "handover_reason": "asked_handover",
            "scenario_stage_at_handover": "block3"
        })
        return route_message(message_text, user_id, force_stage="block5")

    st = get_state(user_id) or {}
    # Любой ответ пользователя «гасит» автокасания
    now_ts = time.time()
    update_state(user_id, {
        "last_sender": "user",
        "last_user_ts": now_ts
    })
    prev_info = st.get("event_description", "")
    updated_description = (prev_info + "\n" + (message_text or "")).strip()
    update_state(user_id, {"event_description": updated_description})

    
    # Быстро добираем поля прямо из текста пользователя (без ИИ)
    try:
        quick = _quick_extract_fields_from_user_text(message_text or "")
        if quick:
            st = upsert_state_safe(user_id, quick)
            # сразу нормализуем место/дату — чтобы не переспрашивать имя/место/дату
            tmp = get_state(user_id) or {}
            normalize_location(tmp)
            normalize_datetime(tmp)
            if tmp:
                update_state(user_id, tmp)
            st = get_state(user_id) or {}
    except Exception as e:
        logger.warning(f"[block03] quick extract failed: {e}")

    # Определяем тип шоу
    event_type = (st.get("show_type") or "").strip().lower()
    if event_type not in {"детское","взрослое","семейное"}:
        # на всякий случай fallback → считаем «взрослое»
        event_type = "взрослое"
    
    from datetime import datetime
    now = datetime.now()
    client_request_date_str = now.strftime("%d %B %Y")
    current_year = now.year

    global_prompt = load_prompt(PROMPTS["global"])
    stage_prompt  = render_prompt(
        PROMPTS["stage_by_type"][event_type],
        client_request_date=client_request_date_str
    )
    struct_prompt = render_prompt(
        PROMPTS["struct_by_type"][event_type],
        message_text=message_text,
        previous_description=prev_info
    )

    # 1) Структурирование (сперва JSON, потом fallback)
    struct_input = (
        struct_prompt
        + f'\n\nСообщение клиента: "{message_text}"'
        + f'\n\nПредыдущее описание: "{prev_info}"'
    )
    structured_reply = (ask_openai(struct_input) or "").strip()
    logger.info("[block03] ответ модели до парсинга:\n%s", structured_reply)

    parsed_data = {}
    try:
        parsed = json.loads(structured_reply)
        if isinstance(parsed, dict):
            parsed_data = parsed
    except Exception:
        parsed_data = parse_structured_pairs(structured_reply)

    # безопасный мердж ответа модели
    st = upsert_state_safe(user_id, parsed_data)
    # повторная нормализация (если модель вернула без кавычек/время в «19.00»)
    tmp = get_state(user_id) or {}
    normalize_location(tmp)
    normalize_datetime(tmp)
    if tmp:
        update_state(user_id, tmp)
    st = get_state(user_id) or {}
    logger.info("[block03] после upsert: %s", {k: st.get(k) for k in SAFE_KEYS})

    # Снепшот
    snap = build_structured_snapshot(st)
    update_state(user_id, {"structured_cache": snap})
    st = get_state(user_id)

    # 2) Дата/время → нормализация, попытка мгновенного availability

    combined_text = f"{prev_info}\n{message_text}".strip()
    match_date = None
    match_time = None

    if not st.get("availability_reply_sent"):
        date_prompt = f"""
Сегодня: {client_request_date_str}

Все сообщения клиента: "{combined_text}"

Определи, указана ли дата проведения.
Если указан только день и месяц — подставь текущий год: {current_year}.
Если указан год — используй его.
Формат: ГГГГ-ММ-ДД. Если даты нет — "нет даты".
"""
        t_prompt = f"""
Все сообщения клиента: "{combined_text}"
Определи, указано ли время проведения.
Если да — формат ЧЧ:ММ. Иначе — "нет времени".
"""
        date_reply = (ask_openai(date_prompt) or "").strip().lower()
        time_reply = (ask_openai(t_prompt) or "").strip().lower()
        match_date = None if date_reply == "нет даты" else date_reply
        match_time = None if time_reply == "нет времени" else time_reply

        if match_date:
            update_state(user_id, {"event_date_iso": _clean_date(match_date)})
        if match_time:
            update_state(user_id, {"event_time_24": _clean_time(match_time)})

        st = get_state(user_id)
        snap = build_structured_snapshot(st)
        update_state(user_id, {"structured_cache": snap})

    # 3) Если есть нормализованные дата+время — проверяем слоты и отвечаем
    if not st.get("availability_reply_sent"):
        date_iso = _clean_date(match_date) if match_date else None
        time_24  = _clean_time(match_time) if match_time else ""
        if date_iso and time_24:
            schedule = load_schedule_from_s3()
            availability = check_date_availability(date_iso, time_24, schedule)
            logger.info(f"[block03] availability={availability} for {date_iso} {time_24}")

            availability_reply = (ask_openai(
                global_prompt + "\n\n" + render_prompt(
                    PROMPTS["availability"],
                    message_text=message_text,
                    date_iso=date_iso,
                    time_24=time_24,
                    client_request_date=client_request_date_str,
                    availability=availability
                )
            ) or "").strip()
            if availability_reply:
                send_reply_func(availability_reply)
                now_ts = time.time()
                update_state(user_id, {
                    "availability_reply_sent": True,
                    "summary_and_availability_sent": True,
                    "date_decision_flag": availability,  # "available" | "need_handover" | "occupied"
                    "last_sender": "bot",
                    "last_bot_ts": now_ts,
                    REM_KEYS["r1_sent"]: False,
                    REM_KEYS["r2_sent"]: False,
                    REM_KEYS["r1_scheduled"]: False,
                    REM_KEYS["r2_scheduled"]: False
                })

            # Резервируем слот (без падений)
            if availability == "available":
                try:
                    import utils.schedule as schedule_utils
                    if hasattr(schedule_utils, "reserve_slot"):
                        schedule_utils.reserve_slot(date_iso, time_24)
                except Exception as e:
                    logger.info("[block03] reserve_slot fail: %s", e)

            if availability in ("need_handover", "occupied"):
                update_state(user_id, {
                    "handover_reason": "early_date_or_busy",
                    "scenario_stage_at_handover": "block3"
                })
                return route_message("", user_id, force_stage="block5")

    # 4) Доспрашивание недостающих полей (до 3 попыток), без «полотенец»
    st = get_state(user_id) or {}
    # используем строгий список пропусков (без уже заполненных)
    missing = compute_missing(st)
    attempts = int(st.get("clarification_attempts", 0))
    logger.info("[block03] missing=%s attempts=%s", missing, attempts)

    if missing and attempts < 3:
        # Список недостающих в человекочитаемом виде (коротко)
        # Берём максимум 3, чтобы не пугать полотенцем; формируем вопросы детерминированно
        short_missing = missing[:3]
        questions = build_questions(st, short_missing)
        if questions:
            # мягкое вступление + наши пули (без LLM, чтобы не было «Как зовут…» когда уже «для Витя»)
            preface = _build_soft_preface(event_type, st)
            final_msg = f"{preface}\n{questions}"
            try:
                send_reply_func(final_msg)
            except Exception:
                pass
            now_ts = time.time()
            update_state(user_id, {
                "stage": "block3",
                "clarification_attempts": attempts + 1,
                "last_bot_question": final_msg,
                "last_sender": "bot",
                "last_bot_ts": now_ts,
                REM_KEYS["r1_sent"]: False,
                REM_KEYS["r2_sent"]: False,
                REM_KEYS["r1_scheduled"]: False,
                REM_KEYS["r2_scheduled"]: False
            })
            # ставим R1, если ещё не ставили
            cur = get_state(user_id) or {}
            if not cur.get(REM_KEYS["r1_scheduled"]):
                plan(user_id, "blocks.block_03:send_first_reminder_if_silent",
                     _eff_delay_sec(DELAY_TO_BLOCK_3_1_HOURS))
                update_state(user_id, {REM_KEYS["r1_scheduled"]: True})
            return

    # 5) Если 3 попытки — принимаем решение
    if missing and attempts >= 3:
        if len(missing) <= 2 and st.get("celebrant_name"):
            logger.info("[block03] данных достаточно — идём дальше")
            return route_message("", user_id, force_stage="block4")
        else:
            logger.info("[block03] не собрали — хендовер")
            update_state(user_id, {
                "handover_reason": "could_not_collect_info",
                "scenario_stage_at_handover": "block3"
            })
            return route_message("", user_id, force_stage="block5")

    # 6) Фолбек: все данные есть, но availability ещё не отправлен
    st = get_state(user_id) or {}
    if (not missing) and (not st.get("availability_reply_sent")) and (st.get("event_date") or st.get("event_date_iso")) and (st.get("event_time") or st.get("event_time_24")):
        date_iso = st.get("event_date_iso") or _clean_date(st.get("event_date"))
        time_24  = st.get("event_time_24") or _clean_time(st.get("event_time"))
        if date_iso and time_24:
            schedule = load_schedule_from_s3()
            availability = check_date_availability(date_iso, time_24, schedule)

            availability_reply = (ask_openai(
                global_prompt + "\n\n" + render_prompt(
                    PROMPTS["availability"],
                    message_text=message_text,
                    date_iso=date_iso,
                    time_24=time_24,
                    client_request_date=client_request_date_str,
                    availability=availability
                )
            ) or "").strip()
            if availability_reply:
                send_reply_func(availability_reply)
                now_ts = time.time()
                update_state(user_id, {
                    "availability_reply_sent": True,
                    "summary_and_availability_sent": True,
                    "date_decision_flag": availability,
                    "last_sender": "bot",
                    "last_bot_ts": now_ts,
                    REM_KEYS["r1_sent"]: False,
                    REM_KEYS["r2_sent"]: False,
                    REM_KEYS["r1_scheduled"]: False,
                    REM_KEYS["r2_scheduled"]: False
                })

            if availability == "available":
                try:
                    import utils.schedule as schedule_utils
                    if hasattr(schedule_utils, "reserve_slot"):
                        schedule_utils.reserve_slot(date_iso, time_24)
                except Exception as e:
                    logger.info("[block03:fallback] reserve_slot fail: %s", e)

            if availability in ("need_handover", "occupied"):
                update_state(user_id, {
                    "handover_reason": "early_date_or_busy",
                    "scenario_stage_at_handover": "block3"
                })
                return route_message("", user_id, force_stage="block5")

    # 7) Переход по флагу доступности
    st = get_state(user_id) or {}
    if not missing:
        flag = st.get("date_decision_flag")
        if flag == "available":
            return route_message("", user_id, force_stage="block4")
        elif flag in ("need_handover", "occupied"):
            update_state(user_id, {
                "handover_reason": "early_date_or_busy",
                "scenario_stage_at_handover": "block3"
            })
            return route_message("", user_id, force_stage="block5")

    # 8) Финальные обновления и (возможная) постановка R1, если был вопрос
    update_state(user_id, {
        "stage": "block3",
        "last_message_ts": time.time()
    })
    cur = get_state(user_id) or {}
    if cur.get("last_bot_question") and not cur.get(REM_KEYS["r1_scheduled"]):
        plan(user_id, "blocks.block_03:send_first_reminder_if_silent",
             _eff_delay_sec(DELAY_TO_BLOCK_3_1_HOURS))
        update_state(user_id, {REM_KEYS["r1_scheduled"]: True})


# ──────────────────────────────────────────────────────────────────────────────
# Напоминания (унифицированные)
# ──────────────────────────────────────────────────────────────────────────────

def send_first_reminder_if_silent(user_id, send_reply_func):
    st = get_state(user_id)
    if not st or st.get("stage") != "block3":
        return
    last_bot_ts  = float(st.get("last_bot_ts") or 0)
    last_user_ts = float(st.get("last_user_ts") or 0)
    now_ts = time.time()
    if now_ts - last_bot_ts < (_eff_delay_sec(DELAY_TO_BLOCK_3_1_HOURS) - _EPSILON_SEC):
        return
    if last_user_ts > last_bot_ts:
        return
    if st.get(REM_KEYS["r1_sent"]):
        return

    global_prompt   = load_prompt(PROMPTS["global"])
    reminder_prompt = load_prompt(PROMPTS["reminder_1"])
    last_q = st.get("last_bot_question","")
    full_prompt = f'{global_prompt}\n\n{reminder_prompt}\n\nПоследний вопрос бота: "{last_q}"'
    reply = (ask_openai(full_prompt) or "").strip()
    if reply:
        send_reply_func(reply)

    now_ts = time.time()
    update_state(user_id, {
        "stage": "block3",
        "last_message_ts": now_ts,
        REM_KEYS["r1_sent"]: True,
        "last_sender": "bot",
        "last_bot_ts": now_ts
    })
    plan(user_id, "blocks.block_03:send_second_reminder_if_silent",
         _eff_delay_sec(DELAY_TO_BLOCK_3_2_HOURS))
    update_state(user_id, {REM_KEYS["r1_scheduled"]: True})

def send_second_reminder_if_silent(user_id, send_reply_func):
    st = get_state(user_id)
    if not st or st.get("stage") != "block3":
        return
    last_bot_ts  = float(st.get("last_bot_ts") or 0)
    last_user_ts = float(st.get("last_user_ts") or 0)
    now_ts = time.time()
    if now_ts - last_bot_ts < (_eff_delay_sec(DELAY_TO_BLOCK_3_2_HOURS) - _EPSILON_SEC):
        return
    if last_user_ts > last_bot_ts:
        return
    if st.get(REM_KEYS["r2_sent"]):
        return

    global_prompt   = load_prompt(PROMPTS["global"])
    reminder_prompt = load_prompt(PROMPTS["reminder_2"])
    last_q = st.get("last_bot_question","")
    full_prompt = f'{global_prompt}\n\n{reminder_prompt}\n\nПоследний вопрос бота: "{last_q}"'
    reply = (ask_openai(full_prompt) or "").strip()
    if reply:
        send_reply_func(reply)

    now_ts = time.time()
    update_state(user_id, {
        "stage": "block3",
        "last_message_ts": now_ts,
        REM_KEYS["r2_sent"]: True,
        "last_sender": "bot",
        "last_bot_ts": now_ts
    })
    plan(user_id, "blocks.block_03:finalize_if_still_silent",
         _eff_delay_sec(FINAL_TIMEOUT_HOURS))
    update_state(user_id, {REM_KEYS["r2_scheduled"]: True})

def finalize_if_still_silent(user_id, send_reply_func):
    from router import route_message
    st = get_state(user_id)
    if not st or st.get("stage") != "block3":
        return
    last_bot_ts  = float(st.get("last_bot_ts") or 0)
    last_user_ts = float(st.get("last_user_ts") or 0)
    if last_user_ts > last_bot_ts:
        return
    if st.get(REM_KEYS["fin_done"]):
        return
    update_state(user_id, {
        "handover_reason": "no_response_after_3_2",
        "scenario_stage_at_handover": "block3",
        REM_KEYS["fin_done"]: True
    })
    route_message("", user_id, force_stage="block5")