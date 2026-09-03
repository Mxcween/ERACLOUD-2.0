"""Головний цикл снайпера.

На кожному оберті ми проходимо всі пари ринок+категорія, беремо найсвіжіші
лоти по наших брендах, поповнюємо статистику цін і відправляємо те, що
пройшло пороги вигоди.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .engine.conditions import StatusMap
from .engine.filters import Candidate, Rejected, screen
from .engine.fx import FxConverter
from .engine.pricing import PriceBook
from .engine.schedule import in_quiet_hours
from .engine.scoring import evaluate
from .health import HealthServer
from .models import Deal, Listing, utc_now_ts
from .notify.formatting import HELP_TEXT, format_startup, format_stats
from .notify.telegram import Command, TelegramNotifier
from .settings import CONFIG_DIR, Category, Market, Settings
from .storage.db import build_engine, build_session_factory
from .storage.repo import Repository
from .vinted.brands import BrandRegistry
from .vinted.client import VintedBlocked, VintedClient, VintedError
from .vinted.ratelimit import RateLimiter

log = logging.getLogger(__name__)

SEEN_RETENTION_SECONDS = 7 * 86400
PRUNE_EVERY_CYCLES = 200


class Sniper:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.started_at = time.monotonic()
        self.paused = False
        self.cycle_count = 0
        self.last_cycle_ts = 0
        self.last_error: str | None = None

        polling = settings.polling or {}
        self.limiter = RateLimiter(float(polling.get("min_request_interval", 0.8)))
        self.per_page = int(polling.get("items_per_page", 96))
        self.cycle_seconds = float(polling.get("cycle_seconds", 45))
        self.max_age = int(polling.get("max_item_age_seconds", 3600))
        self.warmup_cycles = int(polling.get("warmup_cycles", 3))

        scoring = settings.scoring or {}
        self.price_book = PriceBook(
            window_size=int(scoring.get("price_window_size", 120)),
            window_seconds=int(scoring.get("price_window_days", 21)) * 86400,
            min_samples=int(scoring.get("min_samples_for_median", 8)),
        )

        self.fx = FxConverter(
            (settings.fx or {}).get("fallback_rates", {}),
            float((settings.fx or {}).get("refresh_hours", 12)),
        )

        engine = build_engine(settings.database_url)
        self.repo = Repository(build_session_factory(engine))

        registry = BrandRegistry.load(CONFIG_DIR / "brand_ids.json")
        if registry is None:
            raise SystemExit(
                "Немає config/brand_ids.json. Запусти: python scripts/resolve_brands.py"
            )
        self.registry = registry

        self.notifier = TelegramNotifier(
            settings.telegram,
            send_photo=bool((settings.alerts or {}).get("send_photo", True)),
            dry_run=settings.dry_run or not settings.telegram.configured,
        )

        self.clients: dict[str, VintedClient] = {}
        self.status_maps: dict[str, StatusMap] = {}
        self.muted: set[int] = set()
        self._alert_times: list[float] = []
        self._telegram_offset = 0
        self._reject_stats: Counter[str] = Counter()

    # ------------------------------------------------------------------ старт

    async def setup(self) -> None:
        accepted = list((self.settings.conditions or {}).get("accepted_ids") or [6, 1, 2, 3])
        buckets = (self.settings.conditions or {}).get("buckets") or {}
        probe_catalog = self.settings.enabled_categories[0].id

        for market in self.settings.enabled_markets:
            client = VintedClient(
                market,
                self.limiter,
                timeout=float((self.settings.polling or {}).get("request_timeout", 20.0)),
                max_retries=int((self.settings.polling or {}).get("max_retries", 3)),
            )
            await client.ensure_session()
            self.clients[market.code] = client

            status_map = StatusMap(market.code, buckets)
            await status_map.resolve(client, accepted, probe_catalog)
            self.status_maps[market.code] = status_map

        await self.fx.refresh()

        since = utc_now_ts() - self.price_book.window_seconds
        rows = await asyncio.to_thread(self.repo.load_observations, since)
        loaded = self.price_book.bulk_load(rows)
        log.info("піднято %s спостережень цін з бази", loaded)

        saved_offset = await asyncio.to_thread(self.repo.get_state, "telegram_offset")
        if saved_offset:
            self._telegram_offset = int(saved_offset)
        self.muted = await asyncio.to_thread(self.repo.muted_brand_ids)

        if self.settings.telegram.configured:
            me = await self.notifier.get_me()
            if me:
                log.info("telegram-бот: @%s", me.get("username"))
            await self.notifier.send_text(
                format_startup(
                    [m.code for m in self.settings.enabled_markets],
                    len(self.settings.enabled_categories),
                    len(self.registry),
                    self.settings.dry_run,
                )
            )
        else:
            log.warning(
                "TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID_TOP не задані, "
                "працюю в режимі логів без відправки"
            )

    async def close(self) -> None:
        for client in self.clients.values():
            await client.close()
        await self.notifier.close()

    # ------------------------------------------------------------- цикл роботи

    async def run_forever(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await self.run_cycle()
                self.last_error = None
            except VintedBlocked as exc:
                self.last_error = str(exc)
                log.warning("Vinted пригальмував нас: %s. Пауза 120с", exc)
                await asyncio.sleep(120)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("цикл впав, продовжую далі")
                await asyncio.sleep(30)

            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1.0, self.cycle_seconds - elapsed))

    async def run_cycle(self) -> None:
        self.cycle_count += 1
        now_ts = utc_now_ts()
        self.last_cycle_ts = now_ts
        warming = self.cycle_count <= self.warmup_cycles

        if self.fx.needs_refresh():
            await self.fx.refresh()

        self.muted = await asyncio.to_thread(self.repo.muted_brand_ids)
        brand_ids = [b for b in self.registry.ids if b not in self.muted]
        accepted = list((self.settings.conditions or {}).get("accepted_ids") or [6, 1, 2, 3])

        observations: list[tuple[int, int, str, float, str, int]] = []
        deals: list[tuple[Deal, int]] = []
        fetched = 0

        for market in self.settings.enabled_markets:
            client = self.clients.get(market.code)
            if client is None:
                continue
            for category in self.settings.enabled_categories:
                try:
                    listings, server_ts = await client.fetch_catalog(
                        catalog_id=category.id,
                        brand_ids=brand_ids,
                        status_ids=accepted,
                        per_page=self.per_page,
                    )
                except (VintedError, httpx.HTTPError) as exc:
                    log.warning("[%s/%s] стрічка не прочиталась: %s", market.code, category.key, exc)
                    continue

                fetched += len(listings)
                self._check_feed_overflow(market, category, listings, server_ts)

                new_ids = await asyncio.to_thread(
                    self.repo.filter_unseen,
                    market.code,
                    [item.item_id for item in listings],
                    server_ts,
                )

                for listing in listings:
                    bucket = self.status_maps[market.code].bucket(listing.status_title)
                    brand = self.registry.by_title(listing.brand_title)

                    # Ціна продавця йде в статистику ринку: саме її ми отримаємо,
                    # коли будемо перепродавати самі.
                    if brand and bucket:
                        asking_eur = self.fx.to_eur(listing.price, listing.currency)
                        self.price_book.record(
                            brand.brand_id, category.id, bucket, asking_eur, server_ts
                        )
                        observations.append(
                            (brand.brand_id, category.id, bucket, asking_eur, market.code, server_ts)
                        )

                    if listing.item_id not in new_ids:
                        continue
                    age = listing.age_seconds
                    if age is not None and age > self.max_age:
                        continue

                    deal = self._assess(listing, market, category, bucket, server_ts)
                    if deal is not None and brand is not None:
                        deals.append((deal, brand.brand_id))

        if observations:
            await asyncio.to_thread(self.repo.add_observations, observations)

        sent = 0
        if not warming:
            for deal, brand_id in sorted(deals, key=lambda d: -d[0].profit_eur):
                if await self._dispatch(deal, brand_id, now_ts):
                    sent += 1
        elif deals:
            log.info("прогрів: %s знахідок не шлю, наповнюю базу цін", len(deals))

        log.info(
            "цикл %s: лотів=%s знахідок=%s відправлено=%s спостережень=%s ключів=%s%s",
            self.cycle_count,
            fetched,
            len(deals),
            sent,
            self.price_book.total_observations,
            self.price_book.tracked_keys,
            " [прогрів]" if warming else "",
        )
        if self._reject_stats and self.cycle_count % 10 == 0:
            log.info("причини відсіву: %s", dict(self._reject_stats.most_common(6)))
            self._reject_stats.clear()

        await self._handle_commands()

        if self.cycle_count % PRUNE_EVERY_CYCLES == 0:
            await self._prune(now_ts)

    # ------------------------------------------------------------------ оцінка

    def _assess(
        self,
        listing: Listing,
        market: Market,
        category: Category,
        bucket: str | None,
        now_ts: int,
    ) -> Deal | None:
        total_eur = self.fx.to_eur(listing.total_price, listing.currency)
        result = screen(
            listing,
            settings=self.settings,
            registry=self.registry,
            category=category,
            price_eur=total_eur,
            bucket=bucket,
        )
        if isinstance(result, Rejected):
            self._reject_stats[result.reason] += 1
            return None
        assert isinstance(result, Candidate)

        return evaluate(
            result,
            settings=self.settings,
            price_book=self.price_book,
            shipping_eur=market.shipping_eur,
            now_ts=now_ts,
        )

    # ------------------------------------------------------------- відправка

    async def _dispatch(self, deal: Deal, brand_id: int, now_ts: int) -> bool:
        if self.paused:
            return False
        if self._in_quiet_hours():
            return False
        if not self._alert_budget_left():
            log.warning("досягнуто ліміт алертів на годину, притримую решту")
            return False

        ok = await self.notifier.send_deal(deal, brand_id=brand_id)
        if ok:
            self._alert_times.append(time.monotonic())
            await asyncio.to_thread(self.repo.log_alert, deal, now_ts)
            log.info(
                "→ %s %s %.2f EUR x%.2f (+%.2f) %s",
                deal.channel.upper(),
                deal.listing.brand_title,
                deal.cost_eur,
                deal.multiple,
                deal.profit_eur,
                deal.listing.url,
            )
        return ok

    def _alert_budget_left(self) -> bool:
        limit = int((self.settings.alerts or {}).get("max_alerts_per_hour", 60))
        if limit <= 0:
            return True
        cutoff = time.monotonic() - 3600
        self._alert_times = [t for t in self._alert_times if t >= cutoff]
        return len(self._alert_times) < limit

    def _in_quiet_hours(self) -> bool:
        windows = (self.settings.alerts or {}).get("quiet_hours") or []
        return in_quiet_hours(windows, self._local_now().hour)

    def _local_now(self) -> datetime:
        name = (self.settings.alerts or {}).get("timezone", "Europe/Kyiv")
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(name))
        except Exception:  # noqa: BLE001
            # Київ це UTC+2 взимку і UTC+3 влітку; без бази таймзон беремо +2
            return datetime.now(timezone(timedelta(hours=2)))

    # ------------------------------------------------------------- діагностика

    def _check_feed_overflow(
        self, market: Market, category: Category, listings: list[Listing], server_ts: int
    ) -> None:
        """Попереджає, якщо сторінка забита свіжаком і ми могли щось прогавити."""
        if len(listings) < self.per_page:
            return
        oldest = listings[-1].uploaded_ts
        if oldest is None:
            return
        span = server_ts - oldest
        if span < self.cycle_seconds * 1.5:
            log.warning(
                "[%s/%s] стрічка переповнена: %s лотів за %sс. Зменш cycle_seconds, "
                "інакше частина лотів проходить повз",
                market.code, category.key, len(listings), span,
            )

    # ---------------------------------------------------------------- команди

    async def _handle_commands(self) -> None:
        if not self.settings.telegram.configured or self.settings.dry_run:
            return
        try:
            new_offset = await self.notifier.poll_commands(
                self._telegram_offset,
                on_command=self._on_command,
                on_callback=self._on_callback,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("не вдалось прочитати команди: %s", exc)
            return
        if new_offset != self._telegram_offset:
            self._telegram_offset = new_offset
            await asyncio.to_thread(self.repo.set_state, "telegram_offset", str(new_offset))

    async def _on_command(self, command: Command) -> str | None:
        name, args = command.name, command.args.strip()

        if name in ("start", "help"):
            return HELP_TEXT

        if name == "stats":
            stats = await asyncio.to_thread(self.repo.stats, utc_now_ts())
            uptime = int(time.monotonic() - self.started_at)
            return format_stats(stats, self.price_book, uptime, self.paused)

        if name == "brands":
            muted = [self.registry.by_id(b) for b in sorted(self.muted)]
            muted_names = ", ".join(m.name for m in muted if m) or "жодного"
            return (
                f"У роботі брендів: <b>{len(self.registry) - len(self.muted)}</b> "
                f"з {len(self.registry)}\nЗаглушені: {muted_names}"
            )

        if name == "pause":
            self.paused = True
            return "⏸ Алерти на паузі. /resume щоб продовжити."

        if name == "resume":
            self.paused = False
            return "▶️ Працюю далі."

        if name in ("mute", "unmute"):
            if not args:
                return f"Вкажи бренд: /{name} Nike"
            brand = self.registry.by_title(args)
            if brand is None:
                return f"Бренд {args!r} не в списку. /brands покаже, що є."
            if name == "mute":
                await asyncio.to_thread(
                    self.repo.mute_brand, brand.brand_id, brand.name, utc_now_ts()
                )
                self.muted.add(brand.brand_id)
                return f"🔕 {brand.name} більше не надсилаю."
            changed = await asyncio.to_thread(self.repo.unmute_brand, brand.brand_id)
            self.muted.discard(brand.brand_id)
            return f"🔔 {brand.name} повернувся." if changed else f"{brand.name} і так не заглушений."

        return None

    async def _on_callback(self, data: str, chat_id: str) -> str | None:
        if not data.startswith("mute:"):
            return None
        try:
            brand_id = int(data.split(":", 1)[1])
        except ValueError:
            return None
        brand = self.registry.by_id(brand_id)
        if brand is None:
            return "Не знаю такого бренду"
        await asyncio.to_thread(self.repo.mute_brand, brand_id, brand.name, utc_now_ts())
        self.muted.add(brand_id)
        return f"{brand.name} заглушено"

    # -------------------------------------------------------------- обслуга

    async def _prune(self, now_ts: int) -> None:
        seen = await asyncio.to_thread(self.repo.prune_seen, now_ts - SEEN_RETENTION_SECONDS)
        obs = await asyncio.to_thread(
            self.repo.prune_observations, now_ts - self.price_book.window_seconds
        )
        if seen or obs:
            log.info("прибирання: -%s переглянутих, -%s спостережень", seen, obs)

    # ---------------------------------------------------------------- health

    def health_status(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.last_error is None else "degraded",
            "paused": self.paused,
            "cycles": self.cycle_count,
            "uptime_seconds": int(time.monotonic() - self.started_at),
            "last_cycle_ts": self.last_cycle_ts,
            "markets": [m.code for m in self.settings.enabled_markets],
            "brands": len(self.registry),
            "muted_brands": len(self.muted),
            "observations": self.price_book.total_observations,
            "tracked_keys": self.price_book.tracked_keys,
            "fx_live": self.fx.is_live,
            "rate_penalty": round(self.limiter.penalty, 2),
            "last_error": self.last_error,
        }


async def main(settings: Settings) -> None:
    sniper = Sniper(settings)
    health = HealthServer(settings.port, sniper.health_status)
    await health.start()
    try:
        await sniper.setup()
        await sniper.run_forever()
    finally:
        await health.stop()
        await sniper.close()
