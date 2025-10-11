# utils/ru_morph.py
from __future__ import annotations
import re
from functools import lru_cache

# Пытаемся использовать pymorphy3 или pymorphy2
_ANALYZER = None
try:
    import pymorphy3 as _pymorphy
    _ANALYZER = _pymorphy.MorphAnalyzer()
except Exception:
    try:
        import pymorphy2 as _pymorphy
        _ANALYZER = _pymorphy.MorphAnalyzer()
    except Exception:
        _ANALYZER = None

# --- утилита: привести dst к регистру src (Title/UPPER/lower/как есть) -----
def _apply_casing_like(src: str, dst: str) -> str:
    if not src:
        return dst
    # Полностью В ВЕРХНЕМ?
    if src.isupper():
        return dst.upper()
    # Полностью в нижнем?
    if src.islower():
        return dst.lower()
    # Title-case? (первая буква заглавная, остальное — как правило строчные)
    # .istitle() в русском работает приемлемо для имён.
    if src.istitle():
        # Титлкейс токена: первая буква заглавная, остальное — нижний
        return dst[:1].upper() + dst[1:].lower()
    # Смешанный/кастомный кейс — стараемся хотя бы первую букву перенести
    if src[0].isalpha() and src[0].isupper():
        return dst[:1].upper() + dst[1:]
    return dst

# Небольшие эвристики на случай отсутствия морфологии
def _fallback_gent(word: str) -> str:
    w = word
    low = w.lower()
    # очень грубые правила; покрывают топ-кейсы
    if low.endswith("а"):
        # Дима → Димы, Оля/Ника после шипящих/й → "и"
        base = w[:-1]
        return base + ("и" if base[-1:].lower() in "гкхжчшщй" else "ы")
    if low.endswith("я"):
        return w[:-1] + "и"   # Илья/Оля → Ильи/Оли
    if low.endswith("й"):
        return w[:-1] + "я"   # Андрей → Андрея
    if low.endswith("ь"):
        return w[:-1] + "я"   # Игорь → Игоря / Любовь → Любови (сложный кейс, но ок для частых муж. имён)
    # муж. имена на согласную → добавляем "а" (Пётр/Макс → Петра/Макса)
    if re.search(r"[бвгджзйклмнпрстфхцчшщ]$", low):
        return w + "а"
    return w  # не меняем, если не уверены

@lru_cache(maxsize=2048)
def _inflect_token(token: str, target_case: str, gender_hint: str|None) -> str:
    if not token or not token[0].isalpha():
        return token
    if _ANALYZER is None:
        base = _fallback_gent(token) if target_case == "gent" else token
        return _apply_casing_like(token, base)

    parses = _ANALYZER.parse(token)
    if not parses:
        base = _fallback_gent(token) if target_case == "gent" else token
        return _apply_casing_like(token, base)

    # выбираем наиболее вероятный разбор, предпочтительно имя собственное с нужным родом
    best = None
    for p in parses:
        if 'Name' in p.tag:
            best = p
            break
    best = best or parses[0]

    grammemes = {target_case}
    g = (gender_hint or "").strip().lower()
    if g in {"м","муж","мужской","male","man"}:
        grammemes.add('masc')
    elif g in {"ж","жен","женский","female","woman"}:
        grammemes.add('femn')

    inf = best.inflect(grammemes)
    if inf:
        return _apply_casing_like(token, inf.word)
    # второй шанс — без рода
    inf = best.inflect({target_case})
    if inf:
        return _apply_casing_like(token, inf.word)
    base = _fallback_gent(token) if target_case == "gent" else token
    return _apply_casing_like(token, base)

def to_case_name(full_name: str, target_case: str = "gent", gender_hint: str|None = None) -> str:
    """Склоняет имя (1–2 слова, дефисы поддерживаем), максимально осторожно.
       Если не получилось — возвращает исходник."""
    if not full_name:
        return full_name
    parts = re.split(r"(\s+|-)", full_name.strip())  # сохраняем разделители
    out = []
    for p in parts:
        if p.strip() and p.strip().replace("'", "").replace("’", "").isalpha():
            out.append(_inflect_token(p, target_case, gender_hint))
        else:
            out.append(p)
    return "".join(out)

def to_genitive_name(full_name: str, gender_hint: str|None = None) -> str:
    return to_case_name(full_name, "gent", gender_hint)

# Публичная утилита: привести имя к «человеческому» виду (каждое слово с заглавной)
def normalize_proper_name(name: str) -> str:
    if not name:
        return name
    # Токенизируем по словам с поддержкой апострофов/дефисов, остальное оставляем как есть
    def _fix(m):
        w = m.group(0)
        return w[:1].upper() + w[1:].lower()
    return re.sub(r"[A-Za-zА-Яа-яЁё]+(?:['’][A-Za-zА-Яа-яЁё]+)?", _fix, name)