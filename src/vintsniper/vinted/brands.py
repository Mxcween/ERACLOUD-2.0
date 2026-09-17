"""Резолв назв брендів у id Vinted та кеш.

Id брендів у Vinted глобальні: Stone Island це 73306 і на vinted.pl, і на
vinted.de. Тому кеш один на всі ринки.
"""
from __future__ import annotations

import json
import logging
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class ResolvedBrand:
    name: str
    brand_id: int
    vinted_title: str
    tier: str
    replica_risk: str
    item_count: int = 0
    requires_authenticity_check: bool = False
    is_luxury: bool = False
    min_multiple: float | None = None


class BrandRegistry:
    """Тримає відповідність назва -> id і зворотну."""

    def __init__(self, brands: list[ResolvedBrand]) -> None:
        self._brands = brands
        self._by_id: dict[int, ResolvedBrand] = {b.brand_id: b for b in brands}
        # Vinted у стрічці віддає brand_title рядком, тому потрібен пошук за назвою
        self._by_title: dict[str, ResolvedBrand] = {}
        for b in brands:
            self._by_title.setdefault(b.vinted_title.casefold(), b)
            self._by_title.setdefault(b.name.casefold(), b)

    def __len__(self) -> int:
        return len(self._brands)

    @property
    def all(self) -> list[ResolvedBrand]:
        return list(self._brands)

    @property
    def ids(self) -> list[int]:
        return [b.brand_id for b in self._brands]

    def by_id(self, brand_id: int) -> ResolvedBrand | None:
        return self._by_id.get(brand_id)

    def by_title(self, title: str) -> ResolvedBrand | None:
        return self._by_title.get((title or "").strip().casefold())

    # ------------------------------------------------------------------ файл

    @classmethod
    def load(cls, path: Path) -> "BrandRegistry | None":
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("не читається кеш брендів %s: %s", path, exc)
            return None
        brands = [ResolvedBrand(**b) for b in raw.get("brands", [])]
        return cls(brands) if brands else None

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "note": "Згенеровано scripts/resolve_brands.py. Правити руками не треба.",
            "brands": [asdict(b) for b in self._brands],
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def pick_best_match(name: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Вибирає бренд із видачі пошуку.

    Vinted на "Carhartt" віддає і сам Carhartt, і десяток колаборацій. Нам
    потрібен точний збіг назви, а якщо його немає - найпопулярніший кандидат,
    у назві якого шуканий рядок стоїть на початку.
    """
    if not candidates:
        return None
    target = _normalise(name)

    exact = [c for c in candidates if _normalise(c.get("title", "")) == target]
    if exact:
        return max(exact, key=lambda c: c.get("item_count") or 0)

    prefixed = [c for c in candidates if _normalise(c.get("title", "")).startswith(target)]
    if prefixed:
        return max(prefixed, key=lambda c: c.get("item_count") or 0)

    return None


def _normalise(value: str) -> str:
    """Прибирає діакритику і розділові знаки: "Stüssy" == "Stussy"."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    out = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    out = out.casefold().strip()
    for ch in (".", "'", "’", "-", " ", "&"):
        out = out.replace(ch, "")
    return out
