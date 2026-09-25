"""Обработка команд целевого бота (/info, /start).

Отправка живёт в bot_sender.py, а здесь — приём: long polling getUpdates.
Telethon не при делах, это второй, независимый канал через Bot API.

Безопасность: бот отвечает ТОЛЬКО в целевой чат. Ответ на /info содержит
настройки сервиса (аккаунт, правила, id чата), и посторонний, нашедший
бота по имени, получить их не должен.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Awaitable, Callable, Dict, Optional, Union

import aiohttp

from .bot_sender import BotApiError, BotSender

log = logging.getLogger(__name__)

Handler = Callable[[], Awaitable[str]]

# Пауза после ошибки опроса: растёт до этого предела, чтобы не долбить API.
MAX_BACKOFF = 60.0


class CommandListener:
    """Опрашивает getUpdates и вызывает обработчик по имени команды."""

    def __init__(
        self,
        sender: BotSender,
        *,
        allowed_chat_id: Union[str, int],
        poll_timeout: int = 15,
    ) -> None:
        self.sender = sender
        self.allowed_chat_id = str(allowed_chat_id)
        self.poll_timeout = poll_timeout
        self._handlers: Dict[str, Handler] = {}
        self._offset: Optional[int] = None

    def register(self, command: str, handler: Handler) -> None:
        self._handlers[command.lstrip("/").lower()] = handler

    @property
    def commands(self) -> tuple:
        return tuple(self._handlers)

    # -- основной цикл ------------------------------------------------------ #

    async def run(self, stop_event: asyncio.Event) -> None:
        """Опрашивает Telegram, пока не попросят остановиться.

        Любая ошибка опроса приводит к паузе и повтору, но не к остановке:
        мониторинг сообщений важнее, чем доступность команд.
        """
        await self._skip_backlog()
        log.info(
            "Слушаю команды: %s (только чат %s)",
            ", ".join("/" + name for name in self._handlers) or "нет",
            self.allowed_chat_id,
        )

        attempt = 0
        while not stop_event.is_set():
            try:
                updates = await self.sender.get_updates(
                    self._offset, self.poll_timeout
                )
                attempt = 0
            except asyncio.CancelledError:
                raise
            except BotApiError as exc:
                attempt += 1
                # 409 Conflict = кто-то ещё опрашивает этого бота или висит webhook.
                if "conflict" in str(exc).lower():
                    log.error(
                        "getUpdates конфликт: этого бота опрашивает кто-то ещё "
                        "(второй экземпляр сервиса или установленный webhook). "
                        "Команды работать не будут, пересылка — будет. %s", exc,
                    )
                else:
                    log.warning("getUpdates отказал: %s", exc)
                await _sleep_or_stop(stop_event, _backoff(attempt))
                continue
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                attempt += 1
                log.warning("getUpdates недоступен (%s: %s)",
                            type(exc).__name__, exc)
                await _sleep_or_stop(stop_event, _backoff(attempt))
                continue

            for update in updates or ():
                self._advance_offset(update)
                await self._dispatch(update)

    async def _skip_backlog(self) -> None:
        """Отбрасывает команды, накопившиеся пока сервис был выключен.

        Иначе после простоя бот разом ответит на все /info из очереди.
        offset=-1 отдаёт только последний апдейт — по нему и сдвигаем позицию.
        """
        try:
            updates = await self.sender.get_updates(-1, 0)
        except (BotApiError, aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            log.warning("Не удалось пропустить очередь апдейтов: %s", exc)
            return
        for update in updates or ():
            self._advance_offset(update)
        if self._offset is not None:
            log.info("Очередь старых апдейтов пропущена (offset=%s)", self._offset)

    def _advance_offset(self, update: Dict[str, Any]) -> None:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            # offset = последний обработанный + 1: подтверждает апдейт Telegram,
            # иначе он придёт снова на следующем опросе.
            self._offset = max(self._offset or 0, update_id + 1)

    # -- разбор апдейта ----------------------------------------------------- #

    async def _dispatch(self, update: Dict[str, Any]) -> None:
        message = update.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return

        if chat_id != self.allowed_chat_id:
            log.warning(
                "Команда %r из чужого чата %s — игнорирую",
                text.split()[0], chat_id or "?",
            )
            return

        name = self._parse_command(text)
        handler = self._handlers.get(name)
        if handler is None:
            log.info("Неизвестная команда /%s", name)
            return

        log.info("Команда /%s", name)
        try:
            reply = await handler()
        except Exception as exc:  # noqa: BLE001 - команда не должна валить сервис
            log.exception("Ошибка обработчика /%s: %s", name, exc)
            return

        if reply and not await self.sender.send_message(reply):
            log.error("Ответ на /%s не доставлен", name)

    @staticmethod
    def _parse_command(text: str) -> str:
        """'/info@my_bot arg' -> 'info'."""
        token = text.split()[0][1:]
        return token.split("@", 1)[0].lower()


def _backoff(attempt: int) -> float:
    return min(MAX_BACKOFF, 2.0 ** min(attempt, 6)) + random.uniform(0, 1.0)


async def _sleep_or_stop(stop_event: asyncio.Event, delay: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except asyncio.TimeoutError:
        pass
