"""Стрічка ТОП-знахідок для Нікса: лише канал top, без дублікатів, новіші вгорі."""
from __future__ import annotations

import dataclasses
from collections import deque

from vintsniper.models import Deal, Listing

from .test_dispatch import make_sniper


def _sniper():
    sniper = make_sniper()
    sniper._top_feed = deque(maxlen=50)  # make_sniper не викликає __init__
    return sniper


def _listing(item_id: int) -> Listing:
    return Listing(
        item_id=item_id, market="pl", catalog_id=1, title="Kurtka", brand_title="Stone Island",
        size_title="M", status_title="Dobry", status_id=3, price=40.0, total_price=44.0,
        currency="EUR", url=f"https://vinted.pl/items/{item_id}", photo_url="https://img/x.jpg",
        seller_id=7, seller_login="s", seller_is_business=False, favourite_count=0,
        view_count=0, uploaded_ts=None, seen_ts=1000,
    )


def _deal(item_id: int, channel: str) -> Deal:
    return Deal(
        listing=_listing(item_id), tier="S", channel=channel, category_key="jackets",
        category_name="Kurtki", price_eur=44.0, resale_eur=130.0, profit_eur=86.0,
        shipping_eur=5.0, multiple=2.95, reference="медіана", sample_size=12,
        condition_bucket="good", replica_risk="low", authenticity_flag=False,
    )


def test_only_top_deals_enter_feed_and_newest_first():
    runner = _sniper()
    runner._feed_top(_deal(1, "top"), 1000)
    runner._feed_top(_deal(2, "top"), 1001)
    runner._feed_top(_deal(1, "top"), 1002)  # дубль
    ids = [e["id"] for e in reversed(runner._top_feed)]
    assert ids == [2, 1]  # найновіші вгорі, так само віддає /health
    entry = runner._top_feed[-1]
    assert entry["brand"] == "Stone Island" and entry["profit_eur"] == 86.0
    assert entry["photo"] == "https://img/x.jpg" and entry["url"].endswith("/2")


def test_expensive_all_channel_stays_out():
    runner = _sniper()
    runner._feed_top(_deal(3, "all"), 1000)  # 44 EUR - дорого для «купи дешево»
    assert len(runner._top_feed) == 0


def test_cheap_all_channel_enters_feed():
    runner = _sniper()
    deal = dataclasses.replace(_deal(4, "all"), price_eur=20.0)
    runner._feed_top(deal, 1000)
    assert [e["id"] for e in runner._top_feed] == [4]
    assert runner._top_feed[0]["channel"] == "all"
    assert runner._top_feed[0]["category"] == "jackets"
