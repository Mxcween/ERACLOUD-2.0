"""Розсилка: ціновий фільтр на Telegram і незалежність Discord від нього."""
from __future__ import annotations

import time

import pytest

from dataclasses import replace

from vintsniper.engine.ranges import PriceRange
from vintsniper.models import Deal
from vintsniper.runner import Sniper

from .conftest import NOW, make_listing


class FakeTelegram:
    def __init__(self, has_target: bool = True) -> None:
        self.has_target = has_target
        self.sent: list[Deal] = []
        self.texts: list[str] = []

    async def send_text(self, text, *, channel: str = "top"):
        self.texts.append(text)
        return True

    async def send_deal(self, deal, *, brand_id):
        if not self.has_target:
            return False
        self.sent.append(deal)
        return True


class FakeDiscord:
    def __init__(self, configured: bool = True) -> None:
        self.configured = configured
        self.sent: list[Deal] = []

    async def send_deal(self, deal):
        self.sent.append(deal)
        return True

    def tier_index(self, price_eur):
        return 0


class FakeJudge:
    """Зір у тестах завжди пропускає: тут перевіряємо розсилку, не зір."""

    configured = True

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.seen: list[str] = []

    async def judge(self, photo_url, **kwargs):
        from vintsniper.engine.vision import Verdict

        self.seen.append(photo_url)
        return Verdict(ok=self.ok, note="" if self.ok else "", flags=[] if self.ok else ["fake"])


class FakeRepo:
    def __init__(self) -> None:
        self.alerts: list[Deal] = []

    def log_alert(self, deal, now_ts):
        self.alerts.append(deal)


class FakeSettings:
    def __init__(self) -> None:
        self.alerts = {"max_alerts_per_hour": 60, "quiet_hours": []}
        self.dry_run = False

    class telegram:  # noqa: N801 - імітуємо атрибут налаштувань
        configured = True


def make_deal(price_eur: float) -> Deal:
    return Deal(
        listing=make_listing(),
        tier="B",
        channel="all",
        category_key="outerwear",
        category_name="Куртки",
        price_eur=price_eur,
        resale_eur=price_eur * 3,
        profit_eur=price_eur * 2,
        shipping_eur=3.5,
        multiple=3.0,
        reference="медіана",
        sample_size=20,
        condition_bucket="very_good",
        replica_risk="high",
        authenticity_flag=True,
    )


def make_sniper(*, has_target: bool = True, discord: bool = True) -> Sniper:
    """Тільки те, чого торкається _dispatch: піднімати весь Sniper тут ні до чого."""
    sniper = object.__new__(Sniper)
    sniper.settings = FakeSettings()
    sniper.notifier = FakeTelegram(has_target)
    sniper.discord = FakeDiscord(discord)
    sniper.repo = FakeRepo()
    sniper.paused = False
    sniper.alert_range = PriceRange.open()
    sniper._pending = []
    sniper._alert_times = []
    sniper._seller_alerts = {}
    sniper._alerts_total = 0
    sniper._vision_rejects = 0
    sniper.judge = FakeJudge()
    return sniper


@pytest.mark.asyncio
async def test_open_range_sends_everywhere():
    sniper = make_sniper()
    assert await sniper._dispatch(make_deal(30), 53, NOW) is True
    assert len(sniper.notifier.sent) == 1
    assert len(sniper.discord.sent) == 1


@pytest.mark.asyncio
async def test_range_filters_telegram_but_not_discord():
    sniper = make_sniper()
    sniper.alert_range = PriceRange(15, 45)

    assert await sniper._dispatch(make_deal(60), 53, NOW) is True
    assert sniper.notifier.sent == []
    assert len(sniper.discord.sent) == 1

    assert await sniper._dispatch(make_deal(20), 53, NOW) is True
    assert len(sniper.notifier.sent) == 1


@pytest.mark.asyncio
async def test_out_of_range_deal_is_not_queued_for_later():
    """Черга чекає на чат, а не на зміну діапазону: лот поза полицею туди не йде."""
    sniper = make_sniper(has_target=False)
    sniper.alert_range = PriceRange(0, 15)
    await sniper._dispatch(make_deal(60), 53, NOW)
    assert sniper._pending == []


@pytest.mark.asyncio
async def test_discord_works_while_telegram_chat_is_unknown():
    sniper = make_sniper(has_target=False)
    assert await sniper._dispatch(make_deal(30), 53, NOW) is True
    assert len(sniper.discord.sent) == 1
    assert len(sniper._pending) == 1


@pytest.mark.asyncio
async def test_nothing_sent_when_neither_channel_can_deliver():
    sniper = make_sniper(has_target=False, discord=False)
    assert await sniper._dispatch(make_deal(30), 53, NOW) is False
    assert sniper.repo.alerts == []


class TestDeepScanNote:
    """Знахідка з дешевого хвоста має бути підписана окремо: її бачили всі,
    хто заходив у ці години, і ніхто не взяв."""

    def test_note_carries_the_age_in_hours(self):
        from vintsniper.runner import Sniper

        deal = make_deal(30)
        deal.notes.clear()
        listing = make_listing(uploaded_ts=NOW - 5 * 3600, seen_ts=NOW)
        sniper = object.__new__(Sniper)
        note = Sniper._deep_note(sniper, listing)  # type: ignore[arg-type]
        assert "5 год" in note
        assert "висить" in note

    def test_unknown_age_says_so_instead_of_lying(self):
        from vintsniper.runner import Sniper

        listing = make_listing(uploaded_ts=None, seen_ts=NOW)
        sniper = object.__new__(Sniper)
        assert "невідомо" in Sniper._deep_note(sniper, listing)  # type: ignore[arg-type]

    def test_fresh_listing_is_not_called_stale(self):
        from vintsniper.runner import Sniper

        listing = make_listing(uploaded_ts=NOW - 600, seen_ts=NOW)
        sniper = object.__new__(Sniper)
        assert "невідомо" in Sniper._deep_note(sniper, listing)  # type: ignore[arg-type]


class TestDeepGate:
    """Залежалий лот проходить за суворішим порогом, ніж свіжий."""

    def _sniper(self, **scoring):
        from vintsniper.runner import Sniper

        sniper = object.__new__(Sniper)
        settings = FakeSettings()
        settings.scoring = {"deep_min_profit_eur": 25.0, "deep_min_multiple": 3.0, **scoring}
        sniper.settings = settings
        return sniper

    @staticmethod
    def _deal(profit: float, multiple: float) -> Deal:
        base = make_deal(10)
        return replace(base, profit_eur=profit, multiple=multiple)

    def test_fat_stale_find_passes(self):
        from vintsniper.runner import Sniper

        assert Sniper._deep_gate(self._sniper(), self._deal(26.0, 3.2)) is True

    def test_thin_multiple_is_rejected_even_with_profit(self):
        from vintsniper.runner import Sniper

        assert Sniper._deep_gate(self._sniper(), self._deal(60.0, 2.4)) is False

    def test_small_money_is_rejected_even_with_a_big_multiple(self):
        """Саме цим забився перший прохід: п'ятиєврові футболки на x6."""
        from vintsniper.runner import Sniper

        assert Sniper._deep_gate(self._sniper(), self._deal(12.0, 6.3)) is False

    def test_thresholds_come_from_config(self):
        from vintsniper.runner import Sniper

        loose = self._sniper(deep_min_profit_eur=10.0, deep_min_multiple=2.0)
        assert Sniper._deep_gate(loose, self._deal(12.0, 6.3)) is True


class TestBurstControl:
    """Після /start бот не має вивалювати все, що назбиралось, одним стосом."""

    def _sniper(self, **alerts):
        sniper = make_sniper()
        sniper.settings.alerts = {
            "max_alerts_per_hour": 60, "quiet_hours": [],
            "max_alerts_per_minute": 5, "flush_batch": 5,
            "pending_stale_seconds": 1800, **alerts,
        }
        sniper._first_flush = True
        return sniper

    @pytest.mark.asyncio
    async def test_minute_cap_queues_instead_of_dropping(self):
        sniper = self._sniper(max_alerts_per_minute=2)
        for i in range(5):
            await sniper._dispatch(make_deal(30 + i), 53, NOW)
        assert len(sniper.notifier.sent) == 2
        # Три, що не пройшли, чекають у черзі, а не зникли
        assert len(sniper._pending) == 3

    @pytest.mark.asyncio
    async def test_flush_sends_the_fattest_first_and_holds_the_rest(self):
        sniper = self._sniper(flush_batch=2, max_alerts_per_minute=0)
        now = time.monotonic()
        for price in (10, 90, 50, 20):
            sniper._pending.append((make_deal(price), 53, now))
        await sniper._flush_pending(NOW)
        prices = [d.price_eur for d in sniper.notifier.sent]
        assert prices == [90, 50]
        assert len(sniper._pending) == 2

    @pytest.mark.asyncio
    async def test_stale_finds_are_dropped_not_sent(self):
        """Лот, знайдений годину тому, або куплений, або нікому не потрібен."""
        sniper = self._sniper(pending_stale_seconds=600)
        old = time.monotonic() - 3600
        sniper._pending.append((make_deal(40), 53, old))
        sniper._pending.append((make_deal(60), 53, time.monotonic()))
        await sniper._flush_pending(NOW)
        assert [d.price_eur for d in sniper.notifier.sent] == [60]
        assert sniper._pending == []

    @pytest.mark.asyncio
    async def test_nothing_queued_means_no_explanation_message(self):
        sniper = self._sniper()
        sniper._pending.append((make_deal(30), 53, time.monotonic()))
        await sniper._flush_pending(NOW)
        assert sniper.notifier.texts == []


class TestDeliveryWorker:
    """Пошук і доставка розведені: перевірка фото не має гальмувати сканування."""

    @pytest.mark.asyncio
    async def test_worker_sends_what_the_cycle_queued(self):
        import asyncio

        sniper = make_sniper()
        sniper.settings.alerts = {"max_alerts_per_hour": 60, "quiet_hours": [],
                                  "max_alerts_per_minute": 0}
        sniper._outbox = asyncio.Queue()
        for price in (10, 20, 30):
            sniper._outbox.put_nowait((make_deal(price), 53))

        worker = asyncio.create_task(sniper.deliver_forever())
        await asyncio.wait_for(sniper._outbox.join(), timeout=2)
        worker.cancel()
        assert [d.price_eur for d in sniper.notifier.sent] == [10, 20, 30]

    @pytest.mark.asyncio
    async def test_one_failing_send_does_not_stop_the_queue(self):
        import asyncio

        sniper = make_sniper()
        sniper.settings.alerts = {"max_alerts_per_hour": 60, "quiet_hours": [],
                                  "max_alerts_per_minute": 0}
        sniper._outbox = asyncio.Queue()
        calls = {"n": 0}
        real = sniper.notifier.send_deal

        async def flaky(deal, *, brand_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("телеграм упав")
            return await real(deal, brand_id=brand_id)

        sniper.notifier.send_deal = flaky
        sniper._outbox.put_nowait((make_deal(10), 53))
        sniper._outbox.put_nowait((make_deal(20), 53))

        worker = asyncio.create_task(sniper.deliver_forever())
        await asyncio.wait_for(sniper._outbox.join(), timeout=2)
        worker.cancel()
        assert [d.price_eur for d in sniper.notifier.sent] == [20]
