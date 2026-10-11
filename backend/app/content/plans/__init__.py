"""Планы к совпадениям: что делать, когда оба выбрали карточку.

Ключ — текст карточки из app/content/wishes.py. Позже весь контент переедет в Directus.
У плана есть вступление и 2–3 варианта; пара выбирает один вдвоём.
Вариант: title, note, items_title + items (список продуктов / что взять), steps.
Если items_title == «Продукты», список можно отправить в покупки.
"""
from ._types import Plan
from .dates import PLANS as _dates
from .home import PLANS as _home
from .romance import PLANS as _romance
from .spicy import PLANS as _spicy
from .travel import PLANS as _travel

PLANS: dict[str, Plan] = {**_dates, **_home, **_travel, **_romance, **_spicy}


def plan_for(card_text: str) -> Plan | None:
    return PLANS.get(card_text)
