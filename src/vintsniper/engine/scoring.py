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
from .titles import find_word


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

    if candidate.price_eur <= 0:
        return None

    # Профіт від собівартості, БЕЗ доставки. Доставка на Vinted платиться за
    # замовлення, а не за річ: беручи чотири речі в одного продавця, платиш її
    # раз. Відкидати добрий лот через доставку неправильно, тому вона йде
    # окремим рядком в алерті, а рішення лишається за людиною.
    profit_eur = round(resale_eur - candidate.price_eur, 2)

    # А множник - від ціни самої речі. Vinted бере доставку за ЗАМОВЛЕННЯ, а не
    # за одиницю товару: беручи в продавця чотири речі, платиш одну доставку.
    # Якщо вішати повні 3.5 євро на кожну кофту за 4 євро, її вартість зростає
    # утричі, і всі дешеві категорії стають недосяжними: щоб вийшов множник x2,
    # кофта мала б коштувати менше 80 центів. Саме через це бот місяцями слав
    # би саме взуття.
    multiple = round(resale_eur / candidate.price_eur, 2)

    # Планка множника залежить від тіру: x2 на куртці Stone Island це 90 євро,
    # x2 на кросівках масового бренду це 12. Тому масовий бренд має бути
    # справжньою крадіжкою, інакше стрічка забивається дрібницею.
    by_tier = scoring.get("min_multiple_by_tier") or {}
    min_multiple = brand.min_multiple or float(
        by_tier.get(brand.tier, scoring.get("min_multiple", 2.0))
    )
    # Коли ціну перепродажу ми не виміряли, а вгадали з базової таблиці, вимагаємо
    # більший запас. Інакше бот сипле маргінальними x2.01 на брендах, по яких
    # реальних даних ще немає.
    if not estimate.is_measured:
        min_multiple += float(scoring.get("unmeasured_multiple_premium", 0.4))
    # Поріг профіту беремо категорійний. Спільний поріг на всі категорії
    # просто вимикає дешеві: футболка масового бренду вся коштує 9 євро, тому
    # 12 євро чистими з неї не вийде НІКОЛИ, і такі лоти зникали мовчки.
    min_profit = (
        category.min_profit_eur
        if category.min_profit_eur is not None
        else float(scoring.get("min_profit_eur", 8.0))
    )
    top_multiple = float(scoring.get("top_multiple", 3.0))
    top_profit = float(scoring.get("top_profit_eur", 20.0))

    if multiple < min_multiple or profit_eur < min_profit:
        return None

    # Занадто добре, щоб бути правдою. Нові кросівки за 1.75 євро це не
    # знахідка, а приманка: такі лоти або міняють ціну після публікації, або
    # їх зносить сама Vinted. Краще пропустити один справжній подарунок долі,
    # ніж регулярно ганятись за фейками.
    max_multiple = float(scoring.get("max_multiple", 0) or 0)
    if max_multiple and multiple > max_multiple:
        return None

    channel = "top" if (multiple >= top_multiple and profit_eur >= top_profit) else "all"

    notes: list[str] = []
    if not estimate.is_measured:
        notes.append("ціна перепродажу поки що оцінна, ринкових даних мало")
    if brand.replica_risk == "high" or brand.requires_authenticity_check:
        notes.append("бренд часто підробляють, перевір бирки і шви на фото")
    # Не відсікаємо: продавці пишуть ці слова і в заперечення ("nie fake"),
    # тому вирішує людина, а не бот.
    hint = find_word(candidate.listing.title, settings.title_warn_words)
    if hint is not None:
        notes.append(f"у назві є слово {hint!r}, прочитай опис уважно")
    if candidate.listing.seller_is_business:
        notes.append("продавець-магазин")
    if candidate.bucket == "good":
        notes.append("стан «добрий», зважай на фото")
    if multiple >= 6.0:
        notes.append("підозріло дешево, перевір продавця і опис перед оплатою")
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
        price_eur=candidate.price_eur,
        resale_eur=resale_eur,
        profit_eur=profit_eur,
        shipping_eur=round(shipping_eur, 2),
        multiple=multiple,
        reference=estimate.label,
        sample_size=estimate.sample_size,
        condition_bucket=candidate.bucket,
        replica_risk=brand.replica_risk,
        authenticity_flag=bool(brand.requires_authenticity_check),
        notes=notes,
    )
