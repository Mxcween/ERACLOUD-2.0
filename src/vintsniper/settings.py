"""Завантаження конфігів з YAML і змінних оточення."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(os.getenv("VINTSNIPER_ROOT") or Path(__file__).resolve().parents[2])
CONFIG_DIR = Path(os.getenv("VINTSNIPER_CONFIG_DIR") or ROOT / "config")


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class Market:
    code: str
    host: str
    currency: str
    locale: str
    shipping_eur: float
    enabled: bool = True

    @property
    def base_url(self) -> str:
        return f"https://{self.host}"


@dataclass(frozen=True)
class Category:
    id: int
    key: str
    name: str
    ceiling_eur: float
    weight_kg: float
    baseline_eur: dict[str, float]
    enabled: bool = True


@dataclass(frozen=True)
class Brand:
    name: str
    tier: str
    replica_risk: str = "low"
    min_multiple: float | None = None


@dataclass(frozen=True)
class TelegramSettings:
    bot_token: str
    chat_id_top: str
    chat_id_all: str
    topic_id_top: int | None = None
    topic_id_all: int | None = None

    @property
    def configured(self) -> bool:
        """Токена достатньо: чат бот підхопить сам, коли йому напишуть /start."""
        return bool(self.bot_token)


@dataclass
class Settings:
    markets: list[Market]
    categories: list[Category]
    brands: list[Brand]
    conditions: dict[str, Any]
    sizes: dict[str, Any]
    category_defaults: dict[str, Any]
    title_blocklist: list[str]
    title_warn_words: list[str]
    polling: dict[str, Any]
    scoring: dict[str, Any]
    seller: dict[str, Any]
    alerts: dict[str, Any]
    fx: dict[str, Any]
    telegram: TelegramSettings
    database_url: str
    log_level: str = "INFO"
    port: int = 10000
    dry_run: bool = False

    # --- зручні вибірки ---
    @property
    def enabled_markets(self) -> list[Market]:
        return [m for m in self.markets if m.enabled]

    @property
    def enabled_categories(self) -> list[Category]:
        return [c for c in self.categories if c.enabled]

    def category_by_id(self, catalog_id: int) -> Category | None:
        return next((c for c in self.categories if c.id == catalog_id), None)

    def brand_by_name(self, name: str) -> Brand | None:
        key = name.strip().casefold()
        return next((b for b in self.brands if b.name.casefold() == key), None)


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_settings(config_dir: Path | None = None) -> Settings:
    load_dotenv(ROOT / ".env", override=False)
    cfg_dir = config_dir or CONFIG_DIR

    main = _load_yaml(cfg_dir / "config.yaml")
    cats_raw = _load_yaml(cfg_dir / "categories.yaml")
    brands_raw = _load_yaml(cfg_dir / "brands.yaml")

    markets = [
        Market(
            code=m["code"],
            host=m["host"],
            currency=m["currency"],
            locale=m.get("locale", "en-GB,en;q=0.9"),
            shipping_eur=float(m.get("shipping_eur", 0.0)),
            enabled=bool(m.get("enabled", True)),
        )
        for m in main.get("markets", [])
    ]

    categories = [
        Category(
            id=int(c["id"]),
            key=c["key"],
            name=c["name"],
            ceiling_eur=float(c["ceiling_eur"]),
            weight_kg=float(c.get("weight_kg", 0.5)),
            baseline_eur={k: float(v) for k, v in (c.get("baseline_eur") or {}).items()},
            enabled=bool(c.get("enabled", True)),
        )
        for c in cats_raw.get("categories", [])
    ]

    brands = [
        Brand(
            name=b["name"],
            tier=b.get("tier", "B"),
            replica_risk=b.get("replica_risk", "low"),
            min_multiple=float(b["min_multiple"]) if b.get("min_multiple") else None,
        )
        for b in brands_raw.get("brands", [])
    ]

    telegram = TelegramSettings(
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        chat_id_top=os.getenv("TELEGRAM_CHAT_ID_TOP", "").strip(),
        chat_id_all=os.getenv("TELEGRAM_CHAT_ID_ALL", "").strip()
        or os.getenv("TELEGRAM_CHAT_ID_TOP", "").strip(),
        topic_id_top=_as_int(os.getenv("TELEGRAM_TOPIC_ID_TOP")),
        topic_id_all=_as_int(os.getenv("TELEGRAM_TOPIC_ID_ALL")),
    )

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        (ROOT / "data").mkdir(exist_ok=True)
        database_url = f"sqlite:///{ROOT / 'data' / 'vintsniper.db'}"
    # Render та Neon віддають postgres://, SQLAlchemy 2 хоче postgresql+psycopg://
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)

    return Settings(
        markets=markets,
        categories=categories,
        brands=brands,
        conditions=cats_raw.get("conditions", {}),
        sizes=cats_raw.get("sizes", {}),
        category_defaults=cats_raw.get("defaults", {}),
        title_blocklist=list(cats_raw.get("title_blocklist") or []),
        title_warn_words=list(cats_raw.get("title_warn_words") or []),
        polling=main.get("polling", {}),
        scoring=main.get("scoring", {}),
        seller=main.get("seller", {}),
        alerts=main.get("alerts", {}),
        fx=main.get("fx", {}),
        telegram=telegram,
        database_url=database_url,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        port=int(os.getenv("PORT", "10000")),
        dry_run=_as_bool(os.getenv("DRY_RUN"), False),
    )
