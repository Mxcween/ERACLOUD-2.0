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
