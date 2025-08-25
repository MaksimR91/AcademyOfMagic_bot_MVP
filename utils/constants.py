# utils/constants.py
"""
Единый «паспорт заявки» – какие данные считаем собранными.
Названия полей ≈ ключи в state – их видит и код, и LLM-промпт.
"""
REQUIRED_FIELDS = [
    # дата/время
    "event_date",          # '2025-07-03'
    "event_time",          # '18:30'
    # место
    "address",             # полный адрес
    "place_type",          # 'home' | 'garden' | 'cafe'
    # виновник торжества
    "celebrant_name",
    "celebrant_gender",    # 'm' / 'f' / None
    "celebrant_age",
    # состав гостей
    "guests_count",        # общее количество
    "children_at_party",   # True – будут ещё дети, False – нет, None – не знаем
    # комбо-параметры
    "package",             # выбранный пакет (mini/standard/…)
    "saw_show_before",     # видел ли клиент шоу ранее
    "has_photo",           # есть ли фото виновника торжества
    "special_wishes",      # особые пожелания, текст
]
# Суффикс, который мы добавляем к каждому полю, когда заводим
# флаг «поле заполнено».
FLAG_SUFFIX: str = "_ok"
# Пример: event_date_ok, address_ok, …
# ------------------------------------------------------------------
MAX_Q_ATTEMPTS = 2          # 1‑е касание + 1 напоминание
REFUSAL_MARKERS = [
    "не хочу", "не буду", "не скажу",
    "позже", "пока не знаю", "не уверен",
]