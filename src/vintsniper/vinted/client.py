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
from typing import Any

import httpx

from ..models import Listing, utc_now_ts
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
    ) -> None:
        self.market = market
        self.limiter = limiter
        self.max_retries = max_retries
        self._user_agent = random.choice(USER_AGENTS)
        self._client = httpx.AsyncClient(
            base_url=market.base_url,
            timeout=timeout,
            follow_redirects=True,
            headers=self._base_headers(),
        )
        self._bootstrapped = False
        self._bootstrap_lock = asyncio.Lock()

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
                    log.warning(
                        "[%s] головна не відкрилась (%s/%s): %s",
                        self.market.code, attempt, self.max_retries, exc,
                    )
                    await asyncio.sleep(2 ** attempt)
                    continue

                if resp.status_code == 200:
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

                if resp.status_code in (403, 429):
                    # Нас пригальмували ще на вході. Тиснути далі тим самим
                    # відбитком безглуздо: чекаємо довше і міняємо User-Agent.
                    self.limiter.penalise()
                    delay = min(120.0, (2 ** attempt) * 10)
                    log.warning(
                        "[%s] головна віддала %s, чекаю %.0fс і міняю відбиток",
                        self.market.code, resp.status_code, delay,
                    )
                    await asyncio.sleep(delay)
                    self._rotate_identity()
                    continue

                log.warning(
                    "[%s] головна віддала %s (%s/%s)",
                    self.market.code, resp.status_code, attempt, self.max_retries,
                )
                await asyncio.sleep(2 ** attempt)

            raise VintedBlocked(f"{self.market.code}: не вдалось підняти сесію")

    def _rotate_identity(self) -> None:
        self._client.cookies.clear()
        self._user_agent = random.choice(USER_AGENTS)
        self._client.headers.update(self._base_headers())

    # ------------------------------------------------------------------ запит

    async def _get_json(self, path: str, params: list[tuple[str, Any]]) -> dict[str, Any]:
        await self.ensure_session()
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            await self.limiter.acquire()
            try:
                resp = await self._client.get(
                    path,
                    params=params,
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "Referer": f"{self.market.base_url}/catalog",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                )
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("[%s] мережа впала (%s/%s): %s", self.market.code, attempt, self.max_retries, exc)
                await asyncio.sleep(2 ** attempt)
                continue

            if resp.status_code == 200:
                self.limiter.relax()
                try:
                    return resp.json()
                except ValueError as exc:
                    last_error = exc
                    log.warning("[%s] відповідь не JSON, довжина %s", self.market.code, len(resp.content))
                    await asyncio.sleep(2 ** attempt)
                    continue

            if resp.status_code in (401, 419):
                log.info("[%s] токен протух, піднімаю сесію заново", self.market.code)
                await self.ensure_session(force=True)
                continue

            if resp.status_code in (403, 429):
                self.limiter.penalise()
                delay = min(60.0, (2 ** attempt) * 5)
                log.warning(
                    "[%s] Vinted віддав %s, пауза %.0fс (штраф x%.1f)",
                    self.market.code, resp.status_code, delay, self.limiter.penalty,
                )
                await asyncio.sleep(delay)
                if attempt == self.max_retries:
                    raise VintedBlocked(f"{self.market.code}: HTTP {resp.status_code}")
                await self.ensure_session(force=True)
                continue

            if 500 <= resp.status_code < 600:
                last_error = VintedError(f"HTTP {resp.status_code}")
                await asyncio.sleep(2 ** attempt)
                continue

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
    ) -> tuple[list[Listing], int]:
        """Свіжі лоти категорії. Повертає (лоти, серверний час)."""
        params: list[tuple[str, Any]] = [
            ("page", page),
            ("per_page", per_page),
            ("order", "newest_first"),
            ("catalog_ids[]", catalog_id),
        ]
        for bid in brand_ids or []:
            params.append(("brand_ids[]", bid))
        for sid in status_ids or []:
            params.append(("status_ids[]", sid))
        if price_to is not None:
            params.append(("price_to", f"{price_to:.2f}"))
            params.append(("currency", self.market.currency))

        payload = await self._get_json("/api/v2/catalog/items", params)
        server_ts = int((payload.get("pagination") or {}).get("time") or utc_now_ts())
        items = [
            self._parse_item(raw, catalog_id, server_ts)
            for raw in payload.get("items") or []
        ]
        return [i for i in items if i is not None], server_ts

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
