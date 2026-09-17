"""HTTP-клієнт до внутрішнього API Vinted.

Публічного API у Vinted немає. Сайт ходить у /api/v2/* зі звичайними куками,
які видає головна сторінка, і ми робимо рівно те саме: спершу GET на головну
(вона ставить access_token_web і __cf_bm), потім запити до каталогу.

Токен протухає, тому на 401/403 сесія піднімається наново.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import Counter
from typing import Any

import httpx

from ..models import Listing, utc_now_ts
from .catalog_page import parse_catalog
from ..settings import Market
from .ratelimit import RateLimiter

log = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
]


class VintedError(RuntimeError):
    pass


class VintedBlocked(VintedError):
    """Vinted відповів 403/429. Не помилка коду, треба просто пригальмувати."""


class VintedClient:
    """Одна сесія на один ринок."""

    def __init__(
        self,
        market: Market,
        limiter: RateLimiter,
        *,
        timeout: float = 20.0,
        max_retries: int = 3,
        known_conditions: tuple[str, ...] = (),
        is_known_brand=None,
    ) -> None:
        self.market = market
        self.limiter = limiter
        self.max_retries = max_retries
        # Потрібні розбору сторінки: мітки полів локалізовані, тому поля
        # впізнаються за значеннями - стан за списком станів, бренд за
        # реєстром. Див. catalog_page._classify.
        self.known_conditions = known_conditions
        self.is_known_brand = is_known_brand or (lambda _: False)
        self._user_agent = random.choice(USER_AGENTS)
        self._client = httpx.AsyncClient(
            base_url=market.base_url,
            timeout=timeout,
            follow_redirects=True,
            headers=self._base_headers(),
        )
        self._bootstrapped = False
        self._bootstrap_lock = asyncio.Lock()
        # Чим саме закінчуються запити. Без цього /health показував
        # "status: ok, last_error: null" на боті, який чотири години не міг
        # прочитати жодної сторінки: мовчазний провал виглядав як тиша на
        # ринку. Тепер видно, відмовляє Vinted чи справді нема лотів.
        self.stats: Counter[str] = Counter()
        self.last_status: int | None = None
        # Запобіжник на 403. Поки він зведений, ринок не чіпаємо взагалі.
        self._blocked_until = 0.0
        self._block_strikes = 0

    def _base_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self._user_agent,
            "Accept-Language": self.market.locale,
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    async def __aenter__(self) -> "VintedClient":
        await self.ensure_session()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ сесія

    async def ensure_session(self, *, force: bool = False) -> None:
        """Забирає анонімні куки з головної сторінки."""
        async with self._bootstrap_lock:
            if self._bootstrapped and not force:
                return
            if force:
                self._rotate_identity()

            html_headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            }
            for attempt in range(1, self.max_retries + 1):
                await self.limiter.acquire()
                try:
                    resp = await self._client.get("/", headers=html_headers)
                except httpx.HTTPError as exc:
                    # Лічильник тут обов'язковий. Без нього провал підняття
                    # сесії не лишав узагалі нічого: ні в /health, ні в
                    # last_error, - і бот тиждень крутив порожні цикли з
                    # усіма нулями, бо кожен запит помирав саме тут.
                    self.stats["bootstrap_network"] += 1
                    log.warning(
                        "[%s] головна не відкрилась (%s/%s): %s",
                        self.market.code, attempt, self.max_retries, exc,
                    )
                    await asyncio.sleep(2 ** attempt)
                    continue

                if resp.status_code == 200:
                    self.stats["bootstrap_ok"] += 1
                    names = set(self._client.cookies.keys())
                    if "access_token_web" not in names:
                        log.warning(
                            "[%s] сесія піднялась без access_token_web, куки: %s",
                            self.market.code, sorted(names),
                        )
                    else:
                        log.info("[%s] сесія піднята, куки отримані", self.market.code)
                    self.limiter.relax()
                    self._bootstrapped = True
                    return

                if resp.status_code == 403:
                    # 403 на головній - це блок адреси, а не темпу. Коротка
                    # пауза тут була моєю помилкою: я підібрав її під 429
                    # ("чекати безглуздо, треба міняти відбиток") і застосував
                    # до обох кодів. За тиждень роботи це дало 36 тисяч відмов
                    # на піднятті сесії - бот довбив заблокований вхід кожні
                    # шість секунд і тим сам тримав блок. Тепер відступаємо
                    # надовго і мовчки.
                    self.stats["bootstrap_403"] += 1
                    self._trip_breaker()
                    raise VintedBlocked(
                        f"{self.market.code}: головна віддала 403, відступаю"
                    )

                if resp.status_code == 429:
                    # А ось тут пауза справді ні до чого: сесія жива, нас
                    # лише просять пригальмувати. Міняємо відбиток і йдемо далі.
                    self.stats["bootstrap_429"] += 1
                    self.limiter.penalise()
                    delay = min(6.0, 2.0 * attempt)
                    log.warning(
                        "[%s] головна віддала 429, міняю відбиток (пауза %.0fс)",
                        self.market.code, delay,
                    )
                    await asyncio.sleep(delay)
                    self._rotate_identity()
                    continue

                self.stats[f"bootstrap_{resp.status_code}"] += 1
                log.warning(
                    "[%s] головна віддала %s (%s/%s)",
                    self.market.code, resp.status_code, attempt, self.max_retries,
                )
                await asyncio.sleep(min(6.0, 2.0 * attempt))

            self.stats["bootstrap_gave_up"] += 1
            raise VintedBlocked(f"{self.market.code}: не вдалось підняти сесію")

    def _rotate_identity(self) -> None:
        self._client.cookies.clear()
        self._user_agent = random.choice(USER_AGENTS)
        self._client.headers.update(self._base_headers())

    # ------------------------------------------------------------------ запит

    def _trip_breaker(self) -> None:
        """403 отримано: відступаємо, і що далі, то довше.

        Кожен наступний блок поспіль подвоює паузу (хвилина, дві, чотири...
        до півгодини). Успішне читання скидає лічильник. Сенс у тому, щоб
        заблокована адреса мала шанс "охолонути": продовжувати стукати в
        зачинені двері - найнадійніший спосіб лишити їх зачиненими.
        """
        self._block_strikes = min(self._block_strikes + 1, 5)
        pause = min(60.0 * (2 ** (self._block_strikes - 1)), 1800.0)
        self._blocked_until = time.monotonic() + pause
        self._rotate_identity()
        self._bootstrapped = False
        log.warning(
            "[%s] 403: відступаю на %.0f хв (блок %s поспіль)",
            self.market.code, pause / 60, self._block_strikes,
        )

    @property
    def blocked_for(self) -> float:
        """Скільки секунд ще не чіпаємо цей ринок."""
        return max(0.0, self._blocked_until - time.monotonic())

    async def _get_json(self, path: str, params: list[tuple[str, Any]]) -> dict[str, Any]:
        resp = await self._request(
            path, params, accept="application/json, text/plain, */*"
        )
        try:
            return resp.json()
        except ValueError as exc:
            raise VintedError(
                f"{self.market.code}: відповідь не JSON ({len(resp.content)} байт)"
            ) from exc

    async def _get_page(self, path: str, params: list[tuple[str, Any]]) -> str:
        """Сторінка каталогу як текст. Той самий шлях відмов, що й у JSON."""
        resp = await self._request(path, params, accept="text/html")
        return resp.text

    async def _request(
        self, path: str, params: list[tuple[str, Any]], *, accept: str
    ) -> httpx.Response:
        if self.blocked_for > 0:
            self.stats["skipped_while_blocked"] += 1
            raise VintedBlocked(
                f"{self.market.code}: під блоком ще {self.blocked_for:.0f}с"
            )
        await self.ensure_session()
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            await self.limiter.acquire()
            try:
                resp = await self._client.get(
                    path,
                    params=params,
                    headers={
                        "Accept": accept,
                        "Referer": f"{self.market.base_url}/catalog",
                    },
                )
            except httpx.HTTPError as exc:
                last_error = exc
                self.stats["network"] += 1
                log.warning("[%s] мережа впала (%s/%s): %s", self.market.code, attempt, self.max_retries, exc)
                await asyncio.sleep(2 ** attempt)
                continue

            self.last_status = resp.status_code
            if resp.status_code == 200:
                self.limiter.relax()
                self.stats["ok"] += 1
                self._block_strikes = 0
                return resp

            if resp.status_code in (401, 419):
                log.info("[%s] токен протух, піднімаю сесію заново", self.market.code)
                await self.ensure_session(force=True)
                continue

            if resp.status_code in (403, 429):
                self.stats[str(resp.status_code)] += 1
                self.limiter.penalise()
                # 429 і 403 - різні речі, і лікуються по-різному.
                #
                # 403 означає "ти підозрілий": тоді відбиток справді треба
                # міняти. 429 означає лише "зашвидко" - сесія при цьому
                # цілком робоча. Ми ж на кожен 429 піднімали її наново, і це
                # било по нас двічі: зайвий запит на головну і свіжі куки
                # замість тих, що вже мали якусь довіру. Заміряно на живому
                # боті: коли інтервал підняли з 3.5 до 5 секунд, відмов стало
                # не менше, а більше (34% -> 63%), бо запитів на цикл менше
                # не стало, а сесія так само скидалась після кожного.
                blocked = resp.status_code == 403
                # Довга пауза саме тут була помилкою. Обмежувач уже подвоїв
                # інтервал для всього ринку, а ця пауза додавалась зверху й
                # тримала весь обхід: 10+20+40 секунд на КОЖНУ категорію,
                # яку Vinted не віддав. Заміряно на живому боті - цикл
                # розтягнувся з 30 секунд до 317, тобто бот майже не дивився
                # на стрічку саме тоді, коли й так ледве проходив.
                # Одна коротка пауза, одна спроба з новою сесією, і йдемо
                # далі: ця категорія повернеться наступним циклом.
                delay = min(8.0, 2.0 * attempt)
                log.warning(
                    "[%s] Vinted віддав %s, пауза %.0fс (штраф x%.1f)",
                    self.market.code, resp.status_code, delay, self.limiter.penalty,
                )
                await asyncio.sleep(delay)
                if attempt >= min(2, self.max_retries):
                    raise VintedBlocked(f"{self.market.code}: HTTP {resp.status_code}")
                if blocked:
                    self._trip_breaker()
                    raise VintedBlocked(f"{self.market.code}: HTTP 403, відступаю")
                continue

            if 500 <= resp.status_code < 600:
                self.stats["5xx"] += 1
                last_error = VintedError(f"HTTP {resp.status_code}")
                await asyncio.sleep(2 ** attempt)
                continue

            # Рахуємо і цей шлях. Саме тут тиждень безслідно гинули 404 після
            # того, як Vinted вимкнув /api/v2/catalog/items: код був
            # "несподіваний", лічильника не мав, і /health показував нулі.
            self.stats[str(resp.status_code)] += 1
            raise VintedError(f"{self.market.code}: несподіваний HTTP {resp.status_code}")

        raise VintedError(f"{self.market.code}: не вдалось після {self.max_retries} спроб: {last_error}")

    # --------------------------------------------------------------- каталог

    async def fetch_catalog(
        self,
        *,
        catalog_id: int,
        brand_ids: list[int] | None = None,
        status_ids: list[int] | None = None,
        price_to: float | None = None,
        per_page: int = 96,
        page: int = 1,
        order: str = "newest_first",
    ) -> tuple[list[Listing], int]:
        """Лоти категорії. Повертає (лоти, серверний час).

        order="newest_first" - звичайна стрічка, найсвіжіше зверху.
        order="price_low_to_high" - найдешевше зверху, незалежно від віку.
        Другий режим потрібен, бо лот, який висить пів дня, зі стрічки
        новинок уже випав, а з дешевого хвоста нікуди не дівається.
        """
        # Сторінка чекає catalog[] замість catalog_ids[] і не знає per_page:
        # вона завжди віддає 96 лотів, як і віддавало API.
        params: list[tuple[str, Any]] = [
            ("page", page),
            ("order", order),
            ("catalog[]", catalog_id),
        ]
        for bid in brand_ids or []:
            params.append(("brand_ids[]", bid))
        for sid in status_ids or []:
            params.append(("status_ids[]", sid))
        if price_to is not None:
            params.append(("price_to", f"{price_to:.2f}"))
            params.append(("currency", self.market.currency))

        page = await self._get_page("/catalog", params)
        server_ts = utc_now_ts()
        return parse_catalog(
            page,
            market_code=self.market.code,
            base_url=self.market.base_url,
            catalog_id=catalog_id,
            server_ts=server_ts,
            currency=self.market.currency,
            known_conditions=self.known_conditions,
            is_known_brand=self.is_known_brand,
        ), server_ts

    async def search_brands(self, keyword: str) -> list[dict[str, Any]]:
        payload = await self._get_json("/api/v2/brands", [("keyword", keyword)])
        return payload.get("brands") or []

    # ---------------------------------------------------------------- парсинг

    def _parse_item(self, raw: dict[str, Any], catalog_id: int, server_ts: int) -> Listing | None:
        try:
            item_id = int(raw["id"])
        except (KeyError, TypeError, ValueError):
            return None

        price = _money(raw.get("price"))
        total = _money(raw.get("total_item_price")) or price
        if price is None:
            return None

        photo = raw.get("photo") or {}
        hi_res = photo.get("high_resolution") or {}
        user = raw.get("user") or {}

        return Listing(
            item_id=item_id,
            market=self.market.code,
            catalog_id=catalog_id,
            title=(raw.get("title") or "").strip(),
            brand_title=(raw.get("brand_title") or "").strip(),
            size_title=(raw.get("size_title") or "").strip(),
            status_title=(raw.get("status") or "").strip(),
            status_id=None,
            price=price,
            total_price=total if total is not None else price,
            currency=(raw.get("price") or {}).get("currency_code") or self.market.currency,
            url=raw.get("url") or f"{self.market.base_url}{raw.get('path', '')}",
            photo_url=photo.get("url"),
            seller_id=_int_or_none(user.get("id")),
            seller_login=user.get("login"),
            seller_is_business=bool(user.get("business")),
            favourite_count=int(raw.get("favourite_count") or 0),
            view_count=int(raw.get("view_count") or 0),
            uploaded_ts=_int_or_none(hi_res.get("timestamp")),
            seen_ts=server_ts,
        )


def _money(node: Any) -> float | None:
    if not isinstance(node, dict):
        return None
    try:
        return float(node.get("amount"))
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
