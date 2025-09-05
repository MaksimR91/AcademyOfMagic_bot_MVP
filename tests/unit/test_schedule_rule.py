import pytest
from datetime import datetime, timedelta
import types

# today/tomorrow => need_handover
@pytest.mark.parametrize("now_local", [
    datetime(2025, 8, 30, 10, 0),
])
def test_today_is_need_handover(monkeypatch, now_local):
    from utils import schedule as sched

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now_local  # модуль schedule сейчас без TZ — фиксируем временем

    monkeypatch.setattr(sched, "datetime", FakeDT)
    assert sched.check_date_availability("2025-08-30", "15:00", []) == "need_handover"

def test_tomorrow_is_need_handover(monkeypatch):
    from utils import schedule as sched
    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2025, 8, 29, 10, 0)
    monkeypatch.setattr(sched, "datetime", FakeDT)

    assert sched.check_date_availability("2025-08-30", "15:00", []) == "need_handover"

@pytest.mark.parametrize("slot_time, req_time, expected", [
    ("12:00","14:59","occupied"),
    ("12:00","15:00","occupied"),
    ("12:00","15:01","available"),
])
def test_same_day_plus_minus_3h_edges(monkeypatch, slot_time, req_time, expected):
    from utils import schedule as sched

    # Делаем так, чтобы 2025-09-03 НЕ был сегодня/завтра
    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2025, 9, 1, 10, 0)  # 3 сентября — через 2 дня

    monkeypatch.setattr(sched, "datetime", FakeDT)

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

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2025, 9, 3, 9, 0)

    monkeypatch.setattr(sched, "datetime", FakeDT)

    slots = [{"date":"2025-09-03","time":"12:00"}]  # есть запись в этот день
    assert sched.check_date_availability("2025-09-03", "15:00", slots) == "need_handover"