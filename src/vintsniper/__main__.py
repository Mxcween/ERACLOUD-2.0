"""Точка входу: python -m vintsniper"""
from __future__ import annotations

import asyncio
import logging
import sys

from .runner import main
from .settings import load_settings


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx логує кожен запит на INFO, це забиває вивід
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def run() -> int:
    settings = load_settings()
    setup_logging(settings.log_level)
    log = logging.getLogger("vintsniper")
    log.info(
        "старт: ринки=%s категорій=%s брендів у конфігу=%s dry_run=%s",
        [m.code for m in settings.enabled_markets],
        len(settings.enabled_categories),
        len(settings.brands),
        settings.dry_run,
    )
    try:
        asyncio.run(main(settings))
    except KeyboardInterrupt:
        log.info("зупинено вручну")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(run())
