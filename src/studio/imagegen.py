"""Обробка фото через Gemini image API.

Вхід — сирий кадр телефоном, вихід — той самий одяг у нормальному кадрі.
Модель обираємо один раз на старті: перша з переліку, яку приймає ключ.
"""
from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from .settings import API_ROOT, StudioSettings
from .styles import style_prompt

log = logging.getLogger(__name__)


class ImageGenError(RuntimeError):
    """Не вийшло згенерувати. Текст призначений користувачу в чат."""


class _TryNextModel(RuntimeError):
    """Ця модель не пішла, але сусідня може."""


class ImageGenerator:
    def __init__(self, settings: StudioSettings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(timeout=settings.timeout)
        self._model: str | None = None

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def model(self) -> str | None:
        return self._model

    async def available_models(self) -> list[str]:
        """Які моделі взагалі бачить цей ключ. Для діагностики в боті."""
        try:
            resp = await self._client.get(
                f"{API_ROOT}/models", params={"key": self.settings.api_key}
            )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise ImageGenError(f"не вдалось спитати список моделей: {exc}") from exc
        if "models" not in data:
            raise ImageGenError(str(data.get("error", {}).get("message", data))[:300])
        names = [m.get("name", "").removeprefix("models/") for m in data["models"]]
        return [n for n in names if "image" in n]

    async def pick_model(self) -> str | None:
        """Знаходить робочу модель. Викликається один раз, ліниво."""
        if self._model:
            return self._model
        try:
            seen = set(await self.available_models())
        except ImageGenError as exc:
            log.warning("студія: %s", exc)
            seen = set()
        for candidate in self.settings.models:
            if not seen or candidate in seen:
                self._model = candidate
                log.info("студія: працюю на моделі %s", candidate)
                return candidate
        # Ключ бачить якісь image-моделі, але жодної з наших: беремо першу його
        if seen:
            self._model = sorted(seen)[0]
            log.info("студія: беру доступну модель %s", self._model)
            return self._model
        return None

    async def transform(self, image: bytes, mime: str, style: str) -> bytes:
        """Пробує моделі по черзі: квота вичерпується окремо для кожної."""
        await self.pick_model()
        order = [self._model] if self._model else []
        order += [m for m in self.settings.models if m != self._model]
        if not order:
            raise ImageGenError(
                "Жодна модель картинок не доступна цьому ключу. "
                "Перевір GEMINI_API_KEY і /models."
            )

        last: ImageGenError | None = None
        for model in order:
            try:
                out = await self._one_shot(image, mime, style, model)
            except _TryNextModel as exc:
                last = ImageGenError(str(exc))
                continue
            self._model = model
            return out

        raise ImageGenError(
            (str(last) if last else "не вийшло")
            + "\n\nЯкщо це квота: у Gemini на безкоштовному тарифі генерації "
            "картинок немає взагалі. Треба увімкнути білінг у AI Studio → "
            "Get API key → Set up billing. Найдешевша модель виходить "
            "близько 4 центів за кадр."
        )

    async def _one_shot(self, image: bytes, mime: str, style: str, model: str) -> bytes:

        payload: dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"inline_data": {"mime_type": mime, "data": base64.b64encode(image).decode()}},
                        {"text": style_prompt(style)},
                    ],
                }
            ],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }

        try:
            resp = await self._client.post(
                f"{API_ROOT}/models/{model}:generateContent",
                params={"key": self.settings.api_key},
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise _TryNextModel(f"{model}: мережа впала ({exc})") from exc

        try:
            data = resp.json()
        except ValueError as exc:
            raise ImageGenError(f"відповідь не JSON ({resp.status_code})") from exc

        if resp.status_code >= 400:
            message = str(data.get("error", {}).get("message", data))[:200]
            # Зникла модель, вичерпана квота або немає доступу - усе це причини
            # спробувати наступну в переліку, а не здаватись одразу
            if resp.status_code in (403, 404, 429):
                self._model = None
                raise _TryNextModel(f"{model}: {resp.status_code} {message}")
            raise ImageGenError(f"{resp.status_code}: {message}")

        out = _extract_image(data)
        if out is None:
            reason = _refusal_reason(data)
            raise ImageGenError(reason or "модель не повернула картинку")
        return out


def _extract_image(data: dict[str, Any]) -> bytes | None:
    for cand in data.get("candidates", []):
        for part in (cand.get("content") or {}).get("parts", []):
            blob = part.get("inlineData") or part.get("inline_data")
            if blob and blob.get("data"):
                try:
                    return base64.b64decode(blob["data"])
                except Exception:  # noqa: BLE001
                    continue
    return None


def _refusal_reason(data: dict[str, Any]) -> str | None:
    """Коли картинки немає, модель зазвичай пояснює текстом - віддамо це в чат."""
    for cand in data.get("candidates", []):
        if cand.get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT"):
            return "модель відмовилась обробляти це фото"
        for part in (cand.get("content") or {}).get("parts", []):
            if part.get("text"):
                return part["text"].strip()[:300]
    return None
