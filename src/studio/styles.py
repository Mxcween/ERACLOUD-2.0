"""Рецепти зйомки.

Перший абзац у кожному однаковий і найважливіший: він забороняє моделі
чіпати саму річ. Плями, катишки й дефекти мають лишитись, інакше покупець
отримає не те, що бачив на фото.
"""
from __future__ import annotations

KEEP_THE_GARMENT = (
    "Keep the exact garment from the input photo completely unchanged: the same "
    "item, same colour and shade, same brand embroidery and labels, same zipper, "
    "collar, cuffs, seams, proportions and folds. Do not redesign, restyle, clean "
    "or repair the clothing. Any stains, pilling, wear or damage visible in the "
    "input must stay exactly as they are. "
)

FRAMING = (
    " Shot from directly overhead, garment laid flat and centered, filling most of "
    "the frame, vertical 4:5 composition. Editorial second-hand shop flat-lay. "
    "No text, no watermark, no people, no hands."
)

STYLES: dict[str, dict[str, str]] = {
    "light": {
        "name": "світлий",
        "note": "кремовий льон, надвечірнє сонце, суха гілочка в кутку",
        "prompt": (
            "Replace only the surroundings: lay the garment on a soft cream linen "
            "sheet with natural creases. Warm late-afternoon sunlight falls "
            "diagonally across the frame casting soft window shadows. A small sprig "
            "of dried yellow flowers rests in the top-left corner. Muted warm "
            "palette, gentle film grain."
        ),
    },
    "dark": {
        "name": "темний",
        "note": "темний бетон, холодне світло, зелений відблиск по краю",
        "prompt": (
            "Replace only the surroundings: lay the garment on dark charcoal "
            "textured concrete. Cool soft daylight from one side, deep controlled "
            "shadows, a faint green rim light along one edge. Muted cold palette of "
            "charcoal, graphite and near-black."
        ),
    },
    "studio": {
        "name": "студія",
        "note": "рівний сірий фон, м'яке рівномірне світло, нуль декору",
        "prompt": (
            "Replace only the surroundings: lay the garment on a seamless mid-grey "
            "studio paper backdrop. Soft even diffused light from a large softbox, "
            "very subtle shadow under the garment, no props at all. Clean neutral "
            "catalogue look."
        ),
    },
}

DEFAULT_STYLE = "light"


def style_prompt(key: str) -> str:
    style = STYLES.get(key) or STYLES[DEFAULT_STYLE]
    return KEEP_THE_GARMENT + style["prompt"] + FRAMING


def style_list() -> str:
    return "\n".join(
        f"<code>/style {k}</code> — {v['name']}: {v['note']}" for k, v in STYLES.items()
    )
