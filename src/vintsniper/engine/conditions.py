"""Стани речі.

Vinted віддає стан локалізованим рядком: "Bardzo dobry" на польському ринку,
"Sehr gut" на німецькому. Числового id у стрічці каталогу немає.

Щоб не тримати словники перекладів для кожної мови, ми один раз на старті
питаємо API по одному лоту на кожен status_id і запам'ятовуємо, який рядок
йому відповідає. Так воно працює на будь-якому ринку, навіть якщо Vinted
завтра змінить формулювання.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..vinted.client import VintedClient

log = logging.getLogger(__name__)

ALL_STATUS_IDS = [6, 1, 2, 3, 4, 7]


class StatusMap:
    """Відповідність локалізований рядок -> status_id для одного ринку."""

    def __init__(self, market_code: str, buckets: dict[str, list[int]]) -> None:
        self.market_code = market_code
        self._title_to_id: dict[str, int] = {}
        self._id_to_bucket: dict[int, str] = {}
        for bucket, ids in buckets.items():
            for sid in ids:
                self._id_to_bucket[int(sid)] = bucket

    async def resolve(self, client: "VintedClient", status_ids: list[int], catalog_id: int) -> None:
        """По одному дешевому запиту на кожен стан, щоб зчитати його назву."""
        for sid in status_ids:
            try:
                items, _ = await client.fetch_catalog(
                    catalog_id=catalog_id, status_ids=[sid], per_page=1
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] не вдалось прочитати назву стану %s: %s", self.market_code, sid, exc)
                continue
            if not items:
                continue
            title = items[0].status_title.strip()
            if title:
                self._title_to_id[title.casefold()] = sid
        log.info(
            "[%s] назви станів: %s",
            self.market_code,
            {t: i for t, i in sorted(self._title_to_id.items(), key=lambda kv: kv[1])},
        )

    def status_id(self, status_title: str) -> int | None:
        return self._title_to_id.get((status_title or "").strip().casefold())

    def bucket(self, status_title: str) -> str | None:
        sid = self.status_id(status_title)
        return self._id_to_bucket.get(sid) if sid is not None else None

    @property
    def resolved(self) -> bool:
        return bool(self._title_to_id)
