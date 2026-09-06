"""Розсилка: ціновий фільтр на Telegram і незалежність Discord від нього."""
from __future__ import annotations

import pytest

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
