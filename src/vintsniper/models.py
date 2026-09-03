"""Доменні моделі. Listing - те, що прийшло з Vinted, Deal - те, що ми вирішили."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class Listing:
    """Один лот з каталогу Vinted, приведений до зручного вигляду."""

    item_id: int
    market: str
    catalog_id: int
    title: str
    brand_title: str
    size_title: str
    status_title: str
    status_id: int | None
    # Ціна продавця без комісії покупця
    price: float
    # Скільки реально спишуть з карти: ціна + захист покупця
    total_price: float
    currency: str
    url: str
    photo_url: str | None
    seller_id: int | None
    seller_login: str | None
    seller_is_business: bool
    favourite_count: int
    view_count: int
    uploaded_ts: int | None
    seen_ts: int

    @property
    def age_seconds(self) -> int | None:
        if self.uploaded_ts is None:
            return None
        return max(0, self.seen_ts - self.uploaded_ts)

    @property
    def age_human(self) -> str:
        age = self.age_seconds
        if age is None:
            return "?"
        if age < 90:
            return f"{age} сек"
        if age < 5400:
            return f"{age // 60} хв"
        return f"{age // 3600} год"


@dataclass(frozen=True)
class Deal:
    """Прорахований лот, готовий до відправки."""

    listing: Listing
    tier: str                    # S / A / B - тір бренду
    channel: str                 # "top" або "all"
    category_key: str
    category_name: str
    cost_eur: float              # скільки віддаси разом з доставкою
    price_eur: float             # ціна лота в євро без доставки
    resale_eur: float            # реалістична ціна перепродажу
    profit_eur: float
    multiple: float
    reference: str               # "медіана" або "базова оцінка"
    sample_size: int
    condition_bucket: str
    replica_risk: str
    authenticity_flag: bool
    notes: list[str] = field(default_factory=list)

    @property
    def profit_pct(self) -> float:
        return (self.multiple - 1.0) * 100.0


def utc_now_ts() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())
