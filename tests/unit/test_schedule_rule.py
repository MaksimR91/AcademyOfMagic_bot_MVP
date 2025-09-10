import pytest
from datetime import datetime, timedelta
import types
from zoneinfo import ZoneInfo

# today/tomorrow => need_handover
@pytest.mark.parametrize("now_local", [
    datetime(2025, 8, 30, 10, 0),
])
def test_today_is_need_handover(monkeypatch, now_local):
    from utils import schedule as sched

    # Фиксируем "сейчас" через _now_atyrau (мокаем момент, не таймзону)
    monkeypatch.setattr(sched, "_now_atyrau", lambda: now_local.replace(tzinfo=ZoneInfo("Asia/Atyrau")))
    assert sched.check_date_availability("2025-08-30", "15:00", []) == "need_handover"

def test_tomorrow_is_need_handover(monkeypatch):
    from utils import schedule as sched
    monkeypatch.setattr(
        sched,
        "_now_atyrau",
        lambda: datetime(2025, 8, 29, 10, 0, tzinfo=ZoneInfo("Asia/Atyrau"))
    )

    assert sched.check_date_availability("2025-08-30", "15:00", []) == "need_handover"

@pytest.mark.parametrize("slot_time, req_time, expected", [
    ("12:00","14:59","occupied"),
    ("12:00","15:00","occupied"),
    ("12:00","15:01","available"),
])
def test_same_day_plus_minus_3h_edges(monkeypatch, slot_time, req_time, expected):
    from utils import schedule as sched

    # Фиксируем "сейчас" так, чтобы 2025-09-03 НЕ был сегодня/завтра
    monkeypatch.setattr(
        sched,
        "_now_atyrau",
        lambda: datetime(2025, 9, 1, 10, 0, tzinfo=ZoneInfo("Asia/Atyrau"))
    )

    slots = [{"date":"2025-09-03","time":slot_time}]
    assert sched.check_date_availability("2025-09-03", req_time, slots) == expected

def test_json_unavailable_leads_to_handover(monkeypatch):
    from utils import schedule as sched
    # имитируем падение S3 при загрузке расписания
    def boom():
        raise RuntimeError("s3 down")
    monkeypatch.setattr(sched, "load_schedule_from_s3", boom)

    assert sched.get_availability("2025-09-05", "10:00") == "need_handover"

def test_today_overrides_slots_to_handover(monkeypatch):
    from utils import schedule as sched

    monkeypatch.setattr(
        sched,
        "_now_atyrau",
        lambda: datetime(2025, 9, 3, 9, 0, tzinfo=ZoneInfo("Asia/Atyrau"))
    )

    slots = [{"date":"2025-09-03","time":"12:00"}]  # есть запись в этот день
    assert sched.check_date_availability("2025-09-03", "15:00", slots) == "need_handover"

# ─────────────────────────────────────────────────────────────────
def test_today_tomorrow_respect_asia_atyrau(monkeypatch):
    from utils import schedule as sched
    # 2025-09-01 23:30 в Атырау — это "сегодня"
    monkeypatch.setattr(
        sched, "_now_atyrau",
        lambda: datetime(2025, 9, 1, 23, 30, tzinfo=ZoneInfo("Asia/Atyrau"))
    )
    assert sched.check_date_availability("2025-09-01", "12:00", []) == "need_handover"
    # А следующий день — "завтра"
    monkeypatch.setattr(
        sched, "_now_atyrau",
        lambda: datetime(2025, 9, 1, 23, 30, tzinfo=ZoneInfo("Asia/Atyrau"))
    )
    assert sched.check_date_availability("2025-09-02", "12:00", []) == "need_handover"

# ─────────────────────────────────────────────────────────────────
# Несколько слотов в один день: выбирается ближайший для окна ±3ч
@pytest.mark.parametrize("req_time, expected", [
    ("15:00", "occupied"),   # ближе к 12:00, ровно +3ч → занято
    ("16:01", "occupied"),   # ближе к 19:00, 19:00 - 16:01 = 2:59 → занято
    ("15:01", "available"),  # до 12:00 = 3:01, до 19:00 = 3:59 → свободно
    ("17:59", "occupied"),   # 19:00 - 17:59 = 1:01 → занято
    ("22:01", "available"),  # 22:01 - 19:00 = 3:01 → свободно
])
def test_multiple_slots_pick_nearest_with_window(monkeypatch, req_time, expected):
    from utils import schedule as sched

    # чтобы 2025-09-10 не был сегодня/завтра
    monkeypatch.setattr(
        sched,
        "_now_atyrau",
        lambda: datetime(2025, 9, 7, 10, 0, tzinfo=ZoneInfo("Asia/Atyrau"))
    )

    slots = [
        {"date": "2025-09-10", "time": "12:00"},
        {"date": "2025-09-10", "time": "19:00"},
    ]
    assert sched.check_date_availability("2025-09-10", req_time, slots) == expected

# ─────────────────────────────────────────────────────────────────
# Пустой JSON от S3 → need_handover
def test_empty_schedule_list_leads_to_handover(monkeypatch):
    from utils import schedule as sched

    def load_ok():        return []  # корректный формат, но пустой список

    monkeypatch.setattr(sched, "load_schedule_from_s3", load_ok)
    assert sched.get_availability("2025-09-12", "11:00") == "need_handover"

# Битый JSON (напр., "{}" строкой) → need_handover
def test_invalid_json_body_leads_to_handover(monkeypatch):
    from utils import schedule as sched

    def load_bad():
        # симулируем слой загрузки, вернув сырой JSON-текст,
        # который парсер распознает как невалидную структуру расписания
        return "{}"

    monkeypatch.setattr(sched, "load_schedule_from_s3", load_bad)
    assert sched.get_availability("2025-09-12", "11:00") == "need_handover"