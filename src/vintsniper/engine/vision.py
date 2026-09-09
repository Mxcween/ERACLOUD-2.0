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
    '"flags":[],"note":"Ukrainian Cyrillic, max 8 words"}}\n'
    "real_item: does the garment plausibly match the claimed brand and title, or "
    "does it look like a counterfeit, a screenshot of another listing, a stock or "
    "catalogue image, a photo of a screen, or a completely different item.\n"
    "condition: be strict. Any visible stain, mark, discolouration, bobbling, "
    "hole, cracked or peeling print, stretched cuffs, yellowing or general "
    "griminess scores 3 or below.\n"
    "photo_ok: is the garment itself actually visible and identifiable.\n"
    "desirable: how easily does THIS piece sell on. You run a busy resale shop, "
    "not a museum - fast-moving stock matters more than rarity.\n"
    "  Score 7-10 for pieces people hunt: archive and vintage, collaborations, "
    "outdoor shells, distinctive designer cuts, bold prints.\n"
    "  Score 4-6 for solid everyday stock: clean, wearable, current pieces from "
    "a known brand in a normal colour - a plain adidas track top, a Nike hoodie, "
    "a Ralph Lauren polo. Ordinary, but it moves. This is the common case.\n"
    "  Score 0-3 ONLY for things that genuinely will not sell: visibly worn out "
    "or misshapen, an entire garment in a dead colour with nothing else going on, "
    "corporate or golf styling, shapeless cuts, kidswear, and anything that looks "
    "like unbranded supermarket clothing.\n"
    "  Colour: an ugly shade as an accent, panel or print is fine and can even "
    "help. Penalise only when the WHOLE garment is a dead muddy shade.\n"
    "  Do not punish a piece merely for being modern, sporty or ordinary.\n"
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
        self.why = "у квоті"


class _Unavailable(_RateLimited):
    """Google прилёг (5xx). Не наша проблема і не привід здаватись."""

    def __init__(self, retry_after: float = 10.0) -> None:
        super().__init__(retry_after)
        self.why = "недоступна"


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

    def _rest(self, model: str, seconds: float, why: str = "у квоті") -> None:
        """Ця модель зараз не відповідає - даємо їй паузу, беремо сусідню."""
        self._cooldown[model] = time.monotonic() + max(seconds, 8.0)
        self._penalty = min(self._penalty * 1.5, 8.0)
        log.info("зір: %s %s на %.0fс, пробую іншу модель", model, why, max(seconds, 8.0))

    def _scrub(self, text: str) -> str:
        """Ключ не має шансу потрапити в лог.

        Заголовок замість query вже прибрав головний шлях витоку, але текст
        помилки приходить з чужої бібліотеки, і покладатись на її акуратність
        не варто: одна зміна формату - і ключ у логах назавжди.
        """
        return text.replace(self.api_key, "***") if self.api_key else text

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
                log.warning("зір: фото не завантажилось (%s), пускаю без перевірки",
                            self._scrub(str(exc)))
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
                    self._rest(model, exc.retry_after, exc.why)
                    continue
                except Exception as exc:  # noqa: BLE001
                    self.failed += 1
                    log.warning("зір: перевірка впала (%s), пускаю без перевірки",
                                self._scrub(str(exc)))
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
        # Ключ іде заголовком, а не в query. У query він потрапляв би в текст
        # будь-якої помилки httpx ("... for url ...?key=..."), а звідти прямо
        # в лог Render. Заголовок у повідомлення про помилку не потрапляє.
        resp = await self._client.post(
            f"{API_ROOT}/models/{model}:generateContent",
            headers={"x-goog-api-key": self.api_key}, json=payload,
        )
        if resp.status_code == 429:
            raise _RateLimited(_retry_after(resp))
        # 503 та інші 5xx - це Google лежить хвилину, а не наша помилка.
        # Раніше ми на цьому здавались і лот ішов із поміткою "перевірити не
        # вдалось". Тепер поводимось як із квотою: ця модель відпочиває,
        # запит іде в сусідню.
        if resp.status_code >= 500:
            raise _Unavailable(_retry_after(resp) or 10.0)
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
