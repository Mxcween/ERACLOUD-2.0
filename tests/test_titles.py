from vintsniper.engine.titles import find_word, normalise

BLOCK = ["patch", "naszywka", "sznurowki", "box only", "metka", "laces", "dust bag"]


class TestWholeWordsOnly:
    def test_does_not_match_inside_another_word(self):
        """Класична пастка: "tag" усередині "vintage"."""
        assert find_word("Vintage Nike jacket", ["tag"]) is None
        assert find_word("Dispatch bag Nike", ["patch"]) is None
        assert find_word("Klapki", ["lapki"]) is None

    def test_matches_standalone_word(self):
        assert find_word("Patch CP Company", BLOCK) == "patch"
        assert find_word("naszywka Stone Island", BLOCK) == "naszywka"

    def test_plural_is_a_separate_entry(self):
        assert find_word("patches everywhere", ["patch"]) is None
        assert find_word("patches everywhere", ["patch", "patches"]) == "patches"


class TestDiacritics:
    def test_ignores_accents(self):
        assert find_word("Różowe sznurówki BAPE", BLOCK) == "sznurowki"
        assert find_word("SZNURÓWKI", BLOCK) == "sznurowki"

    def test_normalise_strips_marks(self):
        assert normalise("Stüssy") == "stussy"
        assert normalise("Aimé") == "aime"


class TestPhrases:
    def test_multiword_phrase(self):
        assert find_word("Nike Air Max box only, no shoes", BLOCK) == "box only"
        assert find_word("Gucci dust bag original", BLOCK) == "dust bag"

    def test_longer_phrase_wins_over_shorter(self):
        found = find_word("box only", ["box", "box only"])
        assert found == "box only"


class TestRealListings:
    """Назви, які бот справді підняв із Vinted і оцінив хибно."""

    def test_cp_company_patch_is_caught(self):
        assert find_word("Patch CP Company", BLOCK) is not None

    def test_bape_laces_are_caught(self):
        assert find_word("Rozowe sznurowki BAPE", BLOCK) is not None

    def test_genuine_listings_pass(self):
        for title in [
            "Corteiz Windbreaker",
            "Carhartt WIP kurtka detroit",
            "Nike Tiempo Legend 10 Club maat 45",
            "adidas Adilette Grosse 40,5 neu blau",
            "Vintage Nike windbreaker",
        ]:
            assert find_word(title, BLOCK) is None, title


class TestEmptyInput:
    def test_no_words_configured(self):
        assert find_word("cokolwiek", []) is None
        assert find_word("cokolwiek", None) is None

    def test_empty_title(self):
        assert find_word("", BLOCK) is None
