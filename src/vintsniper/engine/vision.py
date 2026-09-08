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
    '{{"real_item":0-10,"condition":0-10,"photo_ok":0-10,"flags":[],"note":"Ukrainian, max 8 words"}}\n'
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


# Ключа немає - перевірки не було й не мало бути. Писати про це в кожному
# алерті означає засмічувати стрічку тим, чого власник і так не просив.
NOT_CONFIGURED = Verdict(ok=True, checked=False, note="")
# А от спроба, яка впала, - це вже інформація: лот пішов невивіреним.
CHECK_FAILED = Verdict(ok=True, checked=False, note="фото перевірити не вдалось")


class _RateLimited(RuntimeError):
    def __init__(self, retry_after: float = 0.0) -> None:
        super().__init__("429")
        self.retry_after = retry_after


def _retry_after(resp: httpx.Response) -> float:
    """Google інколи каже, скільки чекати. Якщо сказав - слухаємо."""
    header = resp.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    try:
        for detail in resp.json().get("error", {}).get("details", []):
            delay = detail.get("retryDelay", "")
            if delay.endswith("s"):
                return float(delay[:-1])
    except Exception:  # noqa: BLE001
        pass
    return 0.0


class PhotoJudge:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        min_real: int = 5,
        min_condition: int = 4,
        min_photo: int = 4,
        min_interval: float = 2.0,
        timeout: float = 25.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.min_real = min_real
        self.min_condition = min_condition
        self.min_photo = min_photo
        # Точних лімітів безкоштовного тарифу Google не публікує, тому пауза
        # самонавчальна: стартуємо швидко, після 429 розтягуємось, після
        # успіхів повертаємось. Так само, як з Vinted.
        self.min_interval = min_interval
        self._penalty = 1.0
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

    @property
    def interval(self) -> float:
        return self.min_interval * self._penalty

    async def _throttle(self) -> None:
        wait = self.interval - (time.monotonic() - self._last)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last = time.monotonic()

    def _penalise(self, retry_after: float = 0.0) -> None:
        self._penalty = min(self._penalty * 2.0, 8.0)
        if retry_after:
            self._last = time.monotonic() + retry_after - self.interval
        log.warning("зір: квота, пауза між фото тепер %.1fс", self.interval)

    def _relax(self) -> None:
        if self._penalty > 1.0:
            self._penalty = max(1.0, self._penalty * 0.85)

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
            return NOT_CONFIGURED
        async with self._lock:
            await self._throttle()
            try:
                image = await self._client.get(photo_url)
                image.raise_for_status()
                data = await self._ask(image.content, brand, title, category, condition, price_eur)
            except _RateLimited as exc:
                self.failed += 1
                self._penalise(exc.retry_after)
                return CHECK_FAILED
            except Exception as exc:  # noqa: BLE001
                self.failed += 1
                log.warning("зір: не вдалось перевірити фото (%s), пускаю без перевірки", exc)
                return CHECK_FAILED
            self._relax()

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
        if resp.status_code == 429:
            raise _RateLimited(_retry_after(resp))
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
