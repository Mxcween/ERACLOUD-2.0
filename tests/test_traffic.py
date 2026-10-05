"""Бюджет трафіку.

Безкоштовний Render дає 100 ГБ на місяць. При циклі 84 секунди бот витрачав
близько 111 і сервіс зупинили серед місяця - саме це цей бюджет і має
унеможливити.
"""
from __future__ import annotations

from datetime import datetime, timezone

from vintsniper.engine.traffic import TrafficBudget

GB = 1_000_000_000


def at(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, 0, 0, tzinfo=timezone.utc)


class TestPace:
    def test_on_pace_runs_at_full_speed(self):
        """Рівно за графіком - жодного гальмування.

        Жовтень має 31 день, тому "половина місяця" це 17-е число, а не 16-е:
        на 16-му витрата в половину бюджету вже на 3% випереджає календар, і
        бот чесно віддає 1.03. Це не похибка, це і є робота бюджету.
        """
        b = TrafficBudget(monthly_gb=100)
        b.add(50 * GB)
        assert b.slowdown(at(17)) == 1.0

    def test_being_slightly_ahead_slows_slightly(self):
        b = TrafficBudget(monthly_gb=100)
        b.add(50 * GB)
        assert 1.0 < b.slowdown(at(16)) < 1.1

    def test_under_pace_also_runs_at_full_speed(self):
        """Витратили менше, ніж могли - прискорюватись не треба, але й гальмувати ні."""
        b = TrafficBudget(monthly_gb=100)
        b.add(20 * GB)
        assert b.slowdown(at(16)) == 1.0

    def test_running_ahead_slows_down(self):
        b = TrafficBudget(monthly_gb=100)
        b.add(50 * GB)                      # половина бюджету за чверть місяця
        assert b.slowdown(at(8, 12)) > 1.5

    def test_nearly_exhausted_slows_down_hard(self):
        """Головний випадок, заради якого бюджет і існує.

        85 ГБ із 90 витрачено на 20-е число. За середнім перевитрачанням це
        лише півтора раза - і саме так рахувала перша версія формули, яка
        спокійно дала б вибрати ліміт за добу. Насправді на останні
        одинадцять днів лишилось пʼять гігабайтів, тобто йти треба на
        порядок повільніше.
        """
        b = TrafficBudget(monthly_gb=90, max_slowdown=8.0)
        b.add(85 * GB)
        assert b.slowdown(at(20, 12)) >= 7.0

    def test_an_exhausted_budget_goes_to_the_floor(self):
        b = TrafficBudget(monthly_gb=90, max_slowdown=8.0)
        b.add(95 * GB)
        assert b.slowdown(at(20)) == 8.0

    def test_slowdown_has_a_ceiling(self):
        b = TrafficBudget(monthly_gb=100, max_slowdown=8.0)
        b.add(99 * GB)
        assert b.slowdown(at(2)) == 8.0

    def test_a_few_megabytes_at_month_start_do_not_panic(self):
        """На першій хвилині місяця частка часу близька до нуля.

        Без запобіжника кілька мегабайтів давали б ділення на майже нуль і
        бот глушив би сам себе з перших хвилин.
        """
        b = TrafficBudget(monthly_gb=100)
        b.add(50 * 1024 * 1024)
        assert b.slowdown(at(1, 0)) == 1.0


class TestMonthRollover:
    def test_a_new_month_resets_the_counter(self):
        b = TrafficBudget(monthly_gb=100)
        b.adopt("2026-09", 90 * GB)
        assert b.used_bytes == 0, "ліміт обнуляється разом з місяцем"

    def test_the_same_month_is_picked_up(self):
        b = TrafficBudget(monthly_gb=100)
        b.adopt(TrafficBudget.current_month(), 42 * GB)
        assert b.used_bytes == 42 * GB

    def test_adding_in_a_new_month_starts_over(self):
        b = TrafficBudget(monthly_gb=100)
        b.adopt("2026-10", 90 * GB)
        b.month = "2026-09"            # імітуємо, що лічильник з минулого місяця
        b.add(1 * GB)
        assert b.used_bytes == 1 * GB


class TestProgress:
    def test_month_progress_spans_zero_to_one(self):
        assert TrafficBudget.month_progress(at(1, 0)) == 0.0
        assert 0.48 < TrafficBudget.month_progress(at(16, 12)) < 0.52
        assert TrafficBudget.month_progress(at(31, 23)) < 1.0

    def test_a_disabled_budget_never_slows_anything(self):
        b = TrafficBudget(monthly_gb=0)
        b.add(500 * GB)
        assert b.slowdown(at(2)) == 1.0
