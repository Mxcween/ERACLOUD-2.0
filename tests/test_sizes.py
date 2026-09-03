from vintsniper.engine.sizes import clothing_size, shoe_size_eu


class TestClothingSize:
    def test_extracts_letter_from_compound_size(self):
        assert clothing_size("M / 38 / 10") == "M"
        assert clothing_size("XL / 54 / 18") == "XL"

    def test_plain_size(self):
        assert clothing_size("L") == "L"
        assert clothing_size("xs") == "XS"

    def test_numeric_and_empty_are_not_letter_sizes(self):
        assert clothing_size("") is None
        assert clothing_size("48 | W32") is None
        assert clothing_size("42") is None


class TestShoeSize:
    def test_parses_eu_sizes(self):
        assert shoe_size_eu("43") == 43.0
        assert shoe_size_eu("40,5") == 40.5
        assert shoe_size_eu("44.5") == 44.5

    def test_rejects_values_outside_eu_range(self):
        # 9 це US/UK, а не європейський розмір
        assert shoe_size_eu("9") is None
        assert shoe_size_eu("") is None
        assert shoe_size_eu("без розміру") is None
