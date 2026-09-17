"""Студійний бот PROSPEKT 23.

Кидаєш фото - повертає готовий кадр. Кидаєш фото з підписом - повертає
кадр і зібраний опис. Пише сам, без нікого посередині.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

import httpx

from . import caption
from .imagegen import ImageGenError, ImageGenerator
from .settings import StudioSettings
from .styles import DEFAULT_STYLE, STYLES, style_list

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"
LONG_POLL = 25
# Скільки секунд Telegram дає на завантаження файлу
FILE_ROOT = "https://api.telegram.org/file"

HELP = (
    "<b>PROSPEKT 23 · студія</b>\n\n"
    "Кинь фото речі — поверну готовий кадр 4:5 під шаблон посту.\n"
    "Кинь фото з підписом — поверну і кадр, і зібраний опис.\n\n"
    + caption.HELP
    + "\n\n<b>Команди</b>\n"
    "/style — змінити фон зйомки\n"
    "/models — які моделі бачить ключ\n"
    "/help — цей текст"
)


class StudioBot:
    def __init__(
        self,
        settings: StudioSettings,
        *,
        get_state: Callable[[str], str | None] | None = None,
        set_state: Callable[[str, str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.gen = ImageGenerator(settings)
        self._client = httpx.AsyncClient(
            base_url=f"{API_ROOT}/bot{settings.bot_token}", timeout=90.0
        )
        self._offset = 0
        self._style = settings.default_style if settings.default_style in STYLES else DEFAULT_STYLE
        self._get_state = get_state
        self._set_state = set_state

    async def close(self) -> None:
        await self._client.aclose()
        await self.gen.close()

    # ------------------------------------------------------------- службове

    async def _call(self, method: str, payload: dict[str, Any], *, timeout: float | None = None):
        try:
            resp = await self._client.post(
                f"/{method}", json=payload, **({"timeout": timeout} if timeout else {})
            )
        except httpx.HTTPError as exc:
            log.warning("студія: telegram %s не відповів: %s", method, exc)
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if not data.get("ok"):
            log.warning("студія: telegram %s відмовив: %s", method, data.get("description"))
            return None
        return data.get("result")

    async def _say(self, chat_id: str, text: str) -> None:
        await self._call(
            "sendMessage",
            {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
             "link_preview_options": {"is_disabled": True}},
        )

    async def _download(self, file_id: str) -> tuple[bytes, str] | None:
        info = await self._call("getFile", {"file_id": file_id})
        if not info or not info.get("file_path"):
            return None
        path = info["file_path"]
        url = f"{FILE_ROOT}/bot{self.settings.bot_token}/{path}"
        try:
            async with httpx.AsyncClient(timeout=90.0) as c:
                resp = await c.get(url)
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("студія: не забрав файл: %s", exc)
            return None
        mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
        return resp.content, mime

    async def _send_photo(self, chat_id: str, image: bytes, caption_text: str = "") -> bool:
        files = {"photo": ("prospekt23.jpg", image, "image/jpeg")}
        data = {"chat_id": chat_id}
        if caption_text:
            data["caption"] = caption_text[:1024]
        try:
            resp = await self._client.post("/sendPhoto", data=data, files=files, timeout=120.0)
            return bool(resp.json().get("ok"))
        except Exception as exc:  # noqa: BLE001
            log.warning("студія: не відправив фото: %s", exc)
            return False

    def _remember_style(self) -> None:
        if self._set_state:
            try:
                self._set_state("studio_style", self._style)
            except Exception:  # noqa: BLE001
                pass

    def _restore_style(self) -> None:
        if not self._get_state:
            return
        try:
            saved = self._get_state("studio_style")
        except Exception:  # noqa: BLE001
            return
        if saved in STYLES:
            self._style = saved

    # ------------------------------------------------------------ обробники

    async def _handle_photo(self, chat_id: str, photos: list[dict], text: str) -> None:
        best = max(photos, key=lambda p: p.get("file_size", 0))
        got = await self._download(best["file_id"])
        if not got:
            await self._say(chat_id, "Не вдалось забрати фото з Telegram, спробуй ще раз.")
            return

        if not self.settings.api_key:
            body = caption.build(text) if text else None
            await self._say(
                chat_id,
                "Ключа GEMINI_API_KEY немає, тому фото не обробляю."
                + (f"\n\nОпис готовий:\n\n<code>{body}</code>" if body else ""),
            )
            return

        await self._call("sendChatAction", {"chat_id": chat_id, "action": "upload_photo"})
        image, mime = got
        try:
            out = await self.gen.transform(image, mime, self._style)
        except ImageGenError as exc:
            await self._say(chat_id, f"Не вийшло обробити: {exc}")
            return

        body = caption.build(text) if text else None
        if not await self._send_photo(chat_id, out):
            await self._say(chat_id, "Обробив, але Telegram не прийняв картинку. Спробуй ще раз.")
            return
        if body:
            await self._say(chat_id, f"<code>{body}</code>")

    async def _handle_text(self, chat_id: str, text: str) -> None:
        name = ""
        args = ""
        if text.startswith("/"):
            head, _, rest = text[1:].partition(" ")
            name = head.split("@", 1)[0].lower()
            args = rest.strip()

        if name in ("start", "help"):
            await self._say(chat_id, HELP)
            return

        if name == "style":
            if args in STYLES:
                self._style = args
                self._remember_style()
                await self._say(chat_id, f"Тепер знімаю в стилі «{STYLES[args]['name']}».")
            else:
                await self._say(
                    chat_id,
                    f"Зараз: <b>{STYLES[self._style]['name']}</b>\n\n{style_list()}",
                )
            return

        if name == "models":
            try:
                models = await self.gen.available_models()
            except ImageGenError as exc:
                await self._say(chat_id, f"Не спитав: {exc}")
                return
            current = self.gen.model or "ще не обрана"
            listing = "\n".join(f"• <code>{m}</code>" for m in models) or "жодної"
            await self._say(chat_id, f"Працюю на: <b>{current}</b>\n\nКлюч бачить:\n{listing}")
            return

        if name:
            await self._say(chat_id, "Не знаю такої команди. /help")
            return

        body = caption.build(text)
        if body:
            await self._say(chat_id, f"<code>{body}</code>")
        else:
            await self._say(chat_id, caption.HELP)

    # ------------------------------------------------------------- головний

    async def run_forever(self) -> None:
        if not self.settings.bot_token:
            log.info("студія: STUDIO_BOT_TOKEN не заданий, бот не запускається")
            return
        self._restore_style()
        me = await self._call("getMe", {})
        if me:
            log.info("студія: бот @%s, стиль %s", me.get("username"), self._style)
        if not self.settings.api_key:
            log.warning("студія: GEMINI_API_KEY порожній — працюють лише описи")

        while True:
            started = asyncio.get_event_loop().time()
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("студія: цикл спіткнувся, продовжую")
            idle = 2.0 - (asyncio.get_event_loop().time() - started)
            if idle > 0:
                await asyncio.sleep(idle)

    async def _poll_once(self) -> None:
        updates = await self._call(
            "getUpdates",
            {"offset": self._offset, "timeout": LONG_POLL, "allowed_updates": ["message"]},
            timeout=LONG_POLL + 20,
        )
        for update in updates or []:
            self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
            message = update.get("message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            if not chat_id:
                continue
            photos = message.get("photo")
            if photos:
                await self._handle_photo(chat_id, photos, (message.get("caption") or "").strip())
            elif message.get("text"):
                await self._handle_text(chat_id, message["text"].strip())
            elif message.get("document"):
                await self._say(
                    chat_id,
                    "Надішли як фото, а не файлом — так Telegram віддає його мені стисненим, "
                    "але у форматі, який я вмію читати.",
                )
