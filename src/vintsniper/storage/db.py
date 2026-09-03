"""Схема бази. За замовчуванням SQLite у ./data, для Render краще Postgres.

Увага для Render: на безкоштовному тарифі диск ефемерний, тому SQLite
стирається при кожному редеплої разом з історією цін. Для постійної бази
підключи безкоштовний Postgres (Neon) через DATABASE_URL.
"""
from __future__ import annotations

import logging

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    delete,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class SeenItem(Base):
    """Лоти, які ми вже бачили. Захист від повторних алертів."""

    __tablename__ = "seen_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(BigInteger, index=True)
    market: Mapped[str] = mapped_column(String(8), index=True)
    first_seen_ts: Mapped[int] = mapped_column(BigInteger, index=True)

    __table_args__ = (UniqueConstraint("item_id", "market", name="uq_seen_item_market"),)


class PriceObservation(Base):
    """Спостереження цін для медіани."""

    __tablename__ = "price_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brand_id: Mapped[int] = mapped_column(BigInteger)
    catalog_id: Mapped[int] = mapped_column(Integer)
    bucket: Mapped[str] = mapped_column(String(16))
    price_eur: Mapped[float] = mapped_column(Float)
    market: Mapped[str] = mapped_column(String(8))
    ts: Mapped[int] = mapped_column(BigInteger, index=True)

    __table_args__ = (
        Index("ix_obs_key", "brand_id", "catalog_id", "bucket", "ts"),
    )


class AlertLog(Base):
    """Історія відправлених алертів. Зручно для розбору 'а що бот знайшов учора'."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(BigInteger, index=True)
    market: Mapped[str] = mapped_column(String(8))
    channel: Mapped[str] = mapped_column(String(8))
    brand: Mapped[str] = mapped_column(String(120))
    category_key: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(String(400))
    size_title: Mapped[str] = mapped_column(String(60), default="")
    cost_eur: Mapped[float] = mapped_column(Float)
    resale_eur: Mapped[float] = mapped_column(Float)
    profit_eur: Mapped[float] = mapped_column(Float)
    multiple: Mapped[float] = mapped_column(Float)
    reference: Mapped[str] = mapped_column(String(40))
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    sent_ts: Mapped[int] = mapped_column(BigInteger, index=True)


class MutedBrand(Base):
    """Бренди, які ти заглушив командою в Telegram."""

    __tablename__ = "muted_brands"

    brand_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    brand_name: Mapped[str] = mapped_column(String(120))
    muted_ts: Mapped[int] = mapped_column(BigInteger)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class BotState(Base):
    """Дрібні значення, які треба пережити рестарт (offset Telegram тощо)."""

    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(400))


def build_engine(database_url: str):
    kwargs: dict = {"pool_pre_ping": True, "future": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(database_url, **kwargs)
    Base.metadata.create_all(engine)
    log.info("база готова: %s", database_url.split("@")[-1])
    return engine


def build_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


__all__ = [
    "AlertLog",
    "Base",
    "BotState",
    "MutedBrand",
    "PriceObservation",
    "SeenItem",
    "build_engine",
    "build_session_factory",
    "delete",
    "func",
    "select",
]
