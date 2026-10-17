"""Налаштування студійного бота.

Живе поруч зі снайпером, але це окремий продукт: інший токен, інший бот
у Telegram, свої змінні оточення. Спільний з ним лише процес — щоб на
безкоштовному Render вистачило одного сервісу.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# Порядок спроб. Перша модель, яку прийме ключ, і буде робочою: назви в
# Google міняються частіше, ніж хочеться, тому не прибиваємо одну цвяхами.
DEFAULT_MODELS = [
    "gemini-3-pro-image",
    "gemini-3-pro-image-preview",
    "gemini-3.1-flash-image",
    "gemini-2.5-flash-image",
]

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"


@dataclass
class StudioSettings:
    bot_token: str
    api_key: str
    models: list[str] = field(default_factory=lambda: list(DEFAULT_MODELS))
    chat_id: str = ""
    default_style: str = "light"
    timeout: float = 180.0

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.api_key)

    @property
    def token_only(self) -> bool:
        """Токен є, ключа немає: описи робимо, фото ні."""
        return bool(self.bot_token) and not self.api_key


def load_studio_settings() -> StudioSettings:
    models = [m.strip() for m in os.getenv("STUDIO_MODELS", "").split(",") if m.strip()]
    return StudioSettings(
        bot_token=os.getenv("STUDIO_BOT_TOKEN", "").strip(),
        api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        models=models or list(DEFAULT_MODELS),
        chat_id=os.getenv("STUDIO_CHAT_ID", "").strip(),
        default_style=os.getenv("STUDIO_STYLE", "light").strip().lower() or "light",
        timeout=float(os.getenv("STUDIO_TIMEOUT", "180")),
    )
