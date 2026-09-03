"""Розбір розмірів Vinted.

Vinted пише розмір як "M / 38 / 10" для одягу, "43" або "40,5" для взуття
і порожнім рядком для більшості аксесуарів.
"""
from __future__ import annotations

import re

_CLOTHING_TOKEN = re.compile(r"^\s*(XXS|XS|S|M|L|XL|XXL|XXXL|\d+XL)\b", re.IGNORECASE)
_SHOE_NUMBER = re.compile(r"(\d{2}(?:[.,]\d)?)")


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
