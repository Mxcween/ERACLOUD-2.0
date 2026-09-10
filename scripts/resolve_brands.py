#!/usr/bin/env python3
"""Перетворює назви з config/brands.yaml на id Vinted і пише config/brand_ids.json.

Запускати треба лише коли міняєш список брендів. Готовий кеш уже лежить у репо,
тому бот стартує без цього кроку.

    python scripts/resolve_brands.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vintsniper.settings import CONFIG_DIR, load_settings  # noqa: E402
from vintsniper.vinted.brands import BrandRegistry, ResolvedBrand, pick_best_match  # noqa: E402
from vintsniper.vinted.client import VintedClient  # noqa: E402
from vintsniper.vinted.ratelimit import RateLimiter  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("resolve")


async def main() -> int:
    settings = load_settings()
    market = settings.enabled_markets[0]
    limiter = RateLimiter(settings.polling.get("min_request_interval", 0.8))

    resolved: list[ResolvedBrand] = []
    missing: list[str] = []

    async with VintedClient(market, limiter) as client:
        for brand in settings.brands:
            try:
                candidates = await client.search_brands(brand.name)
            except Exception as exc:  # noqa: BLE001
                log.warning("  %-22s помилка пошуку: %s", brand.name, exc)
                missing.append(brand.name)
                continue

            match = pick_best_match(brand.name, candidates)
            if not match:
                log.warning("  %-22s НЕ ЗНАЙДЕНО", brand.name)
                missing.append(brand.name)
                continue

            resolved.append(
                ResolvedBrand(
                    name=brand.name,
                    brand_id=int(match["id"]),
                    vinted_title=match.get("title", brand.name),
                    tier=brand.tier,
                    replica_risk=brand.replica_risk,
                    item_count=int(match.get("item_count") or 0),
                    requires_authenticity_check=bool(match.get("requires_authenticity_check")),
                    is_luxury=bool(match.get("is_luxury")),
                    min_multiple=brand.min_multiple,
                )
            )
            log.info(
                "  %-22s id=%-9s %-26s лотів=%s%s",
                brand.name,
                match["id"],
                match.get("title", ""),
                match.get("pretty_item_count") or match.get("item_count"),
                "  [перевірка автентичності]" if match.get("requires_authenticity_check") else "",
            )

    registry = BrandRegistry(resolved)
    out = CONFIG_DIR / "brand_ids.json"
    registry.save(out)
    log.info("\nЗбережено %s брендів у %s", len(resolved), out)
    if missing:
        log.warning("Не знайдено (%s): %s", len(missing), ", ".join(missing))
    return 0 if resolved else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
