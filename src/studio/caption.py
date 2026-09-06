"""Складання опису під пост із короткого рядка.

Формат навмисно один: бренд / річ / розмір / ціна / стан / заміри.
Порядок полів фіксований, зайві поля можна не писати. Розділювач -
скісна риска або новий рядок, бо на телефоні і те, і те зручно.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

HASHTAGS = "#prospekt23 #секондхенд #вінтаж #оригінал"

FIELD_ORDER = ["brand", "item", "size", "price", "condition", "measurements"]

# "48 56 68 64", "48-56-68-64", "48/56/68/64"
_MEASURE_LABELS = ["Плечі", "Груди", "Довжина", "Рукав"]


@dataclass
class Draft:
    brand: str = ""
    item: str = ""
    size: str = ""
    price: str = ""
    condition: str = ""
    measurements: list[str] | None = None

    @property
    def usable(self) -> bool:
        return bool(self.brand or self.item)


def parse(text: str) -> Draft:
    parts = [p.strip() for p in re.split(r"[/\n|]+", text or "") if p.strip()]
    d = Draft()
    for name, value in zip(FIELD_ORDER, parts):
        if name == "measurements":
            nums = re.findall(r"\d+(?:[.,]\d+)?", value)
            d.measurements = nums or None
        else:
            setattr(d, name, value)
    # Заміри могли поїхати в поле стану, якщо щось пропустили
    if d.measurements is None and len(parts) > len(FIELD_ORDER) - 1:
        nums = re.findall(r"\d+", parts[-1])
        if len(nums) >= 3:
            d.measurements = nums
    return d


def _price(raw: str) -> str:
    if not raw:
        return ""
    if re.search(r"[₴$€]|грн|uah", raw, re.I):
        return raw
    digits = re.sub(r"[^\d]", "", raw)
    return f"{digits} ₴" if digits else raw


def _condition(raw: str) -> str:
    if not raw:
        return ""
    m = re.fullmatch(r"\s*(\d{1,2})\s*(?:/\s*10)?\s*", raw)
    return f"Стан {m.group(1)}/10" if m else raw.strip()[0].upper() + raw.strip()[1:]


def _measure_line(nums: list[str] | None) -> str:
    if not nums:
        return ""
    pairs = [f"{lbl.lower()} {n}" for lbl, n in zip(_MEASURE_LABELS, nums)]
    return ", ".join(pairs).capitalize() + "."


def build(text: str) -> str | None:
    d = parse(text)
    if not d.usable:
        return None

    head = " · ".join(x for x in [d.brand, d.item, d.size, _price(d.price)] if x)

    cond = _condition(d.condition)
    if cond and not cond.endswith("."):
        cond += "."
    second = " ".join(x for x in [cond, _measure_line(d.measurements)] if x)

    lines = [head]
    if second:
        lines.append("")
        lines.append(second)
    lines.append("")
    lines.append("Пиши «+» у дірект, відправлю сьогодні.")
    lines.append("")
    tags = HASHTAGS
    if d.brand:
        slug = re.sub(r"[^a-zа-яіїєґ0-9]+", "", d.brand.lower())
        if slug:
            tags = f"#prospekt23 #{slug} " + HASHTAGS.removeprefix("#prospekt23 ")
    lines.append(tags)
    return "\n".join(lines)


HELP = (
    "Опис збираю з одного рядка. Порядок такий:\n\n"
    "<code>бренд / річ / розмір / ціна / стан / заміри</code>\n\n"
    "Наприклад:\n"
    "<code>Ralph Lauren / світшот на блискавці / M / 1450 / 9 / 48 56 68 64</code>\n\n"
    "Заміри — плечі, груди, довжина, рукав. Що не знаєш, просто пропусти "
    "з кінця. Можна написати це підписом до фото — тоді поверну і фото, "
    "і готовий опис одразу."
)
