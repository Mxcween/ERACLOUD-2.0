"""Фоновий робітник не має права померти тихо.

Живий випадок, і не один: власник писав "бот заглох, не реагує зовсім". Процес
при цьому був живий, /health віддавав ok, сканування йшло. Помирала одна
задача з чотирьох - і разом з нею або команди, або доставка знахідок, або
самопінг, після якого хостинг через чверть години приспить сервіс. Кожен
робітник ловить свої винятки у власному while True, але виняток, що вилетів
із самого while, не ловить ніхто: asyncio просто ставить задачі галочку
"завершена з помилкою" і більше нікого не турбує.
"""
from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from vintsniper.runner import WORKER_BACKOFF, Sniper


def supervisor() -> Sniper:
    s = object.__new__(Sniper)
    s._worker_restarts = Counter()
    return s


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch):
    """У бою пауза між підняттями - секунди й хвилини, тут вона нам ні до чого."""
    monkeypatch.setattr("vintsniper.runner.WORKER_BACKOFF", (0.0,))


class TestSupervisor:
    @pytest.mark.asyncio
    async def test_a_worker_that_dies_is_raised_again(self):
        starts = 0

        async def worker():
            nonlocal starts
            starts += 1
            raise RuntimeError("впав")

        s = supervisor()
        task = asyncio.create_task(s.supervise("test", worker))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert starts > 2, "одного підняття недостатньо, піднімати треба щоразу"
        # Лічильник росте на кожній смерті, тому він або рівний числу
        # запусків, або на один менший - залежить від того, де саме задачу
        # застала зупинка.
        assert s._worker_restarts["test"] in (starts - 1, starts)

    @pytest.mark.asyncio
    async def test_a_worker_that_finishes_on_purpose_is_left_alone(self):
        """Чистий return означає "роботи для мене немає", а не поломку.

        Так виходять слухач команд без токена і самопінг без
        RENDER_EXTERNAL_URL. Піднімати їх заново - гарячий цикл на порожньому
        місці, тобто та сама поломка, від якої ми тут лікуємось.
        """
        starts = 0

        async def worker():
            nonlocal starts
            starts += 1

        s = supervisor()
        await asyncio.wait_for(s.supervise("test", worker), timeout=1.0)
        assert starts == 1
        assert s._worker_restarts["test"] == 0

    @pytest.mark.asyncio
    async def test_cancelling_the_supervisor_does_not_restart_the_worker(self):
        """Зупинка процесу - не падіння, піднімати нікого не треба."""
        starts = 0

        async def worker():
            nonlocal starts
            starts += 1
            await asyncio.sleep(10)

        s = supervisor()
        task = asyncio.create_task(s.supervise("test", worker))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert starts == 1

    @pytest.mark.asyncio
    async def test_each_worker_is_counted_separately(self):
        async def dead():
            raise RuntimeError("впав")

        s = supervisor()
        tasks = [
            asyncio.create_task(s.supervise("commands", dead)),
            asyncio.create_task(s.supervise("delivery", dead)),
        ]
        await asyncio.sleep(0.05)
        for t in tasks:
            t.cancel()
            with pytest.raises(asyncio.CancelledError):
                await t
        assert s._worker_restarts["commands"] > 0
        assert s._worker_restarts["delivery"] > 0


class TestBackoff:
    def test_the_backoff_starts_short_and_grows(self):
        """Випадкова помилка не має коштувати хвилин простою, а зламане
        назавжди не має крутитись у гарячому циклі."""
        assert WORKER_BACKOFF[0] <= 5.0
        assert list(WORKER_BACKOFF) == sorted(WORKER_BACKOFF)
        assert WORKER_BACKOFF[-1] >= 60.0


class TestCycleWatchdog:
    """Оберт циклу теж може повиснути назавжди.

    Виняток - не єдиний спосіб зупинитись: достатньо одного await, який не
    завершується. Бот при цьому назавжди "в циклі", last_error порожній,
    status ok - і жодного алерта. Цю саму поломку ми вже лікували в слухачі
    команд, і там вона тижнями виглядала як здоровий бот.
    """

    @pytest.mark.asyncio
    async def test_a_cycle_that_hangs_forever_is_abandoned(self, monkeypatch):
        monkeypatch.setattr("vintsniper.runner.CYCLE_WATCHDOG_SECONDS", 0.02)
        entered = 0

        async def hangs():
            nonlocal entered
            entered += 1
            await asyncio.sleep(3600)

        s = object.__new__(Sniper)
        s._worker_restarts = Counter()
        s._cycles_stuck = 0
        s._scan_errors = 0
        s.last_error = None
        s.cycle_seconds = 0.01
        s.limiters = {}
        s.traffic = type("T", (), {"slowdown": lambda self: 1.0})()
        s.run_cycle = hangs
        s._warn_if_throttled = lambda pace: asyncio.sleep(0)

        task = asyncio.create_task(s.run_forever())
        # Пауза між обертами має власну нижню межу в секунду (щоб зламаний
        # цикл не крутився на повній), тому вікно мусить її перекрити.
        await asyncio.sleep(1.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert entered > 1, "зависший оберт треба кидати і починати новий"
        assert s._cycles_stuck > 0
        assert s.last_error and "завис" in s.last_error
