import pytest

from vintsniper.engine.filters import Candidate
from vintsniper.engine.pricing import PriceBook
from vintsniper.engine.scoring import evaluate
from vintsniper.notify.formatting import _plural, format_deal, format_startup

NOW = 1_700_000_000


@pytest.fixture
def deal(listing_factory, settings, registry):
    category = settings.category_by_id(1206)
    book = PriceBook(min_samples=4)
    for _ in range(10):
        book.record(53, 1206, "very_good", 100.0, NOW)
    candidate = Candidate(
        listing=listing_factory(),
        brand=registry.by_title("Nike"),
        category=category,
        price_eur=20.0,
        bucket="very_good",
    )
    return evaluate(
        candidate, settings=settings, price_book=book, shipping_eur=3.5, now_ts=NOW
    )


class TestDealMessage:
    def test_contains_the_facts_needed_to_decide(self, deal):
        text = format_deal(deal)
        assert "Nike" in text
        assert "Верхній одяг" in text
        assert "🇵🇱 Польща" in text
        assert "PLN" in text                 # ціна в валюті ринку
        assert "EUR" in text                 # і в євро
        assert f"x{deal.multiple:.2f}" in text
        assert "Розмір" in text

    def test_top_deals_are_marked(self, deal):
        assert deal.channel == "top"
        assert "ТОП" in format_deal(deal)

    def test_escapes_html_in_user_content(self, listing_factory, settings, registry):
        category = settings.category_by_id(1206)
        candidate = Candidate(
            listing=listing_factory(title="<script>alert(1)</script> куртка"),
            brand=registry.by_title("Nike"),
            category=category,
            price_eur=2.0,
            bucket="very_good",
        )
        deal = evaluate(
            candidate, settings=settings, price_book=PriceBook(min_samples=8),
            shipping_eur=3.5, now_ts=NOW,
        )
        text = format_deal(deal)
        assert "<script>" not in text
        assert "&lt;script&gt;" in text

    def test_warnings_are_rendered(self, deal):
        text = format_deal(deal)
        assert "⚠️" in text


class TestPlural:
    @pytest.mark.parametrize(
        "count,expected",
        [(1, ""), (2, "и"), (4, "и"), (5, "ів"), (11, "ів"), (14, "ів"),
         (21, ""), (22, "и"), (25, "ів"), (101, ""), (112, "ів")],
    )
    def test_ukrainian_endings(self, count, expected):
        assert _plural(count) == expected


class TestStartup:
    def test_mentions_markets_and_dry_run(self):
        text = format_startup(["PL", "DE"], 7, 76, dry_run=True)
        assert "Польща" in text and "Німеччина" in text
        assert "76" in text
        assert "сухий прогін" in text
