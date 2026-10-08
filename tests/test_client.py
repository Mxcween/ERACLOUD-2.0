"""Поведінка клієнта, коли Vinted нас не пускає.

Живий бот чотири години крутив цикли по 317 секунд замість 30 і не знайшов
жодного лота, а /health при цьому показував "status: ok". Обидві причини
перевіряються тут: скільки часу коштує відмова і чи видно її назовні.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from vintsniper.settings import Market
from vintsniper.vinted.client import (
    COOKIELESS_RETRY_SECONDS,
    VintedBlocked,
    VintedClient,
    VintedError,
)
from vintsniper.vinted.ratelimit import RateLimiter

MARKET = Market(code="PL", host="www.vinted.pl", currency="PLN", locale="pl", shipping_eur=3.5)


def build(handler) -> VintedClient:
    client = VintedClient(MARKET, RateLimiter(min_interval=0.0, jitter=0.0), max_retries=3)
    client._client = httpx.AsyncClient(
        base_url=MARKET.base_url, transport=httpx.MockTransport(handler)
    )
    return client


@pytest.fixture
def no_sleep(monkeypatch):
    """Рахуємо, скільки бот ЗБИРАВСЯ спати, не витрачаючи на це тест."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


class TestBlocked:
    @pytest.mark.asyncio
    async def test_refusal_costs_seconds_not_minutes(self, no_sleep):
        """403 на все підряд має коштувати секунди, а не пів циклу.

        Раніше одна заблокована категорія спала 10+20+40 у запиті і ще
        20+40+80 у кожному піднятті сесії. Одинадцять категорій - і цикл
        з'їдений цілком, тобто бот не дивиться на стрічку саме тоді, коли й
        так ледве проходить.
        """
        client = build(lambda request: httpx.Response(403, text="no"))
        with pytest.raises(VintedBlocked):
            await client.fetch_catalog(catalog_id=1206, brand_ids=[53], per_page=96)
        await client.close()

        assert sum(no_sleep) < 30, f"відмова коштувала {sum(no_sleep):.0f}с сну"

    @pytest.mark.asyncio
    async def test_refusal_is_counted_so_health_can_show_it(self, no_sleep):
        """Мовчазний провал виглядав як тиша на ринку. Тепер він порахований."""
        client = build(lambda request: httpx.Response(403, text="no"))
        with pytest.raises(VintedBlocked):
            await client.fetch_catalog(catalog_id=1206, brand_ids=[53], per_page=96)
        await client.close()

        assert client.stats["ok"] == 0
        assert client.stats["403"] + client.stats["bootstrap_403"] > 0

    @pytest.mark.asyncio
    async def test_success_is_counted_too(self, no_sleep):
        payload = {"items": [], "pagination": {"total_entries": 0, "time": 1700000000}}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/":
                return httpx.Response(200, text="<html></html>")
            return httpx.Response(200, json=payload)

        client = build(handler)
        listings, _ = await client.fetch_catalog(catalog_id=1206, brand_ids=[53], per_page=96)
        await client.close()

        assert listings == []
        assert client.stats["ok"] == 1
        assert client.stats["403"] == 0


class TestIpWideCeiling:
    """Vinted рахує запити по IP, а не по хосту.

    Два ринки з окремими лічильниками кожен вважав, що дотримується
    інтервалу, а разом видавали вдвічі більшу частоту, ніж написано в
    конфізі. Живий бот ловив 429 ще на піднятті сесії.
    """

    @pytest.mark.asyncio
    async def test_two_markets_share_one_ceiling(self):
        import time

        parent = RateLimiter(min_interval=0.05, jitter=0.0)
        pl = RateLimiter(min_interval=0.0, jitter=0.0, parent=parent)
        de = RateLimiter(min_interval=0.0, jitter=0.0, parent=parent)

        started = time.monotonic()
        await asyncio.gather(*(m.acquire() for m in (pl, de, pl, de)))
        elapsed = time.monotonic() - started

        # Чотири запити при стелі 0.05с не можуть коштувати менше трьох пауз
        assert elapsed >= 0.15 - 0.02, f"стеля не спрацювала: {elapsed:.3f}с"

    @pytest.mark.asyncio
    async def test_without_a_parent_nothing_holds_them_back(self):
        import time

        pl = RateLimiter(min_interval=0.0, jitter=0.0)
        de = RateLimiter(min_interval=0.0, jitter=0.0)

        started = time.monotonic()
        await asyncio.gather(*(m.acquire() for m in (pl, de, pl, de)))
        assert time.monotonic() - started < 0.05


class TestRefusalKind:
    """429 і 403 лікуються по-різному.

    429 це "зашвидко" - сесія робоча, міняти відбиток безглуздо і шкідливо:
    зайвий запит на головну плюс свіжі куки замість тих, що вже мали довіру.
    403 це "ти підозрілий" - ось там відбиток і треба міняти.
    """

    @pytest.mark.asyncio
    async def test_rate_limit_keeps_the_session(self, no_sleep):
        homepage_hits = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal homepage_hits
            if request.url.path == "/":
                homepage_hits += 1
                return httpx.Response(200, text="<html></html>")
            return httpx.Response(429, text="slow down")

        client = build(handler)
        with pytest.raises(VintedBlocked):
            await client.fetch_catalog(catalog_id=1206, brand_ids=[53], per_page=96)
        await client.close()

        assert homepage_hits == 1, "429 не має піднімати сесію заново"

    @pytest.mark.asyncio
    async def test_forbidden_rotates_the_fingerprint_and_backs_off(self, no_sleep):
        """На 403 відбиток міняємо, але далі не стукаємо.

        Раніше тут перевірялось, що сесія піднімається заново - тобто що бот
        одразу пробує ще раз з новим відбитком. За тиждень живої роботи стало
        видно, чим це закінчується: 36578 відмов на піднятті сесії. Міняти
        відбиток правильно, продовжувати одразу - ні.
        """
        user_agents = set()

        def handler(request: httpx.Request) -> httpx.Response:
            user_agents.add(request.headers.get("user-agent"))
            if request.url.path == "/":
                return httpx.Response(200, text="<html></html>")
            return httpx.Response(403, text="no")

        client = build(handler)
        with pytest.raises(VintedBlocked):
            await client.fetch_catalog(catalog_id=1206, brand_ids=[53], per_page=96)
        await client.close()

        assert client.blocked_for > 0, "після 403 ринок має бути на паузі"
        # Перевіряємо наслідок зміни відбитка, а не сам User-Agent: він
        # береться випадково зі списку й може збігтись сам із собою, через
        # що тест падав лише в повному прогоні, а не поодинці.
        assert client._bootstrapped is False, "сесію треба піднімати наново"


class TestCategoryRotation:
    """Квота Vinted менша за наш обхід, тому категорії йдуть по колу.

    Головне тут - нічого не загубити: за повний оберт мають бути прочитані
    всі категорії рівно по разу, інакше якась стрічка тихо випаде назавжди.
    """

    def _sniper(self, count: int, per_cycle: int):
        from types import SimpleNamespace

        from vintsniper.runner import Sniper

        cats = [SimpleNamespace(key=f"c{i}", id=i) for i in range(count)]
        s = object.__new__(Sniper)
        s.settings = SimpleNamespace(enabled_categories=cats)
        s._cats_per_cycle = per_cycle
        s._cat_cursor = 0
        return s, cats

    def test_a_full_turn_covers_every_category_once(self):
        s, cats = self._sniper(11, 4)
        seen: list[str] = []
        # 11 категорій по 4 за цикл: повний оберт це 11 циклів
        for _ in range(11):
            seen.extend(c.key for c in s._category_slice())
        from collections import Counter
        counts = Counter(seen)
        assert set(counts) == {c.key for c in cats}, "якась категорія випала"
        assert set(counts.values()) == {4}, f"нерівномірно: {counts}"

    def test_zero_means_everything(self):
        s, cats = self._sniper(11, 0)
        assert [c.key for c in s._category_slice()] == [c.key for c in cats]

    def test_slice_wraps_around_the_end(self):
        s, _ = self._sniper(5, 3)
        assert [c.key for c in s._category_slice()] == ["c0", "c1", "c2"]
        assert [c.key for c in s._category_slice()] == ["c3", "c4", "c0"]


class TestPenaltyCeiling:
    """Штраф не має заганяти бота в кому.

    Заміряно: ширші паузи відмов не зменшують, бо ліміт Vinted стоїть на
    кількості запитів, а не на їх частоті. При половині відмов штраф уже не
    спадав ніколи, бо подвоєння після кожної перебивало ті 15%, що знімає
    успіх. Виходило, що ми платимо паузами за те, чого не купуємо.
    """

    def test_penalty_stops_well_short_of_a_coma(self):
        lim = RateLimiter(min_interval=2.0, jitter=0.0)
        for _ in range(20):
            lim.penalise()
        assert lim.penalty <= RateLimiter.CEILING
        assert lim.min_interval * lim.penalty <= 5.0, "інтервал розрісся до коми"

    def test_a_success_still_walks_it_back(self):
        lim = RateLimiter(min_interval=2.0, jitter=0.0)
        lim.penalise()
        peak = lim.penalty
        for _ in range(10):
            lim.relax()
        assert lim.penalty < peak
        assert lim.penalty >= 1.0


class TestForbiddenBreaker:
    """403 - це блок адреси, і довбити далі означає тримати його зведеним.

    Заміряно на живому боті за тиждень роботи: 36578 відмов на піднятті
    сесії проти 16351 успішного читання. Пауза була 6 секунд - я підібрав її
    під 429 ("чекати безглуздо, треба міняти відбиток") і застосував до обох
    кодів, що для 403 рівно навпаки.
    """

    @pytest.mark.asyncio
    async def test_a_forbidden_homepage_stops_the_market(self, no_sleep):
        hits = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal hits
            hits += 1
            return httpx.Response(403, text="no")

        client = build(handler)
        with pytest.raises(VintedBlocked):
            await client.fetch_catalog(catalog_id=1206, brand_ids=[53], per_page=96)
        assert client.blocked_for > 0, "запобіжник не звівся"

        before = hits
        with pytest.raises(VintedBlocked):
            await client.fetch_catalog(catalog_id=1206, brand_ids=[53], per_page=96)
        await client.close()
        assert hits == before, "під блоком не має бути жодного запиту"

    @pytest.mark.asyncio
    async def test_each_block_in_a_row_backs_off_further(self, no_sleep):
        client = build(lambda request: httpx.Response(403, text="no"))
        pauses = []
        for _ in range(3):
            client._blocked_until = 0.0        # імітуємо, що пауза вийшла
            with pytest.raises(VintedBlocked):
                await client.fetch_catalog(catalog_id=1206, brand_ids=[53], per_page=96)
            pauses.append(client.blocked_for)
        await client.close()
        assert pauses[0] < pauses[1] < pauses[2], pauses
        assert pauses[-1] <= 1800

    @pytest.mark.asyncio
    async def test_a_success_clears_the_strikes(self, no_sleep):
        payload = {"items": [], "pagination": {"total_entries": 0, "time": 1700000000}}
        state = {"forbid": True}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/":
                return httpx.Response(200, text="<html></html>")
            if state["forbid"]:
                return httpx.Response(403, text="no")
            return httpx.Response(200, json=payload)

        client = build(handler)
        with pytest.raises(VintedBlocked):
            await client.fetch_catalog(catalog_id=1206, brand_ids=[53], per_page=96)
        assert client._block_strikes == 1

        state["forbid"] = False
        client._blocked_until = 0.0
        await client.fetch_catalog(catalog_id=1206, brand_ids=[53], per_page=96)
        await client.close()
        assert client._block_strikes == 0, "успіх мав зняти лічильник"

    @pytest.mark.asyncio
    async def test_rate_limiting_does_not_trip_the_breaker(self, no_sleep):
        """429 не має зупиняти ринок: це темп, а не підозра."""
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/":
                return httpx.Response(200, text="<html></html>")
            return httpx.Response(429, text="slow")

        client = build(handler)
        with pytest.raises(VintedBlocked):
            await client.fetch_catalog(catalog_id=1206, brand_ids=[53], per_page=96)
        await client.close()
        assert client.blocked_for == 0


class TestFailuresAreVisible:
    """Провал підняття сесії має лишати слід.

    Живий бот тиждень крутив порожні цикли: 4 оберти за 6 хвилин, жодного
    запиту, /health показував status "ok", last_error "None" і всі лічильники
    нерухомими. Кожен запит помирав у ensure_session, де не було лічильника
    ні на мережеву помилку, ні на несподіваний код.
    """

    @pytest.mark.asyncio
    async def test_a_network_failure_is_counted(self, no_sleep):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("мережа впала")

        client = build(handler)
        # Падає вже саме читання, а не підняття сесії: куки для читання не
        # потрібні, тому провал головної більше не вирішує долю запиту.
        with pytest.raises(VintedError):
            await client.fetch_catalog(catalog_id=1206, brand_ids=[53], per_page=96)
        await client.close()

        assert client.stats["bootstrap_network"] > 0
        assert client.stats["bootstrap_gave_up"] == 1

    @pytest.mark.asyncio
    async def test_an_unexpected_status_is_counted(self, no_sleep):
        client = build(lambda request: httpx.Response(503, text="maintenance"))
        with pytest.raises(VintedError):
            await client.fetch_catalog(catalog_id=1206, brand_ids=[53], per_page=96)
        await client.close()

        assert client.stats["bootstrap_503"] > 0, dict(client.stats)
        assert client.stats["bootstrap_gave_up"] == 1

    @pytest.mark.asyncio
    async def test_a_healthy_bootstrap_is_counted_too(self, no_sleep):
        payload = {"items": [], "pagination": {"total_entries": 0, "time": 1700000000}}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/":
                return httpx.Response(200, text="<html></html>")
            return httpx.Response(200, json=payload)

        client = build(handler)
        await client.fetch_catalog(catalog_id=1206, brand_ids=[53], per_page=96)
        await client.close()
        assert client.stats["bootstrap_ok"] == 1


class TestHomepageIsOptional:
    """403 на головній не має зупиняти ні бот, ні навіть один ринок.

    Спершу цей виняток проходив наскрізь і валив процес цілком - разом з
    другим ринком, слухачем команд і студією. Потім він лише зводив
    запобіжник, і це здавалось правильним: "нас запідозрили, відступимо".

    Заміряно на живому боті за добу: 101 відмова на головній, 30 тисяч
    пропущених читань проти 133 успішних, 68 знахідок за 26 годин замість
    кількох тисяч. Ринок стояв 99% часу. І тут же, холодним клієнтом без
    жодної куки: 96 лотів із 96 на обох ринках. Куки для читання каталогу
    не потрібні, тому головна - необовʼязкова.
    """

    @pytest.mark.asyncio
    async def test_a_forbidden_homepage_does_not_block_the_market(self, no_sleep):
        client = build(lambda request: httpx.Response(403, text="no"))
        await client.ensure_session()          # не кидає
        assert client.blocked_for == 0, "запобіжник тут - це простій на годину"
        assert client._cookieless is True
        assert client.stats["bootstrap_403"] == 1
        await client.close()

    @pytest.mark.asyncio
    async def test_the_catalog_is_still_read_without_cookies(self, no_sleep):
        page = (
            '<div data-testid="product-item-id-11"></div>'
            '<a href="/items/11-x"></a>'
            '<span title="Kurtka Nike, Marka: Nike, Stan: Dobry, Rozmiar: L, 40 zł, 44 zł">x</span>'
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/":
                return httpx.Response(403, text="no")
            return httpx.Response(200, text=page)

        client = build(handler)
        items, _ = await client.fetch_catalog(catalog_id=1206, per_page=96)
        assert len(items) == 1, "403 на головній не має коштувати нам лотів"
        assert client.stats["ok"] == 1
        await client.close()

    @pytest.mark.asyncio
    async def test_the_homepage_is_not_hammered_while_cookieless(self, no_sleep):
        """Довбити зачинені двері - найнадійніший спосіб лишити їх зачиненими."""
        hits = {"home": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/":
                hits["home"] += 1
                return httpx.Response(403, text="no")
            return httpx.Response(200, text="")

        client = build(handler)
        await client.ensure_session()
        first_round = hits["home"]
        assert first_round == 1, "одна спроба, далі працюємо без кук"

        for _ in range(5):
            await client.ensure_session()
        assert hits["home"] == first_round, (
            "після переходу на роботу без кук головну більше не чіпаємо"
        )
        await client.close()

    @pytest.mark.asyncio
    async def test_cookies_are_picked_up_again_once_the_block_lifts(self, no_sleep):
        state = {"forbid": True}

        def handler(request: httpx.Request) -> httpx.Response:
            if state["forbid"]:
                return httpx.Response(403, text="no")
            return httpx.Response(200, text="<html></html>")

        client = build(handler)
        await client.ensure_session()
        assert client._cookieless is True

        # Минув час наступної спроби, і блок уже зняли
        state["forbid"] = False
        client._cookieless_since -= COOKIELESS_RETRY_SECONDS + 1
        await client.ensure_session()
        assert client._cookieless is False, "куки варто мати, якщо їх дають"
        assert client.stats["bootstrap_ok"] == 1
        await client.close()
