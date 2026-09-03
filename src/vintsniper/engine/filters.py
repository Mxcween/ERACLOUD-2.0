"""Жорсткі відсіви.

Все, що тут не пройшло, навіть не рахується. Головний запобіжник - стеля ціни
по категорії: футболка дорожча за 16 євро не пройде, хай навіть це Stone Island.
Саме це не дає боту слати "брендові" лоти без запасу на перепродаж.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models import Listing
from ..settings import Category, Settings
from ..vinted.brands import BrandRegistry, ResolvedBrand
from .sizes import clothing_size, shoe_size_eu
from .titles import find_word


@dataclass(frozen=True)
class Candidate:
    listing: Listing
    brand: ResolvedBrand
    category: Category
    price_eur: float
    bucket: str


@dataclass(frozen=True)
class Rejected:
    reason: str


FilterResult = Candidate | Rejected


def screen(
    listing: Listing,
    *,
    settings: Settings,
    registry: BrandRegistry,
    category: Category,
    price_eur: float,
    bucket: str | None,
) -> FilterResult:
    """Один лот проти всіх жорстких правил."""
    brand = registry.by_title(listing.brand_title)
    if brand is None:
        return Rejected("бренд не в списку")

    if bucket is None:
        return Rejected("невідомий стан")

    min_price = float((settings.category_defaults or {}).get("min_price_eur", 1.0))
    if price_eur < min_price:
        return Rejected(f"ціна нижча за мінімум ({min_price:.0f} EUR)")

    if price_eur > category.ceiling_eur:
        return Rejected(f"дорожче за стелю категорії ({category.ceiling_eur:.0f} EUR)")

    if not _size_ok(listing, settings, category):
        return Rejected(f"розмір {listing.size_title!r} не підходить")

    # Нашивка CP Company лежить у категорії "верхній одяг" і коштує менше за
    # стелю куртки, тому без цього її оцінили б як куртку.
    junk = find_word(listing.title, settings.title_blocklist)
    if junk is not None:
        return Rejected(f"не сама річ, а {junk!r}")

    # Слова, заборонені саме в цій категорії. Бігові кросівки не продаються,
    # але вітрівка з "running" у назві це нормальний лот.
    local = find_word(listing.title, settings.blocklist_by_category.get(category.key))
    if local is not None:
        return Rejected(f"{local!r} у категорії {category.name.lower()}")

    # Кепки беремо лише в люксу: у масових брендів це мертвий товар.
    if brand.name.casefold() not in settings.cap_allowed_brands:
        cap = find_word(listing.title, settings.cap_words)
        if cap is not None:
            return Rejected(f"головний убір ({cap!r}) не в люксовому бренді")

    seller_cfg = settings.seller or {}
    if seller_cfg.get("skip_business_sellers") and listing.seller_is_business:
        return Rejected("продавець-магазин")

    return Candidate(
        listing=listing,
        brand=brand,
        category=category,
        price_eur=price_eur,
        bucket=bucket,
    )


def _size_ok(listing: Listing, settings: Settings, category: Category) -> bool:
    sizes_cfg = settings.sizes or {}
    if category.key in (sizes_cfg.get("skip_size_check_categories") or []):
        return True

    if category.key == "shoes":
        eu = shoe_size_eu(listing.size_title)
        if eu is None:
            # Взуття без вказаного розміру продати важче, але це не привід
            # викидати лот, якщо він дуже дешевий. Пропускаємо далі.
            return True
        lo = float(sizes_cfg.get("shoes_eu_min", 0))
        hi = float(sizes_cfg.get("shoes_eu_max", 99))
        return lo <= eu <= hi

    allowed = {s.upper() for s in (sizes_cfg.get("clothing") or [])}
    if not allowed:
        return True
    size = clothing_size(listing.size_title)
    if size is None:
        # Порожній розмір трапляється в аксесуарах і частині верхнього одягу
        return not listing.size_title.strip()
    return size in allowed
