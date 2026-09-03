"""Оцінка ринкової ціни бренду.

Логіка проста і чесна: ми весь час бачимо свіжі оголошення по наших брендах,
складаємо їхні ціни у вікно і беремо медіану. Медіана стійкіша за середнє,
бо один продавець із Stone Island за 900 євро не перекошує картину.

Поки спостережень мало, працює базова оцінка з categories.yaml, помножена на
поправку за стан речі. Тобто бот корисний з першої хвилини, а з часом
стає точнішим.
"""
from __future__ import annotations

import logging
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Iterable

log = logging.getLogger(__name__)

ObservationKey = tuple[int, int, str]  # (brand_id, catalog_id, condition_bucket)


@dataclass(frozen=True)
class PriceEstimate:
    value_eur: float
    source: str          # "median" | "median_any_condition" | "baseline"
    sample_size: int

    @property
    def is_measured(self) -> bool:
        return self.source.startswith("median")

    @property
    def label(self) -> str:
        return {
            "median": "медіана ринку",
            "median_any_condition": "медіана (усі стани)",
            "baseline": "базова оцінка",
        }.get(self.source, self.source)


@dataclass
class Observation:
    price_eur: float
    ts: int


class PriceBook:
    """Ковзне вікно цін по зв'язці бренд + категорія + стан."""

    def __init__(
        self,
        *,
        window_size: int = 120,
        window_seconds: int = 21 * 86400,
        min_samples: int = 8,
    ) -> None:
        self.window_size = window_size
        self.window_seconds = window_seconds
        self.min_samples = min_samples
        self._data: dict[ObservationKey, Deque[Observation]] = defaultdict(
            lambda: deque(maxlen=window_size)
        )

    # ------------------------------------------------------------- наповнення

    def record(self, brand_id: int, catalog_id: int, bucket: str, price_eur: float, ts: int) -> None:
        if price_eur <= 0:
            return
        self._data[(brand_id, catalog_id, bucket)].append(Observation(price_eur, ts))

    def bulk_load(self, rows: Iterable[tuple[int, int, str, float, int]]) -> int:
        count = 0
        for brand_id, catalog_id, bucket, price_eur, ts in rows:
            self.record(brand_id, catalog_id, bucket, price_eur, ts)
            count += 1
        return count

    def sample_size(self, brand_id: int, catalog_id: int, bucket: str, now_ts: int) -> int:
        return len(self._fresh(self._data.get((brand_id, catalog_id, bucket)), now_ts))

    def under_sampled(self, pairs: Iterable[tuple[int, int]], buckets: list[str], now_ts: int) -> list[tuple[int, int]]:
        """Пари бренд+категорія, де ще замало даних для медіани."""
        out = []
        for brand_id, catalog_id in pairs:
            total = sum(
                self.sample_size(brand_id, catalog_id, b, now_ts) for b in buckets
            )
            if total < self.min_samples:
                out.append((brand_id, catalog_id))
        return out

    # ---------------------------------------------------------------- оцінка

    def estimate(
        self,
        *,
        brand_id: int,
        catalog_id: int,
        bucket: str,
        now_ts: int,
        baseline_eur: float,
        condition_factor: float,
        all_buckets: list[str] | None = None,
    ) -> PriceEstimate:
        """Найкраща доступна оцінка ціни перепродажу, у євро."""
        exact = self._fresh(self._data.get((brand_id, catalog_id, bucket)), now_ts)
        if len(exact) >= self.min_samples:
            return PriceEstimate(
                value_eur=round(statistics.median(p.price_eur for p in exact), 2),
                source="median",
                sample_size=len(exact),
            )

        # Замало даних по цьому стану - беремо всі стани і коригуємо поправкою
        if all_buckets:
            pooled: list[float] = []
            for other in all_buckets:
                pooled.extend(
                    o.price_eur for o in self._fresh(self._data.get((brand_id, catalog_id, other)), now_ts)
                )
            if len(pooled) >= self.min_samples:
                base = statistics.median(pooled)
                return PriceEstimate(
                    value_eur=round(base * condition_factor, 2),
                    source="median_any_condition",
                    sample_size=len(pooled),
                )

        return PriceEstimate(
            value_eur=round(baseline_eur * condition_factor, 2),
            source="baseline",
            sample_size=len(exact),
        )

    # ------------------------------------------------------------------ хелп

    def _fresh(self, bucket: Deque[Observation] | None, now_ts: int) -> list[Observation]:
        if not bucket:
            return []
        cutoff = now_ts - self.window_seconds
        return [o for o in bucket if o.ts >= cutoff]

    @property
    def total_observations(self) -> int:
        return sum(len(v) for v in self._data.values())

    @property
    def tracked_keys(self) -> int:
        return len(self._data)
