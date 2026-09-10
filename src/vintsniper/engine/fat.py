"""Фільтр жиру: пропускає лише найкращі знахідки, а не всі прохідні.

Пороги в scoring відповідають на питання "чи це вигідно". На них бот і
будувався, і вони працюють: збиткового не пропустять. Але вигідних лотів на
Vinted багато, і якщо слати всі, стрічка перетворюється на потік, у якому
справді жирна знахідка нічим не виділяється серед десятка посередніх.

Тут відповідь на інше питання: "чи це КРАЩЕ за те, що зазвичай трапляється".
Планка не задана числом, а рахується з самого потоку - беремо профіт останніх
знахідок і пускаємо лише верхній хвіст. Так вона сама піднімається в багатий
день і сама опускається в бідний, і її не треба підкручувати руками щоразу,
як ринок змінився.

Абсолютна підлога лишається окремо: перцентиль каже "краще за звичайне", але
в зовсім порожню добу звичайне може бути дрібним, і верхні 20% від дрібного
це все одно дрібне.
"""
from __future__ import annotations

from collections import deque


class FatGate:
    def __init__(
        self,
        *,
        percentile: float = 80.0,
        window: int = 200,
        floor_eur: float = 20.0,
        warmup_samples: int = 40,
    ) -> None:
        self.percentile = max(0.0, min(100.0, percentile))
        self.floor_eur = floor_eur
        self.warmup_samples = max(0, warmup_samples)
        self._seen: deque[float] = deque(maxlen=max(1, window))
        self.passed = 0
        self.rejected = 0

    def observe(self, profit_eur: float) -> None:
        """Запам'ятовує знахідку, щоб планка знала, що таке "звичайне".

        Сюди йде КОЖНА знахідка, яка пройшла пороги вигоди, а не тільки
        відправлена. Інакше вибірка складалась би з самих переможців і
        планка повзла б угору, поки не перекрила б потік повністю.
        """
        self._seen.append(profit_eur)

    @property
    def ready(self) -> bool:
        return len(self._seen) >= self.warmup_samples

    @property
    def bar_eur(self) -> float:
        """Поточна планка: більша з абсолютної підлоги і перцентиля."""
        if not self.ready:
            return self.floor_eur
        return max(self.floor_eur, _percentile(self._seen, self.percentile))

    def verdict(self, profit_eur: float) -> tuple[bool, str]:
        """Пропускати? І короткий підпис для алерта чи лога."""
        bar = self.bar_eur
        if profit_eur >= bar:
            self.passed += 1
            if self.ready:
                return True, f"жир: +{profit_eur:.0f}€ проти планки {bar:.0f}€"
            return True, ""
        self.rejected += 1
        return False, f"дрібне: +{profit_eur:.0f}€ при планці {bar:.0f}€"

    def stats(self) -> dict[str, float | int | bool]:
        return {
            "bar_eur": round(self.bar_eur, 1),
            "samples": len(self._seen),
            "ready": self.ready,
            "passed": self.passed,
            "rejected": self.rejected,
        }


def _percentile(values: deque[float] | list[float], pct: float) -> float:
    """Перцентиль з лінійною інтерполяцією.

    Свій, бо тягнути numpy заради одного рядка в контейнер на безкоштовному
    тарифі - погана угода.
    """
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (pct / 100.0)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight
