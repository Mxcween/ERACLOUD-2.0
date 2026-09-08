"""Головний цикл снайпера.

На кожному оберті ми проходимо всі пари ринок+категорія, беремо найсвіжіші
лоти по наших брендах, поповнюємо статистику цін і відправляємо те, що
пройшло пороги вигоди.
"""
from __future__ import annotations

import asyncio
import logging
import os
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
from .engine.vision import PhotoJudge
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

try:  # студія необов'язкова: без неї снайпер працює як раніше
    from studio.bot import StudioBot
    from studio.settings import load_studio_settings
except ImportError:  # pragma: no cover
    StudioBot = None  # type: ignore[assignment]

    def load_studio_settings():  # type: ignore[misc]
        class _Off:
            bot_token = ""
        return _Off()

log = logging.getLogger(__name__)

SEEN_RETENTION_SECONDS = 7 * 86400
PRUNE_EVERY_CYCLES = 80
# Скільки знахідок тримаємо, поки чат невідомий
MAX_PENDING_ALERTS = 25
# Скільки знахідок може чекати на відправку. Черга потрібна, бо перевірка
# фото інколи думає 20 секунд, і тримати через це сканування не можна.
MAX_OUTBOX = 60
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
        # По лімітеру на ринок: різні хости, різні лічильники
        self._request_interval = float(polling.get("min_request_interval", 0.8))
        self.limiters: dict[str, RateLimiter] = {}
        self.per_page = int(polling.get("items_per_page", 96))
        self.cycle_seconds = float(polling.get("cycle_seconds", 45))
        self.max_age = int(polling.get("max_item_age_seconds", 3600))
        self.warmup_cycles = int(polling.get("warmup_cycles", 3))
        # Глибокий прохід по дешевому хвосту: кожні N циклів, по стільки сторінок
        self.deep_every = int(polling.get("deep_scan_every_cycles", 20))
        self.deep_pages = max(1, int(polling.get("deep_scan_pages", 2)))

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
        # Зір: та сама модель, що й у студії, але тут вона тільки дивиться і
        # відповідає текстом - на безкоштовному тарифі це доступно.
        vision_cfg = (settings.scoring or {}).get("vision") or {}
        self.judge = PhotoJudge(
            os.getenv("GEMINI_API_KEY", "").strip(),
            model=str(vision_cfg.get("model", "gemini-2.5-flash")),
            min_real=int(vision_cfg.get("min_real", 5)),
            min_condition=int(vision_cfg.get("min_condition", 4)),
            min_photo=int(vision_cfg.get("min_photo", 4)),
            min_interval=float(vision_cfg.get("min_interval_seconds", 4.0)),
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
        self._pending: list[tuple[Deal, int, float]] = []
        self._telegram_offset = 0
        # Щоб пояснення про чергу пролунало один раз, а не щоцикла
        self._first_flush = True
        # Полиця цін, у якій власник зараз хоче бачити алерти в Telegram.
        # Discord це не чіпає: там канали розкладені по ціні самі.
        self.alert_range = PriceRange.open()
        self._reject_stats: Counter[str] = Counter()
        # Лічильники для /health: без них не видно, чи бот мовчить тому, що
        # нічого не знаходить, чи тому, що нема куди слати
        self._deals_total = 0
        self._alerts_total = 0
        self._vision_rejects = 0
        # Пошук і доставка розведені: цикл тільки складає знахідки сюди,
        # а окремий робітник шле їх у своєму темпі.
        self._outbox: asyncio.Queue[tuple[Deal, int]] = asyncio.Queue()

    # ------------------------------------------------------------------ старт

    async def setup(self) -> None:
        accepted = self.settings.accepted_status_ids()
        buckets = (self.settings.conditions or {}).get("buckets") or {}
        probe_catalog = self.settings.enabled_categories[0].id

        for market in self.settings.enabled_markets:
            client = VintedClient(
                market,
                self.limiters.setdefault(
                    market.code, RateLimiter(self._request_interval)
                ),
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

        if not self.notifier.has_target:
            # База на безкоштовному Render не переживає редеплой разом із
            # запам'ятованим чатом. Спробуємо дістати його з черги оновлень,
            # щоб не мовчати, поки власник не напише ще раз.
            if await self.notifier.discover_chat(self._telegram_offset):
                await asyncio.to_thread(
                    self.repo.set_state, "chat_id_top", self.notifier.chat_top
                )

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
                "Чат ще не відомий, АЛЕРТИ НІКУДИ НЕ ЙДУТЬ. Напиши боту%s "
                "команду /start. Щоб не повторювати це після кожного деплою, "
                "задай TELEGRAM_CHAT_ID_TOP у змінних оточення.",
                f" @{me.get('username')}" if me else "",
            )

    async def close(self) -> None:
        await self.judge.close()
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

        async def sweep(market: Market) -> tuple[int, int]:
            seen = fresh = 0
            for category in self.settings.enabled_categories:
                s, f = await self._scan(
                    market, category, brand_ids,
                    observations=observations, deals=deals,
                    backlog_mode=backlog_mode,
                )
                seen += s
                fresh += f
            return seen, fresh

        for seen, fresh in await asyncio.gather(
            *(sweep(m) for m in self.settings.enabled_markets)
        ):
            fetched += seen
            fresh_count += fresh

        # Раз на N циклів проходимось по дешевому хвосту: лот, який висить
        # кілька годин, зі стрічки новинок давно випав, але з сортування за
        # ціною нікуди не дівається.
        if self.deep_every and self.cycle_count % self.deep_every == 0:
            before = len(deals)
            deep_seen, deep_fresh = await self._deep_scan(brand_ids, observations, deals)
            fetched += deep_seen
            fresh_count += deep_fresh
            # Лишаємо тільки найжирніші: перший прохід інакше вивалює сотню
            # дрібних лотів, серед яких губиться те, заради чого все затівалось.
            limit = int((self.settings.scoring or {}).get("deep_max_alerts", 6))
            found = deals[before:]
            if len(found) > limit:
                found.sort(key=lambda d: -d[0].profit_eur)
                del deals[before:]
                deals.extend(found[:limit])
                log.info("глибокий прохід: %s знахідок, лишив %s найжирніших",
                         len(found), limit)

        if observations:
            await asyncio.to_thread(self.repo.add_observations, observations)

        queued = 0
        if not warming:
            for deal, brand_id in sorted(deals, key=lambda d: -d[0].profit_eur):
                if self._outbox.qsize() >= MAX_OUTBOX:
                    log.warning("черга відправки повна, найдрібніші знахідки не влізли")
                    break
                self._outbox.put_nowait((deal, brand_id))
                queued += 1
        elif deals:
            log.info("прогрів: %s знахідок не шлю, наповнюю базу цін", len(deals))
        self._deals_total += len(deals)

        # Розмір книги цін навмисне НЕ логуємо як показник роботи: вікно на
        # 120 записів по ключу насичується, і популярні бренди перестають
        # збільшувати лічильник, хоч нові лоти й далі надходять.
        log.info(
            "цикл %s: переглянуто=%s нових=%s знахідок=%s у черзі=%s "
            "у базі цін=%s по %s ключах%s",
            self.cycle_count,
            fetched,
            fresh_count,
            len(deals),
            self._outbox.qsize(),
            self.price_book.total_observations,
            self.price_book.tracked_keys,
            " [прогрів]" if warming else "",
        )
        if self._reject_stats and self.cycle_count % 10 == 0:
            log.info("причини відсіву: %s", dict(self._reject_stats.most_common(6)))
            self._reject_stats.clear()

        if self.cycle_count % PRUNE_EVERY_CYCLES == 0:
            await self._prune(now_ts)

    async def _scan(
        self,
        market: Market,
        category: Category,
        brand_ids: list[int],
        *,
        observations: list,
        deals: list,
        backlog_mode: bool,
        order: str = "newest_first",
        page: int = 1,
        deep: bool = False,
    ) -> tuple[int, int]:
        """Один запит до однієї пари ринок+категорія. Повертає (взято, нових)."""
        client = self.clients.get(market.code)
        if client is None:
            return 0, 0
        try:
            listings, server_ts = await client.fetch_catalog(
                catalog_id=category.id,
                brand_ids=brand_ids,
                # Стани звужуємо вже на боці Vinted: у взутті "добре"
                # означає затерту підошву, і такі лоти краще не тягнути
                # взагалі, ніж фільтрувати їх у себе.
                status_ids=self.settings.accepted_status_ids(category.key),
                per_page=self.per_page,
                page=page,
                order=order,
            )
        except (VintedError, httpx.HTTPError) as exc:
            log.warning("[%s/%s] стрічка не прочиталась: %s", market.code, category.key, exc)
            return 0, 0

        if not deep:
            self._check_feed_overflow(market, category, listings, server_ts)

        new_ids = await asyncio.to_thread(
            self.repo.filter_unseen,
            market.code,
            [item.item_id for item in listings],
            server_ts,
        )

        fresh = 0
        for listing in listings:
            # Кожен лот враховуємо в статистиці рівно один раз, при першій
            # зустрічі. Інакше річ, яку ніхто не купує і яка тижнями висить
            # у стрічці, потрапляла б у медіану сотні разів і завищувала
            # оцінку продажу. Заразом це тримає базу в розумних розмірах.
            if listing.item_id not in new_ids:
                continue
            fresh += 1

            bucket = self.status_maps[market.code].bucket(listing.status_title)
            brand = self.registry.by_title(listing.brand_title)

            # Ціна продавця йде в статистику ринку: саме її ми отримаємо,
            # коли будемо перепродавати самі.
            if brand and bucket:
                asking_eur = self.fx.to_eur(listing.price, listing.currency)
                # Перевіряємо ДО запису: якщо вікно по цьому ключу вже повне,
                # у пам'яті ми найстаріше витіснимо, а в базу писати не варто.
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
            # після простою. У глибокому проході вік не фільтруємо взагалі -
            # ми туди саме за старими лотами й ходимо.
            if backlog_mode and not deep:
                age = listing.age_seconds
                if age is not None and age > self.max_age:
                    continue

            deal = self._assess(listing, market, category, bucket, server_ts, deep=deep)
            if deal is not None and brand is not None:
                deals.append((deal, brand.brand_id))

        return len(listings), fresh

    async def _deep_scan(
        self, brand_ids: list[int], observations: list, deals: list
    ) -> tuple[int, int]:
        """Дешевий хвіст: те, що висить годинами і зі стрічки новинок випало."""
        async def sweep(market: Market) -> tuple[int, int]:
            seen = fresh = 0
            for category in self.settings.enabled_categories:
                for page in range(1, self.deep_pages + 1):
                    s, f = await self._scan(
                        market, category, brand_ids,
                        observations=observations, deals=deals,
                        backlog_mode=False,
                        order="price_low_to_high",
                        page=page,
                        deep=True,
                    )
                    seen += s
                    fresh += f
            return seen, fresh

        seen = fresh = 0
        for s_, f_ in await asyncio.gather(
            *(sweep(m) for m in self.settings.enabled_markets)
        ):
            seen += s_
            fresh += f_
        log.info("глибокий прохід: переглянуто=%s нових=%s", seen, fresh)
        return seen, fresh

    # ------------------------------------------------------------------ оцінка

    def _assess(
        self,
        listing: Listing,
        market: Market,
        category: Category,
        bucket: str | None,
        now_ts: int,
        deep: bool = False,
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

        deal = evaluate(
            result,
            settings=self.settings,
            price_book=self.price_book,
            shipping_eur=market.shipping_eur,
            now_ts=now_ts,
        )
        if deal is not None and deep:
            if not self._deep_gate(deal):
                return None
            deal.notes.append(self._deep_note(listing))
        return deal

    def _deep_gate(self, deal: Deal) -> bool:
        """Окремий, суворіший поріг для залежалих лотів.

        Свіжий лот дешевий тому, що його ще ніхто не бачив - це наша
        перевага. Залежалий дешевий тому, що його бачили всі й не взяли, а
        чому саме, ми не знаємо. За цю невідомість беремо надбавку.
        """
        scoring = self.settings.scoring or {}
        min_profit = float(scoring.get("deep_min_profit_eur", 25.0))
        min_multiple = float(scoring.get("deep_min_multiple", 3.0))
        return deal.profit_eur >= min_profit and deal.multiple >= min_multiple

    def _deep_note(self, listing: Listing) -> str:
        """Підпис для знахідки з дешевого хвоста.

        Її бачили всі, хто заходив у ці години, і ніхто не взяв. Причина
        буває поважна, тому видавати такий лот за свіжак не можна.
        """
        age = listing.age_seconds
        hours = f"{age // 3600} год" if age and age >= 3600 else "невідомо скільки"
        return f"⏳ висить {hours}, зі стрічки новинок уже випало"

    # ------------------------------------------------------------- відправка

    async def _dispatch(self, deal: Deal, brand_id: int, now_ts: int) -> bool:
        if self.paused:
            return False
        if self._in_quiet_hours():
            return False
        if not self._alert_budget_left():
            log.warning("досягнуто ліміт алертів на годину, притримую решту")
            return False
        if not self._burst_budget_left():
            # Не викидаємо: лот стає в чергу і піде наступним циклом. Так
            # знахідки приходять рівним струмком, а не стосом, у якому
            # найкраще губиться серед посереднього.
            if len(self._pending) < MAX_PENDING_ALERTS:
                self._pending.append((deal, brand_id, time.monotonic()))
            return False
        if not self._seller_budget_left(deal.listing.seller_id):
            log.info(
                "продавець %s уже в стрічці цієї години, пропускаю %s",
                deal.listing.seller_id, deal.listing.url,
            )
            return False

        # Фото дивимось в останню чергу: тільки для лотів, які реально
        # зараз підуть. Так запитів на хвилину виходить рівно стільки,
        # скільки алертів, і безкоштовна квота не тріщить.
        verdict = await self.judge.judge(
            deal.listing.photo_url,
            brand=deal.listing.brand_title,
            title=deal.listing.title,
            category=deal.category_name,
            condition=deal.listing.status_title,
            price_eur=deal.price_eur,
        )
        if not verdict.ok:
            self._vision_rejects += 1
            log.info("зір відсіяв: %s | %s", verdict.reason, deal.listing.url)
            return False
        if verdict.note:
            deal.notes.append(("👁 " if verdict.checked else "👁? ") + verdict.note)
        elif not verdict.checked and self.judge.configured:
            deal.notes.append("👁? фото не перевірено")

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
            self._pending.append((deal, brand_id, time.monotonic()))

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

    async def deliver_forever(self) -> None:
        """Окремий робітник доставки.

        Перевірка фото інколи думає двадцять секунд. Поки вона була
        всередині циклу, кожна така пауза відсувала наступне сканування, і
        бот пропускав свіжі лоти саме тоді, коли працював найстаранніше.
        Тепер цикл лише складає знахідки в чергу, а звідси вони йдуть у
        своєму темпі.
        """
        while True:
            deal, brand_id = await self._outbox.get()
            try:
                await self._dispatch(deal, brand_id, utc_now_ts())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("не вдалось відправити знахідку, беру наступну")
            finally:
                self._outbox.task_done()

    async def _flush_pending(self, now_ts: int) -> None:
        """Досилає знахідки, які чекали, поки з'ясується чат.

        Не залпом. Поки чат був невідомий, могло назбиратись два десятки
        лотів, і вивалити їх одним стосом означає поховати найкращий серед
        решти. Тому: спершу викидаємо протухле (лот, знайдений годину тому,
        уже або куплений, або нікому не потрібен), далі шлемо найжирніші
        і не більше кількох за раз, решта чекає наступного циклу.
        """
        if not self._pending or not self.notifier.has_target:
            return

        alerts = self.settings.alerts or {}
        stale_after = float(alerts.get("pending_stale_seconds", 1800))
        per_flush = max(1, int(alerts.get("flush_batch", 5)))

        now = time.monotonic()
        alive = [row for row in self._pending if now - row[2] <= stale_after]
        dropped = len(self._pending) - len(alive)
        alive.sort(key=lambda row: -row[0].profit_eur)

        batch, self._pending = alive[:per_flush], alive[per_flush:]
        if dropped or self._pending:
            log.info(
                "черга: шлю %s, чекають %s, викинув протухлих %s",
                len(batch), len(self._pending), dropped,
            )
        if self._first_flush and (dropped or self._pending):
            self._first_flush = False
            await self.notifier.send_text(
                f"Поки чат був невідомий, назбиралось знахідок: "
                f"<b>{len(batch) + len(self._pending) + dropped}</b>.\n"
                f"Шлю найжирніші по {per_flush} за раз"
                + (f", {dropped} уже протухли й пропускаю" if dropped else "")
                + "."
            )

        for deal, brand_id, _ in batch:
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

    def _burst_budget_left(self) -> bool:
        """Скільки алертів пускаємо за хвилину. Захист від залпу."""
        limit = int((self.settings.alerts or {}).get("max_alerts_per_minute", 5))
        if limit <= 0:
            return True
        cutoff = time.monotonic() - 60
        return len([t for t in self._alert_times if t >= cutoff]) < limit

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
                "✅ Готово, цей чат тепер отримує алерти.\n\n"
                + self._pin_chat_hint(command.chat_id)
                + "\n\n"
                + HELP_TEXT
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

    def _pin_chat_hint(self, chat_id: str) -> str:
        """Як зробити, щоб чат не губився після кожного редеплою.

        На безкоштовному Render немає диска: база лежить у памʼяті контейнера
        і зникає з кожним новим деплоєм, а разом з нею і запамʼятований чат.
        Змінна оточення переживає все, тому просимо власника вписати її раз.
        """
        if self.settings.telegram.chat_id_top:
            return ""
        return (
            f"📌 Щоб я не забував цей чат після кожного оновлення, встав у "
            f"Render → Environment:\n<code>TELEGRAM_CHAT_ID_TOP={chat_id}</code>\n"
            "Один раз - і більше /start не треба."
        )

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
            "outbox": self._outbox.qsize(),
            "vision": {
                "on": self.judge.configured,
                "checked": self.judge.checked,
                "rejected": self.judge.rejected,
                "failed": self.judge.failed,
            },
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
            "rate_penalty": {
                code: round(lim.penalty, 2) for code, lim in self.limiters.items()
            },
            "last_error": self.last_error,
        }


async def main(settings: Settings) -> None:
    sniper = Sniper(settings)
    health = HealthServer(settings.port, sniper.health_status)
    await health.start()

    # Студійний бот - окремий продукт з власним токеном, але живе в цьому ж
    # процесі: на безкоштовному Render один сервіс, один пінгер, нуль грошей.
    studio_settings = load_studio_settings()
    studio = (
        StudioBot(
            studio_settings,
            get_state=sniper.repo.get_state,
            set_state=sniper.repo.set_state,
        )
        if studio_settings.bot_token
        else None
    )

    tasks: list[asyncio.Task[None]] = []
    try:
        await sniper.setup()
        tasks.append(asyncio.create_task(sniper.listen_commands()))
        tasks.append(asyncio.create_task(sniper.deliver_forever()))
        if studio is not None:
            tasks.append(asyncio.create_task(studio.run_forever()))
        await sniper.run_forever()
    finally:
        for task in tasks:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if studio is not None:
            await studio.close()
        await health.stop()
        await sniper.close()
