#!/usr/bin/env python3
"""Живий прогін без відправки в Telegram.

Робить кілька справжніх циклів проти Vinted, показує що знайшлось і чому
решта відсіялась. Нічого нікуди не шле.

    python scripts/smoke_test.py [кількість_циклів]
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("DRY_RUN", "1")

from vintsniper.__main__ import setup_logging  # noqa: E402
from vintsniper.runner import Sniper  # noqa: E402
from vintsniper.settings import load_settings  # noqa: E402

log = logging.getLogger("smoke")


async def main(cycles: int) -> int:
    settings = load_settings()
    settings.dry_run = True
    # У смоук-тесті не прогріваємось, хочемо побачити знахідки одразу
    settings.polling["warmup_cycles"] = 0
    setup_logging("INFO")

    sniper = Sniper(settings)
    await sniper.setup()
    try:
        for i in range(cycles):
            log.info("--- цикл %s з %s ---", i + 1, cycles)
            await sniper.run_cycle()
            if i < cycles - 1:
                await asyncio.sleep(5)
    finally:
        await sniper.close()

    stats = sniper.repo.stats(0)
    log.info(
        "\nПІДСУМОК: переглянуто %s лотів, спостережень %s, ключів %s, алертів у базі %s",
        stats.seen_items, stats.observations, sniper.price_book.tracked_keys, stats.alerts_total,
    )
    return 0


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    raise SystemExit(asyncio.run(main(count)))
