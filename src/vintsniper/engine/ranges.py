"""Ціновий діапазон, у якому власник хоче бачити алерти в Telegram.

Фільтр стоїть на собівартості лота (скільки платиш продавцю, без доставки) -
тобто на тому ж числі, за яким Discord розкладає знахідки по каналах. Пороги
вигоди він не чіпає: лот усе одно має пройти множник і поріг профіту, просто
власник вирішує, з якою полицею цін йому зараз зручно працювати.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# "все", "всі", "any" - зняти обмеження
_OPEN_WORDS = {"", "all", "any", "off", "*", "-", "все", "всі", "усі", "весь", "любий"}
# Кома тут роздільник між межами ("15,45"), а не десяткова крапка.
_NUM = r"\d+(?:\.\d+)?"


def _to_float(raw: str) -> float:
    return float(raw)


@dataclass(frozen=True)
class PriceRange:
    """Нижня межа включно, верхня включно. high=None означає «і дорожче»."""

    low: float = 0.0
    high: float | None = None

    @property
    def is_open(self) -> bool:
        return self.low <= 0 and self.high is None

    def contains(self, price_eur: float) -> bool:
        if price_eur < self.low:
            return False
        return self.high is None or price_eur <= self.high

    @property
    def label(self) -> str:
        if self.is_open:
            return "all"
        low = f"{self.low:g}"
        return f"{low}-{self.high:g}" if self.high is not None else f"{low}+"

    def __str__(self) -> str:  # для запису в базу
        return self.label

    @classmethod
    def open(cls) -> "PriceRange":
        return cls()

    @classmethod
    def parse(cls, text: str) -> "PriceRange | None":
        """Розбирає '0-15', '15 45', '45+', 'до 30', '30', 'all'. None = не зрозумів."""
        raw = (text or "").strip().lower().replace("€", " ").replace("eur", " ")
        raw = raw.replace("—", "-").replace("–", "-").strip()
        if raw in _OPEN_WORDS:
            return cls.open()

        # "від 15" / "from 15" / "15+" - без верхньої межі
        m = re.fullmatch(rf"(?:від|from|>=?)\s*({_NUM})|({_NUM})\s*\+", raw)
        if m:
            return cls(low=_to_float(m.group(1) or m.group(2)))

        # "до 30" / "<30" / "-30" - від нуля
        m = re.fullmatch(rf"(?:до|under|<=?|-)\s*({_NUM})", raw)
        if m:
            return cls(high=_to_float(m.group(1)))

        # "0-15", "15 45", "15,45" з довільним роздільником
        m = re.fullmatch(rf"({_NUM})\s*(?:-|\.\.|[,;]|\s)\s*({_NUM})", raw)
        if m:
            low, high = _to_float(m.group(1)), _to_float(m.group(2))
            if low > high:
                low, high = high, low
            return cls(low=low, high=high)

        # Просто число: читаємо як стелю, бо «/range 30» це «покажи до 30».
        if re.fullmatch(_NUM, raw):
            return cls(high=_to_float(raw))

        return None


def suggestions(bounds: list[float]) -> list[str]:
    """Готові варіанти діапазонів з тих самих меж, що ділять канали Discord."""
    if not bounds:
        return ["all"]
    ordered = sorted(bounds)
    out = [PriceRange(high=ordered[0]).label]
    for low, high in zip(ordered, ordered[1:]):
        out.append(PriceRange(low=low, high=high).label)
    out.append(PriceRange(low=ordered[-1]).label)
    return out
