from vintsniper.engine.fx import FxConverter


class TestConversion:
    def test_converts_using_fallback_rates(self):
        fx = FxConverter({"EUR": 1.0, "PLN": 4.30})
        assert fx.to_eur(43.0, "PLN") == 10.0
        assert fx.to_eur(10.0, "EUR") == 10.0

    def test_round_trip(self):
        fx = FxConverter({"EUR": 1.0, "PLN": 4.0})
        assert fx.from_eur(fx.to_eur(100.0, "PLN"), "PLN") == 100.0

    def test_unknown_currency_falls_back_to_one_to_one(self):
        fx = FxConverter({"EUR": 1.0})
        assert fx.to_eur(25.0, "XYZ") == 25.0

    def test_case_insensitive(self):
        fx = FxConverter({"EUR": 1.0, "PLN": 4.0})
        assert fx.to_eur(40.0, "pln") == 10.0

    def test_starts_offline_and_needs_refresh(self):
        fx = FxConverter({"EUR": 1.0, "PLN": 4.0})
        assert fx.is_live is False
        assert fx.needs_refresh() is True


class TestRefreshBookkeeping:
    def test_needs_refresh_before_first_fetch(self):
        """На щойно завантаженій машині monotonic() малий: перевірка має це пережити."""
        fx = FxConverter({"EUR": 1.0, "PLN": 4.0}, refresh_hours=12)
        assert fx.needs_refresh() is True

    def test_no_refresh_needed_right_after_fetch(self, monkeypatch):
        fx = FxConverter({"EUR": 1.0, "PLN": 4.0}, refresh_hours=12)
        fx._fetched_at = 1000.0
        monkeypatch.setattr("vintsniper.engine.fx.time.monotonic", lambda: 1010.0)
        assert fx.needs_refresh() is False

    def test_refresh_needed_after_window(self, monkeypatch):
        fx = FxConverter({"EUR": 1.0, "PLN": 4.0}, refresh_hours=1)
        fx._fetched_at = 1000.0
        monkeypatch.setattr("vintsniper.engine.fx.time.monotonic", lambda: 1000.0 + 3601)
        assert fx.needs_refresh() is True
