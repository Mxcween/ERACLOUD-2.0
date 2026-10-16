"""Колір речі з назви лота.

Vinted окремим полем колір у каталозі не віддає, тому беремо слова з назви:
"Nike Hoodie Black - M", "Bluza niebieska", "Pulli schwarz". Знайшли колір,
якого немає в списку дозволених, - лот не проходить. Не знайшли жодного
кольорового слова - пропускаємо, бо це просто звичайна назва. Фото потім
перевіряє Нікс.

Дозволені задаються змінною ALLOWED_COLOURS (через кому), за замовчуванням
чорний, білий, фіолетовий, синій, червоний, рожевий.
"""
from __future__ import annotations

import os

from .titles import find_word

# Канонічна назва -> слова різними мовами (на Vinted це PL, DE, FR, IT, EN)
COLOUR_WORDS: dict[str, tuple[str, ...]] = {
    "black": ("black", "schwarz", "czarny", "czarna", "czarne", "noir", "nero", "negro"),
    "white": ("white", "weiss", "weisse", "biały", "biala", "biała", "biale", "blanc", "bianco", "blanco"),
    "purple": ("purple", "violet", "lila", "fioletowy", "fioletowa", "fiolet", "purpur", "viola"),
    "blue": ("blue", "blau", "niebieski", "niebieska", "niebieskie", "bleu", "blu", "navy", "marine", "azzurro"),
    "orange": ("orange", "orangen", "pomarańczowy", "pomaranczowy", "pomarańczowa", "pomaranczowa", "orangefarben", "arancione"),
    "red": ("red", "rot", "rote", "czerwony", "czerwona", "czerwone", "rouge", "rosso"),
    "green": ("green", "grün", "gruen", "zielony", "zielona", "zielone", "vert", "verde"),
    "yellow": ("yellow", "gelb", "żółty", "zolty", "żółta", "zolta", "jaune", "giallo"),
    "pink": ("pink", "rosa", "różowy", "rozowy", "różowa", "rozowa", "rose"),
    "grey": ("grey", "gray", "grau", "szary", "szara", "szare", "gris", "grigio"),
    "brown": ("brown", "braun", "brązowy", "brazowy", "brązowa", "brazowa", "marron", "marrone"),
    "beige": ("beige", "creme", "kremowy", "kremowa", "khaki"),
}

DEFAULT_ALLOWED = ("black", "white", "purple", "blue", "red", "pink")


def allowed_colours() -> set[str]:
    raw = os.environ.get("ALLOWED_COLOURS", "").strip()
    if not raw:
        return set(DEFAULT_ALLOWED)
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def colours_in(title: str) -> set[str]:
    found: set[str] = set()
    for colour, words in COLOUR_WORDS.items():
        if find_word(title, words) is not None:
            found.add(colour)
    return found


def colour_rejection(title: str) -> str | None:
    """Причина відсіву або None, якщо колір годиться (чи його не видно)."""
    found = colours_in(title)
    if not found:
        return None
    allowed = allowed_colours()
    if found & allowed:
        return None
    return f"колір {'/'.join(sorted(found))} не в списку"
