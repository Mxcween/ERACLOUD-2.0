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
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .engine.conditions import StatusMap
from .engine.filters import Candidate, Rejected, screen
from .engine.fx import FxConverter
from .engine.pricing import PriceBook
from .engine.ranges import PriceRange, suggestions
from .engine.schedule import in_quiet_hours
from .engine.scoring import evaluate
from .health import HealthServer
from .models import Deal, Listing, utc_now_ts
from .notify.formatting import HELP_TEXT, format_startup, format_stats
from .notify.discord import DiscordNotifier
from .notify.telegram import Command, TelegramNotifier
from .settings import CONFIG_DIR, Category, Market, Settings
from .storage.db import build_engine, build_session_factory
from .storage.repo import Repository
from .vinted.brands import BrandRegistry
from .vinted.client import VintedBlocked, VintedClient, VintedError
from .vinted.ratelimit import RateLimiter

log = logging.getLogger(__name__)

SEEN_RETENTION_SECONDS = 7 * 86400
PRUNE_EVERY_CYCLES = 80
# Скільки знахідок тримаємо, поки чат невідомий
MAX_PENDING_ALERTS = 25
# Скільки секунд Telegram тримає getUpdates відкритим, чекаючи на команду.
# Слухач живе окремо від циклу, тому відповідь приходить одразу.
COMMAND_LONG_POLL_SECONDS = 25


class Sniper:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.started_at = time.monotonic()
        self.paused = False
        self.cycle_count = 0
        self.last_cycle_ts = 0
        self._last_cycle_monotonic: float | None = None
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
        self.discord = DiscordNotifier(
            settings.discord.webhooks,
            settings.discord.bounds,
            dry_run=settings.dry_run,
        )

        self.clients: dict[str, VintedClient] = {}
        self.status_maps: dict[str, StatusMap] = {}
        self.muted: set[int] = set()
        self._alert_times: list[float] = []
        # Коли який продавець востаннє потрапляв у стрічку. Приманки йдуть
        # пачками з одного акаунта: пʼять однакових пар у різних розмірах
        # за пару хвилин. Одного-двох на годину досить, решта це спам.
        self._seller_alerts: dict[int, list[float]] = {}
        # Знахідки, які трапились до того, як став відомий чат. Тримаємо їх,
        # а не викидаємо: лот уже позначений переглянутим і вдруге не спливе.
        self._pending: list[tuple[Deal, int]] = []
        self._telegram_offset = 0
        # Полиця цін, у якій власник зараз хоче бачити алерти в Telegram.
        # Discord це не чіпає: там канали розкладені по ціні самі.
        self.alert_range = PriceRange.open()
        self._reject_stats: Counter[str] = Counter()
        # Лічильники для /health: без них не видно, чи бот мовчить тому, що
        # нічого не знаходить, чи тому, що нема куди слати
        self._deals_total = 0
        self._alerts_total = 0

    # ------------------------------------------------------------------ старт

    async def setup(self) -> None:
        accepted = self.settings.accepted_status_ids()
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

        saved_range = await asyncio.to_thread(self.repo.get_state, "alert_range")
        if saved_range:
            parsed = PriceRange.parse(saved_range)
            if parsed is not None:
                self.alert_range = parsed
                if not parsed.is_open:
                    log.info("діапазон алертів: %s EUR", parsed.label)

        if not self.settings.telegram.configured:
            log.warning(
                "TELEGRAM_BOT_TOKEN не заданий, працюю в режимі логів без відправки"
            )
            return

        if not self.notifier.has_target:
            saved = await asyncio.to_thread(self.repo.get_state, "chat_id_top")
            if saved:
                self.notifier.adopt_chat(saved)

        me = await self.notifier.get_me()
        if me:
            log.info("telegram-бот: @%s", me.get("username"))

        if self.notifier.has_target:
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
                "Чат ще не відомий. Напиши боту%s у Telegram команду /start, "
                "і він запам'ятає цей чат для алертів.",
                f" @{me.get('username')}" if me else "",
            )

    async def close(self) -> None:
        for client in self.clients.values():
            await client.close()
        await self.notifier.close()
        await self.discord.close()

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

        # Чи був розрив у роботі. На старті, після падіння або після того як
        # Render приспав безкоштовний інстанс, уся стрічка виглядає новою, і без
        # запобіжника бот вивалив би добу історії одним залпом.
        gap = (
            float("inf")
            if self._last_cycle_monotonic is None
            else time.monotonic() - self._last_cycle_monotonic
        )
        backlog_mode = gap > max(3 * self.cycle_seconds, 180.0)
        self._last_cycle_monotonic = time.monotonic()
        if backlog_mode and self.cycle_count > 1:
            log.info(
                "розрив у роботі %.0f хв, цей цикл беру тільки свіжі лоти", gap / 60
            )

        if self.fx.needs_refresh():
            await self.fx.refresh()

        # Команди слухає окрема задача (listen_commands), тут лише досилаємо
        # те, що чекало на чат.
        await self._flush_pending(now_ts)

        self.muted = await asyncio.to_thread(self.repo.muted_brand_ids)
        brand_ids = [b for b in self.registry.ids if b not in self.muted]

        observations: list[tuple[int, int, str, float, str, int]] = []
        deals: list[tuple[Deal, int]] = []
        fetched = 0
        fresh_count = 0

        for market in self.settings.enabled_markets:
            client = self.clients.get(market.code)
            if client is None:
                continue
            for category in self.settings.enabled_categories:
                try:
                    listings, server_ts = await client.fetch_catalog(
                        catalog_id=category.id,
                        brand_ids=brand_ids,
                        # Стани звужуємо вже на боці Vinted: у взутті "добре"
                        # означає затерту підошву, і такі лоти краще не тягнути
                        # взагалі, ніж фільтрувати їх у себе.
                        status_ids=self.settings.accepted_status_ids(category.key),
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
                    # Кожен лот враховуємо в статистиці рівно один раз, при першій
                    # зустрічі. Інакше річ, яку ніхто не купує і яка тижнями висить
                    # у стрічці, потрапляла б у медіану сотні разів і завищувала
                    # оцінку продажу. Заразом це тримає базу в розумних розмірах.
                    if listing.item_id not in new_ids:
                        continue
                    fresh_count += 1

                    bucket = self.status_maps[market.code].bucket(listing.status_title)
                    brand = self.registry.by_title(listing.brand_title)

                    # Ціна продавця йде в статистику ринку: саме її ми отримаємо,
                    # коли будемо перепродавати самі.
                    if brand and bucket:
                        asking_eur = self.fx.to_eur(listing.price, listing.currency)
                        # Перевіряємо ДО запису: якщо вікно по цьому ключу вже повне,
                        # у пам'яті ми найстаріше витіснимо, а в базу писати не варто.
                        # Так база тримається в межах ключі * window_size замість
                        # того, щоб рости нескінченно.
                        persist = self.price_book.has_capacity(
                            brand.brand_id, category.id, bucket, server_ts
                        )
                        self.price_book.record(
                            brand.brand_id, category.id, bucket, asking_eur, server_ts
                        )
                        if persist:
                            observations.append(
                                (brand.brand_id, category.id, bucket, asking_eur,
                                 market.code, server_ts)
                            )

                    # Вік беремо з таймстемпа фото, а він бреше для перевиставлених
                    # речей: фото старе, а оголошення щойно опубліковане. Тому в
                    # звичайній роботі покладаємось на дедуплікацію (не бачили =
                    # нове), а вік застосовуємо тільки щоб не вивалити backlog
                    # після простою.
                    if backlog_mode:
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
        self._deals_total += len(deals)

        # Розмір книги цін навмисне НЕ логуємо як показник роботи: вікно на
        # 120 записів по ключу насичується, і популярні бренди перестають
        # збільшувати лічильник, хоч нові лоти й далі надходять.
        log.info(
            "цикл %s: переглянуто=%s нових=%s знахідок=%s відправлено=%s "
            "у базі цін=%s по %s ключах%s",
            self.cycle_count,
            fetched,
            fresh_count,
            len(deals),
            sent,
            self.price_book.total_observations,
            self.price_book.tracked_keys,
            " [прогрів]" if warming else "",
        )
        if self._reject_stats and self.cycle_count % 10 == 0:
            log.info("причини відсіву: %s", dict(self._reject_stats.most_common(6)))
            self._reject_stats.clear()

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
        if not self._seller_budget_left(deal.listing.seller_id):
            log.info(
                "продавець %s уже в стрічці цієї години, пропускаю %s",
                deal.listing.seller_id, deal.listing.url,
            )
            return False

        # Telegram і Discord незалежні. Якщо чат Telegram ще невідомий, а токен
        # заданий, лот чекає в черзі (_flush_pending); Discord тим часом працює
        # своїм маршрутом за ціною і на це чекання не зважає.
        sent_telegram = False
        in_range = self.alert_range.contains(deal.price_eur)
        if not in_range:
            log.debug(
                "%s поза діапазоном %s, у Telegram не шлю",
                deal.listing.url, self.alert_range.label,
            )
        elif self.settings.dry_run or self.notifier.has_target:
            sent_telegram = await self.notifier.send_deal(deal, brand_id=brand_id)
        elif self.settings.telegram.configured and len(self._pending) < MAX_PENDING_ALERTS:
            self._pending.append((deal, brand_id))

        sent_discord = await self.discord.send_deal(deal) if self.discord.configured else False

        ok = sent_telegram or sent_discord
        if ok:
            self._alert_times.append(time.monotonic())
            self._remember_seller(deal.listing.seller_id)
            self._alerts_total += 1
            await asyncio.to_thread(self.repo.log_alert, deal, now_ts)
            via = []
            if sent_telegram:
                via.append("tg")
            if sent_discord:
                via.append(f"discord#{self.discord.tier_index(deal.price_eur)}")
            log.info(
                "→ %s %s %.2f EUR x%.2f (+%.2f) [%s] %s",
                deal.channel.upper(),
                deal.listing.brand_title,
                deal.cost_eur,
                deal.multiple,
                deal.profit_eur,
                "+".join(via),
                deal.listing.url,
            )
        return ok

    async def _flush_pending(self, now_ts: int) -> None:
        """Досилає знахідки, які чекали, поки з'ясується чат."""
        if not self._pending or not self.notifier.has_target:
            return
        queued, self._pending = self._pending, []
        log.info("чат зʼявився, досилаю %s відкладених знахідок", len(queued))
        for deal, brand_id in sorted(queued, key=lambda d: -d[0].profit_eur):
            await self._dispatch(deal, brand_id, now_ts)

    def _seller_budget_left(self, seller_id: int | None) -> bool:
        """Скільки лотів від одного продавця пускаємо за годину."""
        limit = int((self.settings.alerts or {}).get("max_alerts_per_seller_per_hour", 0) or 0)
        if limit <= 0 or seller_id is None:
            return True
        cutoff = time.monotonic() - 3600
        seen = [t for t in self._seller_alerts.get(seller_id, []) if t >= cutoff]
        self._seller_alerts[seller_id] = seen
        return len(seen) < limit

    def _remember_seller(self, seller_id: int | None) -> None:
        if seller_id is None:
            return
        self._seller_alerts.setdefault(seller_id, []).append(time.monotonic())
        # Не даємо словнику рости нескінченно
        if len(self._seller_alerts) > 5000:
            cutoff = time.monotonic() - 3600
            self._seller_alerts = {
                k: [t for t in v if t >= cutoff]
                for k, v in self._seller_alerts.items()
                if any(t >= cutoff for t in v)
            }

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

    async def listen_commands(self) -> None:
        """Окремий слухач команд.

        Раніше команди читались раз на цикл, і відповіді доводилось чекати до
        хвилини. Тепер з'єднання висить на Telegram і реакція миттєва, а цикл
        цим взагалі не займається.
        """
        if not self.settings.telegram.configured or self.settings.dry_run:
            log.info("слухач команд не потрібен: Telegram не налаштований")
            return
        log.info("слухаю команди в Telegram")
        while True:
            started = time.monotonic()
            try:
                await self._handle_commands(long_poll=COMMAND_LONG_POLL_SECONDS)
                await self._flush_pending(utc_now_ts())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("слухач команд спіткнувся, продовжую")
            # Якщо Telegram відмовляє миттєво (наприклад, битий токен), не
            # довбимо його в порожньому циклі
            idle = 2.0 - (time.monotonic() - started)
            if idle > 0:
                await asyncio.sleep(idle)

    async def _handle_commands(self, *, long_poll: int = 0) -> None:
        if not self.settings.telegram.configured or self.settings.dry_run:
            return
        try:  # noqa: SIM105 - обробка нижче
            new_offset = await self.notifier.poll_commands(
                self._telegram_offset,
                on_command=self._on_command,
                on_callback=self._on_callback,
                long_poll=long_poll,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("не вдалось прочитати команди: %s", exc)
            return
        if new_offset != self._telegram_offset:
            self._telegram_offset = new_offset
            await asyncio.to_thread(self.repo.set_state, "telegram_offset", str(new_offset))

    async def _on_command(self, command: Command) -> str | None:
        name, args = command.name, command.args.strip()

        if self.notifier.adopt_chat(command.chat_id):
            await asyncio.to_thread(self.repo.set_state, "chat_id_top", command.chat_id)
            return (
                "✅ Готово, цей чат тепер отримує алерти.\n\n" + HELP_TEXT
            )

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

        if name == "range":
            return await self._set_range(args)

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

    async def _set_range(self, args: str) -> str:
        """Ціновий фільтр на алерти в Telegram, який власник крутить на ходу."""
        options = " | ".join(suggestions(self.settings.discord.bounds))
        if not args:
            current = (
                "весь діапазон"
                if self.alert_range.is_open
                else f"<b>{self.alert_range.label}</b> EUR"
            )
            return (
                f"Зараз шлю: {current}\n\n"
                f"Змінити: <code>/range {options.replace(' | ', '</code> | <code>/range ')}</code>\n"
                "Або свій: <code>/range 20-35</code>, <code>/range до 25</code>, "
                "<code>/range від 60</code>\n"
                "<code>/range all</code> - зняти обмеження"
            )

        parsed = PriceRange.parse(args)
        if parsed is None:
            return (
                f"Не зрозумів {args!r}. Приклади: <code>/range 0-15</code>, "
                f"<code>/range 45+</code>, <code>/range all</code>.\n"
                f"Готові варіанти: {options}"
            )

        self.alert_range = parsed
        await asyncio.to_thread(self.repo.set_state, "alert_range", parsed.label)
        if parsed.is_open:
            return "🎯 Знято обмеження по ціні, шлю всі знахідки."
        return (
            f"🎯 Тепер у Telegram тільки лоти <b>{parsed.label}</b> EUR "
            "(ціна лота, без доставки).\nПороги вигоди не змінились, "
            "Discord так само розкладає все по своїх каналах."
        )

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
            "deals_found": self._deals_total,
            "alerts_sent": self._alerts_total,
            "telegram": {
                "configured": self.settings.telegram.configured,
                # Головна причина мовчання: бот не знає, у який чат слати
                "has_target": self.notifier.has_target,
                "pending": len(self._pending),
                "range": self.alert_range.label,
            },
            "discord": {
                "configured": self.discord.configured,
                "channels": len(self.settings.discord.bounds) + 1,
            },
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
    listener: asyncio.Task[None] | None = None
    try:
        await sniper.setup()
        listener = asyncio.create_task(sniper.listen_commands())
        await sniper.run_forever()
    finally:
        if listener is not None:
            listener.cancel()
            with suppress(asyncio.CancelledError):
                await listener
        await health.stop()
        await sniper.close()
