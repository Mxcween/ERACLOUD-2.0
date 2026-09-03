"""Відправка алертів і обробка команд у Telegram.

Працюємо напряму через Bot API, без важких бібліотек: httpx уже є в проєкті,
а нам треба лише sendPhoto, sendMessage і getUpdates.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from ..models import Deal
from ..settings import TelegramSettings
from .formatting import format_deal

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"
# Telegram ріже підпис до фото на 1024 символах
CAPTION_LIMIT = 1024


@dataclass
class Command:
    name: str
    args: str
    chat_id: str


class TelegramNotifier:
    def __init__(
        self,
        settings: TelegramSettings,
        *,
        send_photo: bool = True,
        dry_run: bool = False,
        timeout: float = 25.0,
    ) -> None:
        self.settings = settings
        self.send_photo = send_photo
        self.dry_run = dry_run
        self._client = httpx.AsyncClient(
            base_url=f"{API_ROOT}/bot{settings.bot_token}", timeout=timeout
        )
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------------- виклики

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if self.dry_run:
            log.info("[dry-run] telegram.%s: %s", method, str(payload)[:220])
            return None
        for attempt in range(1, 4):
            try:
                resp = await self._client.post(f"/{method}", json=payload)
            except httpx.HTTPError as exc:
                log.warning("telegram %s мережа впала (%s/3): %s", method, attempt, exc)
                await asyncio.sleep(2 * attempt)
                continue

            if resp.status_code == 429:
                retry_after = 5
                try:
                    retry_after = int(resp.json().get("parameters", {}).get("retry_after", 5))
                except Exception:  # noqa: BLE001
                    pass
                log.warning("telegram просить зачекати %sс", retry_after)
                await asyncio.sleep(retry_after + 1)
                continue

            data: dict[str, Any]
            try:
                data = resp.json()
            except ValueError:
                log.warning("telegram %s: відповідь не JSON (%s)", method, resp.status_code)
                await asyncio.sleep(2 * attempt)
                continue

            if data.get("ok"):
                return data.get("result")

            description = data.get("description", "")
            log.warning("telegram %s відмовив: %s", method, description)
            # Немає сенсу повторювати помилку конфігурації
            if resp.status_code in (400, 401, 403):
                return None
            await asyncio.sleep(2 * attempt)
        return None

    # ----------------------------------------------------------------- алерти

    def _target(self, channel: str) -> tuple[str, int | None]:
        if channel == "top":
            return self.settings.chat_id_top, self.settings.topic_id_top
        return self.settings.chat_id_all, self.settings.topic_id_all

    async def send_deal(self, deal: Deal, brand_id: int | None = None) -> bool:
        text = format_deal(deal)
        chat_id, topic_id = self._target(deal.channel)

        if self.dry_run or not chat_id:
            # Без цього сухий прогін мовчазний і незрозуміло, що саме бот знайшов
            plain = text.replace("<b>", "").replace("</b>", "")
            plain = plain.replace("<i>", "").replace("</i>", "")
            log.info("[dry-run] алерт у канал %s:\n%s\n%s", deal.channel, plain, deal.listing.url)
            return True

        keyboard = {
            "inline_keyboard": [
                [{"text": "🛒 Відкрити на Vinted", "url": deal.listing.url}],
                (
                    [{"text": "🔕 Не слати цей бренд", "callback_data": f"mute:{brand_id}"}]
                    if brand_id
                    else []
                ),
            ]
        }
        keyboard["inline_keyboard"] = [row for row in keyboard["inline_keyboard"] if row]

        base: dict[str, Any] = {
            "chat_id": chat_id,
            "parse_mode": "HTML",
            "reply_markup": keyboard,
        }
        if topic_id:
            base["message_thread_id"] = topic_id

        async with self._lock:
            photo = deal.listing.photo_url
            if self.send_photo and photo and len(text) <= CAPTION_LIMIT:
                result = await self._call("sendPhoto", {**base, "photo": photo, "caption": text})
                if result is not None or self.dry_run:
                    return True
                # Фото могло не завантажитись з боку Telegram, шлемо текстом
                log.info("не вийшло з фото, шлю текстом")

            result = await self._call(
                "sendMessage",
                {**base, "text": text, "link_preview_options": {"is_disabled": True}},
            )
            return result is not None or self.dry_run

    async def send_text(self, text: str, *, channel: str = "top") -> bool:
        chat_id, topic_id = self._target(channel)
        if not chat_id:
            return False
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if topic_id:
            payload["message_thread_id"] = topic_id
        async with self._lock:
            return await self._call("sendMessage", payload) is not None or self.dry_run

    # ---------------------------------------------------------------- команди

    async def poll_commands(
        self,
        offset: int,
        *,
        on_command: Callable[[Command], Awaitable[str | None]],
        on_callback: Callable[[str, str], Awaitable[str | None]],
    ) -> int:
        """Один прохід getUpdates. Повертає новий offset."""
        if self.dry_run:
            return offset
        result = await self._call(
            "getUpdates",
            {"offset": offset, "timeout": 0, "allowed_updates": ["message", "callback_query"]},
        )
        if not result:
            return offset

        new_offset = offset
        for update in result:
            new_offset = max(new_offset, int(update.get("update_id", 0)) + 1)

            message = update.get("message")
            if message:
                text = (message.get("text") or "").strip()
                if text.startswith("/"):
                    parts = text[1:].split(maxsplit=1)
                    name = parts[0].split("@", 1)[0].lower()
                    args = parts[1].strip() if len(parts) > 1 else ""
                    chat_id = str((message.get("chat") or {}).get("id", ""))
                    reply = await on_command(Command(name=name, args=args, chat_id=chat_id))
                    if reply:
                        await self._call(
                            "sendMessage",
                            {"chat_id": chat_id, "text": reply, "parse_mode": "HTML"},
                        )
                continue

            callback = update.get("callback_query")
            if callback:
                data = callback.get("data") or ""
                chat_id = str(((callback.get("message") or {}).get("chat") or {}).get("id", ""))
                reply = await on_callback(data, chat_id)
                await self._call(
                    "answerCallbackQuery",
                    {"callback_query_id": callback["id"], "text": reply or "Готово"},
                )
        return new_offset

    async def get_me(self) -> dict[str, Any] | None:
        return await self._call("getMe", {})
