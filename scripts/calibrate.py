#!/usr/bin/env python3
"""Перераховує базові ціни в categories.yaml на зібраних даних.

Базові ціни потрібні лише поки бот не назбирав власної статистики по
конкретному бренду. Але що ближче вони до правди, то менше хибних алертів
у перші дні після запуску та після кожного редеплою на Render.

Через тиждень роботи запусти це, подивись на таблицю і застосуй:

    python scripts/calibrate.py            # тільки показати
    python scripts/calibrate.py --apply    # і переписати categories.yaml
"""
from __future__ import annotations

import argparse
import logging
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vintsniper.settings import CONFIG_DIR, load_settings  # noqa: E402
from vintsniper.storage.db import build_engine, build_session_factory  # noqa: E402
from vintsniper.storage.repo import Repository  # noqa: E402
from vintsniper.vinted.brands import BrandRegistry  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("calibrate")

MIN_SAMPLES = 25
TIERS = ("S", "A", "B")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="переписати categories.yaml")
    parser.add_argument(
        "--min-samples", type=int, default=MIN_SAMPLES,
        help=f"скільки спостережень треба, щоб довіряти клітинці (типово {MIN_SAMPLES})",
    )
    args = parser.parse_args()

    settings = load_settings()
    registry = BrandRegistry.load(CONFIG_DIR / "brand_ids.json")
    if registry is None:
        log.error("немає config/brand_ids.json")
        return 1

    repo = Repository(build_session_factory(build_engine(settings.database_url)))
    rows = repo.load_observations(0)
    if not rows:
        log.error("у базі немає спостережень. Дай боту попрацювати хоча б кілька годин.")
        return 1

    factors = (settings.conditions or {}).get("resale_factor") or {}
    categories = {c.id: c for c in settings.categories}

    # Зводимо всі ціни до стану "дуже добре", щоб клітинки були порівнянні
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for brand_id, catalog_id, bucket, price_eur, _ts in rows:
        brand = registry.by_id(brand_id)
        category = categories.get(catalog_id)
        if brand is None or category is None:
            continue
        factor = float(factors.get(bucket, 1.0)) or 1.0
        groups[(category.key, brand.tier)].append(price_eur / factor)

    log.info("Спостережень у базі: %s\n", len(rows))
    header = f"{'категорія':<20} {'тір':<4} {'n':>6} {'медіана':>9} {'зараз':>7}   зміна"
    log.info(header)
    log.info("-" * len(header))

    updates: dict[str, dict[str, int]] = {}
    for category in settings.categories:
        for tier in TIERS:
            values = groups.get((category.key, tier), [])
            current = category.baseline_eur.get(tier)
            if len(values) < args.min_samples:
                log.info(
                    "%-20s %-4s %6s %9s %7s   замало даних",
                    category.key, tier, len(values), "-", current,
                )
                continue
            median = round(statistics.median(values))
            updates.setdefault(category.key, {})[tier] = median
            delta = ""
            if current:
                pct = (median - current) / current * 100
                delta = f"{pct:+.0f}%" + ("  ← варто оновити" if abs(pct) >= 15 else "")
            log.info(
                "%-20s %-4s %6s %9s %7s   %s",
                category.key, tier, len(values), median, current, delta,
            )

    if not args.apply:
        log.info("\nЦе був лише перегляд. Щоб застосувати: --apply")
        return 0

    if not updates:
        log.info("\nНічого застосовувати: жодна клітинка не набрала %s спостережень", args.min_samples)
        return 0

    applied = _rewrite(CONFIG_DIR / "categories.yaml", updates, settings)
    log.info("\nОновлено категорій: %s у config/categories.yaml", applied)
    log.info("Перезапусти бота, щоб він підхопив нові значення.")
    return 0


def _rewrite(path: Path, updates: dict[str, dict[str, int]], settings) -> int:
    """Правимо тільки рядки baseline_eur, решту файлу і коментарі не чіпаємо."""
    text = path.read_text(encoding="utf-8")
    applied = 0
    for category in settings.categories:
        new_values = updates.get(category.key)
        if not new_values:
            continue
        merged = {t: new_values.get(t, int(category.baseline_eur.get(t, 0))) for t in TIERS}
        rendered = "{" + ", ".join(f"{t}: {merged[t]}" for t in TIERS) + "}"
        pattern = re.compile(
            r"(key:\s*" + re.escape(category.key) + r"\b.*?baseline_eur:\s*)\{[^}]*\}",
            re.S,
        )
        text, count = pattern.subn(lambda m: m.group(1) + rendered, text, count=1)
        applied += count
    path.write_text(text, encoding="utf-8")
    return applied


if __name__ == "__main__":
    raise SystemExit(main())
