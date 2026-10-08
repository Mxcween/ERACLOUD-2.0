from vintsniper.engine.pricing import PriceBook

NOW = 1_700_000_000


def fill(book: PriceBook, prices, *, brand=53, catalog=1206, bucket="very_good", ts=NOW):
    for price in prices:
        book.record(brand, catalog, bucket, price, ts)


class TestMedian:
    def test_uses_median_once_enough_samples(self):
        book = PriceBook(min_samples=5)
        fill(book, [10, 20, 30, 40, 50])
        est = book.estimate(
            brand_id=53, catalog_id=1206, bucket="very_good", now_ts=NOW,
            baseline_eur=99, condition_factor=1.0,
        )
        assert est.source == "median"
        assert est.value_eur == 30.0
        assert est.sample_size == 5

    def test_median_ignores_outlier_better_than_mean(self):
        book = PriceBook(min_samples=5)
        fill(book, [20, 22, 24, 26, 900])
        est = book.estimate(
            brand_id=53, catalog_id=1206, bucket="very_good", now_ts=NOW,
            baseline_eur=99, condition_factor=1.0,
        )
        assert est.value_eur == 24.0

    def test_falls_back_to_baseline_when_too_few(self):
        book = PriceBook(min_samples=8)
        fill(book, [10, 20, 30])
        est = book.estimate(
            brand_id=53, catalog_id=1206, bucket="very_good", now_ts=NOW,
            baseline_eur=50, condition_factor=1.15,
        )
        assert est.source == "baseline"
        assert est.value_eur == 57.5
        assert est.is_measured is False


class TestConditionPooling:
    def test_pools_other_conditions_with_factor(self):
        book = PriceBook(min_samples=4)
        fill(book, [40, 50, 60, 70], bucket="very_good")
        est = book.estimate(
            brand_id=53, catalog_id=1206, bucket="good", now_ts=NOW,
            baseline_eur=200, condition_factor=0.8,
            all_buckets=["new", "very_good", "good"],
        )
        assert est.source == "median_any_condition"
        assert est.value_eur == 44.0  # медіана 55 * 0.8
        assert est.is_measured is True


class TestWindow:
    def test_stale_observations_drop_out(self):
        book = PriceBook(min_samples=3, window_seconds=86400)
        fill(book, [10, 20, 30])
        fresh = book.estimate(
            brand_id=53, catalog_id=1206, bucket="very_good", now_ts=NOW,
            baseline_eur=99, condition_factor=1.0,
        )
        assert fresh.source == "median"

        later = book.estimate(
            brand_id=53, catalog_id=1206, bucket="very_good", now_ts=NOW + 200_000,
            baseline_eur=99, condition_factor=1.0,
        )
        assert later.source == "baseline"

    def test_window_size_caps_memory(self):
        book = PriceBook(window_size=10, min_samples=3)
        fill(book, range(100))
        assert book.total_observations == 10

    def test_under_sampled_reports_thin_pairs(self):
        book = PriceBook(min_samples=5)
        fill(book, [10, 20, 30])
        thin = book.under_sampled([(53, 1206), (999, 1206)], ["very_good"], NOW)
        assert (999, 1206) in thin
        assert (53, 1206) in thin


class TestCapacity:
    def test_has_capacity_until_window_is_full(self):
        book = PriceBook(window_size=5, min_samples=3)
        for i in range(4):
            assert book.has_capacity(53, 1206, "very_good", NOW) is True
            book.record(53, 1206, "very_good", 10 + i, NOW)
        book.record(53, 1206, "very_good", 99, NOW)
        assert book.has_capacity(53, 1206, "very_good", NOW) is False

    def test_capacity_returns_with_stale_data(self):
        book = PriceBook(window_size=3, min_samples=2, window_seconds=1000)
        for _ in range(3):
            book.record(53, 1206, "very_good", 10, NOW)
        assert book.has_capacity(53, 1206, "very_good", NOW) is False
        # старі спостереження випали з вікна, місце звільнилось
        assert book.has_capacity(53, 1206, "very_good", NOW + 5000) is True

    def test_unknown_key_always_has_capacity(self):
        book = PriceBook(window_size=1, min_samples=1)
        assert book.has_capacity(999, 999, "new", NOW) is True
