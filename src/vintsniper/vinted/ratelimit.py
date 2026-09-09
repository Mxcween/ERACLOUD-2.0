"""Обмежувач частоти запитів — один на ринок.

vinted.pl і vinted.de це різні хости з окремими лічильниками, тому спільна
черга на обидва тільки подвоювала час циклу, не зменшуючи ризику 403.
Штраф після 429 теж рахується окремо: пригальмувати треба той ринок, який
поскаржився, а не обидва.
"""
from __future__ import annotations

import asyncio
import random
import time


class RateLimiter:
    def __init__(
        self,
        min_interval: float,
        jitter: float = 0.35,
        parent: "RateLimiter | None" = None,
    ) -> None:
        self.min_interval = min_interval
        self.jitter = jitter
        # Vinted рахує запити по IP, а не по хосту. Два ринки з окремими
        # лічильниками думали, що йдуть по одному запиту на дві секунди
        # кожен, а з боку Vinted це був один запит на секунду - і 429
        # прилітав з першої ж хвилини, ще на піднятті сесії. Спільний
        # батьківський обмежувач тримає стелю на весь процес; штраф
        # лишається персональним, бо гальмувати треба той ринок, який
        # поскаржився.
        self._parent = parent
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0
        # Множник, який росте після 429 і повільно спадає після успіхів
        self._penalty = 1.0

    async def acquire(self) -> None:
        if self._parent is not None:
            await self._parent.acquire()
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            gap = self.min_interval * self._penalty
            gap += random.uniform(0, self.jitter * self.min_interval)
            self._next_allowed = now + gap

    #: Стеля штрафу. Була x8 і виявилась шкідливою: заміряно, що ширші
    #: паузи відмов НЕ зменшують (з 3.5с на запит було 34% відмов, з 5.0с
    #: стало 63%), бо впираємось у квоту на кількість, а не в частоту. При
    #: половині відмов штраф уже не спадав ніколи - подвоєння після кожної
    #: перебивало ті 15%, що знімає успіх, - і намертво тримав інтервал у
    #: 16 секунд. Ми платили паузами за те, що все одно не купувалось.
    CEILING = 2.0

    def penalise(self) -> None:
        """Vinted сказав пригальмувати. Трохи розтягуємо паузи."""
        self._penalty = min(self._penalty * 2.0, self.CEILING)

    def relax(self) -> None:
        """Успішний запит. Повертаємось до норми поступово."""
        if self._penalty > 1.0:
            self._penalty = max(1.0, self._penalty * 0.85)

    @property
    def penalty(self) -> float:
        return self._penalty
