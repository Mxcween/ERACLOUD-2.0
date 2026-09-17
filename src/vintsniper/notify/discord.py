"""Відправка знахідок у Discord через вебхуки, розкладені по цінових каналах.

Discord вебхук простіший за Telegram Bot API: не треба довгого опитування
команд, досить POST-запиту з готовим "embed" на URL каналу. Кожен ціновий
діапазон - це окремий канал з окремим вебхуком, тому маршрутизація тут
означає "обрати правильний URL за собівартістю лота".
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..models import Deal

log = logging.getLogger(__name__)

# Ті самі правила повторів, що й у Telegram: вебхук не ідемпотентний, тому
# повторювати можна лише запит, який точно не дійшов до Discord.
NEVER_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

TIER_COLOR = 0xFFD700   # золотий для ТОП
ALL_COLOR = 0x5865F2    # звичайний discord-блакитний


class DiscordNotifier:
    """Один клієнт на всі цінові канали. `webhooks[i]` покриває лоти до
    `bounds[i]` включно, останній елемент `webhooks` - усе, що дорожче."""

    def __init__(
        self,
        webhooks: list[str],
        bounds: list[float],
        *,
        dry_run: bool = False,
        timeout: float = 20.0,
    ) -> None:
        if len(webhooks) != len(bounds) + 1:
            raise ValueError(
                f"вебхуків має бути на один більше за меж: "
                f"{len(webhooks)} вебхуків, {len(bounds)} меж"
            )
        self.webhooks = webhooks
        self.bounds = sorted(bounds)
        self.dry_run = dry_run
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def configured(self) -> bool:
        return any(url.strip() for url in self.webhooks)

    def tier_index(self, price_eur: float) -> int:
        for i, bound in enumerate(self.bounds):
            if price_eur <= bound:
                return i
        return len(self.bounds)

    def webhook_for(self, price_eur: float) -> str:
        return self.webhooks[self.tier_index(price_eur)]

    async def send_deal(self, deal: Deal) -> bool:
        url = self.webhook_for(deal.price_eur).strip()
        if not url:
            return False

        embed = _build_embed(deal)
        payload: dict[str, Any] = {"embeds": [embed]}

        if self.dry_run:
            log.info("[dry-run] discord embed (%s): %s", deal.channel, embed["title"])
            return True

        for attempt in range(1, 4):
            try:
                resp = await self._client.post(url, json=payload)
            except NEVER_SENT_ERRORS as exc:
                log.warning("discord вебхук не підключився (%s/3): %s", attempt, exc)
                await asyncio.sleep(2 * attempt)
                continue
            except httpx.HTTPError as exc:
                # Обрив уже після відправки: могло дійти, повтор дав би дубль
                log.warning("discord вебхук обірвався після відправки (%s), не повторюю", exc)
                return False

            if resp.status_code in (200, 204):
                return True
            if resp.status_code == 429:
                retry_after = 1.0
                try:
                    retry_after = float(resp.json().get("retry_after", 1.0))
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(retry_after + 0.5)
                continue

            log.warning("discord вебхук відмовив: %s %s", resp.status_code, resp.text[:200])
            return False
        return False


def _build_embed(deal: Deal) -> dict[str, Any]:
    listing = deal.listing
    is_top = deal.channel == "top"

    fields = [
        {"name": "Собівартість", "value": f"{deal.price_eur:.2f} EUR", "inline": True},
        {"name": "Оцінка продажу", "value": f"{deal.resale_eur:.2f} EUR", "inline": True},
        {"name": "Профіт", "value": f"+{deal.profit_eur:.2f} EUR · x{deal.multiple:.2f}", "inline": True},
        {"name": "Розмір", "value": listing.size_title or "не вказано", "inline": True},
        {"name": "Стан", "value": deal.condition_bucket, "inline": True},
        {"name": "Ринок", "value": listing.market, "inline": True},
        {
            "name": "Доставка",
            "value": f"−{deal.shipping_eur:.2f} EUR → чистими {deal.net_profit_eur:.2f} EUR",
            "inline": False,
        },
    ]
    if deal.notes:
        fields.append({"name": "Увага", "value": "\n".join(f"⚠️ {n}" for n in deal.notes), "inline": False})

    embed: dict[str, Any] = {
        "title": f"{'🔥 ТОП' if is_top else '💰'} {listing.brand_title} · {listing.title[:200]}",
        "url": listing.url,
        "color": TIER_COLOR if is_top else ALL_COLOR,
        "fields": fields,
        "footer": {"text": f"{deal.category_name} · {deal.tier}"},
    }
    if listing.photo_url:
        embed["thumbnail"] = {"url": listing.photo_url}
    return embed
