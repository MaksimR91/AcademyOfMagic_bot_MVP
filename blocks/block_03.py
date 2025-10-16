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
from utils.ru_morph import to_genitive_name, normalize_proper_name

# ──────────────────────────────────────────────────────────────────────────────
# Конфиг по типам шоу
# event_type берём из state["show_type"] ∈ {"детское","взрослое","семейное"}
# ──────────────────────────────────────────────────────────────────────────────

PROMPTS = {
    "global": "prompts/global_prompt.txt",
    "availability": "prompts/block03_availability_prompt.txt",
    # статические тексты повторных касаний (без ИИ)
    "reminder_1_static": "prompts/block03_reminder_1_static.txt",
    "reminder_2_static": "prompts/block03_reminder_2_static.txt",
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
    "guests_age":             "возраст гостей",
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
    "guests_count", "guests_age",
    "guests_children_gender", "children_adult_ratio",
    "compere_availability", "no_celebrant",
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
        "guests_count","guests_children_age"
    ],
    "взрослое": [
        "event_format","event_date","event_time",
        "event_location","celebrant_name","celebrant_gender",
        "celebrant_age","guests_count",
        "guests_age","compere_availability"
    ],
    "семейное": [
        "event_date","event_time","event_location",
        "guests_count","children_adult_ratio",
        "guests_children_age"
        # блок по имениннику добавляется динамически, если no_celebrant != "Да"
        # см. missing_info_keys(...)
    ],
}

# Тайминги
DELAY_TO_BLOCK_3_1_HOURS = 4
DELAY_TO_BLOCK_3_2_HOURS = 12
FINAL_TIMEOUT_HOURS      = 4
DELAY_TO_BLOCK_3_1_SEC = int(DELAY_TO_BLOCK_3_1_HOURS * 3600)
DELAY_TO_BLOCK_3_2_SEC = int(DELAY_TO_BLOCK_3_2_HOURS * 3600)
FINAL_TIMEOUT_SEC       = int(FINAL_TIMEOUT_HOURS * 3600)
_EPSILON_SEC = 1.0  # допуск на джиттер

# Единые ключи для напоминаний блока 3
REM_KEYS = {
    "r1_sent": "r1_sent_b3",
    "r2_sent": "r2_sent_b3",
    "r1_scheduled": "r1_scheduled_b3",
    "r2_scheduled": "r2_scheduled_b3",
    "fin_done": "fin_scheduled_b3_done",
}

# Ключи для «сессионной» защиты напоминаний
REM_SESS = {
    "session": "b3_session",
    "r1_sid": "r1_session_id_b3",
    "r2_sid": "r2_session_id_b3",
    "fin_sid": "fin_session_id_b3",
}

# ── helpers ──────────────────────────────────────────────────────────────
def _event_dt_key(st: dict) -> str | None:
    d = (st.get("event_date_iso") or "").strip()
    t = (st.get("event_time_24") or "").strip()
    if d and t:
        return f"{d} {t}"
    return None

def _current_b3_session(st: dict) -> int:
    try:
        return int(st.get(REM_SESS["session"]) or 0)
    except Exception:
        return 0

def _bump_b3_session(user_id: str) -> int:
    """Новая «итерация» сбора данных в блоке 3: повышаем сессию и сбрасываем флаги напоминаний.
    Старые запланированные задачи по времени всё ещё могут сработать, но они увидят,
    что их session_id устарел, и ничего не отправят.
    """
    st = get_state(user_id) or {}
    sid = _current_b3_session(st) + 1
    update_state(user_id, {
        REM_SESS["session"]: sid,
        # сбрасываем только плановые флаги; флаги отправки не трогаем — это факт
        REM_KEYS["r1_scheduled"]: False,
        REM_KEYS["r2_scheduled"]: False,
        REM_KEYS["r1_sent"]: False,
        REM_KEYS["r2_sent"]: False,
        REM_KEYS["fin_done"]: False,
    })
    return sid

def _is_service_msg(text: str) -> bool:
    return (text or "").strip().startswith("#")

# В этом блоке ускорение применяет сам reminder_engine.plan().
def _raw_delay_sec(hours: float) -> int:
    return int(hours * 3600)

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
# Время: разрешаем только двоеточие как разделитель (исключаем «5.11»)
_TIME = re.compile(r"\b([01]?\d|2[0-3]):(\d{2})\b")
_AGE  = re.compile(r"\b(\d{1,2})\s*(год|года|лет)\b", re.IGNORECASE)
_CNT  = re.compile(r"\b(\d{1,3})\s*(гостей|гост[яе]?|чел(?:овек[а]?)*|человек|детей|участник(?:ов)?)\b", re.IGNORECASE)
_NAME = re.compile(r"(?:для|у|сын|дочь|именинник|именинница)\s+([А-ЯЁA-Z][а-яёa-z\-]+)")
# Имя будет давать только ИИ/доспрашивание. Тут — только валидация для страховки.
MONTH_TOKENS = {
    "январь","января","февраль","февраля","март","марта","апрель","апреля",
    "май","мая","июнь","июня","июль","июля","август","августа",
    "сентябрь","сентября","октябрь","октября","ноябрь","ноября","декабрь","декабря"
}
KIN_TOKENS = {"сын","дочь","ребёнок","ребенок","именинник","именинница","юбиляр"}
GENERIC_TOKENS = {"день","рождения","праздник","юбилей","др","dr","пати"}
FEMALE_HINT_TOKENS = {
    "дочь","дочка","дочери","доченька","девочка","девочке","именинница","невеста"
}
MALE_HINT_TOKENS = {
    "сын","сынишка","мальчик","мальчишка","именинник","жених","юбиляр"
}

def _is_probable_name(s: str) -> bool:
    if not s: return False
    t = s.strip().strip('«»"').replace("  "," ")
    # допустимы 1–2 слова (имя или имя+уменьш.)
    parts = [p for p in re.split(r"\s+", t) if p]
    if not (1 <= len(parts) <= 2): 
        return False
    # каждое слово начинается с буквы и не является месяцем/общим словом/родством
    for p in parts:
        pl = p.lower()
        if pl in MONTH_TOKENS or pl in KIN_TOKENS or pl in GENERIC_TOKENS:
            return False
        if not re.match(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\-']*$", p):
            return False
    # первое слово с заглавной
    if not parts[0][0].isalpha() or not parts[0][0].isupper():
        return False
    # не слишком коротко
    if len(parts[0]) < 3:
        return False
    return True
# Место (с кавычками и без)
RE_LOCATION_QUOTED = re.compile(r'(?:кафе|ресторан|ТРЦ|бар|клуб|школа|сад|дет(?:ский)? сад|дом|квартира)\s*[«"](.*?)[»"]', re.IGNORECASE)
RE_LOCATION_GENERIC = re.compile(r'\b(дом|квартира|дет(?:ский)? сад|школа|ресторан|кафе|ТРЦ|бар|клуб)\b', re.IGNORECASE)
# Важно: здесь извлекаем место в «оригинальном» регистре (а не lower),
# чтобы не терять «Парус» → «Парус»
_PLACE_HINTS = ["кафе","бар","ресторан","зал","лофт","школ","сад","трц","тц","дом","клуб"]
# Слова, которые часто ошибочно принимают за имя при standalone-матче
STOP_NAME_TOKENS = {
    "День","Рождения","Праздник","Юбилей","Сын","Дочь","Ребёнок","Ребенок","Пати","Праздуха","DR","ДР"
}

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
    # Берём ТОЛЬКО форматы с двоеточием, чтобы «5.11» не считалось временем
    m = _TIME.search(s)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return f"{hh:02d}:{mm:02d}"
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
    # ИМЯ НЕ ПАРСИМ регекспами — отдаём это ИИ/доспрашиванию.
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

def _infer_gender_hint(state: dict, text: str) -> str | None:
    """
    Пытаемся мягко вывести пол ключевого участника по словам из текста/истории.
    Возвращаем 'женский' | 'мужской' | None.
    """
    # если уже явно установлен — уважаем существующее значение
    g = (state or {}).get("celebrant_gender")
    if isinstance(g, str) and g.strip():
        gl = g.strip().lower()
        if gl.startswith("жен"): return "женский"
        if gl.startswith("муж"): return "мужской"
    low = (text or "").lower()
    if any(tok in low for tok in FEMALE_HINT_TOKENS):
        return "женский"
    if any(tok in low for tok in MALE_HINT_TOKENS):
        return "мужской"
    return None

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
    raw_name = (st or {}).get("celebrant_name")
    # имя уже проходит ваш _is_probable_name — оставляем это как есть
    name = raw_name if _is_probable_name(raw_name or "") else None
    if name:
        # склоняем под предлог "для" → родительный падеж
        gender = (st or {}).get("celebrant_gender")  # "мужской"/"женский"/…
        name = to_genitive_name(name, gender_hint=gender)

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
    # Фолбэк на "взрослое", если тип шоу пока не задан (важно для юнит-тестов и безопасный дефолт в проде)
    event_type = (state.get("show_type") or "").strip().lower() or "взрослое"
    if event_type not in REQUIRED_BY_TYPE:
        event_type = "взрослое"
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
    # взрослое: делаем celebrant_age обязательным ТОЛЬКО для ДР/юбилея; на свадьбе — исключаем
    if event_type == "взрослое":
        kind = _detect_event_context(state, state.get("event_description") or "")
        if kind == "wedding":
            if "celebrant_age" in missing:
                missing.remove("celebrant_age")
        elif kind in ("birthday", "jubilee"):
            # убедимся, что возраст реально в списке требований
            if ("celebrant_age" not in required) and (state.get("celebrant_age") in (None, "",)):
                missing.append("celebrant_age")
    return missing

# Базовые (универсальные) формулировки; для celebrant_* будут подменяться контекстом
QUESTION_LABELS_BASE = {
    "event_date": "Дата мероприятия?",
    "event_time": "Время мероприятия?",
    "event_location": "Где будет проходить мероприятие (назовите место, адрес не обязателен)?",
    "celebrant_name": "Как зовут ключевого участника?",
    "celebrant_gender": "Какой пол ключевого участника?",
    "celebrant_age": "Сколько лет ключевому участнику?",
    "guests_count": "Сколько гостей ожидается?",
    "guests_age": "Какой возраст у гостей (все взрослые или будут дети)?",
    "guests_children_age": "Какой возраст у гостей детского возраста?",
    "compere_availability": "Нужен ли ведущий?",
    "children_adult_ratio": "Какое соотношение детей и взрослых?",
}

# ── определение контекста по ключевым словам из истории диалога -------------
BIRTHDAY_TOKENS = ("др", "день рождения", "имен")            # «имен» покроет «именинник/именины»
WEDDING_TOKENS  = ("свад", "бракосочетан")
JUBILEE_TOKENS  = ("юбил",)

def _detect_event_context(state: dict, text: str | None) -> str | None:
    """
    Возвращает 'birthday' | 'wedding' | 'jubilee' | None.
    Источники: текст (history/сообщение), event_format, celebrant_gender.
    """
    pieces = [
        (text or ""),
        str((state or {}).get("event_description") or ""),
        str((state or {}).get("event_format") or ""),
        str((state or {}).get("celebrant_gender") or ""),
    ]
    low = " ".join(pieces).lower()
    if any(t in low for t in WEDDING_TOKENS):
        return "wedding"
    if any(t in low for t in BIRTHDAY_TOKENS):
        return "birthday"
    if any(t in low for t in JUBILEE_TOKENS):
        return "jubilee"
    return None

def _labels_for_context(state: dict, context_text: str) -> dict:
    """
    Возвращает словарь подписи вопросов с учётом:
      • ключевых слов (др/свадьба/юбилей);
      • флага no_celebrant — если он true, celebrant_* не спрашиваем.
    Остальные вопросы не меняем.
    """
    labels = dict(QUESTION_LABELS_BASE)

    # Если пользователь явно указал «нет ключевого участника» — убираем блок celebrant_*
    if _true(state.get("no_celebrant")):
        for k in ("celebrant_name", "celebrant_gender", "celebrant_age"):
            labels.pop(k, None)
        return labels

    kind = _detect_event_context(state, context_text)
    if kind == "birthday":
        # подставляем женские/мужские формы по подсказкам
        g_hint = _infer_gender_hint(state, context_text)
        if g_hint == "женский":
            labels["celebrant_name"]   = "Как зовут именинницу?"
            labels["celebrant_gender"] = "Какой пол именинницы?"
            labels["celebrant_age"]    = "Сколько лет имениннице?"
        elif g_hint == "мужской":
            labels["celebrant_name"]   = "Как зовут именинника?"
            labels["celebrant_gender"] = "Какой пол именинника?"
            labels["celebrant_age"]    = "Сколько лет имениннику?"
        else:
            # если не уверены — оставляем нейтральные базовые формулировки
            labels["celebrant_name"]   = "Как зовут именинника/именинницу?"
            labels["celebrant_gender"] = "Какой пол именинника/именинницы?"
            labels["celebrant_age"]    = "Сколько лет имениннику/имениннице?"
    elif kind == "wedding":
        labels["celebrant_name"]   = "Как зовут пару (имена жениха и невесты)?"
        labels["celebrant_age"]    = "Возраст (если уместно) — можно пропустить."
    elif kind == "jubilee":
        labels["celebrant_name"]   = "Как зовут юбиляра?"
        labels["celebrant_gender"] = "Пол юбиляра?"
        labels["celebrant_age"]    = "Какой юбилей/возраст?"
    # else: оставляем универсальные формулировки
    return labels

def build_questions(state: dict, missing_keys: list[str], *, context_text: str = "") -> str:
    """
    Возвращаем только список пунктов — без вступления (чтобы не дублировать префейс).
    Для celebrant_* подставляем формулировки по ключевым словам (др/свадьба/юбилей)
    и учитываем no_celebrant.
    """
    labels = _labels_for_context(state, context_text)
    bullets = [f"- {labels[k]}" for k in missing_keys if k in labels]
    return "\n".join(bullets)

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
        "guests_age":             r"Возраст\s+гостей\s*[-—:]\s*([^\n\r]+)",
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
    # Больше НЕ заменяем точки на двоеточие — это ломало «5.11»
    s = (raw_time or "").strip().replace(" ", "")
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
    # Любой ответ пользователя «гасит» автокасания (через новую сессию)
    now_ts = time.time()
    update_state(user_id, {
        "last_sender": "user",
        "last_user_ts": now_ts,
        # глобальный флаг блока 3: «после ответа клиента напоминания запрещены»
        "cancel_block3_reminders": True
    })
    # ВАЖНО: поднимем сессию блока 3 — все старые R1/R2, если сработают, не пройдут по sid
    cur_sid = _bump_b3_session(user_id)
    # после подъёма сессии флаги отправок R1/R2 сброшены → считаем по «текущей» сессии
    st = get_state(user_id) or {}
    had_prior_reminder = bool(st.get(REM_KEYS["r1_sent"]) or st.get(REM_KEYS["r2_sent"]))
    prev_info = st.get("event_description", "")
    updated_description = (prev_info + "\n" + (message_text or "")).strip()
    update_state(user_id, {"event_description": updated_description})

    # Мягкая авто-установка пола по тексту, если ещё не задан
    try:
        g_autohint = _infer_gender_hint(st, updated_description)
        if g_autohint and not (st.get("celebrant_gender") or "").strip():
            update_state(user_id, {"celebrant_gender": g_autohint})
            st = get_state(user_id) or {}
    except Exception as e:
        logger.debug("[block03] gender autohint failed: %s", e)

    
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

    # ❗ Валидация / нормализация celebrant_name перед апсертом
    combined_for_ctx = f"{prev_info}\n{message_text}".strip()
    kind_ctx = _detect_event_context(st, combined_for_ctx)
    if "celebrant_name" in parsed_data:
        nm = (parsed_data.get("celebrant_name") or "").strip()
        if kind_ctx == "wedding":
            # Пытаемся вытащить оба имени из текста клиента.
            # Примеры: "Жених Виктор невеста Екатерина", "Невеста Катя, жених Витя"
            _w1 = re.search(r"жених\s+([A-Za-zА-Яа-яЁё\-']+)[^\n\r]*?невест[аы]\s+([A-Za-zА-Яа-яЁё\-']+)",
                            combined_for_ctx, re.IGNORECASE)
            _w2 = re.search(r"невест[аы]\s+([A-Za-zА-Яа-яЁё\-']+)[^\n\r]*?жених\s+([A-Za-zА-Яа-яЁё\-']+)",
                            combined_for_ctx, re.IGNORECASE)
            if _w1 or _w2:
                n1, n2 = (_w1 or _w2).groups()
                parsed_data["celebrant_name"] = (
                    f"Жених {normalize_proper_name(n1)} невеста {normalize_proper_name(n2)}"
                )
            else:
                # Если двух имён в тексте нет — оставляем как есть (без строгой проверки),
                # чтобы не терять одиночное имя, которое дала модель.
                if not nm:
                    parsed_data.pop("celebrant_name", None)
        else:
            # Обычная жёсткая проверка для ДР/юбилеев/прочего
            if not _is_probable_name(nm):
                parsed_data.pop("celebrant_name", None)
            else:
                parsed_data["celebrant_name"] = normalize_proper_name(nm)
                # если имя явно женское по контексту — ещё раз подсветим пол (мягко)
                g_autohint2 = _infer_gender_hint(st, combined_for_ctx)
                if g_autohint2 and not (st.get("celebrant_gender") or "").strip():
                    parsed_data.setdefault("celebrant_gender", g_autohint2)
    
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

        # запомним старые значения чтобы понять — изменилась ли дата/время
        old_date_iso = (st.get("event_date_iso") or "").strip()
        old_time_24  = (st.get("event_time_24") or "").strip()
        if match_date:
            update_state(user_id, {"event_date_iso": _clean_date(match_date)})
        if match_time:
            update_state(user_id, {"event_time_24": _clean_time(match_time)})
        # если ключ даты-времени изменился → сбросить availability_lock
        st_after_dt = get_state(user_id) or {}
        new_date_iso = (st_after_dt.get("event_date_iso") or "").strip()
        new_time_24  = (st_after_dt.get("event_time_24") or "").strip()
        if f"{old_date_iso} {old_time_24}".strip() != f"{new_date_iso} {new_time_24}".strip():
            update_state(user_id, {"availability_lock": None})

        st = get_state(user_id)
        snap = build_structured_snapshot(st)
        update_state(user_id, {"structured_cache": snap})

    # 3) Если есть нормализованные дата+время — проверяем слоты и отвечаем
    if not st.get("availability_reply_sent"):
        date_iso = _clean_date(match_date) if match_date else None
        time_24  = _clean_time(match_time) if match_time else ""
        if date_iso and time_24:
            # ── Гейт по availability_lock: не пере-проверяем и не пере-уведомляем для той же даты-времени
            st = get_state(user_id) or {}
            cur_key = _event_dt_key(st)
            alock = (st.get("availability_lock") or {}) if cur_key else {}
            same_dt = (alock.get("for_dt") == cur_key) if cur_key else False
            already_notified = bool(alock.get("notified")) if same_dt else False

            if not same_dt:
                schedule = load_schedule_from_s3()
                availability = check_date_availability(date_iso, time_24, schedule)
                logger.info(f"[block03] availability={availability} for {date_iso} {time_24}")
                update_state(user_id, {
                    "availability_lock": {
                        "for_dt": cur_key,
                        "decision": availability,   # "available" | "need_handover" | "occupied"
                        "notified": False,
                        "ts": time.time(),
                    }
                })
                st = get_state(user_id) or {}
                alock = st.get("availability_lock") or {}
                same_dt = True
                already_notified = False

            # уведомляем только если ещё не уведомляли по этой дате-времени
            if same_dt and not already_notified:
                availability = alock.get("decision", "available")
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
                    alock["notified"] = True
                    update_state(user_id, {
                        "availability_lock": alock,
                        # эти флаги ставим, ТОЛЬКО если мы действительно отправили уведомление
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
                    # Инвалидируем все ранее запланированные напоминания этой итерации
                    _bump_b3_session(user_id)

            # Резервируем слот (без падений)
            if (get_state(user_id) or {}).get("date_decision_flag") == "available":
                try:
                    import utils.schedule as schedule_utils
                    if hasattr(schedule_utils, "reserve_slot"):
                        schedule_utils.reserve_slot(date_iso, time_24)
                except Exception as e:
                    logger.info("[block03] reserve_slot fail: %s", e)

            # если решение уже известно и оно не «available» — переводим в блок5
            decision = (get_state(user_id) or {}).get("date_decision_flag")
            if decision in ("need_handover", "occupied"):
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
        questions = build_questions(st, short_missing, context_text=combined_text)
        if questions:
            # мягкое вступление + наши пули (без LLM, чтобы не было «Как зовут…» когда уже «для Витя»)
            preface = _build_soft_preface(event_type, st)
            # избегаем двойного вступления: build_questions возвращает ТОЛЬКО буллеты
            if questions.strip():
                final_msg = f"{preface}\n{questions}"
            else:
                final_msg = preface
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
                REM_KEYS["r2_scheduled"]: False,
                # привязываем будущие напоминания к текущей сессии
                REM_SESS["r1_sid"]: _current_b3_session(get_state(user_id) or {})
            })
            # ИДЕМПОТЕНТНОСТЬ ПОСЛЕ ОТВЕТА: если до ответа уже был отправлен R1/R2,
            # новый R1 в этой же обработке НЕ ставим.
            cur = get_state(user_id) or {}
            if (not had_prior_reminder) and (not cur.get(REM_KEYS["r1_scheduled"])):
                delay1 = DELAY_TO_BLOCK_3_1_SEC
                logger.info("[block03] Schedule R1 (raw) in %ds", delay1)
                plan(user_id, "blocks.block_03:send_first_reminder_if_silent", delay1)
                update_state(user_id, {
                    REM_KEYS["r1_scheduled"]: True,
                    REM_SESS["r1_sid"]: _current_b3_session(get_state(user_id) or {})
                })
                # мы намеренно запускаем новую цепочку напоминаний → разрешаем их снова
                update_state(user_id, {"cancel_block3_reminders": False})
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

    # 6) Фолбек: все данные есть, но availability ещё не отправлен (учитываем availability_lock)
    st = get_state(user_id) or {}
    if (not missing) and (not st.get("availability_reply_sent")) and (st.get("event_date") or st.get("event_date_iso")) and (st.get("event_time") or st.get("event_time_24")):
        date_iso = st.get("event_date_iso") or _clean_date(st.get("event_date"))
        time_24  = st.get("event_time_24") or _clean_time(st.get("event_time"))
        if date_iso and time_24:
            cur_key = _event_dt_key(st)
            alock = (st.get("availability_lock") or {}) if cur_key else {}
            same_dt = (alock.get("for_dt") == cur_key) if cur_key else False
            already_notified = bool(alock.get("notified")) if same_dt else False

            if not same_dt:
                schedule = load_schedule_from_s3()
                availability = check_date_availability(date_iso, time_24, schedule)
                update_state(user_id, {
                    "availability_lock": {
                        "for_dt": cur_key,
                        "decision": availability,
                        "notified": False,
                        "ts": time.time(),
                    }
                })
                st = get_state(user_id) or {}
                alock = st.get("availability_lock") or {}
                same_dt = True
                already_notified = False

            if same_dt and not already_notified:
                availability = alock.get("decision", "available")
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
                    alock["notified"] = True
                    update_state(user_id, {
                        "availability_lock": alock,
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
                    _bump_b3_session(user_id)

            if (get_state(user_id) or {}).get("date_decision_flag") == "available":
                try:
                    import utils.schedule as schedule_utils
                    if hasattr(schedule_utils, "reserve_slot"):
                        schedule_utils.reserve_slot(date_iso, time_24)
                except Exception as e:
                    logger.info("[block03:fallback] reserve_slot fail: %s", e)

            decision = (get_state(user_id) or {}).get("date_decision_flag")
            if decision in ("need_handover", "occupied"):
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
    # Хвостовая постановка R1 — тоже не должна срабатывать сразу после ответа,
    # если в прошлой итерации уже был R1/R2
    if cur.get("last_bot_question") and not cur.get(REM_KEYS["r1_scheduled"]) and not had_prior_reminder:
        plan(user_id, "blocks.block_03:send_first_reminder_if_silent", DELAY_TO_BLOCK_3_1_SEC)
        update_state(user_id, {
            REM_KEYS["r1_scheduled"]: True,
            REM_SESS["r1_sid"]: _current_b3_session(cur),
            "cancel_block3_reminders": False,
    })

# ──────────────────────────────────────────────────────────────────────────────
# Напоминания (унифицированные)
# ──────────────────────────────────────────────────────────────────────────────

def send_first_reminder_if_silent(user_id, send_reply_func):
    st = get_state(user_id)
    if not st or st.get("stage") != "block3":
        return
    if st.get("cancel_block3_reminders"):
        return
    # Напоминаем только если всё ещё есть незаполненные данные
    missing = compute_missing(st)
    if not missing:
        return
    # Сессионная защита: если sid задан и НЕ равен текущей сессии — выходим.
    # Если sid отсутствует (None) — разрешаем (для юнит-тестов и бэкоффа).
    cur_sid = _current_b3_session(st)
    planned_sid = st.get(REM_SESS["r1_sid"])
    if planned_sid is not None and planned_sid != cur_sid:
        return
    last_bot_ts  = float(st.get("last_bot_ts") or 0)
    last_user_ts = float(st.get("last_user_ts") or 0)
    if last_user_ts > last_bot_ts:
        return
    if st.get(REM_KEYS["r1_sent"]):
        return

    # Формируем статическое сообщение R1:
    # 1) шапка из файла block03_reminder_1_static.txt
    # 2) список недостающих вопросов (как в основном блоке, максимум 3)
    # 3) концовка
    header = (load_prompt(PROMPTS["reminder_1_static"]) or "").strip()
    missing = compute_missing(st)
    short_missing = missing[:3]
    # используем историю клиента для определения контекста (свадьба/др/юбилей)
    history_text = str(st.get("event_description") or "")
    questions = build_questions(st, short_missing, context_text=history_text).strip()
    ending = "Я на связи в любое удобное для вас время!"
    parts = [p for p in [header, questions, ending] if p]
    reply = "\n".join(parts)
    sent_ok = False
    if reply:
        send_reply_func(reply)
        sent_ok = True

    now_ts = time.time()
    upd = {
        "stage": "block3",
        "last_message_ts": now_ts,
        "last_bot_question": questions or st.get("last_bot_question",""),
        REM_KEYS["r1_sent"]: True if sent_ok else st.get(REM_KEYS["r1_sent"]),
        "last_sender": "bot",
        "last_bot_ts": now_ts
     }
    update_state(user_id, upd)
    # Планируем второе напоминание (R2) только если реально отправили R1 в этой сессии
    if sent_ok:
        delay2 = DELAY_TO_BLOCK_3_2_SEC
        logger.info("[block03] Schedule R2 (raw) in %ds", delay2)
        plan(user_id, "blocks.block_03:send_second_reminder_if_silent", delay2)
        update_state(user_id, {
            REM_KEYS["r2_scheduled"]: True,
            REM_SESS["r2_sid"]: _current_b3_session(get_state(user_id) or {})
        })
 
def send_second_reminder_if_silent(user_id, send_reply_func):
    st = get_state(user_id)
    if not st or st.get("stage") != "block3":
        return
    if st.get("cancel_block3_reminders"):
        return
    # Напоминаем только если всё ещё есть незаполненные данные
    missing = compute_missing(st)
    if not missing:
        return
    # Сессионная защита (аналогично R1): разрешаем, если sid отсутствует.
    cur_sid = _current_b3_session(st)
    planned_sid = st.get(REM_SESS["r2_sid"])
    if planned_sid is not None and planned_sid != cur_sid:
        return
    last_bot_ts  = float(st.get("last_bot_ts") or 0)
    last_user_ts = float(st.get("last_user_ts") or 0)
    if last_user_ts > last_bot_ts:
        return
    if st.get(REM_KEYS["r2_sent"]):
        return

    # Формируем статическое сообщение R2:
    # 1) шапка из файла block03_reminder_2_static.txt
    # 2) список недостающих вопросов (как в основном блоке, максимум 3)
    # 3) концовка
    header = (load_prompt(PROMPTS["reminder_2_static"]) or "").strip()
    missing = compute_missing(st)
    short_missing = missing[:3]
    # используем историю клиента для определения контекста (свадьба/др/юбилей)
    history_text = str(st.get("event_description") or "")
    questions = build_questions(st, short_missing, context_text=history_text).strip()
    ending = ("Арсений готов подарить вам незабываемые эмоции, но ему будет гораздо проще,  "
              "если вы расскажете подробно о вашем празднике. Если сейчас неудобно, вы можете вновь "
              "обратиться в любое удобное время.")
    parts = [p for p in [header, questions, ending] if p]
    reply = "\n".join(parts)
    sent_ok = False
    if reply:
        send_reply_func(reply)
        sent_ok = True

    now_ts = time.time()
    upd = {
        "stage": "block3",
        "last_message_ts": now_ts,
        "last_bot_question": questions or st.get("last_bot_question",""),
        REM_KEYS["r2_sent"]: True if sent_ok else st.get(REM_KEYS["r2_sent"]),
        "last_sender": "bot",
        "last_bot_ts": now_ts
    }
    update_state(user_id, upd)
    # Финал планируем только если R2 реально было отправлено в этой сессии
    if sent_ok:
        plan(user_id, "blocks.block_03:finalize_if_still_silent", FINAL_TIMEOUT_SEC)
        update_state(user_id, {
            REM_KEYS["r2_scheduled"]: True,
            REM_SESS["fin_sid"]: _current_b3_session(get_state(user_id) or {})
        })

def finalize_if_still_silent(user_id, send_reply_func):
    from router import route_message
    st = get_state(user_id)
    if not st or st.get("stage") != "block3":
        return
    if st.get("cancel_block3_reminders"):
        return
    # Напоминаем только если всё ещё есть незаполненные данные
    missing = compute_missing(st)
    if not missing:
        return
    # Сессионная защита финала: если sid отсутствует — разрешаем, иначе требуем совпадение.
    cur_sid = _current_b3_session(st)
    planned_sid = st.get(REM_SESS["fin_sid"])
    if planned_sid is not None and planned_sid != cur_sid:
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