"""Розсилка: ціновий фільтр на Telegram і незалежність Discord від нього."""
from __future__ import annotations

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
