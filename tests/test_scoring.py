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
        assert deal.cost_eur == 23.5             # ціна + доставка
        assert deal.resale_eur == 72.0           # 100 * 0.72
        assert deal.profit_eur == 48.5           # профіт чистий, з доставкою
        # Множник рахується від ціни самої речі, а не від ціни з доставкою
        assert deal.multiple == pytest.approx(3.6, abs=0.01)
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
        # ціна така, щоб множник вийшов рівно 2.2: між 2.0 і 2.4.
        # Множник рахується від ціни речі, доставку сюди не додаємо.
        price = round(resale / 2.2, 2)
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


class TestRelistSignal:
    def test_old_photo_on_a_fresh_listing_is_flagged(
        self, listing_factory, settings, registry, outerwear
    ):
        """Vinted віддає час ФОТО, не публікації. Старе фото = перевиставлення."""
        listing = listing_factory(uploaded_ts=NOW - 20 * 86400, seen_ts=NOW)
        cand = candidate(listing, registry, outerwear, 20.0)
        deal = score(cand, settings, book_with(100.0))
        assert any("перевиставлення" in n for n in deal.notes)

    def test_recent_photo_is_not_flagged(self, listing_factory, settings, registry, outerwear):
        cand = candidate(listing_factory(), registry, outerwear, 20.0)
        deal = score(cand, settings, book_with(100.0))
        assert not any("перевиставлення" in n for n in deal.notes)


class TestReplicaHint:
    def test_suspicious_word_in_title_adds_a_note(
        self, listing_factory, settings, registry, outerwear
    ):
        cand = candidate(listing_factory(title="Nike jacket replica"), registry, outerwear, 20.0)
        deal = score(cand, settings, book_with(100.0))
        assert any("replica" in n for n in deal.notes)

    def test_clean_title_gets_no_such_note(self, listing_factory, settings, registry, outerwear):
        cand = candidate(listing_factory(title="Nike windbreaker"), registry, outerwear, 20.0)
        deal = score(cand, settings, book_with(100.0))
        assert not any("прочитай опис" in n for n in deal.notes)


class TestPerCategoryProfitFloor:
    """Спільний поріг профіту вимикав дешеві категорії повністю.

    При 12 євро на всі категорії футболка масового бренду не могла пройти
    ніколи: вона вся коштує близько 9 євро, тож 12 чистими з неї не вийде
    навіть якби її віддавали безкоштовно. За перші 57 алертів було 25
    взуття і рівно нуль футболок.
    """

    def test_cheap_categories_have_a_reachable_floor(self, settings):
        for key in ("tops_tshirts", "knitwear_hoodies", "trousers", "jeans"):
            cat = next(c for c in settings.categories if c.key == key)
            resale_at_median = cat.baseline_eur["B"] * 0.72
            assert cat.min_profit_eur < resale_at_median, (
                f"{key}: поріг {cat.min_profit_eur} недосяжний, "
                f"продаж масового бренду тут лише {resale_at_median:.1f}"
            )

    def test_expensive_categories_keep_a_higher_floor(self, settings):
        outerwear = next(c for c in settings.categories if c.key == "outerwear")
        shoes = next(c for c in settings.categories if c.key == "shoes")
        tshirts = next(c for c in settings.categories if c.key == "tops_tshirts")
        assert outerwear.min_profit_eur > tshirts.min_profit_eur
        assert shoes.min_profit_eur > tshirts.min_profit_eur

    def test_hoodie_that_used_to_be_silently_dropped_now_alerts(
        self, listing_factory, settings, registry
    ):
        """Nike-кофта за 4 євро: продаж 15, профіт 7.5. Раніше різалась порогом 12."""
        knitwear = settings.category_by_id(79)
        book = book_with(21.0, brand_id=53, catalog_id=79)
        cand = Candidate(
            listing=listing_factory(catalog_id=79, title="Nike hoodie"),
            brand=registry.by_title("Nike"), category=knitwear,
            price_eur=4.0, bucket="very_good",
        )
        deal = evaluate(
            cand, settings=settings, price_book=book, shipping_eur=3.5, now_ts=NOW
        )
        assert deal is not None
        assert deal.profit_eur >= knitwear.min_profit_eur
        assert deal.multiple >= 2.0


class TestMultipleIgnoresShipping:
    """Vinted бере доставку за замовлення, а не за річ.

    Поки множник рахувався від "ціна + доставка", кофта за 4 євро мала
    вартість 7.5 і множник 1.4 замість 2.7. Дешеві категорії через це були
    заблоковані арифметично, і бот слав саме взуття.
    """

    def test_multiple_uses_item_price(self, listing_factory, settings, registry, outerwear):
        cand = candidate(listing_factory(), registry, outerwear, 20.0)
        deal = score(cand, settings, book_with(100.0), shipping=3.5)
        assert deal.multiple == pytest.approx(72.0 / 20.0, abs=0.01)

    def test_profit_still_pays_full_shipping(
        self, listing_factory, settings, registry, outerwear
    ):
        cand = candidate(listing_factory(), registry, outerwear, 20.0)
        deal = score(cand, settings, book_with(100.0), shipping=3.5)
        assert deal.cost_eur == 23.5
        assert deal.profit_eur == round(72.0 - 23.5, 2)

    def test_shipping_still_decides_cheap_categories(
        self, listing_factory, settings, registry
    ):
        """Доставка лишається головним важелем: вона з'їдає профіт, не множник."""
        knitwear = settings.category_by_id(79)
        book = book_with(15.0, brand_id=53, catalog_id=79)
        cand = Candidate(
            listing=listing_factory(catalog_id=79, title="Nike hoodie"),
            brand=registry.by_title("Nike"), category=knitwear,
            # Продаж 10.8, поріг профіту 7. За доставки 3.5 лишається 4.8 і лот
            # не проходить, за доставки 1.0 лишається 7.3 і проходить.
            price_eur=2.5, bucket="very_good",
        )
        expensive = evaluate(
            cand, settings=settings, price_book=book, shipping_eur=3.5, now_ts=NOW
        )
        cheap = evaluate(
            cand, settings=settings, price_book=book, shipping_eur=1.0, now_ts=NOW
        )
        assert expensive is None, "за 3.5 доставки профіт не дотягує до порога"
        assert cheap is not None, "за 1.0 доставки та сама кофта вже вигідна"
        # Множник в обох випадках однаковий: доставка на нього не впливає
        assert cheap.multiple == pytest.approx(15.0 * 0.72 / 2.5, abs=0.01)
