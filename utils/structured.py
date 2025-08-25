# utils/structured.py
KEY_ORDER = [
    "event_date","event_time","event_location",
    "celebrant_name","celebrant_gender","celebrant_age",
    "guests_count","children_adult_ratio",
    "guests_children_age","guests_children_gender",
    "no_celebrant"
]

_TRUE = {"true","yes","да","y","1"}
def _is_true(v): 
    return str(v).strip().lower() in _TRUE

def build_structured_snapshot(state: dict) -> dict:
    """Единый «снимок» данных для передачи Арсению/CRM.
    Источник истины — только state. Ничего не пишет обратно."""
    st = state or {}
    snap = {}
    for k in KEY_ORDER:
        v = st.get(k)
        if v is None or str(v).strip() == "":
            snap[k] = "не указано"
        else:
            if k == "no_celebrant":
                snap[k] = "Да" if _is_true(v) else "нет"
            else:
                snap[k] = str(v).strip()
    return snap