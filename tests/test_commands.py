"""Слухач команд не має права зависнути назавжди.

Живий випадок: власник заглушив два бренди, і бот перестав відповідати на
будь-яку команду. При цьому він справно сканував Vinted і слав алерти, тому
зовні виглядав здоровим, а /health показував "status: ok". Слухач не впав -
один прохід просто не завершився ніколи, а ловити винятки від такого не
рятує: виняток так і не стався.
"""
from __future__ import annotations

import asyncio

import pytest

from vintsniper.runner import COMMAND_MIN_IDLE, COMMAND_PASS_GRACE, Sniper


def listener(pass_impl, timeout: float = 0.05) -> Sniper:
    s = object.__new__(Sniper)
    s._commands_polled = 0
    s._commands_stuck = 0
    s._commands_last_ok = 0.0
    s._command_pass_timeout = timeout
    s._command_idle = 0.0  # у бою тут 2с, щоб не довбити Telegram даремно
    s._command_pass = pass_impl
    s.settings = type(
        "S", (), {"telegram": type("T", (), {"configured": True})(), "dry_run": False}
    )()
    return s


async def run_briefly(sniper: Sniper, seconds: float) -> None:
    task = asyncio.create_task(sniper.listen_commands())
    await asyncio.sleep(seconds)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class TestWatchdog:
    @pytest.mark.asyncio
    async def test_a_pass_that_never_returns_is_abandoned(self):
        """Прохід, який висить вічно, кидаємо і починаємо новий."""
        entered = 0

        async def hangs_forever() -> None:
            nonlocal entered
            entered += 1
            await asyncio.Event().wait()

        sniper = listener(hangs_forever)
        await run_briefly(sniper, 0.5)

        assert entered > 1, "слухач так і не почав новий прохід"
        assert sniper._commands_stuck > 0, "зависання не порахувалось"
        assert sniper._commands_polled == 0, "завислий прохід порахувався як успішний"

    @pytest.mark.asyncio
    async def test_a_healthy_pass_is_counted(self):
        done = 0

        async def quick() -> None:
            nonlocal done
            done += 1

        sniper = listener(quick, timeout=5.0)
        await run_briefly(sniper, 0.3)

        assert sniper._commands_polled > 0
        assert sniper._commands_stuck == 0
        assert sniper._commands_last_ok > 0

    @pytest.mark.asyncio
    async def test_an_exception_still_does_not_kill_the_listener(self):
        tries = 0

        async def explodes() -> None:
            nonlocal tries
            tries += 1
            raise RuntimeError("бум")

        sniper = listener(explodes, timeout=5.0)
        await run_briefly(sniper, 0.3)

        assert tries > 1, "слухач помер від винятку"

    def test_the_watchdog_leaves_room_for_a_healthy_long_poll(self):
        """Сторож не має спрацьовувати на здоровому довгому опитуванні."""
        assert COMMAND_PASS_GRACE >= 20

    def test_the_idle_floor_is_never_zero(self):
        """Нуль тут означав би гарячий цикл на кожній миттєвій помилці."""
        assert COMMAND_MIN_IDLE > 0
