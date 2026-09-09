"""Поведінка клієнта, коли Vinted нас не пускає.

Живий бот чотири години крутив цикли по 317 секунд замість 30 і не знайшов
жодного лота, а /health при цьому показував "status: ok". Обидві причини
перевіряються тут: скільки часу коштує відмова і чи видно її назовні.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from vintsniper.settings import Market
from vintsniper.vinted.client import VintedBlocked, VintedClient
from vintsniper.vinted.ratelimit import RateLimiter

MARKET = Market(code="PL", host="www.vinted.pl", currency="PLN", locale="pl", shipping_eur=3.5)


def build(handler) -> VintedClient:
    client = VintedClient(MARKET, RateLimiter(min_interval=0.0, jitter=0.0), max_retries=3)
    client._client = httpx.AsyncClient(
        base_url=MARKET.base_url, transport=httpx.MockTransport(handler)
    )
    return client


@pytest.fixture
def no_sleep(monkeypatch):
    """Рахуємо, скільки бот ЗБИРАВСЯ спати, не витрачаючи на це тест."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


class TestBlocked:
    @pytest.mark.asyncio
    async def test_refusal_costs_seconds_not_minutes(self, no_sleep):
        """403 на все підряд має коштувати секунди, а не пів циклу.

        Раніше одна заблокована категорія спала 10+20+40 у запиті і ще
        20+40+80 у кожному піднятті сесії. Одинадцять категорій - і цикл
        з'їдений цілком, тобто бот не дивиться на стрічку саме тоді, коли й
        так ледве проходить.
        """
        client = build(lambda request: httpx.Response(403, text="no"))
        with pytest.raises(VintedBlocked):
            await client.fetch_catalog(catalog_id=1206, brand_ids=[53], per_page=96)
        await client.close()

        assert sum(no_sleep) < 30, f"відмова коштувала {sum(no_sleep):.0f}с сну"

    @pytest.mark.asyncio
    async def test_refusal_is_counted_so_health_can_show_it(self, no_sleep):
        """Мовчазний провал виглядав як тиша на ринку. Тепер він порахований."""
        client = build(lambda request: httpx.Response(403, text="no"))
        with pytest.raises(VintedBlocked):
            await client.fetch_catalog(catalog_id=1206, brand_ids=[53], per_page=96)
        await client.close()

        assert client.stats["ok"] == 0
        assert client.stats["403"] + client.stats["bootstrap_403"] > 0

    @pytest.mark.asyncio
    async def test_success_is_counted_too(self, no_sleep):
        payload = {"items": [], "pagination": {"total_entries": 0, "time": 1700000000}}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/":
                return httpx.Response(200, text="<html></html>")
            return httpx.Response(200, json=payload)

        client = build(handler)
        listings, _ = await client.fetch_catalog(catalog_id=1206, brand_ids=[53], per_page=96)
        await client.close()

        assert listings == []
        assert client.stats["ok"] == 1
        assert client.stats["403"] == 0
