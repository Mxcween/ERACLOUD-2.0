"""Глобальний обмежувач частоти запитів.

Ми ходимо в Vinted з одного IP, тому черга спільна для всіх ринків: неважливо,
скільки в нас стрічок, назовні це рівний повільний потік.
"""
from __future__ import annotations

import asyncio
import random
import time


class RateLimiter:
    def __init__(self, min_interval: float, jitter: float = 0.35) -> None:
        self.min_interval = min_interval
        self.jitter = jitter
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0
        # Множник, який росте після 429 і повільно спадає після успіхів
        self._penalty = 1.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            gap = self.min_interval * self._penalty
            gap += random.uniform(0, self.jitter * self.min_interval)
            self._next_allowed = now + gap

    def penalise(self) -> None:
        """Vinted сказав пригальмувати. Розтягуємо паузи, стеля x8."""
        self._penalty = min(self._penalty * 2.0, 8.0)

    def relax(self) -> None:
        """Успішний запит. Повертаємось до норми поступово."""
        if self._penalty > 1.0:
            self._penalty = max(1.0, self._penalty * 0.9)

    @property
    def penalty(self) -> float:
        return self._penalty
