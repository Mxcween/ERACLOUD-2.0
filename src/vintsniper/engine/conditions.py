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

# Запасні назви станів, якщо опитування на старті не вдалось.
#
# Опитування - головний шлях, бо переживе будь-яку зміну формулювань у
# Vinted. Але воно робиться ОДИН раз на старті, саме тоді, коли бот
# найагресивніше довбить API і найлегше ловить 429. Заміряно на живому
# боті: польська мапа не піднялась, і весь ринок PL мовчки відкидав усе
# підряд - 126 лотів за 25 хвилин з поміткою "невідомий стан 'Bardzo
# dobry'". Ринок був сліпий до наступного деплою, і дізнались ми про це
# лише тому, що причина відмови почала називати сам рядок.
#
# Тому тут лежить те, що Vinted віддає сьогодні. Опитування перекриє ці
# значення, щойно спрацює; словник потрібен рівно для того, щоб провал
# опитування коштував неточності, а не сліпоти.
FALLBACK_TITLES: dict[int, tuple[str, ...]] = {
    6: ("Nowy z metką", "Neu mit Etikett", "New with tags", "Neuf avec étiquette",
        "Nuovo con cartellino", "Nuevo con etiquetas", "Nieuw met prijskaartje"),
    1: ("Nowy bez metki", "Neu ohne Etikett", "New without tags", "Neuf sans étiquette",
        "Nuovo senza cartellino", "Nuevo sin etiquetas", "Nieuw zonder prijskaartje"),
    2: ("Bardzo dobry", "Sehr gut", "Very good", "Très bon état",
        "Ottime condizioni", "Muy bueno", "Zeer goed"),
    3: ("Dobry", "Gut", "Good", "Bon état", "Buone condizioni", "Bueno", "Goed"),
    4: ("Zadowalający", "Zufriedenstellend", "Satisfactory", "Satisfaisant",
        "Condizioni discrete", "Satisfactorio", "Redelijk"),
    7: ("Uszkodzony", "Beschädigt", "Damaged", "Endommagé", "Danneggiato", "Dañado"),
}


class StatusMap:
    """Відповідність локалізований рядок -> status_id для одного ринку."""

    def __init__(self, market_code: str, buckets: dict[str, list[int]]) -> None:
        self.market_code = market_code
        self._title_to_id: dict[str, int] = {
            title.casefold(): sid
            for sid, titles in FALLBACK_TITLES.items()
            for title in titles
        }
        self._probed = False
        self._id_to_bucket: dict[int, str] = {}
        for bucket, ids in buckets.items():
            for sid in ids:
                self._id_to_bucket[int(sid)] = bucket

    async def resolve(self, client: "VintedClient", status_ids: list[int], catalog_id: int) -> None:
        """По одному дешевому запиту на кожен стан, щоб зчитати його назву."""
        found = 0
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
                found += 1
        self._probed = found >= len(status_ids)
        if not self._probed:
            log.warning(
                "[%s] назви станів прочитались не повністю (%s з %s), "
                "поки працюю за запасним словником",
                self.market_code, found, len(status_ids),
            )
        log.info(
            "[%s] назви станів: %s",
            self.market_code,
            {t: i for t, i in sorted(self._title_to_id.items(), key=lambda kv: kv[1])},
        )

    @property
    def probed(self) -> bool:
        """Чи вдалось прочитати назви з API, чи тримаємось запасних."""
        return self._probed

    def status_id(self, status_title: str) -> int | None:
        return self._title_to_id.get((status_title or "").strip().casefold())

    def bucket(self, status_title: str) -> str | None:
        sid = self.status_id(status_title)
        return self._id_to_bucket.get(sid) if sid is not None else None

    @property
    def resolved(self) -> bool:
        return bool(self._title_to_id)
