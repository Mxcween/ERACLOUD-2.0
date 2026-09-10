from vintsniper.engine.schedule import in_quiet_hours


class TestQuietHours:
    def test_no_windows_means_always_on(self):
        assert in_quiet_hours([], 3) is False
        assert in_quiet_hours(None, 3) is False

    def test_simple_window(self):
        windows = [[1, 8]]
        assert in_quiet_hours(windows, 0) is False
        assert in_quiet_hours(windows, 1) is True
        assert in_quiet_hours(windows, 7) is True
        assert in_quiet_hours(windows, 8) is False

    def test_window_crossing_midnight(self):
        windows = [[22, 6]]
        assert in_quiet_hours(windows, 23) is True
        assert in_quiet_hours(windows, 0) is True
        assert in_quiet_hours(windows, 5) is True
        assert in_quiet_hours(windows, 6) is False
        assert in_quiet_hours(windows, 12) is False

    def test_multiple_windows(self):
        windows = [[1, 6], [13, 14]]
        assert in_quiet_hours(windows, 3) is True
        assert in_quiet_hours(windows, 13) is True
        assert in_quiet_hours(windows, 10) is False

    def test_malformed_window_is_skipped(self):
        assert in_quiet_hours([["a", "b"], [1, 8]], 3) is True
        assert in_quiet_hours([[5]], 5) is False

    def test_equal_bounds_disables_window(self):
        assert in_quiet_hours([[3, 3]], 3) is False
