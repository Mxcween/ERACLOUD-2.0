import pytest

from vintsniper.engine.filters import Candidate, Rejected, screen


@pytest.fixture
def outerwear(settings):
    return settings.category_by_id(1206)


@pytest.fixture
def tshirts(settings):
    return settings.category_by_id(76)


@pytest.fixture
def shoes(settings):
    return settings.category_by_id(1231)


def run(listing, settings, registry, category, price_eur, bucket="very_good"):
    return screen(
        listing, settings=settings, registry=registry,
        category=category, price_eur=price_eur, bucket=bucket,
    )


class TestBrandGate:
    def test_accepts_known_brand(self, listing_factory, settings, registry, outerwear):
        result = run(listing_factory(), settings, registry, outerwear, 20.0)
        assert isinstance(result, Candidate)
        assert result.brand.name == "Nike"

    def test_rejects_unknown_brand(self, listing_factory, settings, registry, outerwear):
        result = run(listing_factory(brand_title="Zara"), settings, registry, outerwear, 20.0)
        assert isinstance(result, Rejected)
        assert "не в списку" in result.reason

    def test_brand_match_is_case_insensitive(self, listing_factory, settings, registry, outerwear):
        result = run(listing_factory(brand_title="NIKE"), settings, registry, outerwear, 20.0)
        assert isinstance(result, Candidate)


class TestPriceCeiling:
    def test_expensive_tshirt_is_rejected_even_for_premium_brand(
        self, listing_factory, settings, registry, tshirts
    ):
        """Головне правило замовника: футболки по 50 євро бути не повинно."""
        listing = listing_factory(brand_title="Stone Island", size_title="L")
        result = run(listing, settings, registry, tshirts, 50.0)
        assert isinstance(result, Rejected)
        assert "стелю" in result.reason

    def test_same_price_passes_for_outerwear(self, listing_factory, settings, registry, outerwear):
        listing = listing_factory(brand_title="Stone Island", size_title="L")
        assert isinstance(run(listing, settings, registry, outerwear, 50.0), Candidate)

    def test_rejects_below_minimum(self, listing_factory, settings, registry, outerwear):
        result = run(listing_factory(), settings, registry, outerwear, 0.5)
        assert isinstance(result, Rejected)
        assert "мінімум" in result.reason


class TestSizeGate:
    def test_rejects_size_outside_whitelist(self, listing_factory, settings, registry, outerwear):
        result = run(listing_factory(size_title="XXL / 56"), settings, registry, outerwear, 20.0)
        assert isinstance(result, Rejected)
        assert "розмір" in result.reason

    def test_accepts_whitelisted_size(self, listing_factory, settings, registry, outerwear):
        assert isinstance(
            run(listing_factory(size_title="XL / 54"), settings, registry, outerwear, 20.0),
            Candidate,
        )

    def test_shoes_use_eu_range(self, listing_factory, settings, registry, shoes):
        ok = run(listing_factory(size_title="43"), settings, registry, shoes, 20.0)
        too_small = run(listing_factory(size_title="37"), settings, registry, shoes, 20.0)
        assert isinstance(ok, Candidate)
        assert isinstance(too_small, Rejected)

    def test_accessories_skip_size_check(self, listing_factory, settings, registry):
        accessories = settings.category_by_id(82)
        result = run(listing_factory(size_title=""), settings, registry, accessories, 20.0)
        assert isinstance(result, Candidate)


class TestConditionGate:
    def test_unknown_condition_is_rejected(self, listing_factory, settings, registry, outerwear):
        result = run(listing_factory(), settings, registry, outerwear, 20.0, bucket=None)
        assert isinstance(result, Rejected)
        assert "стан" in result.reason


class TestJunkListings:
    """Нашивка бренду це не куртка бренду, хоч і лежить у тій самій категорії."""

    def test_patch_is_rejected(self, listing_factory, settings, registry, outerwear):
        listing = listing_factory(brand_title="Nike", title="Patch Nike vintage")
        result = run(listing, settings, registry, outerwear, 14.0)
        assert isinstance(result, Rejected)
        assert "не сама річ" in result.reason

    def test_laces_are_rejected(self, listing_factory, settings, registry, shoes):
        listing = listing_factory(brand_title="Nike", title="Sznurówki Nike", size_title="43")
        result = run(listing, settings, registry, shoes, 8.0)
        assert isinstance(result, Rejected)

    def test_real_jacket_still_passes(self, listing_factory, settings, registry, outerwear):
        listing = listing_factory(brand_title="Nike", title="Kurtka wiatrówka Nike")
        assert isinstance(run(listing, settings, registry, outerwear, 20.0), Candidate)

    def test_vintage_in_title_is_not_treated_as_a_tag(
        self, listing_factory, settings, registry, outerwear
    ):
        listing = listing_factory(brand_title="Nike", title="Vintage Nike windbreaker")
        assert isinstance(run(listing, settings, registry, outerwear, 20.0), Candidate)


class TestSlidesAndSlippers:
    """Тапки замовник просив не слати: дешеві й погано продаються."""

    @pytest.mark.parametrize(
        "title",
        ["Pantofle", "Napapijri papucs 44/45", "Adidas Yeezy slides",
         "Klapki Nike", "adidas Adilette Aqua", "Badelatschen Nike", "Ciabatte Nike"],
    )
    def test_slides_are_rejected(self, listing_factory, settings, registry, shoes, title):
        listing = listing_factory(brand_title="Nike", title=title, size_title="43")
        assert isinstance(run(listing, settings, registry, shoes, 12.0), Rejected), title

    def test_real_sneakers_still_pass(self, listing_factory, settings, registry, shoes):
        for title in ["Nike Air Force 1 T. 41", "Jordan 4 oreo", "Nike court vission 41"]:
            listing = listing_factory(brand_title="Nike", title=title, size_title="43")
            assert isinstance(run(listing, settings, registry, shoes, 12.0), Candidate), title


class TestCaps:
    """Кепки лише в люксу: у масових брендів це мертвий товар."""

    @pytest.mark.parametrize(
        "title", ["Nike cap", "Czapka Nike", "Nike Kappe", "Snapback Nike", "Nike bucket hat"]
    )
    def test_mass_brand_caps_rejected(self, listing_factory, settings, registry, title):
        accessories = settings.category_by_id(82)
        listing = listing_factory(brand_title="Nike", title=title, size_title="")
        result = run(listing, settings, registry, accessories, 12.0)
        assert isinstance(result, Rejected), title
        assert "головний убір" in result.reason

    def test_luxury_caps_pass(self, listing_factory, settings, registry):
        accessories = settings.category_by_id(82)
        for brand in ("Gucci", "Louis Vuitton"):
            listing = listing_factory(brand_title=brand, title=f"{brand} cap", size_title="")
            assert isinstance(run(listing, settings, registry, accessories, 25.0), Candidate), brand

    def test_vintage_is_not_mistaken_for_a_hat(self, listing_factory, settings, registry, outerwear):
        listing = listing_factory(brand_title="Nike", title="Vintage Nike jacket")
        assert isinstance(run(listing, settings, registry, outerwear, 20.0), Candidate)
