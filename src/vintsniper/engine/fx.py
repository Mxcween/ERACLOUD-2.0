"""Конвертація валют у євро.

Ринки в нас різні: Польща рахує в злотих, Німеччина в євро. Щоб порівнювати
ціни й профіт, все зводимо до євро. Курси тягнемо з frankfurter.app - це
безкоштовно і без ключів. Якщо сервіс лежить, беремо резервні з конфігу.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest"


class FxConverter:
    def __init__(self, fallback_rates: dict[str, float], refresh_hours: float = 12.0) -> None:
        # Курси зберігаємо як "скільки одиниць валюти за 1 EUR"
        self._rates: dict[str, float] = {k.upper(): float(v) for k, v in fallback_rates.items()}
        self._rates.setdefault("EUR", 1.0)
        self._refresh_seconds = refresh_hours * 3600
        # None означає "ще жодного разу не тягнули". Нуль тут не годиться:
        # на щойно завантаженій машині time.monotonic() малий, і порівняння
        # з нулем давало б "оновлювати не треба" при першому ж запуску.
        self._fetched_at: float | None = None
        self._live = False

    @property
    def is_live(self) -> bool:
        return self._live

    @property
    def rates(self) -> dict[str, float]:
        return dict(self._rates)

    def needs_refresh(self) -> bool:
        if self._fetched_at is None:
            return True
        return (time.monotonic() - self._fetched_at) > self._refresh_seconds

    async def refresh(self, client: httpx.AsyncClient | None = None) -> bool:
        symbols = ",".join(sorted(k for k in self._rates if k != "EUR"))
        own_client = client is None
        http = client or httpx.AsyncClient(timeout=15.0, follow_redirects=True)
        try:
            resp = await http.get(FRANKFURTER_URL, params={"from": "EUR", "to": symbols})
            resp.raise_for_status()
            payload: dict[str, Any] = resp.json()
            rates = payload.get("rates") or {}
            if not rates:
                raise ValueError("порожня відповідь")
            for code, value in rates.items():
                self._rates[code.upper()] = float(value)
            self._rates["EUR"] = 1.0
            self._fetched_at = time.monotonic()
            self._live = True
            log.info("курси оновлено: %s", {k: round(v, 3) for k, v in sorted(self._rates.items())})
            return True
        except Exception as exc:  # noqa: BLE001
            # Не критично: працюємо на резервних курсах з конфігу
            self._fetched_at = time.monotonic()
            log.warning("курси не оновились (%s), лишаюсь на резервних", exc)
            return False
        finally:
            if own_client:
                await http.aclose()

    def to_eur(self, amount: float, currency: str) -> float:
        code = (currency or "EUR").upper()
        rate = self._rates.get(code)
        if not rate or rate <= 0:
            log.warning("немає курсу для %s, вважаю 1:1 до євро", code)
            return round(amount, 2)
        return round(amount / rate, 2)

    def from_eur(self, amount_eur: float, currency: str) -> float:
        code = (currency or "EUR").upper()
        rate = self._rates.get(code) or 1.0
        return round(amount_eur * rate, 2)
