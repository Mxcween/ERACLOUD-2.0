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
# Квота на безкоштовному тарифі рахується ОКРЕМО для кожної моделі, тому
# тримаємо дві: коли одна впирається в ліміт, запит іде в другу. Одна модель
# на цьому тарифі дає приблизно половину відмов.
DEFAULT_MODELS = ["gemini-2.5-flash", "gemini-3.1-flash-lite"]

PROMPT = (
    "You are a streetwear and vintage reseller deciding whether to buy this "
    "second-hand listing to flip it.\n"
    "The listing claims: brand {brand!r}, title {title!r}, category {category!r}, "
    "condition {condition!r}, price {price:.0f} EUR.\n"
    "Answer ONLY compact JSON:\n"
    '{{"real_item":0-10,"condition":0-10,"photo_ok":0-10,"desirable":0-10,'
    '"flags":[],"note":"Ukrainian, max 8 words"}}\n'
    "real_item: does the garment plausibly match the claimed brand and title, or "
    "does it look like a counterfeit, a screenshot of another listing, a stock or "
    "catalogue image, a photo of a screen, or a completely different item.\n"
    "condition: be strict. Any visible stain, mark, discolouration, bobbling, "
    "hole, cracked or peeling print, stretched cuffs, yellowing or general "
    "griminess scores 3 or below.\n"
    "photo_ok: is the garment itself actually visible and identifiable.\n"
    "desirable: would this exact piece sell quickly to a young streetwear or "
    "vintage buyer. Score LOW for: dull or dated colourways, muddy browns, "
    "washed-out pastels, unflattering cuts, plain gym basics with no design, "
    "corporate or golf styling, tiny logo-only pieces with nothing else going "
    "on, and anything that looks like generic supermarket clothing regardless "
    "of the label. Score HIGH for: bold or clean colourways, recognisable "
    "silhouettes, technical or archive pieces, big graphics, and cuts people "
    "currently wear.\n"
    "flags: short tags from: fake, screenshot, stock_photo, wrong_item, stained, "
    "damaged, worn_out, blurry, not_visible, dated, boring, bad_colour, kids_size, bait."
)


@dataclass
class Verdict:
    ok: bool
    real_item: int = 10
    condition: int = 10
    photo_ok: int = 10
    desirable: int = 10
    flags: list[str] = field(default_factory=list)
    note: str = ""
    checked: bool = True

    @property
    def reason(self) -> str:
        if self.ok:
            return ""
        bad = ", ".join(self.flags) if self.flags else "низькі оцінки"
        return (f"{bad} (справжність {self.real_item}, стан {self.condition}, "
                f"видно {self.photo_ok}, попит {self.desirable})")


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
        models: list[str] | None = None,
        min_real: int = 5,
        min_condition: int = 6,
        min_photo: int = 4,
        min_desirable: int = 5,
        min_interval: float = 2.0,
        timeout: float = 25.0,
    ) -> None:
        self.api_key = api_key
        self.models = list(models or DEFAULT_MODELS)
        self.min_real = min_real
        self.min_condition = min_condition
        self.min_photo = min_photo
        self.min_desirable = min_desirable
        # Точних лімітів безкоштовного тарифу Google не публікує, тому пауза
        # самонавчальна. Плюс у кожної моделі свій "відпочинок" після 429.
        self.min_interval = min_interval
        self._penalty = 1.0
        self._last = 0.0
        self._cooldown: dict[str, float] = {}
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

    def _ready_model(self) -> str | None:
        now = time.monotonic()
        for model in self.models:
            if self._cooldown.get(model, 0.0) <= now:
                return model
        return None

    def _rest(self, model: str, seconds: float) -> None:
        """Ця модель упёрлась у квоту - даємо їй перепочити, беремо сусідню."""
        self._cooldown[model] = time.monotonic() + max(seconds, 8.0)
        self._penalty = min(self._penalty * 1.5, 8.0)
        log.info("зір: %s у квоті на %.0fс, пробую іншу модель", model, max(seconds, 8.0))

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
            try:
                image = await self._client.get(photo_url)
                image.raise_for_status()
                blob = image.content
            except Exception as exc:  # noqa: BLE001
                self.failed += 1
                log.warning("зір: фото не завантажилось (%s), пускаю без перевірки", exc)
                return CHECK_FAILED

            # Стільки спроб, скільки моделей, плюс одна після паузи: доставка
            # йде окремим робітником, тому почекати тут нічого не коштує.
            for attempt in range(len(self.models) + 1):
                model = self._ready_model()
                if model is None:
                    nap = min(20.0, max(1.0, min(self._cooldown.values()) - time.monotonic()))
                    if attempt >= len(self.models):
                        break
                    await asyncio.sleep(nap)
                    continue
                await self._throttle()
                try:
                    data = await self._ask(blob, brand, title, category, condition,
                                           price_eur, model)
                except _RateLimited as exc:
                    self._rest(model, exc.retry_after)
                    continue
                except Exception as exc:  # noqa: BLE001
                    self.failed += 1
                    log.warning("зір: перевірка впала (%s), пускаю без перевірки", exc)
                    return CHECK_FAILED

                self._relax()
                self.checked += 1
                verdict = _parse(data, self.min_real, self.min_condition, self.min_photo,
                                       self.min_desirable)
                if not verdict.ok:
                    self.rejected += 1
                return verdict

        self.failed += 1
        log.warning("зір: усі моделі в квоті, пускаю лот без перевірки")
        return CHECK_FAILED

    async def _ask(
        self, image: bytes, brand: str, title: str, category: str,
        condition: str, price_eur: float, model: str,
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
            f"{API_ROOT}/models/{model}:generateContent",
            params={"key": self.api_key}, json=payload,
        )
        if resp.status_code == 429:
            raise _RateLimited(_retry_after(resp))
        resp.raise_for_status()
        parts = resp.json()["candidates"][0]["content"]["parts"]
        import json as _json
        return _json.loads(parts[0]["text"])


def _parse(data: dict[str, Any], min_real: int = 5, min_condition: int = 6,
           min_photo: int = 4, min_desirable: int = 5) -> Verdict:
    def num(key: str) -> int:
        try:
            return max(0, min(10, int(float(data.get(key, 10)))))
        except (TypeError, ValueError):
            return 10

    real, cond = num("real_item"), num("condition")
    photo, want = num("photo_ok"), num("desirable")
    flags = [str(f) for f in (data.get("flags") or [])][:5]
    ok = (real >= min_real and cond >= min_condition
          and photo >= min_photo and want >= min_desirable)
    return Verdict(
        ok=ok, real_item=real, condition=cond, photo_ok=photo, desirable=want,
        flags=flags, note=str(data.get("note", ""))[:120],
    )
