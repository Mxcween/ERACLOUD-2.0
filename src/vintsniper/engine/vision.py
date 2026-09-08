"""Перевірка фото лота перед відправкою.

Пороги по цифрах не бачать різниці між справжньою курткою за 20 євро і
скріншотом чужого оголошення за ті самі 20. Модель із зором бачить, тому
саме вона відсіює сміття - а не занижені множники, через які губилось усе
підряд.

Ключове правило: якщо перевірка не спрацювала (мережа, квота, таймаут),
лот іде далі З ПОМІТКОЮ, а не викидається. Збій зору не має робити бота
німим.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
# Текстова модель із зором: на безкоштовному тарифі вона доступна, на
# відміну від генерації картинок
DEFAULT_MODEL = "gemini-2.5-flash"

PROMPT = (
    "You are checking a second-hand clothing listing photo before a reseller buys it.\n"
    "The listing claims: brand {brand!r}, title {title!r}, category {category!r}, "
    "condition {condition!r}, price {price:.0f} EUR.\n"
    "Answer ONLY compact JSON:\n"
    '{{"real_item":0-10,"condition":0-10,"photo_ok":0-10,"flags":[],"note":"max 10 words"}}\n'
    "real_item: does the garment plausibly match the claimed brand and title, or does it look "
    "like a counterfeit, a screenshot of another listing, a stock/catalogue image, a photo of a "
    "screen, or a completely different item.\n"
    "condition: visible stains, holes, heavy pilling, cracked print, yellowing.\n"
    "photo_ok: is the garment itself actually visible and identifiable in the frame.\n"
    "flags: short tags from: fake, screenshot, stock_photo, wrong_item, damaged, blurry, "
    "not_visible, kids_size, bait."
)


@dataclass
class Verdict:
    ok: bool
    real_item: int = 10
    condition: int = 10
    photo_ok: int = 10
    flags: list[str] = field(default_factory=list)
    note: str = ""
    checked: bool = True

    @property
    def reason(self) -> str:
        if self.ok:
            return ""
        bad = ", ".join(self.flags) if self.flags else "низькі оцінки"
        return f"{bad} (справжність {self.real_item}, стан {self.condition}, видно {self.photo_ok})"


UNCHECKED = Verdict(ok=True, checked=False, note="фото не перевірено")


class PhotoJudge:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        min_real: int = 5,
        min_condition: int = 4,
        min_photo: int = 4,
        min_interval: float = 4.0,
        timeout: float = 25.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.min_real = min_real
        self.min_condition = min_condition
        self.min_photo = min_photo
        # Безкоштовний тариф рахує запити на хвилину, тому тримаємо паузу
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(timeout=timeout)
        self.checked = 0
        self.rejected = 0
        self.failed = 0

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def close(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last = time.monotonic()

    async def judge(
        self,
        photo_url: str,
        *,
        brand: str,
        title: str,
        category: str,
        condition: str,
        price_eur: float,
    ) -> Verdict:
        if not self.configured or not photo_url:
            return UNCHECKED
        async with self._lock:
            await self._throttle()
            try:
                image = await self._client.get(photo_url)
                image.raise_for_status()
                data = await self._ask(image.content, brand, title, category, condition, price_eur)
            except Exception as exc:  # noqa: BLE001
                self.failed += 1
                log.warning("зір: не вдалось перевірити фото (%s), пускаю без перевірки", exc)
                return UNCHECKED

        self.checked += 1
        verdict = _parse(data, self.min_real, self.min_condition, self.min_photo)
        if not verdict.ok:
            self.rejected += 1
        return verdict

    async def _ask(
        self, image: bytes, brand: str, title: str, category: str,
        condition: str, price_eur: float,
    ) -> dict[str, Any]:
        payload = {
            "contents": [{"role": "user", "parts": [
                {"inline_data": {"mime_type": "image/jpeg",
                                 "data": base64.b64encode(image).decode()}},
                {"text": PROMPT.format(brand=brand, title=title, category=category,
                                       condition=condition, price=price_eur)},
            ]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                # Роздуми тут тільки додають секунди: питання просте
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        resp = await self._client.post(
            f"{API_ROOT}/models/{self.model}:generateContent",
            params={"key": self.api_key}, json=payload,
        )
        resp.raise_for_status()
        parts = resp.json()["candidates"][0]["content"]["parts"]
        import json as _json
        return _json.loads(parts[0]["text"])


def _parse(data: dict[str, Any], min_real: int = 5, min_condition: int = 4,
           min_photo: int = 4) -> Verdict:
    def num(key: str) -> int:
        try:
            return max(0, min(10, int(float(data.get(key, 10)))))
        except (TypeError, ValueError):
            return 10

    real, cond, photo = num("real_item"), num("condition"), num("photo_ok")
    flags = [str(f) for f in (data.get("flags") or [])][:5]
    ok = real >= min_real and cond >= min_condition and photo >= min_photo
    return Verdict(
        ok=ok, real_item=real, condition=cond, photo_ok=photo,
        flags=flags, note=str(data.get("note", ""))[:120],
    )
