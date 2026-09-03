"""Пошук слів у назві лота.

Назви на Vinted багатомовні й із діакритикою, тому порівнюємо нормалізовано.
Шукаємо саме цілі слова: інакше "tag" знайшовся б усередині "vintage", а
"patch" усередині "dispatch".
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache


def normalise(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.casefold()


@lru_cache(maxsize=32)
def _compile(words: tuple[str, ...]) -> re.Pattern[str] | None:
    cleaned = [normalise(w).strip() for w in words if w and w.strip()]
    if not cleaned:
        return None
    # Довші фрази першими, щоб "box only" перемагав над "box"
    cleaned.sort(key=len, reverse=True)
    alternatives = "|".join(re.escape(w) for w in cleaned)
    # (?<!\w) замість \b: працює і для фраз, і для слів з цифрами на кшталт "1:1"
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)")


def find_word(title: str, words: list[str] | tuple[str, ...] | None) -> str | None:
    """Повертає перше знайдене слово зі списку або None."""
    if not words:
        return None
    pattern = _compile(tuple(words))
    if pattern is None:
        return None
    match = pattern.search(normalise(title))
    return match.group(0) if match else None
