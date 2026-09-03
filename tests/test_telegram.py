from vintsniper.notify.telegram import TelegramNotifier
from vintsniper.settings import TelegramSettings


def notifier(**overrides):
    defaults = dict(bot_token="123:ABC", chat_id_top="", chat_id_all="")
    defaults.update(overrides)
    return TelegramNotifier(TelegramSettings(**defaults), dry_run=True)


class TestChatAdoption:
    def test_starts_without_a_target(self):
        assert notifier().has_target is False

    def test_adopts_first_chat_that_writes(self):
        n = notifier()
        assert n.adopt_chat("555") is True
        assert n.has_target is True
        assert n.chat_top == "555"
        assert n.chat_all == "555"

    def test_does_not_override_configured_chat(self):
        n = notifier(chat_id_top="111", chat_id_all="222")
        assert n.adopt_chat("555") is False
        assert n.chat_top == "111"
        assert n.chat_all == "222"

    def test_ignores_empty_chat_id(self):
        n = notifier()
        assert n.adopt_chat("") is False
        assert n.has_target is False


class TestChannelRouting:
    def test_top_and_all_go_to_separate_chats(self):
        n = notifier(chat_id_top="111", chat_id_all="222")
        assert n._target("top")[0] == "111"
        assert n._target("all")[0] == "222"

    def test_single_chat_receives_both_channels(self):
        n = notifier(chat_id_top="111", chat_id_all="111")
        assert n._target("top")[0] == "111"
        assert n._target("all")[0] == "111"

    def test_topics_are_passed_through(self):
        n = notifier(chat_id_top="111", chat_id_all="111", topic_id_top=5, topic_id_all=9)
        assert n._target("top") == ("111", 5)
        assert n._target("all") == ("111", 9)


class TestDeliveryHonesty:
    """Регресія: без відомого чату відправка НЕ вважається успішною.

    Раніше "сухий прогін" і "чат ще невідомий" ділили одну гілку і обидва
    повертали True. Через це знахідка писалась у базу як доставлена, лот
    ішов у список переглянутих, і людина її ніколи не бачила.
    """

    def _deal(self, listing_factory, settings, registry):
        from vintsniper.engine.filters import Candidate
        from vintsniper.engine.pricing import PriceBook
        from vintsniper.engine.scoring import evaluate

        now = 1_700_000_000
        book = PriceBook(min_samples=4)
        for _ in range(10):
            book.record(53, 1206, "very_good", 100.0, now)
        cand = Candidate(
            listing=listing_factory(),
            brand=registry.by_title("Nike"),
            category=settings.category_by_id(1206),
            price_eur=20.0,
            bucket="very_good",
        )
        return evaluate(cand, settings=settings, price_book=book, shipping_eur=3.5, now_ts=now)

    async def test_returns_false_when_chat_unknown(
        self, listing_factory, settings, registry
    ):
        n = TelegramNotifier(
            TelegramSettings(bot_token="123:ABC", chat_id_top="", chat_id_all=""),
            dry_run=False,
        )
        try:
            assert await n.send_deal(self._deal(listing_factory, settings, registry)) is False
        finally:
            await n.close()

    async def test_dry_run_still_reports_success(
        self, listing_factory, settings, registry
    ):
        n = TelegramNotifier(
            TelegramSettings(bot_token="123:ABC", chat_id_top="", chat_id_all=""),
            dry_run=True,
        )
        try:
            assert await n.send_deal(self._deal(listing_factory, settings, registry)) is True
        finally:
            await n.close()
