from vintsniper.engine.ranges import PriceRange, suggestions


def test_parses_plain_range():
    assert PriceRange.parse("0-15") == PriceRange(0, 15)
    assert PriceRange.parse("15 45") == PriceRange(15, 45)
    # Кома це роздільник між межами, а не десяткова крапка
    assert PriceRange.parse("15,45") == PriceRange(15, 45)


def test_swaps_reversed_bounds():
    assert PriceRange.parse("45-15") == PriceRange(15, 45)


def test_open_upper_bound():
    for text in ("45+", "від 45", ">45", "from 45"):
        assert PriceRange.parse(text) == PriceRange(45, None), text


def test_open_lower_bound():
    for text in ("до 30", "<30", "-30", "under 30"):
        assert PriceRange.parse(text) == PriceRange(0, 30), text


def test_bare_number_is_a_ceiling():
    """«/range 30» це «покажи мені до 30», а не «від 30»."""
    assert PriceRange.parse("30") == PriceRange(0, 30)


def test_words_that_remove_the_filter():
    for text in ("", "all", "всі", "any", "*"):
        assert PriceRange.parse(text).is_open, text


def test_garbage_returns_none():
    assert PriceRange.parse("хуйня") is None
    assert PriceRange.parse("10-20-30") is None


def test_currency_noise_is_ignored():
    assert PriceRange.parse("15€ - 45 eur") == PriceRange(15, 45)


def test_contains_is_inclusive_on_both_ends():
    r = PriceRange(15, 45)
    assert not r.contains(14.99)
    assert r.contains(15) and r.contains(30) and r.contains(45)
    assert not r.contains(45.01)


def test_open_range_takes_everything():
    r = PriceRange.open()
    assert r.contains(0) and r.contains(999)
    assert r.label == "all"


def test_label_round_trips_through_parse():
    for r in (PriceRange(0, 15), PriceRange(15, 45), PriceRange(45, None), PriceRange.open()):
        assert PriceRange.parse(r.label) == r


def test_suggestions_cover_the_discord_bounds():
    assert suggestions([15, 45]) == ["0-15", "15-45", "45+"]
    assert suggestions([]) == ["all"]
