"""Складання тексту алерта."""
from __future__ import annotations

from html import escape

from ..models import Deal

MARKET_LABEL = {
    "PL": "🇵🇱 Польща",
    "DE": "🇩🇪 Німеччина",
    "FR": "🇫🇷 Франція",
    "IT": "🇮🇹 Італія",
    "ES": "🇪🇸 Іспанія",
    "LT": "🇱🇹 Литва",
    "CZ": "🇨🇿 Чехія",
    "GB": "🇬🇧 Британія",
    "NL": "🇳🇱 Нідерланди",
}

CONDITION_LABEL = {
    "new": "нове",
    "very_good": "дуже добре",
    "good": "добре",
}

TIER_LABEL = {
    "S": "преміум",
    "A": "робочий тір",
    "B": "масовий",
}


def format_deal(deal: Deal) -> str:
    listing = deal.listing
    head = "🔥 <b>ТОП ЗНАХІДКА</b>" if deal.channel == "top" else "💰 <b>Знахідка</b>"

    brand = escape(listing.brand_title or "без бренду")
    title = escape(listing.title or "без назви")
    size = escape(listing.size_title or "не вказано")
    market = MARKET_LABEL.get(listing.market, listing.market)
    condition = CONDITION_LABEL.get(deal.condition_bucket, deal.condition_bucket)

    price_local = f"{listing.total_price:.2f} {escape(listing.currency)}"
    price_line = f"{price_local}"
    if listing.currency != "EUR":
        price_line += f" ≈ {deal.price_eur:.2f} EUR"

    reference = escape(deal.reference)
    if deal.sample_size:
        reference += f", {deal.sample_size} лот{_plural(deal.sample_size)}"

    lines = [
        f"{head} · <b>{brand}</b>",
        f"<i>{deal.category_name} · {TIER_LABEL.get(deal.tier, deal.tier)}</i>",
        "",
        f"{title}",
        f"Розмір: <b>{size}</b> · Стан: {condition}",
        f"{market} · фото завантажено {listing.age_human} тому",
        "",
        f"Ціна: <b>{price_line}</b>",
        f"З доставкою: <b>{deal.cost_eur:.2f} EUR</b>",
        f"Оцінка продажу: <b>{deal.resale_eur:.2f} EUR</b> <i>({reference})</i>",
        f"Профіт: <b>+{deal.profit_eur:.2f} EUR</b> чистими, з доставкою",
        f"Множник: <b>x{deal.multiple:.2f}</b> від ціни речі",
    ]

    if deal.notes:
        lines.append("")
        lines.extend(f"⚠️ {escape(note)}" for note in deal.notes)

    return "\n".join(lines)


def _plural(count: int) -> str:
    """Українське закінчення для слова «лот»."""
    if 11 <= count % 100 <= 14:
        return "ів"
    last = count % 10
    if last == 1:
        return ""
    if last in (2, 3, 4):
        return "и"
    return "ів"


def format_startup(markets: list[str], categories: int, brands: int, dry_run: bool) -> str:
    mode = " <b>(сухий прогін, нічого не шлю)</b>" if dry_run else ""
    return (
        "🟢 <b>Vinted-снайпер запущено</b>" + mode + "\n\n"
        f"Ринки: {', '.join(MARKET_LABEL.get(m, m) for m in markets)}\n"
        f"Категорій: {categories} · брендів: {brands}\n\n"
        "Команди: /stats /brands /mute НАЗВА /unmute НАЗВА /pause /resume /help"
    )


def format_stats(stats, price_book, uptime_seconds: int, paused: bool) -> str:
    hours, rem = divmod(max(0, uptime_seconds), 3600)
    minutes = rem // 60
    state = "⏸ на паузі" if paused else "▶️ працює"
    return (
        f"📊 <b>Статистика</b> · {state}\n\n"
        f"Аптайм: {hours} год {minutes} хв\n"
        f"Переглянуто лотів: {stats.seen_items}\n"
        f"Спостережень цін: {stats.observations}\n"
        f"Зв'язок бренд+категорія: {price_book.tracked_keys}\n"
        f"Алертів усього: {stats.alerts_total}\n"
        f"Алертів за добу: {stats.alerts_24h}\n"
        f"Заглушених брендів: {stats.muted}"
    )


HELP_TEXT = (
    "<b>Команди</b>\n\n"
    "/stats - що бот наробив\n"
    "/brands - скільки брендів у роботі та які заглушені\n"
    "/mute Nike - більше не слати цей бренд\n"
    "/unmute Nike - повернути назад\n"
    "/pause - тимчасово зупинити алерти\n"
    "/resume - продовжити\n"
    "/help - цей текст"
)
