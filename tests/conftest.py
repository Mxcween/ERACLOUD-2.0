from __future__ import annotations

import pytest

from vintsniper.models import Listing
from vintsniper.settings import load_settings
from vintsniper.vinted.brands import BrandRegistry, ResolvedBrand

NOW = 1_700_000_000


@pytest.fixture(scope="session")
def settings():
    return load_settings()


@pytest.fixture
def registry():
    return BrandRegistry(
        [
            ResolvedBrand(
                name="Nike", brand_id=53, vinted_title="Nike", tier="B",
                replica_risk="high", requires_authenticity_check=True,
            ),
            ResolvedBrand(
                name="Stone Island", brand_id=73306, vinted_title="Stone Island",
                tier="S", replica_risk="high",
            ),
            ResolvedBrand(
                name="Carhartt WIP", brand_id=872289, vinted_title="Carhartt WIP",
                tier="A", replica_risk="medium",
            ),
            ResolvedBrand(
                name="Gucci", brand_id=10, vinted_title="Gucci",
                tier="S", replica_risk="high",
            ),
            ResolvedBrand(
                name="Louis Vuitton", brand_id=11, vinted_title="Louis Vuitton",
                tier="S", replica_risk="high",
            ),
        ]
    )


def make_listing(**overrides) -> Listing:
    defaults = dict(
        item_id=1,
        market="PL",
        catalog_id=1206,
        title="Kurtka Nike",
        brand_title="Nike",
        size_title="M / 38 / 10",
        status_title="Bardzo dobry",
        status_id=None,
        price=40.0,
        total_price=44.0,
        currency="PLN",
        url="https://www.vinted.pl/items/1",
        photo_url="https://images1.vinted.net/x.jpg",
        seller_id=7,
        seller_login="seller",
        seller_is_business=False,
        favourite_count=0,
        view_count=0,
        uploaded_ts=NOW - 120,
        seen_ts=NOW,
    )
    defaults.update(overrides)
    return Listing(**defaults)


@pytest.fixture
def listing_factory():
    return make_listing
