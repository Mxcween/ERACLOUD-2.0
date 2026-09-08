"""Зір: як читаються вироки і що робиться, коли перевірка падає."""
import pytest

from vintsniper.engine.vision import CHECK_FAILED, NOT_CONFIGURED, PhotoJudge, _parse


class TestVerdictParsing:
    def test_clean_listing_passes(self):
        v = _parse({"real_item": 9, "condition": 8, "photo_ok": 9, "flags": [], "note": "ok"})
        assert v.ok and v.checked and v.note == "ok"

    def test_counterfeit_is_rejected(self):
        v = _parse({"real_item": 2, "condition": 8, "photo_ok": 9, "flags": ["fake"]})
        assert not v.ok
        assert "fake" in v.reason

    def test_screenshot_of_another_listing_is_rejected(self):
        v = _parse({"real_item": 3, "condition": 7, "photo_ok": 8, "flags": ["screenshot"]})
        assert not v.ok

    def test_damaged_item_is_rejected(self):
        assert not _parse({"real_item": 9, "condition": 1, "photo_ok": 9}).ok

    def test_item_not_visible_is_rejected(self):
        assert not _parse({"real_item": 8, "condition": 8, "photo_ok": 1}).ok

    def test_missing_fields_default_to_trusting(self):
        """Модель мовчить про поле - не привід викидати лот."""
        assert _parse({"note": "нічого не сказала"}).ok

    def test_garbage_values_do_not_crash(self):
        v = _parse({"real_item": "дев'ять", "condition": None, "photo_ok": 99})
        assert v.ok and v.photo_ok == 10

    def test_scores_are_clamped(self):
        v = _parse({"real_item": -5, "condition": 50, "photo_ok": 7})
        assert v.real_item == 0 and v.condition == 10

    def test_thresholds_are_configurable(self):
        data = {"real_item": 6, "condition": 6, "photo_ok": 6}
        assert _parse(data).ok
        assert not _parse(data, min_real=8).ok

    def test_reason_is_empty_when_it_passed(self):
        assert _parse({"real_item": 9, "condition": 9, "photo_ok": 9}).reason == ""


class TestFailOpen:
    """Збій зору не має робити бота німим."""

    def test_unchecked_verdict_still_passes(self):
        assert NOT_CONFIGURED.ok and not NOT_CONFIGURED.checked
        assert CHECK_FAILED.ok and not CHECK_FAILED.checked

    def test_missing_key_says_nothing_but_a_failure_does(self):
        """Без ключа перевірки не було й не мало бути - мовчимо.
        А от спроба, що впала, це інформація: лот пішов невивіреним."""
        assert NOT_CONFIGURED.note == ""
        assert CHECK_FAILED.note

    @pytest.mark.asyncio
    async def test_no_api_key_means_no_check_and_no_loss(self):
        judge = PhotoJudge("")
        assert judge.configured is False
        v = await judge.judge("https://example.com/x.jpg", brand="Nike", title="t",
                              category="футболки", condition="Дуже добре", price_eur=12.0)
        assert v.ok and not v.checked
        await judge.close()

    @pytest.mark.asyncio
    async def test_listing_without_a_photo_is_not_dropped(self):
        judge = PhotoJudge("key")
        v = await judge.judge("", brand="Nike", title="t", category="футболки",
                              condition="Добре", price_eur=9.0)
        assert v.ok and not v.checked
        await judge.close()

    @pytest.mark.asyncio
    async def test_network_failure_lets_the_lot_through(self, monkeypatch):
        judge = PhotoJudge("key", min_interval=0.0)

        async def boom(*a, **k):
            raise RuntimeError("мережа впала")

        monkeypatch.setattr(judge._client, "get", boom)
        v = await judge.judge("https://example.com/x.jpg", brand="Nike", title="t",
                              category="футболки", condition="Добре", price_eur=9.0)
        assert v.ok and not v.checked and v.note
        assert judge.failed == 1
        await judge.close()


class TestCounters:
    @pytest.mark.asyncio
    async def test_rejections_are_counted(self, monkeypatch):
        judge = PhotoJudge("key", min_interval=0.0)

        class Resp:
            content = b"jpeg"
            def raise_for_status(self): pass

        async def fake_get(*a, **k):
            return Resp()

        async def fake_ask(*a, **k):
            return {"real_item": 1, "condition": 9, "photo_ok": 9, "flags": ["fake"]}

        monkeypatch.setattr(judge._client, "get", fake_get)
        monkeypatch.setattr(judge, "_ask", fake_ask)
        v = await judge.judge("u", brand="Nike", title="t", category="c",
                              condition="Добре", price_eur=9.0)
        assert not v.ok
        assert (judge.checked, judge.rejected, judge.failed) == (1, 1, 0)
        await judge.close()


class TestModelRotation:
    """Квота на безкоштовному тарифі рахується окремо для кожної моделі."""

    def test_first_ready_model_is_used(self):
        judge = PhotoJudge("key", models=["a", "b"])
        assert judge._ready_model() == "a"

    def test_model_in_quota_is_skipped_for_a_while(self):
        judge = PhotoJudge("key", models=["a", "b"])
        judge._rest("a", 5.0)
        assert judge._ready_model() == "b"

    def test_all_models_resting_means_none_ready(self):
        judge = PhotoJudge("key", models=["a", "b"])
        judge._rest("a", 5.0)
        judge._rest("b", 5.0)
        assert judge._ready_model() is None

    def test_rest_is_never_shorter_than_the_floor(self):
        """Google інколи каже 'зачекай 0с' — вірити цьому не варто."""
        import time as _t

        judge = PhotoJudge("key", models=["a"])
        judge._rest("a", 0.0)
        assert judge._cooldown["a"] - _t.monotonic() >= 7.0

    def test_quota_stretches_the_gap_and_success_walks_it_back(self):
        judge = PhotoJudge("key", min_interval=2.0, models=["a"])
        judge._rest("a", 5.0)
        assert judge.interval == 3.0
        for _ in range(30):
            judge._relax()
        assert judge.interval == 2.0

    def test_gap_growth_is_capped(self):
        judge = PhotoJudge("key", min_interval=2.0, models=["a"])
        for _ in range(20):
            judge._rest("a", 1.0)
        assert judge.interval == 16.0

    @pytest.mark.asyncio
    async def test_quota_on_one_model_retries_on_the_other(self, monkeypatch):
        from vintsniper.engine.vision import _RateLimited

        judge = PhotoJudge("key", min_interval=0.0, models=["busy", "free"])
        tried: list[str] = []

        class Resp:
            content = b"jpeg"
            def raise_for_status(self): pass

        async def fake_get(*a, **k):
            return Resp()

        async def ask(image, brand, title, category, condition, price, model):
            tried.append(model)
            if model == "busy":
                raise _RateLimited(1.0)
            return {"real_item": 9, "condition": 9, "photo_ok": 9}

        monkeypatch.setattr(judge._client, "get", fake_get)
        monkeypatch.setattr(judge, "_ask", ask)
        v = await judge.judge("u", brand="Nike", title="t", category="c",
                              condition="Добре", price_eur=9.0)
        assert v.ok and v.checked
        assert tried == ["busy", "free"]
        assert judge.checked == 1 and judge.failed == 0
        await judge.close()

    @pytest.mark.asyncio
    async def test_every_model_in_quota_lets_the_lot_through(self, monkeypatch):
        from vintsniper.engine.vision import _RateLimited

        judge = PhotoJudge("key", min_interval=0.0, models=["a", "b"])

        class Resp:
            content = b"jpeg"
            def raise_for_status(self): pass

        async def fake_get(*a, **k):
            return Resp()

        async def always_busy(*a, **k):
            raise _RateLimited(0.0)

        monkeypatch.setattr(judge._client, "get", fake_get)
        monkeypatch.setattr(judge, "_ask", always_busy)
        monkeypatch.setattr("asyncio.sleep", lambda *_: asyncio_noop())

        async def asyncio_noop():
            return None

        v = await judge.judge("u", brand="Nike", title="t", category="c",
                              condition="Добре", price_eur=9.0)
        assert v.ok and not v.checked
        assert judge.failed == 1
        await judge.close()


class TestRetryAfter:
    def test_reads_the_header(self):
        import httpx

        from vintsniper.engine.vision import _retry_after

        assert _retry_after(httpx.Response(429, headers={"retry-after": "12"})) == 12.0

    def test_reads_googles_json_detail(self):
        import httpx

        from vintsniper.engine.vision import _retry_after

        resp = httpx.Response(429, json={"error": {"details": [{"retryDelay": "31s"}]}})
        assert _retry_after(resp) == 31.0

    def test_silence_means_use_our_own_backoff(self):
        import httpx

        from vintsniper.engine.vision import _retry_after

        assert _retry_after(httpx.Response(429, json={"error": {}})) == 0.0
