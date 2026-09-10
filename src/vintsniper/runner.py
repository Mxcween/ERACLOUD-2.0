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
from .engine.fat import FatGate
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
# Скільки ще даємо проходу поверх його власного очікування, перш ніж
# визнати, що він завис. Сам getUpdates уже має свій таймаут, тож запас
# потрібен лише на відповідь і на запис у базу.
COMMAND_PASS_GRACE = 45.0
# Нижня межа паузи між проходами. Захист від гарячого циклу, коли прохід
# падає миттєво: без неї кожен оберт писав би трейсбек, і бот заклинило б.
COMMAND_MIN_IDLE = 0.05


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
        # Скільки категорій беремо за цикл (0 = всі). Див. _category_slice.
        self._cats_per_cycle = int(polling.get("categories_per_cycle", 0))
        self._cat_cursor = 0
        # Стеля на весь процес, бо Vinted рахує по IP, а не по хосту.
        # Без неї два ринки разом видавали вдвічі більшу частоту, ніж
        # показував конфіг, і 429 починався ще на піднятті сесії.
        self._ip_limiter = RateLimiter(
            float(polling.get("ip_request_interval", self._request_interval)),
            jitter=0.25,
        )
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
        # Планка жиру: рахується з самого потоку знахідок, а не задана числом
        fat_cfg = (settings.scoring or {}).get("fat") or {}
        self.fat = FatGate(
            percentile=float(fat_cfg.get("percentile", 80.0)),
            window=int(fat_cfg.get("window", 200)),
            floor_eur=float(fat_cfg.get("floor_eur", 20.0)),
            warmup_samples=int(fat_cfg.get("warmup_samples", 40)),
        )
        self.fat_max_per_cycle = int(fat_cfg.get("max_per_cycle", 3))

        self.discord = DiscordNotifier(
            settings.discord.webhooks,
            settings.discord.bounds,
            dry_run=settings.dry_run,
        )

        self.clients: dict[str, VintedClient] = {}
        self.status_maps: dict[str, StatusMap] = {}
        self.muted: set[int] = set()
        # Живий слухач команд видно тільки зсередини: зовні бот, який не
        # відповідає, не відрізняється від бота, у якого немає команд.
        self._commands_polled = 0
        self._commands_stuck = 0
        self._commands_last_ok = 0.0
        self._command_pass_timeout = COMMAND_LONG_POLL_SECONDS + COMMAND_PASS_GRACE
        self._command_idle = 2.0
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
        # Чому знахідка не дійшла до чату. Питання "чому так мало алертів"
        # виникало стільки разів, що вгадувати відповідь по інших числах
        # виявилось дорожче, ніж порахувати її один раз тут.
        self._drops: Counter[str] = Counter()
        # Скільки знахідок цикл не поставив у чергу через прогрів або стелю
        self._not_queued: Counter[str] = Counter()
        # Лічильники для /health: без них не видно, чи бот мовчить тому, що
        # нічого не знаходить, чи тому, що нема куди слати
        self._deals_total = 0
        self._alerts_total = 0
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
                    market.code,
                    RateLimiter(self._request_interval, parent=self._ip_limiter),
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

            # Коли Vinted починає віддавати 429, коротшати паузу безглуздо:
            # штраф множить інтервал між запитами, цикл усе одно розтягується,
            # а ми лише дратуємо його далі. Тому період росте разом зі штрафом
            # і сам повертається, щойно все заспокоїлось.
            # Штраф уже розтягує КОЖЕН запит, тому обхід сам собою стає
            # довшим - це видно в elapsed. Множити на нього ще й паузу
            # означало платити за одну відмову двічі: на живому боті цикл
            # виходив 330 секунд при заданих 100, бо до вже розтягнутого
            # обходу додавався простій у 300. Тому множник тут символічний,
            # а справжнє гальмо - обмежувач.
            penalty = min(1.5, max((lim.penalty for lim in self.limiters.values()),
                                   default=1.0))
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1.0, self.cycle_seconds * penalty - elapsed))

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

        # Мапа станів піднімається на старті, коли бот найлегше ловить 429.
        # Якщо тоді не вдалось, спроба має повторитись сама: інакше ринок
        # працює за запасним словником до наступного деплою, а це тиха
        # неточність, про яку ніхто не дізнається.
        await self._reprobe_status_maps()

        # Команди слухає окрема задача (listen_commands), тут лише досилаємо
        # те, що чекало на чат.
        await self._flush_pending(now_ts)

        self.muted = await asyncio.to_thread(self.repo.muted_brand_ids)
        brand_ids = [b for b in self.registry.ids if b not in self.muted]

        observations: list[tuple[int, int, str, float, str, int]] = []
        deals: list[tuple[Deal, int]] = []
        fetched = 0
        fresh_count = 0

        slice_ = self._category_slice()

        async def sweep(market: Market) -> tuple[int, int]:
            seen = fresh = 0
            for category in slice_:
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
            deep_seen, deep_fresh = await self._deep_scan(
                brand_ids, observations, deals, categories=slice_
            )
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

        # Планка жиру вчиться на ВСІХ знахідках, включно з тими, які самі не
        # дійдуть: інакше вибірка складалась би з переможців і планка повзла
        # б угору, поки не перекрила б потік.
        for deal, _ in deals:
            self.fat.observe(deal.profit_eur)

        queued = 0
        if not warming:
            # Найжирніші першими, і не більше кількох за цикл. Навіть коли
            # ринок щедрий, десять алертів підряд ховають найкращий серед
            # решти - а весь сенс у тому, щоб його було видно.
            for deal, brand_id in sorted(deals, key=lambda d: -d[0].profit_eur):
                if queued >= self.fat_max_per_cycle:
                    self._not_queued["ліміт за цикл"] += len(deals) - queued
                    log.info(
                        "цього циклу вже %s знахідок, решту (%s) лишаю ринку",
                        queued, len(deals) - queued,
                    )
                    break
                if self._outbox.qsize() >= MAX_OUTBOX:
                    self._not_queued["черга повна"] += len(deals) - queued
                    log.warning("черга відправки повна, найдрібніші знахідки не влізли")
                    break
                self._outbox.put_nowait((deal, brand_id))
                queued += 1
        elif deals:
            self._not_queued["прогрів"] += len(deals)
            log.info("прогрів: %s знахідок не шлю, наповнюю базу цін", len(deals))
        self._deals_total += len(deals)

        # Розмір книги цін навмисне НЕ логуємо як показник роботи: вікно на
        # 120 записів по ключу насичується, і популярні бренди перестають
        # збільшувати лічильник, хоч нові лоти й далі надходять.
        log.info(
            "цикл %s: переглянуто=%s нових=%s знахідок=%s у черзі=%s "
            "у базі цін=%s по %s ключах штраф=%s%s",
            self.cycle_count,
            fetched,
            fresh_count,
            len(deals),
            self._outbox.qsize(),
            self.price_book.total_observations,
            self.price_book.tracked_keys,
            {c: round(lim.penalty, 1) for c, lim in self.limiters.items()},
            " [прогрів]" if warming else "",
        )
        if self._reject_stats and self.cycle_count % 10 == 0:
            log.info("причини відсіву: %s", dict(self._reject_stats.most_common(6)))
            self._reject_stats.clear()

        if self.cycle_count % PRUNE_EVERY_CYCLES == 0:
            await self._prune(now_ts)

    async def _reprobe_status_maps(self) -> None:
        """Добирає назви станів для ринків, де опитування не вдалось."""
        accepted = self.settings.accepted_status_ids()
        probe_catalog = self.settings.enabled_categories[0].id
        for code, status_map in self.status_maps.items():
            if status_map.probed:
                continue
            client = self.clients.get(code)
            if client is None:
                continue
            log.info("[%s] пробую дочитати назви станів", code)
            await status_map.resolve(client, accepted, probe_catalog)

    def _category_slice(self) -> list[Category]:
        """Скільки категорій беремо цього циклу.

        Vinted дає цьому серверу близько чотирьох успішних читань каталогу
        на хвилину - заміряно, не вгадано. Обхід усіх одинадцяти категорій
        на два ринки це 22 запити, тобто вдвічі більше, ніж нам дозволено, і
        зайве не просто пропадає: кожна відмова подвоює штраф, той розтягує
        вже КОЖЕН наступний запит, і врешті ми читаємо менше, ніж якби
        просили менше. На живому боті це давало 55% відмов і штраф, що
        намертво стояв у стелі.

        Тому беремо стільки, скільки влазить у квоту, і йдемо по колу.
        Категорія читається рідше, зате ЧИТАЄТЬСЯ: краще чистий знімок
        чотирьох стрічок, ніж половина від одинадцяти навмання.
        """
        cats = self.settings.enabled_categories
        step = self._cats_per_cycle
        if step <= 0 or step >= len(cats):
            return list(cats)
        start = self._cat_cursor % len(cats)
        self._cat_cursor = (start + step) % len(cats)
        # Беремо по колу, тому зріз може перестрибнути через кінець списку
        return [cats[(start + i) % len(cats)] for i in range(step)]

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
            # Не тільки в лог: якщо Vinted не віддає стрічку, бот НЕ здоровий,
            # хай навіть він бадьоро крутить цикли. Раніше /health показував
            # "ok" і порожній last_error, поки жодна категорія не читалась.
            self.last_error = f"[{market.code}/{category.key}] {exc}"
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
        self,
        brand_ids: list[int],
        observations: list,
        deals: list,
        *,
        categories: list[Category] | None = None,
    ) -> tuple[int, int]:
        """Дешевий хвіст: те, що висить годинами і зі стрічки новинок випало.

        Ходить тими самими категоріями, що й цикл, а не всіма. Інакше раз на
        двадцять циклів прилітав залп у 22 запити поверх звичайних восьми -
        при квоті в чотири читання на хвилину це п'ять хвилин бюджету за раз
        і штраф у стелі надовго після.
        """
        async def sweep(market: Market) -> tuple[int, int]:
            seen = fresh = 0
            for category in (categories or self.settings.enabled_categories):
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
            self._drops["пауза"] += 1
            return False
        if self._in_quiet_hours():
            self._drops["тихі години"] += 1
            return False
        if not self._alert_budget_left():
            self._drops["ліміт на годину"] += 1
            log.warning("досягнуто ліміт алертів на годину, притримую решту")
            return False
        if not self._burst_budget_left():
            # Не викидаємо: лот стає в чергу і піде наступним циклом. Так
            # знахідки приходять рівним струмком, а не стосом, у якому
            # найкраще губиться серед посереднього.
            self._drops["ліміт на хвилину"] += 1
            if len(self._pending) < MAX_PENDING_ALERTS:
                self._pending.append((deal, brand_id, time.monotonic()))
            return False
        if not self._seller_budget_left(deal.listing.seller_id):
            self._drops["той самий продавець"] += 1
            log.info(
                "продавець %s уже в стрічці цієї години, пропускаю %s",
                deal.listing.seller_id, deal.listing.url,
            )
            return False

        # Остання перевірка: чи ця знахідка краща за те, що трапляється
        # зазвичай. Пороги вигоди вище відповідають на питання "чи вигідно",
        # і вигідного багато; тут відсікаємо все, крім верхнього хвоста, щоб
        # справді жирний лот не губився серед десятка прохідних.
        fat_ok, note = self.fat.verdict(deal.profit_eur)
        if not fat_ok:
            self._drops["дрібне"] += 1
            log.info("%s | %s", note, deal.listing.url)
            return False
        if note:
            deal.notes.append("💰 " + note)

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
                # Сторожовий таймер. Ловити винятки було недостатньо: слухач
                # помер не від помилки, а від того, що один прохід не
                # завершився ніколи - і оскільки він ЖЕ не падав, зовні це
                # виглядало як живий бот, який просто не відповідає на
                # команди. Прохід не має права тривати довше за своє власне
                # очікування плюс запас; якщо триває - кидаємо його і
                # починаємо новий.
                await asyncio.wait_for(
                    self._command_pass(), timeout=self._command_pass_timeout
                )
                self._commands_polled += 1
                self._commands_last_ok = time.monotonic()
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                self._commands_stuck += 1
                log.warning(
                    "прохід по командах завис довше за %.0fс, кидаю і починаю новий "
                    "(таких разів: %s)",
                    self._command_pass_timeout, self._commands_stuck,
                )
            except Exception:  # noqa: BLE001
                log.exception("слухач команд спіткнувся, продовжую")
            # Якщо Telegram відмовляє миттєво (наприклад, битий токен), не
            # довбимо його в порожньому циклі
            # Пауза має нижню межу, і не нульову. Прохід, який падає миттєво
            # (битий токен, помилка в обробнику), інакше крутив би цикл на
            # повній швидкості: кожен оберт пише трейсбек у лог, і одна
            # зламана дрібниця вішає весь процес разом зі скануванням і
            # доставкою. Межа дешева й робить це неможливим.
            idle = self._command_idle - (time.monotonic() - started)
            await asyncio.sleep(max(COMMAND_MIN_IDLE, idle))

    async def _command_pass(self) -> None:
        """Один прохід: прочитати команди й досилати те, що чекало на чат."""
        await self._handle_commands(long_poll=COMMAND_LONG_POLL_SECONDS)
        await self._flush_pending(utc_now_ts())

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
            # Планка жиру: скільки зараз треба заробити, щоб лот дійшов
            "fat": self.fat.stats(),
            # Куди поділись знахідки, які не стали алертами. Дивитись сюди,
            # коли алертів менше, ніж знахідок: тут написано, хто їх з'їв.
            "drops": dict(self._drops),
            # Чому лот не став навіть знахідкою. Найчастіші причини вгорі:
            # тут видно, чи ми ріжемо потік розміром, стелею ціни чи станом.
            "screened_out": dict(self._reject_stats.most_common(8)),
            "not_queued": dict(self._not_queued),
            # Слухач команд живе окремою задачею, і колись він завис так, що
            # бот справно слав алерти й мовчав на будь-яку команду. Тут видно
            # одразу: since_ok росте - слухач стоїть.
            "commands": {
                "polls": self._commands_polled,
                "stuck": self._commands_stuck,
                "since_ok": (
                    int(time.monotonic() - self._commands_last_ok)
                    if self._commands_last_ok
                    else None
                ),
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
            # Чим закінчуються запити до Vinted, по ринках. Дивитись сюди
            # ПЕРШИМ, коли алертів нема: нуль в "ok" означає, що нас не
            # пускають, і жодні пороги фільтра тут ні до чого.
            "fetch": {
                code: dict(client.stats) for code, client in self.clients.items()
            },
            # false означає, що ринок працює за запасним словником назв
            # станів, а не за прочитаними з API
            "status_probed": {
                code: m.probed for code, m in self.status_maps.items()
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
