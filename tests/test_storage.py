import pytest

from vintsniper.storage.db import build_engine, build_session_factory
from vintsniper.storage.repo import Repository


@pytest.fixture
def repo():
    return Repository(build_session_factory(build_engine("sqlite:///:memory:")))


class TestDeduplication:
    def test_first_sighting_returns_all(self, repo):
        assert repo.filter_unseen("PL", [1, 2, 3], 100) == {1, 2, 3}

    def test_second_sighting_returns_nothing(self, repo):
        repo.filter_unseen("PL", [1, 2, 3], 100)
        assert repo.filter_unseen("PL", [1, 2, 3], 200) == set()

    def test_only_new_ids_come_back(self, repo):
        repo.filter_unseen("PL", [1, 2], 100)
        assert repo.filter_unseen("PL", [2, 3], 200) == {3}

    def test_markets_are_independent(self, repo):
        repo.filter_unseen("PL", [1], 100)
        assert repo.filter_unseen("DE", [1], 100) == {1}

    def test_empty_input(self, repo):
        assert repo.filter_unseen("PL", [], 100) == set()


class TestObservations:
    def test_store_and_load(self, repo):
        repo.add_observations([(53, 1206, "very_good", 40.0, "PL", 100)])
        rows = repo.load_observations(0)
        assert rows == [(53, 1206, "very_good", 40.0, 100)]

    def test_load_respects_cutoff(self, repo):
        repo.add_observations(
            [(53, 1206, "very_good", 40.0, "PL", 100), (53, 1206, "very_good", 50.0, "PL", 500)]
        )
        assert len(repo.load_observations(300)) == 1

    def test_prune_drops_old(self, repo):
        repo.add_observations([(53, 1206, "very_good", 40.0, "PL", 100)])
        assert repo.prune_observations(300) == 1
        assert repo.load_observations(0) == []


class TestMuting:
    def test_mute_and_unmute(self, repo):
        repo.mute_brand(53, "Nike", 100)
        assert repo.muted_brand_ids() == {53}
        assert repo.unmute_brand(53) is True
        assert repo.muted_brand_ids() == set()

    def test_unmute_unknown_brand_is_noop(self, repo):
        assert repo.unmute_brand(999) is False

    def test_mute_is_idempotent(self, repo):
        repo.mute_brand(53, "Nike", 100)
        repo.mute_brand(53, "Nike", 200)
        assert repo.muted_brand_ids() == {53}


class TestState:
    def test_set_and_get(self, repo):
        repo.set_state("telegram_offset", "42")
        assert repo.get_state("telegram_offset") == "42"

    def test_overwrite(self, repo):
        repo.set_state("k", "1")
        repo.set_state("k", "2")
        assert repo.get_state("k") == "2"

    def test_missing_key(self, repo):
        assert repo.get_state("nope") is None


class TestChatAdoption:
    def test_chat_id_survives_restart(self, repo):
        repo.set_state("chat_id_top", "-1001234567890")
        assert repo.get_state("chat_id_top") == "-1001234567890"


class TestDuplicateIdsInOneBatch:
    def test_repeated_id_does_not_break_the_batch(self, repo):
        """Дубль усередині пачки не повинен зносити всю вставку."""
        result = repo.filter_unseen("PL", [1, 2, 2, 3, 1], 100)
        assert result == {1, 2, 3}

    def test_those_ids_are_remembered(self, repo):
        repo.filter_unseen("PL", [1, 1, 2], 100)
        assert repo.filter_unseen("PL", [1, 2, 3], 200) == {3}


class TestDatabaseUrlFromAHost:
    """Рядок від Neon чи Supabase має працювати без правок руками.

    Вони видають postgres:// або postgresql://, а SQLAlchemy за таким
    рядком шукає psycopg2, якого в нас нема. Власник вставить рядок як є, і
    без цього бот падав би на старті.
    """

    def test_bare_postgres_scheme_gets_a_driver(self):
        from vintsniper.storage.db import normalise_url

        assert normalise_url("postgres://u:p@host/db") == "postgresql+psycopg://u:p@host/db"
        assert normalise_url("postgresql://u:p@host/db") == "postgresql+psycopg://u:p@host/db"

    def test_an_explicit_driver_is_left_alone(self):
        from vintsniper.storage.db import normalise_url

        url = "postgresql+psycopg://u:p@host/db"
        assert normalise_url(url) == url

    def test_sqlite_is_untouched(self):
        from vintsniper.storage.db import normalise_url

        assert normalise_url("sqlite:///data/x.db") == "sqlite:///data/x.db"

    def test_query_string_survives(self):
        from vintsniper.storage.db import normalise_url

        got = normalise_url("postgresql://u:p@host/db?sslmode=require")
        assert got.endswith("?sslmode=require")
