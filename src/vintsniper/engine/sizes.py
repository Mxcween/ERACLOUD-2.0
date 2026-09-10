"""Розбір розмірів Vinted.

Vinted пише розмір як "M / 38 / 10" для одягу, "43" або "40,5" для взуття
і порожнім рядком для більшості аксесуарів.
"""
from __future__ import annotations

import re

_CLOTHING_TOKEN = re.compile(r"^\s*(XXS|XS|S|M|L|XL|XXL|XXXL|\d+XL)\b", re.IGNORECASE)
_SHOE_NUMBER = re.compile(r"(\d{2}(?:[.,]\d)?)")
# Талія в дюймах: "W32", "W 32", "32W"
_WAIST_INCHES = re.compile(r"\bW\s?(\d{2})\b|\b(\d{2})\s?W\b", re.IGNORECASE)
# Європейський розмір штанів окремим числом: "DE 48", "46 | W30", "38"
_TROUSER_EU = re.compile(r"\b(\d{2})\b")


def clothing_size(size_title: str) -> str | None:
    """Витягує буквений розмір: "M / 38 / 10" -> "M"."""
    if not size_title:
        return None
    match = _CLOTHING_TOKEN.match(size_title.strip())
    return match.group(1).upper() if match else None


def shoe_size_eu(size_title: str) -> float | None:
    """Витягує європейський розмір взуття: "40,5" -> 40.5."""
    if not size_title:
        return None
    match = _SHOE_NUMBER.search(size_title.replace(",", "."))
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    # Відсікаємо явно не-європейські числа (US/UK розміри та довжини в см)
    return value if 30.0 <= value <= 55.0 else None


def waist_sizes(size_title: str) -> tuple[int | None, int | None]:
    """Розбирає розмір штанів: повертає (талія в дюймах, європейський).

    Vinted пише штани по-різному навіть в одній категорії: "W32 | DE 48",
    "46 | W30", просто "38". Букви там бувають хіба в джогерах, тому фільтр,
    який знає лише S/M/L, викидав майже всі джинси й штани - їх качали з
    Vinted і одразу відкидали як "розмір не підходить".
    """
    if not size_title:
        return None, None

    inches = None
    match = _WAIST_INCHES.search(size_title)
    if match:
        inches = int(match.group(1) or match.group(2))

    eu = None
    for raw in _TROUSER_EU.findall(size_title):
        value = int(raw)
        # Дюйми ми вже забрали вище; лишається європейська сітка. 40-60 не
        # перетинається з дюймовою (26-40), тому плутанини не буде.
        if 40 <= value <= 60 and value != inches:
            eu = value
            break
    return inches, eu
