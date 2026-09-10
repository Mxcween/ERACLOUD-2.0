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


class TestWaistSizes:
    """Штани Vinted міряє талією, а не буквами.

    Заміряно на живому потоці: "W32 | DE 48", "46 | W30", "50 | W34" - для
    буквеного фільтра це просто не розмір, тож джинси й штани качались з
    Vinted і одразу відкидались. Ціла категорія працювала вхолосту.
    """

    def test_reads_inches_and_european_together(self):
        from vintsniper.engine.sizes import waist_sizes

        assert waist_sizes("W32 | DE 48") == (32, 48)
        assert waist_sizes("46 | W30") == (30, 46)
        assert waist_sizes("50 | W34") == (34, 50)

    def test_inches_alone(self):
        from vintsniper.engine.sizes import waist_sizes

        assert waist_sizes("W36") == (36, None)

    def test_european_alone(self):
        from vintsniper.engine.sizes import waist_sizes

        assert waist_sizes("52") == (None, 52)

    def test_no_waist_in_a_letter_size(self):
        from vintsniper.engine.sizes import waist_sizes

        assert waist_sizes("M") == (None, None)
        assert waist_sizes("") == (None, None)

    def test_inches_are_not_mistaken_for_european(self):
        """Дюйми 26-40 і європейські 40-60 не мають плутатись між собою."""
        from vintsniper.engine.sizes import waist_sizes

        inches, eu = waist_sizes("W48")
        assert inches == 48
        assert eu is None, "те саме число не має рахуватись двічі"
