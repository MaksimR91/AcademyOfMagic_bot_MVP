# tests/integration/test_openai_gpt.py
import os
import time
import pytest
from dataclasses import dataclass
from typing import List, Tuple, Any
from pathlib import Path

"""
Требуется ENV:
  OPENAI_API_KEY          — ключ
  OPENAI_MODEL_CLASSIFY   — (опц.) модель для классификации, по умолчанию gpt-4o-mini
  OPENAI_MODEL_TEXT       — (опц.) модель для текста, по умолчанию gpt-4o-mini

Запуск:
  pytest -q tests/integration/test_openai_gpt.py -s
"""

try:
    from openai import OpenAI  # runtime использование
except Exception:  # pragma: no cover
    OpenAI = None  # noqa: N816  (оставляем имя для проверок ниже)


REQUIRED = ("OPENAI_API_KEY",)
MODEL_CLASSIFY = os.getenv("OPENAI_MODEL_CLASSIFY", "gpt-4o-mini")
MODEL_TEXT     = os.getenv("OPENAI_MODEL_TEXT", "gpt-4o-mini")
REQUEST_TIMEOUT = float(os.getenv("OPENAI_TIMEOUT", "20"))
MAX_DATASET = int(os.getenv("OPENAI_TEST_LIMIT", "9999"))  # можно задать, напр. 12 в CI
EXPECTED_LABELS = {"детское", "взрослое", "семейное", "нестандартное"}

# пути к промптам
GLOBAL_PROMPT_PATH = Path("prompts/global_prompt.txt")
CLASSIF_PROMPT_PATH = Path("prompts/block02_classification_prompt.txt")
BLOCK4_PROMPT_PATH  = Path("prompts/block04_prompt.txt")
AVAIL_PROMPT_PATH   = Path("prompts/block03_availability_prompt.txt")



def _require_env():
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("Missing OPENAI_API_KEY; skipping OpenAI live tests")


def _client() -> Any:
    # Вешаем дефолтный таймаут на все запросы клиента
    return OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=REQUEST_TIMEOUT)



def classify_show(client: Any, text: str) -> str:
    """Классификация по реальным промптам: global + block02_classification_prompt.txt"""
    assert GLOBAL_PROMPT_PATH.exists(), "Нет prompts/global_prompt.txt"
    assert CLASSIF_PROMPT_PATH.exists(), "Нет prompts/block02_classification_prompt.txt"
    sys_prompt = GLOBAL_PROMPT_PATH.read_text(encoding="utf-8")
    # плейсхолдер {message_text} в шаблоне
    user_msg = CLASSIF_PROMPT_PATH.read_text(encoding="utf-8").format(message_text=text)
    rsp = client.chat.completions.create(
        model=MODEL_CLASSIFY,
        temperature=0,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_msg},
        ],
        timeout=REQUEST_TIMEOUT,
        max_tokens=8,
    )
    label = rsp.choices[0].message.content or ""
    label = label.strip().lower()
    # нормализация хвостов/синонимов
    label = label.replace("вариант:", "").replace(".", "").strip()
    syn = {
        "детская": "детское",
        "взрослая": "взрослое",
        "семейная": "семейное",
        "нестандартная": "нестандартное",
        "неклассическое": "нестандартное",
    }
    label = syn.get(label, label)
    return label


def gen_materials_text(client: Any, show_type: str) -> str:
    """Короткий сопроводительный текст для отправки КП+видео по реальному промпту block04_prompt.txt."""
    assert GLOBAL_PROMPT_PATH.exists(), "Нет prompts/global_prompt.txt"
    assert BLOCK4_PROMPT_PATH.exists(), "Нет prompts/block04_prompt.txt"
    sys_prompt = GLOBAL_PROMPT_PATH.read_text(encoding="utf-8")
    # допускаем {show_type} в stage-промпте
    base = BLOCK4_PROMPT_PATH.read_text(encoding="utf-8")
    try:
        user_msg = base.format(show_type=show_type)
    except Exception:
        user_msg = base
    # жёсткое ограничение длины в самом промпте
    user_msg += "\n\nТребование: сделай краткий сопроводительный текст 2–4 предложения, без приветствий и подписей, без ссылок и эмодзи, максимум 350 символов."
    rsp = client.chat.completions.create(
        model=MODEL_TEXT,
        temperature=0.2,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_msg},
        ],
        timeout=REQUEST_TIMEOUT,
        max_tokens=300,
     )
    txt = (rsp.choices[0].message.content or "").strip()
    # финальная защита от пролёта по длине
    return txt[:400]


@dataclass
class Case:
    text: str
    expected: str  # один из EXPECTED_LABELS


def _dataset() -> List[Case]:
    """
    ≥20 фраз, покрываем все классы.
    """
    ds: List[Case] = [
        # детское
        Case("День рождения сына, 7 лет, дома, будет много детей", "детское"),
        Case("Праздник в детсаду у дочки, 5 лет, утренник", "детское"),
        Case("Шоу для школьников, класс 2-Б, после обеда", "детское"),
        Case("Мальчику 10 лет, кафе, 15 друзей", "детское"),
        Case("Ёлка во дворе для малышей, нужен фокусник", "детское"),
        # взрослое
        Case("Свадьба в ресторане, 50 гостей, сцена есть", "взрослое"),
        Case("Юбилей у папы, 60 лет, зал небольшой", "взрослое"),
        Case("Юбилей дедушки 70 лет, дома", "взрослое"),
        Case("Свадьба на природе, 100 гостей", "взрослое"),
        Case("Годовщина свадьбы, семья и друзья", "взрослое"),
        # семейное
        Case("Семейный праздник, будут и дети, и взрослые", "семейное"),
        Case("Поход всей семьи в кафе, 20 человек, поровну детей и взрослых", "семейное"),
        Case("Крещение племянника, семейный ужин", "семейное"),
        Case("Праздник во дворе, семьи с детьми", "семейное"),
        Case("Домашний праздник: половина дети, половина взрослые", "семейное"),
        # нестандартное
        Case("Съёмка клипа, хотим необычные трюки на камеру", "нестандартное"),
        Case("Квест в торговом центре, нужно интерактив с фокусами", "нестандартное"),
        Case("Уличный перформанс на фестивале, открытая площадка", "нестандартное"),
        Case("Рекламная интеграция с продуктом, нужен спец-трюк", "нестандартное"),
        Case("Сюрприз-предложение руки и сердца с иллюзией", "нестандартное"),
    ]
    return ds


def test_openai_classification_accuracy_live():
    _require_env()
    assert OpenAI is not None, "openai SDK not installed"
    client = _client()

    ds = _dataset()
    if len(ds) > MAX_DATASET:
        ds = ds[:MAX_DATASET]
    ok = 0
    results: List[Tuple[str, str, str]] = []  # (text, expected, got)
    for case in ds:
        got = classify_show(client, case.text)
        results.append((case.text, case.expected, got))
        if got not in EXPECTED_LABELS:
            pytest.fail(f"Модель вернула недопустимый класс: {got!r} for text={case.text!r}")
        if got == case.expected:
            ok += 1
        time.sleep(0.15)  # щадим rate limit

    acc = ok / len(ds)
    # Лог в случае провала
    if acc < 0.9:
        pretty = "\n".join([f"- {t} → ожидалось {e}, получено {g}" for t, e, g in results if e != g])
        pytest.fail(f"Точность классификации {acc:.2%} < 90%.\nОшибки:\n{pretty}")

    assert acc >= 0.90


@pytest.mark.parametrize("show_type", ["детское", "взрослое", "семейное"])
def test_openai_materials_text_live(show_type):
    _require_env()
    client = _client()
    txt = gen_materials_text(client, show_type)
    # Проверки по ТЗ
    assert isinstance(txt, str) and len(txt) > 0
    assert len(txt) <= 400
    # без ссылок/эмодзи — грубая эвристика
    assert "http" not in txt.lower()
    forbidden = {"🎉", "✨", "😊", "👍", "😉", "🔥"}
    assert not any(ch in txt for ch in forbidden), f"Эмодзи не допускаются: {txt}"


def _call_availability_prompt(client: Any, *, message_text: str, date_iso: str, time_24: str, availability: str) -> str:
    """Генерим текст подтверждения даты/времени: global + block03a_availability_prompt.txt."""
    assert GLOBAL_PROMPT_PATH.exists(), "Нет prompts/global_prompt.txt"
    assert AVAIL_PROMPT_PATH.exists(),  "Нет prompts/block03_availability_prompt.txt"
    from datetime import datetime
    sys_prompt = GLOBAL_PROMPT_PATH.read_text(encoding="utf-8")
    client_request_date = datetime.now().strftime("%d %B %Y")
    user_msg = AVAIL_PROMPT_PATH.read_text(encoding="utf-8").format(
        message_text=message_text,
        date_iso=date_iso,
        time_24=time_24,
        client_request_date=client_request_date,
        availability=availability,
    )
    rsp = client.chat.completions.create(
        model=MODEL_TEXT,
        temperature=0.0,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_msg},
        ],
        timeout=REQUEST_TIMEOUT,
        max_tokens=250,
    )
    return (rsp.choices[0].message.content or "").strip()


@pytest.mark.parametrize("availability,expect_free", [
    ("available", True),
    ("occupied", False),
    ("need_handover", False),
])
def test_openai_availability_text_live(availability, expect_free):
    _require_env()
    client = _client()
    txt = _call_availability_prompt(
        client,
        message_text="Хотим 3 сентября в 15:00. Юбилей сына.",
        date_iso="2025-09-03",
        time_24="15:00",
        availability=availability,
    )
    assert txt, "Пустой ответ для availability"
    # грубые проверки стиля
    assert "http" not in txt.lower()
    for ch in ("🎉","✨","😊","👍","😉","🔥"):
        assert ch not in txt
    # смысл: при available — обещание выступить, иначе — «свяжется позже»
    low = txt.lower()
    if expect_free:
        assert ("сможет" in low or "свободн" in low or "подтверж" in low), f"Нет явного подтверждения: {txt}"
    else:
        assert ("свяж" in low or "позже" in low or "уточн" in low), f"Нет явного сообщения про дальнейшую связь: {txt}"
    time.sleep(0.2)
