"""Мінімальний HTTP-сервер здоровя.

Потрібен з двох причин. Перша: безкоштовний Web Service на Render мусить
слухати порт, інакше деплой вважається невдалим. Друга: безкоштовний інстанс
засинає без вхідного трафіку, тому зовнішній пінгер раз на 10 хвилин смикає
/health і тримає бота живим.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

log = logging.getLogger(__name__)


class HealthServer:
    def __init__(self, port: int, status_provider: Callable[[], dict[str, Any]]) -> None:
        self.port = port
        self.status_provider = status_provider
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "0.0.0.0", self.port)
        log.info("health-сервер слухає порт %s", self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readline(), timeout=5.0)
            # Дочитуємо заголовки, щоб клієнт не отримав reset
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if line in (b"\r\n", b"\n", b""):
                    break

            parts = request.decode("latin-1", "replace").split()
            path = parts[1] if len(parts) > 1 else "/"

            if path.startswith("/health") or path == "/":
                payload = json.dumps(self.status_provider(), ensure_ascii=False).encode("utf-8")
                status, ctype = "200 OK", "application/json; charset=utf-8"
            else:
                payload = b"not found"
                status, ctype = "404 Not Found", "text/plain; charset=utf-8"

            writer.write(
                b"HTTP/1.1 " + status.encode() + b"\r\n"
                b"Content-Type: " + ctype.encode() + b"\r\n"
                b"Content-Length: " + str(len(payload)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + payload
            )
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionError):
            pass
        except Exception as exc:  # noqa: BLE001
            log.debug("health-запит впав: %s", exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, RuntimeError):
                pass
