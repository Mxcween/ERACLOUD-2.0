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
        assert deal.price_eur == 20.0            # собівартість
        assert deal.resale_eur == 72.0           # 100 * 0.72
        assert deal.profit_eur == 52.0           # продаж мінус собівартість
        assert deal.shipping_eur == 3.5          # довідково, у відборі не бере участі
        assert deal.net_profit_eur == 48.5       # що лишиться після доставки
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
        # Nike це масовий тір, йому потрібен множник від 2.8. Медіана 80 дає
        # 2.88: достатньо для стрічки, замало для ТОПу.
        cand = candidate(listing_factory(), registry, outerwear, 20.0)
        modest = score(cand, settings, book_with(80.0))
        assert modest is not None
        assert modest.channel == "all"
        assert 2.8 <= modest.multiple < 3.0


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
        # 5 EUR за куртку Nike: запас великий, але не настільки, щоб це
        # виглядало приманкою
        cand = candidate(listing_factory(), registry, outerwear, 5.0)
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

    def test_floor_is_reachable_for_the_tier_that_matters(self, settings):
        """Поріг має бути досяжним хоча б для робочого тіру.

        Для масових брендів у дешевих категоріях маржі просто немає: футболка
        Nike вся коштує 8 євро. Такі речі й не мусять проходити. А от футболка
        Carhartt чи поло Ralph Lauren мусять, тому перевіряємо тір A.
        """
        for key in ("tops_tshirts", "polo", "knitwear_hoodies", "trousers", "jeans"):
            cat = next(c for c in settings.categories if c.key == key)
            reachable = cat.baseline_eur["A"] * 0.72
            assert cat.min_profit_eur < reachable, (
                f"{key}: поріг {cat.min_profit_eur} недосяжний навіть для тіру A, "
                f"де продаж лише {reachable:.1f}"
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
        """Кофта Carhartt WIP за 8 євро. Раніше різалась спільним порогом 12."""
        knitwear = settings.category_by_id(79)
        book = book_with(35.0, brand_id=872289, catalog_id=79)
        cand = Candidate(
            listing=listing_factory(catalog_id=79, title="Carhartt hoodie"),
            brand=registry.by_title("Carhartt WIP"), category=knitwear,
            price_eur=8.0, bucket="very_good",
        )
        deal = evaluate(
            cand, settings=settings, price_book=book, shipping_eur=3.5, now_ts=NOW
        )
        assert deal is not None
        assert deal.profit_eur >= knitwear.min_profit_eur
        assert deal.multiple >= 2.0


class TestShippingIsInformationalOnly:
    """Доставка не бере участі у відборі взагалі.

    Vinted бере її за замовлення, а не за річ, тому відкидати гарний лот
    через неї неправильно. Вона показується окремим рядком, а рішення
    лишається за людиною.
    """

    def test_multiple_uses_item_price(self, listing_factory, settings, registry, outerwear):
        cand = candidate(listing_factory(), registry, outerwear, 20.0)
        deal = score(cand, settings, book_with(100.0), shipping=3.5)
        assert deal.multiple == pytest.approx(72.0 / 20.0, abs=0.01)

    def test_profit_excludes_shipping(self, listing_factory, settings, registry, outerwear):
        cand = candidate(listing_factory(), registry, outerwear, 20.0)
        deal = score(cand, settings, book_with(100.0), shipping=3.5)
        assert deal.profit_eur == 52.0
        assert deal.net_profit_eur == 48.5

    def test_shipping_never_blocks_a_find(self, listing_factory, settings, registry):
        """Той самий лот проходить і за дешевої, і за дорогої доставки."""
        knitwear = settings.category_by_id(79)
        book = book_with(35.0, brand_id=872289, catalog_id=79)
        cand = Candidate(
            listing=listing_factory(catalog_id=79, title="Carhartt hoodie"),
            brand=registry.by_title("Carhartt WIP"), category=knitwear,
            price_eur=8.0, bucket="very_good",
        )
        cheap = evaluate(cand, settings=settings, price_book=book, shipping_eur=1.0, now_ts=NOW)
        pricey = evaluate(cand, settings=settings, price_book=book, shipping_eur=14.0, now_ts=NOW)
        assert cheap is not None and pricey is not None
        assert cheap.profit_eur == pricey.profit_eur, "відбір не залежить від доставки"
        assert cheap.multiple == pricey.multiple
        # А ось чисті гроші вже різні, і людина бачить обидва числа
        assert cheap.net_profit_eur > pricey.net_profit_eur


class TestBaitListings:
    """Приманки: нові adidas за 1.75 євро.

    Реальний випадок: один продавець за 18 хвилин виставив пʼять однакових
    пар у розмірах 41-44 за 1.75 євро кожна, множник вийшов x12.34. Через
    годину всі пʼять сторінок віддавали 404 - лоти знесли. Множник вище
    певної межі це не знахідка, а приманка.
    """

    def test_absurd_multiple_is_rejected(self, listing_factory, settings, registry, outerwear):
        # ринок 100, ціна 5 -> множник 14.4
        cand = candidate(listing_factory(), registry, outerwear, 5.0)
        assert score(cand, settings, book_with(100.0)) is None

    def test_the_exact_adidas_bait_is_rejected(self, listing_factory, settings, registry):
        """1.75 євро при медіані 30 - рівно той лот, що прийшов замовнику."""
        shoes = settings.category_by_id(1242)
        cand = Candidate(
            listing=listing_factory(catalog_id=1242, title="New Sneakers adidas shoes 43",
                                    size_title="43"),
            brand=registry.by_title("Nike"), category=shoes,
            price_eur=1.75, bucket="very_good",
        )
        book = book_with(30.0, brand_id=53, catalog_id=1242)
        assert evaluate(cand, settings=settings, price_book=book,
                        shipping_eur=4.5, now_ts=NOW) is None

    def test_strong_but_believable_deal_survives(
        self, listing_factory, settings, registry, outerwear
    ):
        """Supreme за 15.70 з множником x6.10 це реальна знахідка, не приманка."""
        cand = candidate(listing_factory(), registry, outerwear, 15.0)
        deal = score(cand, settings, book_with(127.0))
        assert deal is not None
        assert 6.0 <= deal.multiple <= 10.0

    def test_high_multiple_carries_a_warning(
        self, listing_factory, settings, registry, outerwear
    ):
        cand = candidate(listing_factory(), registry, outerwear, 15.0)
        deal = score(cand, settings, book_with(127.0))
        assert any("підозріло дешево" in n for n in deal.notes)
