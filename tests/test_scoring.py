import pytest

from vintsniper.engine.filters import Candidate
from vintsniper.engine.pricing import PriceBook
from vintsniper.engine.scoring import evaluate

NOW = 1_700_000_000


@pytest.fixture
def outerwear(settings):
    return settings.category_by_id(1206)


def candidate(listing, registry, category, price_eur, bucket="very_good", brand="Nike"):
    return Candidate(
        listing=listing,
        brand=registry.by_title(brand),
        category=category,
        price_eur=price_eur,
        bucket=bucket,
    )


def book_with(median_price, *, brand_id=53, catalog_id=1206, bucket="very_good", n=12):
    book = PriceBook(min_samples=8)
    for _ in range(n):
        book.record(brand_id, catalog_id, bucket, median_price, NOW)
    return book


def score(cand, settings, book, shipping=3.5):
    return evaluate(
        cand, settings=settings, price_book=book, shipping_eur=shipping, now_ts=NOW
    )


class TestThresholds:
    def test_clear_win_is_reported(self, listing_factory, settings, registry, outerwear):
        # ринок 100 EUR, купівля 20 + 3.5 доставка
        cand = candidate(listing_factory(), registry, outerwear, 20.0)
        deal = score(cand, settings, book_with(100.0))
        assert deal is not None
        assert deal.cost_eur == 23.5
        assert deal.resale_eur == 72.0          # 100 * 0.72
        assert deal.profit_eur == 48.5
        assert deal.multiple == pytest.approx(3.06, abs=0.01)
        assert deal.channel == "top"

    def test_thin_margin_is_dropped(self, listing_factory, settings, registry, outerwear):
        # ринок 40 EUR -> продаж 28.8, вартість 23.5, множник лише 1.23
        cand = candidate(listing_factory(), registry, outerwear, 20.0)
        assert score(cand, settings, book_with(40.0)) is None

    def test_good_multiple_but_tiny_absolute_profit_is_dropped(
        self, listing_factory, settings, registry, outerwear
    ):
        """x2.5 на дешевій речі це +6 EUR. Возитись нема сенсу."""
        cand = candidate(listing_factory(), registry, outerwear, 1.5)
        deal = score(cand, settings, book_with(17.5), shipping=0.5)
        assert deal is None

    def test_channel_split(self, listing_factory, settings, registry, outerwear):
        cand = candidate(listing_factory(), registry, outerwear, 20.0)
        modest = score(cand, settings, book_with(72.0))
        assert modest is not None
        assert modest.channel == "all"
        assert modest.multiple < 3.0


class TestUncertaintyPremium:
    def test_unmeasured_estimate_needs_bigger_margin(
        self, listing_factory, settings, registry, outerwear
    ):
        """Без ринкових даних поріг зростає з 2.0 до 2.4.

        Беремо Carhartt WIP (тір A), щоб абсолютний профіт свідомо перевищував
        мінімальні 12 євро і тест перевіряв саме множник, а не поріг профіту.
        """
        baseline = outerwear.baseline_eur["A"]
        resale = baseline * 0.72
        # ціна така, щоб множник вийшов рівно 2.2: між 2.0 і 2.4
        price = round(resale / 2.2 - 3.5, 2)
        cand = candidate(
            listing_factory(brand_title="Carhartt WIP"),
            registry, outerwear, price, brand="Carhartt WIP",
        )

        guessed = score(cand, settings, PriceBook(min_samples=8))
        assert guessed is None, "оцінка навмання не повинна проходити з множником 2.2"

        measured = score(cand, settings, book_with(baseline, brand_id=872289))
        assert measured is not None
        assert measured.reference == "медіана ринку"
        assert measured.multiple == pytest.approx(2.2, abs=0.02)
        assert measured.profit_eur >= 12.0


class TestNotes:
    def test_warns_about_replica_prone_brand(
        self, listing_factory, settings, registry, outerwear
    ):
        cand = candidate(listing_factory(), registry, outerwear, 20.0)
        deal = score(cand, settings, book_with(100.0))
        assert any("підробляють" in n for n in deal.notes)

    def test_warns_when_estimate_is_a_guess(self, listing_factory, settings, registry, outerwear):
        # 2 EUR за куртку Nike: запас такий, що проходить навіть на оцінці навмання
        cand = candidate(listing_factory(), registry, outerwear, 2.0)
        deal = score(cand, settings, PriceBook(min_samples=8))
        assert deal is not None
        assert any("оцінна" in n for n in deal.notes)
        assert deal.reference == "базова оцінка"

    def test_flags_business_seller(self, listing_factory, settings, registry, outerwear):
        cand = candidate(
            listing_factory(seller_is_business=True), registry, outerwear, 20.0
        )
        deal = score(cand, settings, book_with(100.0))
        assert any("магазин" in n for n in deal.notes)


class TestConditionEffect:
    def test_new_items_are_valued_higher_than_worn(
        self, listing_factory, settings, registry, outerwear
    ):
        book = book_with(100.0, bucket="very_good", n=20)
        as_new = score(
            candidate(listing_factory(), registry, outerwear, 20.0, bucket="new"),
            settings, book,
        )
        as_good = score(
            candidate(listing_factory(), registry, outerwear, 20.0, bucket="good"),
            settings, book,
        )
        assert as_new.resale_eur > as_good.resale_eur
