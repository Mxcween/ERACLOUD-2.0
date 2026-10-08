"""Планка жиру.

Пороги вигоди відповідають на питання "чи це вигідно", і вигідного на Vinted
багато: якщо слати все прохідне, справді жирна знахідка тоне серед десятка
посередніх. Тут перевіряємо друге питання - "чи це краще за звичайне".
"""
from __future__ import annotations

from vintsniper.engine.fat import FatGate, _percentile


class TestPercentile:
    def test_matches_hand_computed_values(self):
        data = [10, 20, 30, 40, 50]
        assert _percentile(data, 0) == 10
        assert _percentile(data, 100) == 50
        assert _percentile(data, 50) == 30

    def test_interpolates_between_samples(self):
        assert _percentile([10, 20], 50) == 15

    def test_survives_an_empty_window(self):
        assert _percentile([], 80) == 0.0


class TestFloorBeforeItHasLearned:
    """Поки знахідок мало, працює сама підлога.

    Рахувати перцентиль по трьох числах означає видавати випадковість за
    статистику: одна дрібна знахідка на старті опустила б планку на дно.
    """

    def test_a_small_find_is_rejected_by_the_floor(self):
        gate = FatGate(floor_eur=20, warmup_samples=40)
        gate.observe(5.0)
        ok, note = gate.verdict(7.0)
        assert not ok
        assert "20" in note

    def test_a_fat_find_passes_even_before_warmup(self):
        gate = FatGate(floor_eur=20, warmup_samples=40)
        ok, _ = gate.verdict(55.0)
        assert ok

    def test_the_bar_is_the_floor_until_ready(self):
        gate = FatGate(floor_eur=20, warmup_samples=40)
        for _ in range(10):
            gate.observe(200.0)
        assert not gate.ready
        assert gate.bar_eur == 20


class TestTheBarFollowsTheMarket:
    def test_a_rich_stream_lifts_the_bar(self):
        """У багатий день "звичайне" вище, тож і планка вища."""
        gate = FatGate(percentile=80, floor_eur=20, warmup_samples=10)
        for value in range(50, 150):  # профіти 50..149
            gate.observe(float(value))
        assert gate.ready
        assert gate.bar_eur > 100, gate.bar_eur
        assert not gate.verdict(60.0)[0], "60 у такому потоці не жир"
        assert gate.verdict(140.0)[0]

    def test_a_lean_stream_does_not_drop_below_the_floor(self):
        gate = FatGate(percentile=80, floor_eur=20, warmup_samples=10)
        for _ in range(50):
            gate.observe(3.0)
        assert gate.bar_eur == 20, "підлога має тримати планку знизу"
        assert not gate.verdict(9.0)[0]

    def test_the_window_forgets_old_days(self):
        """Планка має йти за ринком, а не пам'ятати торішній жир."""
        gate = FatGate(percentile=80, window=20, floor_eur=1, warmup_samples=5)
        for _ in range(20):
            gate.observe(500.0)
        rich = gate.bar_eur
        for _ in range(20):
            gate.observe(10.0)
        assert gate.bar_eur < rich / 10


class TestCounters:
    def test_verdicts_are_counted_for_health(self):
        gate = FatGate(floor_eur=20, warmup_samples=0)
        gate.observe(20.0)
        gate.verdict(50.0)
        gate.verdict(1.0)
        stats = gate.stats()
        assert stats["passed"] == 1
        assert stats["rejected"] == 1
        assert stats["samples"] == 1
