"""Бюджет трафіку: щоб місячного ліміту вистачило до кінця місяця.

Безкоштовний Render дає 100 ГБ на місяць. Після переходу на сторінки
каталогу бот при циклі 84 секунди витрачав близько 111 ГБ - і сервіс
зупинили серед місяця. Поставити фіксовано довший цикл можна, але це погана
угода: на початку місяця бот їхав би повільніше, ніж йому дозволено, а
будь-яка зміна на боці Vinted (важчі сторінки, більше категорій) знову
вибила б за ліміт, і дізнались би ми про це тим самим способом - зупинкою.

Тому темп рахується з двох часток: скільки бюджету вже з'їдено і скільки
місяця вже минуло. Поки перша менша за другу, бот іде на повній. Щойно вона
починає випереджати - пауза між циклами розтягується рівно настільки,
наскільки ми забігли вперед. Так бюджет витрачається рівномірно й гарантовано
доживає до кінця місяця.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timezone


class TrafficBudget:
    def __init__(
        self,
        *,
        monthly_gb: float,
        max_slowdown: float = 8.0,
        grace_bytes: int = 200 * 1024 * 1024,
    ) -> None:
        self.monthly_bytes = int(monthly_gb * 1_000_000_000)
        self.max_slowdown = max_slowdown
        # Поки витрачено менше цього, темп не чіпаємо взагалі: на старті
        # місяця частка минулого часу близька до нуля, і будь-яке ділення на
        # неї дає дику цифру з кількох мегабайтів.
        self.grace_bytes = grace_bytes
        self.used_bytes = 0
        self.month = ""

    # ------------------------------------------------------------- облік

    def adopt(self, month: str, used_bytes: int) -> None:
        """Підхоплює збережений лічильник. Чужий місяць - починаємо з нуля."""
        now = self.current_month()
        self.month = now
        self.used_bytes = int(used_bytes) if month == now else 0

    def add(self, nbytes: int, *, now: datetime | None = None) -> None:
        month = self.current_month(now)
        if month != self.month:
            # Місяць змінився - ліміт обнулився разом з ним
            self.month = month
            self.used_bytes = 0
        self.used_bytes += max(0, int(nbytes))

    @staticmethod
    def current_month(now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        return f"{now.year:04d}-{now.month:02d}"

    # -------------------------------------------------------------- темп

    @staticmethod
    def month_progress(now: datetime | None = None) -> float:
        """Яка частка місяця вже минула, від 0 до 1."""
        now = now or datetime.now(timezone.utc)
        days = calendar.monthrange(now.year, now.month)[1]
        seconds_in_month = days * 86400
        passed = (
            (now.day - 1) * 86400 + now.hour * 3600 + now.minute * 60 + now.second
        )
        return min(1.0, max(0.0, passed / seconds_in_month))

    @property
    def share_used(self) -> float:
        if self.monthly_bytes <= 0:
            return 0.0
        return self.used_bytes / self.monthly_bytes

    def slowdown(self, now: datetime | None = None) -> float:
        """У скільки разів розтягнути паузу між циклами.

        1.0 означає "йди на повній". Більше - рівно настільки, наскільки
        витрата випереджає календар.
        """
        if self.monthly_bytes <= 0 or self.used_bytes <= self.grace_bytes:
            return 1.0
        elapsed = self.month_progress(now)
        if elapsed <= 0.0:
            return 1.0

        # Рахуємо ЗАЛИШОК на залишок часу, а не середнє перевитрачання за
        # минуле. Різниця принципова: 85 ГБ із 90 витрачено на 20-е число -
        # за середнім це лише півтора раза перевитрати, а насправді на
        # останні одинадцять днів лишилось пʼять гігабайтів, і йти треба
        # вдесятеро повільніше. Перша версія цієї формули дала б 1.5 і
        # спокійно вибрала б ліміт за добу.
        left_budget = max(0.0, 1.0 - self.share_used)
        left_time = max(1e-6, 1.0 - elapsed)
        if left_budget <= 0.0:
            return self.max_slowdown
        allowed_rate = left_budget / left_time
        current_rate = self.share_used / elapsed
        return min(self.max_slowdown, max(1.0, current_rate / allowed_rate))

    def stats(self) -> dict[str, float | str]:
        elapsed = self.month_progress()
        return {
            "month": self.month or self.current_month(),
            "used_gb": round(self.used_bytes / 1_000_000_000, 2),
            "budget_gb": round(self.monthly_bytes / 1_000_000_000, 1),
            "used_share": round(self.share_used, 3),
            "month_share": round(elapsed, 3),
            "slowdown": round(self.slowdown(), 2),
        }
