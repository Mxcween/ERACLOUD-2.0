"""Розрахунок вигоди.

    вартість  = ціна з комісією покупця + доставка до тебе
    продаж    = оцінка ринку * поправка на реальність * (1 - комісія майданчика)
    профіт    = продаж - вартість
    множник   = продаж / вартість

Множник 2.0 означає "гроші відіб'ються вдвічі", тобто чистий профіт дорівнює
вкладеному. Це нижня межа, нижче бот мовчить.
"""
from __future__ import annotations

from ..models import Deal
from ..settings import Settings
from .filters import Candidate
from .pricing import PriceBook


def evaluate(
    candidate: Candidate,
    *,
    settings: Settings,
    price_book: PriceBook,
    shipping_eur: float,
    now_ts: int,
) -> Deal | None:
    """Рахує лот і повертає Deal, якщо він проходить пороги. Інакше None."""
    scoring = settings.scoring or {}
    conditions = settings.conditions or {}
    buckets = list((conditions.get("buckets") or {}).keys())
    factors = conditions.get("resale_factor") or {}

    brand = candidate.brand
    category = candidate.category
    condition_factor = float(factors.get(candidate.bucket, 1.0))
    baseline = float(category.baseline_eur.get(brand.tier, 0.0))

    estimate = price_book.estimate(
        brand_id=brand.brand_id,
        catalog_id=category.id,
        bucket=candidate.bucket,
        now_ts=now_ts,
        baseline_eur=baseline,
        condition_factor=condition_factor,
        all_buckets=buckets,
    )
    if estimate.value_eur <= 0:
        return None

    haircut = float(scoring.get("resale_haircut", 0.85))
    fee_rate = float(scoring.get("resale_fee_rate", 0.0))
    resale_eur = round(estimate.value_eur * haircut * (1.0 - fee_rate), 2)

    cost_eur = round(candidate.price_eur + shipping_eur, 2)
    if cost_eur <= 0:
        return None

    profit_eur = round(resale_eur - cost_eur, 2)
    multiple = round(resale_eur / cost_eur, 2)

    min_multiple = brand.min_multiple or float(scoring.get("min_multiple", 2.0))
    # Коли ціну перепродажу ми не виміряли, а вгадали з базової таблиці, вимагаємо
    # більший запас. Інакше бот сипле маргінальними x2.01 на брендах, по яких
    # реальних даних ще немає.
    if not estimate.is_measured:
        min_multiple += float(scoring.get("unmeasured_multiple_premium", 0.4))
    min_profit = float(scoring.get("min_profit_eur", 8.0))
    top_multiple = float(scoring.get("top_multiple", 3.0))
    top_profit = float(scoring.get("top_profit_eur", 20.0))

    if multiple < min_multiple or profit_eur < min_profit:
        return None

    channel = "top" if (multiple >= top_multiple and profit_eur >= top_profit) else "all"

    notes: list[str] = []
    if not estimate.is_measured:
        notes.append("ціна перепродажу поки що оцінна, ринкових даних мало")
    if brand.replica_risk == "high" or brand.requires_authenticity_check:
        notes.append("бренд часто підробляють, перевір бирки і шви на фото")
    if candidate.listing.seller_is_business:
        notes.append("продавець-магазин")
    if candidate.bucket == "good":
        notes.append("стан «добрий», зважай на фото")
    age = candidate.listing.age_seconds
    if age is not None and age > 7 * 86400:
        # Оголошення щойно з'явилось у стрічці, а фото старе: річ перевиставляють.
        # Часто це означає, що за старою ціною її ніхто не забрав.
        notes.append("схоже на перевиставлення: фото старші за тиждень")

    return Deal(
        listing=candidate.listing,
        tier=brand.tier,
        channel=channel,
        category_key=category.key,
        category_name=category.name,
        cost_eur=cost_eur,
        price_eur=candidate.price_eur,
        resale_eur=resale_eur,
        profit_eur=profit_eur,
        multiple=multiple,
        reference=estimate.label,
        sample_size=estimate.sample_size,
        condition_bucket=candidate.bucket,
        replica_risk=brand.replica_risk,
        authenticity_flag=bool(brand.requires_authenticity_check),
        notes=notes,
    )
