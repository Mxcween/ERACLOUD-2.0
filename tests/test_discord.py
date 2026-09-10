import pytest

from vintsniper.engine.filters import Candidate
from vintsniper.engine.pricing import PriceBook
from vintsniper.engine.scoring import evaluate
from vintsniper.notify.discord import DiscordNotifier, _build_embed

NOW = 1_700_000_000


def make_deal(listing_factory, settings, registry, price_eur=10.0, median=100.0):
    outerwear = settings.category_by_id(1206)
    book = PriceBook(min_samples=4)
    for _ in range(10):
        book.record(53, 1206, "very_good", median, NOW)
    cand = Candidate(
        listing=listing_factory(), brand=registry.by_title("Nike"),
        category=outerwear, price_eur=price_eur, bucket="very_good",
    )
    return evaluate(cand, settings=settings, price_book=book, shipping_eur=3.5, now_ts=NOW)


class TestTierRouting:
    """Межі каналів: bounds=[15, 45] дають три канали 0-15, 15-45, 45+."""

    def test_boundaries_are_inclusive_lower_tier(self):
        n = DiscordNotifier(["a", "b", "c"], [15, 45])
        assert n.tier_index(15.0) == 0    # рівно на межі йде в дешевший канал
        assert n.tier_index(14.99) == 0
        assert n.tier_index(15.01) == 1
        assert n.tier_index(45.0) == 1
        assert n.tier_index(45.01) == 2
        assert n.tier_index(9999) == 2

    def test_webhook_lookup_matches_tier(self):
        n = DiscordNotifier(["cheap", "mid", "expensive"], [15, 45])
        assert n.webhook_for(5.0) == "cheap"
        assert n.webhook_for(30.0) == "mid"
        assert n.webhook_for(100.0) == "expensive"

    def test_mismatched_lengths_are_rejected(self):
        with pytest.raises(ValueError):
            DiscordNotifier(["a", "b"], [15, 45])

    def test_no_bounds_means_one_channel(self):
        n = DiscordNotifier(["only"], [])
        assert n.tier_index(1.0) == 0
        assert n.tier_index(999.0) == 0


class TestConfigured:
    def test_all_empty_is_not_configured(self):
        assert DiscordNotifier(["", ""], [15]).configured is False

    def test_one_filled_webhook_is_configured(self):
        assert DiscordNotifier(["", "https://discord.com/x"], [15]).configured is True


class TestSendRouting:
    async def _notifier(self, webhooks, bounds=(15, 45)):
        return DiscordNotifier(list(webhooks), list(bounds), dry_run=True)

    async def test_empty_webhook_for_tier_returns_false(
        self, listing_factory, settings, registry
    ):
        deal = make_deal(listing_factory, settings, registry, price_eur=10.0)
        n = await self._notifier(["", "https://discord.com/mid", "https://discord.com/top"])
        try:
            assert await n.send_deal(deal) is False
        finally:
            await n.close()

    async def test_configured_tier_sends_in_dry_run(
        self, listing_factory, settings, registry
    ):
        deal = make_deal(listing_factory, settings, registry, price_eur=10.0)
        n = await self._notifier(["https://discord.com/cheap", "", ""])
        try:
            assert await n.send_deal(deal) is True
        finally:
            await n.close()

    async def test_expensive_deal_routes_to_top_channel(
        self, listing_factory, settings, registry
    ):
        # Nike (тір B) потребує множник 2.8+: медіана 300 при ціні 50 дає x4.3
        deal = make_deal(listing_factory, settings, registry, price_eur=50.0, median=300.0)
        n = await self._notifier(["", "", "https://discord.com/top"])
        try:
            assert await n.send_deal(deal) is True
            assert n.webhook_for(deal.price_eur) == "https://discord.com/top"
        finally:
            await n.close()


class TestEmbed:
    def test_embed_contains_the_key_numbers(self, listing_factory, settings, registry):
        deal = make_deal(listing_factory, settings, registry, price_eur=10.0)
        embed = _build_embed(deal)
        assert deal.listing.brand_title in embed["title"]
        assert embed["url"] == deal.listing.url
        joined = " ".join(f"{f['name']} {f['value']}" for f in embed["fields"])
        assert f"{deal.price_eur:.2f}" in joined
        assert f"{deal.profit_eur:.2f}" in joined
        assert f"{deal.shipping_eur:.2f}" in joined
        assert f"{deal.net_profit_eur:.2f}" in joined

    def test_top_channel_is_gold_all_is_blurple(self, listing_factory, settings, registry):
        from vintsniper.notify.discord import ALL_COLOR, TIER_COLOR

        deal = make_deal(listing_factory, settings, registry, price_eur=10.0)
        embed = _build_embed(deal)
        assert embed["color"] == (TIER_COLOR if deal.channel == "top" else ALL_COLOR)

    def test_photo_becomes_thumbnail_when_present(self, listing_factory, settings, registry):
        deal = make_deal(listing_factory, settings, registry, price_eur=10.0)
        embed = _build_embed(deal)
        if deal.listing.photo_url:
            assert embed["thumbnail"]["url"] == deal.listing.photo_url
