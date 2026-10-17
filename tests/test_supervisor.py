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
import time
from collections import Counter

import pytest

from vintsniper.runner import SILENCE_ALARM_SECONDS, WORKER_BACKOFF, Sniper


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
        s._warn_if_silent = lambda: asyncio.sleep(0)

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


class TestSilenceAlarm:
    """Тиша має дві різні причини, і зовні вони однакові.

    "Нема вигідних лотів" лікується порогами. "Нас не пускають до стрічки" не
    лікується нічим із конфігу. Поки бот не розрізняв їх уголос, перший
    варіант завжди здавався правдоподібнішим: живий бот дві години крутив
    цикли з нулем прочитаних стрічок і мовчав, поки власник не написав сам.
    """

    def build(self, *, ok: int, refusals: int = 0):
        from collections import Counter
        s = object.__new__(Sniper)
        s._last_read_at = time.monotonic()
        s._reads_seen = 0
        s._silence_warned_at = None
        client = type("C", (), {})()
        client.stats = Counter({"ok": ok, "403": refusals})
        s.clients = {"PL": client}
        sent: list[str] = []

        async def send_text(text, **kw):
            sent.append(text)
            return True

        s.notifier = type("N", (), {"send_text": staticmethod(send_text)})()
        return s, sent, client

    @pytest.mark.asyncio
    async def test_a_successful_read_keeps_the_clock_reset(self):
        s, sent, client = self.build(ok=5)
        await s._warn_if_silent()
        s._last_read_at -= SILENCE_ALARM_SECONDS * 2
        client.stats["ok"] = 6                       # щось прочиталось
        await s._warn_if_silent()
        assert sent == [], "читання є - скаржитись нема на що"
        assert s.silent_for < 1.0

    @pytest.mark.asyncio
    async def test_a_short_quiet_spell_says_nothing(self):
        s, sent, _ = self.build(ok=5)
        await s._warn_if_silent()
        s._last_read_at -= SILENCE_ALARM_SECONDS / 2
        await s._warn_if_silent()
        assert sent == [], "смуга відмов на кілька хвилин - звичайна річ"

    @pytest.mark.asyncio
    async def test_a_long_silence_is_reported(self):
        s, sent, _ = self.build(ok=0, refusals=11)
        await s._warn_if_silent()
        s._last_read_at -= SILENCE_ALARM_SECONDS + 1
        await s._warn_if_silent()
        assert len(sent) == 1
        assert "403" in sent[0], "причину треба назвати, а не просто поскаржитись"

    @pytest.mark.asyncio
    async def test_network_trouble_reads_differently_from_a_block(self):
        s, sent, _ = self.build(ok=0, refusals=0)
        await s._warn_if_silent()
        s._last_read_at -= SILENCE_ALARM_SECONDS + 1
        await s._warn_if_silent()
        assert "мереж" in sent[0]

    @pytest.mark.asyncio
    async def test_the_warning_does_not_repeat_every_cycle(self):
        """Блок знімається не швидко, і нагадувати щохвилини - це спам."""
        s, sent, _ = self.build(ok=0, refusals=11)
        await s._warn_if_silent()
        s._last_read_at -= SILENCE_ALARM_SECONDS + 1
        for _ in range(10):
            await s._warn_if_silent()
        assert len(sent) == 1

    @pytest.mark.asyncio
    async def test_the_first_warning_is_not_swallowed_on_a_fresh_process(
        self, monkeypatch
    ):
        """Лічильник "коли попереджав востаннє" не можна починати з нуля.

        time.monotonic() на щойно запущеній машині теж близько нуля, тому
        різниця з нулем читається як "щойно попереджав", і перше
        попередження глохне на шість годин - рівно після перезапуску, коли
        воно найпотрібніше. Тест імітує саме цей випадок: процес живе
        хвилину.
        """
        monkeypatch.setattr("vintsniper.runner.time.monotonic", lambda: 60.0)
        s, sent, _ = self.build(ok=0, refusals=11)
        s._last_read_at = 60.0 - (SILENCE_ALARM_SECONDS + 1)
        await s._warn_if_silent()
        assert len(sent) == 1
