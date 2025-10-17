# utils/lang_detect.py
from langdetect import detect, DetectorFactory, LangDetectException

# фиксируем сид, чтобы результаты были стабильны
DetectorFactory.seed = 0

RU_CODES = {"ru"}  # по ТЗ «только русский»
# кириллица вне русского алфавита (частые маркеры kk/uk/и др.)
NON_RU_CYR = set("іїєґЎўІЇЄҐәӘңҢұҰүҮөӨқҚғҒһҺʼ’ˈ`")
RUSSIAN_CYR = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюя")  # базовый набор

def detect_lang(text: str) -> str:
    try:
        return detect(text or "")
    except LangDetectException:
        # пустое/мусор → считаем русским, чтобы не блокировать
        return "ru"

def is_russian(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    low = t.lower()
    # быстрые классы символов
    has_cyr = any("а" <= ch <= "я" or ch in ("ё", "Ё") or ("А" <= ch <= "Я") for ch in low)
    has_lat = any("a" <= ch <= "z" or "A" <= ch <= "Z" for ch in t)
    # если явно присутствуют не-русские кириллические буквы — это не русский
    if any(ch in NON_RU_CYR for ch in low):
        return False
    # если доля русской кириллицы среди всех букв ≥ 20% — считаем русским
    # считаем только буквенные символы, цифры/знаки препинания игнорируем
    letters = [ch for ch in low if ch.isalpha()]
    if letters:
        ru_cyr_cnt = sum(1 for ch in letters if ch in RUSSIAN_CYR or ch == "ё")
        ratio = ru_cyr_cnt / len(letters)
        if ratio >= 0.20:
            return True
    # fallback к детектору
    lang = detect_lang(t)
    if lang in RU_CODES:
        return True
    # мягкая эвристика по распространённым русским словам
    RU_HINTS = ("здравствуйте", "привет", "можно", "хочу", "на", "и", "как", "шоу")
    if has_cyr and any(w in low for w in RU_HINTS):
        return True
    return False

# простая эвристика согласия/отказа по нескольким языкам
YES_WORDS = {
    "ru": {"да", "ок", "хорошо", "ага", "конечно"},
    "en": {"yes", "ok", "okay", "sure", "yep"},
    "tr": {"evet", "tamam", "olur"},
    "kk": {"иә", "иа", "ok"},
    "uk": {"так", "ок", "гаразд"},
}
NO_WORDS = {
    "ru": {"нет", "не", "не хочу"},
    "en": {"no", "nope", "nah"},
    "tr": {"hayır", "hayir", "yok"},
    "kk": {"жоқ", "joq"},
    "uk": {"ні", "не"},
}

def is_affirmative(text: str, lang: str) -> bool:
    t = (text or "").strip().lower()
    bags = set()
    bags |= YES_WORDS.get("ru", set())
    bags |= YES_WORDS.get("en", set())
    bags |= YES_WORDS.get(lang, set())
    return any(w in t for w in bags)

def is_negative(text: str, lang: str) -> bool:
    t = (text or "").strip().lower()
    bags = set()
    bags |= NO_WORDS.get("ru", set())
    bags |= NO_WORDS.get("en", set())
    bags |= NO_WORDS.get(lang, set())
    return any(w in t for w in bags)