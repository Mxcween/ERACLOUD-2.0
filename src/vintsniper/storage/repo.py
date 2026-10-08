"""Робота з базою. Синхронний SQLAlchemy, викликається з asyncio через to_thread."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..models import Deal
from .db import AlertLog, BotState, MutedBrand, PriceObservation, SeenItem

log = logging.getLogger(__name__)


@dataclass
class Stats:
    seen_items: int
    observations: int
    alerts_total: int
    alerts_24h: int
    muted: int


class Repository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    # --------------------------------------------------------------- дедуплікація

    def filter_unseen(self, market: str, item_ids: list[int], now_ts: int) -> set[int]:
        """Повертає id, яких ще не було, і одразу їх записує.

        Робимо це однією транзакцією, щоб рестарт посеред циклу не призвів до
        повторної розсилки тих самих лотів.
        """
        if not item_ids:
            return set()
        # Один і той самий id у пачці зламав би вставку через унікальний індекс,
        # а разом з нею і всю пачку. Порядок зберігаємо.
        unique_ids = list(dict.fromkeys(item_ids))
        with self._sf() as session:
            existing = set(
                session.scalars(
                    select(SeenItem.item_id).where(
                        SeenItem.market == market, SeenItem.item_id.in_(unique_ids)
                    )
                ).all()
            )
            fresh = [i for i in unique_ids if i not in existing]
            if fresh:
                session.add_all(
                    [SeenItem(item_id=i, market=market, first_seen_ts=now_ts) for i in fresh]
                )
                try:
                    session.commit()
                except IntegrityError:
                    # Хтось записав ці ж лоти паралельно. Краще промовчати, ніж
                    # ризикнути повторним алертом на ту саму річ.
                    session.rollback()
                    log.warning(
                        "[%s] гонка при записі %s переглянутих лотів, пропускаю цикл",
                        market, len(fresh),
                    )
                    return set()
            return set(fresh)

    def prune_seen(self, older_than_ts: int) -> int:
        with self._sf() as session:
            result = session.execute(
                delete(SeenItem).where(SeenItem.first_seen_ts < older_than_ts)
            )
            session.commit()
            return result.rowcount or 0

    # ------------------------------------------------------------------- ціни

    def add_observations(
        self, rows: list[tuple[int, int, str, float, str, int]]
    ) -> int:
        if not rows:
            return 0
        with self._sf() as session:
            session.add_all(
                [
                    PriceObservation(
                        brand_id=b, catalog_id=c, bucket=k, price_eur=p, market=m, ts=t
                    )
                    for b, c, k, p, m, t in rows
                ]
            )
            session.commit()
            return len(rows)

    def load_observations(self, since_ts: int) -> list[tuple[int, int, str, float, int]]:
        with self._sf() as session:
            rows = session.execute(
                select(
                    PriceObservation.brand_id,
                    PriceObservation.catalog_id,
                    PriceObservation.bucket,
                    PriceObservation.price_eur,
                    PriceObservation.ts,
                )
                .where(PriceObservation.ts >= since_ts)
                .order_by(PriceObservation.ts)
            ).all()
            return [tuple(r) for r in rows]

    def prune_observations(self, older_than_ts: int) -> int:
        with self._sf() as session:
            result = session.execute(
                delete(PriceObservation).where(PriceObservation.ts < older_than_ts)
            )
            session.commit()
            return result.rowcount or 0

    # ----------------------------------------------------------------- алерти

    def log_alert(self, deal: Deal, now_ts: int) -> None:
        listing = deal.listing
        with self._sf() as session:
            session.add(
                AlertLog(
                    item_id=listing.item_id,
                    market=listing.market,
                    channel=deal.channel,
                    brand=listing.brand_title[:120],
                    category_key=deal.category_key,
                    title=listing.title[:300],
                    url=listing.url[:400],
                    size_title=listing.size_title[:60],
                    cost_eur=deal.cost_eur,
                    resale_eur=deal.resale_eur,
                    profit_eur=deal.profit_eur,
                    multiple=deal.multiple,
                    reference=deal.reference[:40],
                    sample_size=deal.sample_size,
                    sent_ts=now_ts,
                )
            )
            session.commit()

    def alerts_since(self, since_ts: int) -> int:
        with self._sf() as session:
            return int(
                session.scalar(
                    select(func.count()).select_from(AlertLog).where(AlertLog.sent_ts >= since_ts)
                )
                or 0
            )

    def recent_alerts(self, limit: int = 10) -> list[AlertLog]:
        with self._sf() as session:
            return list(
                session.scalars(
                    select(AlertLog).order_by(AlertLog.sent_ts.desc()).limit(limit)
                ).all()
            )

    # ------------------------------------------------------------- заглушені

    def muted_brand_ids(self) -> set[int]:
        with self._sf() as session:
            return set(
                session.scalars(
                    select(MutedBrand.brand_id).where(MutedBrand.active.is_(True))
                ).all()
            )

    def mute_brand(self, brand_id: int, brand_name: str, now_ts: int) -> None:
        with self._sf() as session:
            row = session.get(MutedBrand, brand_id)
            if row is None:
                session.add(
                    MutedBrand(
                        brand_id=brand_id, brand_name=brand_name, muted_ts=now_ts, active=True
                    )
                )
            else:
                row.active = True
                row.muted_ts = now_ts
            session.commit()

    def unmute_brand(self, brand_id: int) -> bool:
        with self._sf() as session:
            row = session.get(MutedBrand, brand_id)
            if row is None or not row.active:
                return False
            row.active = False
            session.commit()
            return True

    # -------------------------------------------------------------- стан бота

    def get_state(self, key: str) -> str | None:
        with self._sf() as session:
            row = session.get(BotState, key)
            return row.value if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._sf() as session:
            row = session.get(BotState, key)
            if row is None:
                session.add(BotState(key=key, value=str(value)))
            else:
                row.value = str(value)
            session.commit()

    # ------------------------------------------------------------- статистика

    def stats(self, now_ts: int) -> Stats:
        with self._sf() as session:
            return Stats(
                seen_items=int(session.scalar(select(func.count()).select_from(SeenItem)) or 0),
                observations=int(
                    session.scalar(select(func.count()).select_from(PriceObservation)) or 0
                ),
                alerts_total=int(session.scalar(select(func.count()).select_from(AlertLog)) or 0),
                alerts_24h=int(
                    session.scalar(
                        select(func.count())
                        .select_from(AlertLog)
                        .where(AlertLog.sent_ts >= now_ts - 86400)
                    )
                    or 0
                ),
                muted=int(
                    session.scalar(
                        select(func.count())
                        .select_from(MutedBrand)
                        .where(MutedBrand.active.is_(True))
                    )
                    or 0
                ),
            )
