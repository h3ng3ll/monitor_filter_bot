"""Приём команд целевого бота: long polling getUpdates.

Отправка живёт в bot_sender.py, а здесь — приём. Telethon не при делах,
это второй, независимый канал через Bot API.

Безопасность: слушатель сам ничего не решает. Кто имеет доступ, спрашивается
у ``authorize`` (в проде — таблица users в SQLite), поэтому отозванный доступ
действует немедленно, без перезапуска. Незнакомцу отвечают только публичные
команды (/start, /help): посторонний, нашедший бота по имени, не должен даже
узнать, какие команды существуют.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple, Union

import aiohttp

from .bot_sender import BotApiError, BotSender

log = logging.getLogger(__name__)

# Пауза после ошибки опроса: растёт до этого предела, чтобы не долбить API.
MAX_BACKOFF = 60.0


@dataclass(frozen=True)
class CommandContext:
    """Кто, откуда и с какими аргументами позвал команду."""

    user_id: int
    chat_id: int
    username: str
    full_name: str
    args: str
    is_admin: bool
    # Заполнены только для нажатия инлайн-кнопки.
    callback_id: Optional[str] = None
    message_id: Optional[int] = None

    @property
    def label(self) -> str:
        name = self.full_name or "без имени"
        handle = f"@{self.username}" if self.username else "без username"
        return f"{name} ({handle}, id={self.user_id})"


@dataclass(frozen=True)
class Access:
    """Решение о доступе. Принимается снаружи, слушатель его только применяет."""

    allowed: bool
    is_admin: bool


Handler = Callable[[CommandContext], Awaitable[Optional[str]]]
Authorizer = Callable[[int], Awaitable[Access]]


@dataclass(frozen=True)
class _Registration:
    handler: Handler
    public: bool      # доступна всем, включая незнакомцев
    admin_only: bool


async def _allow_everything(_: int) -> Access:
    """Поведение по умолчанию, если авторизатор не задан (тесты, одиночный режим)."""
    return Access(allowed=True, is_admin=True)


class CommandListener:
    """Опрашивает getUpdates и вызывает обработчик по имени команды."""

    def __init__(
        self,
        sender: BotSender,
        *,
        authorize: Optional[Authorizer] = None,
        owner_chat_id: Optional[Union[str, int]] = None,
        poll_timeout: int = 15,
    ) -> None:
        self.sender = sender
        self._authorize = authorize or _allow_everything
        # Чат владельца из .env: полный доступ даже если БД пуста.
        self.owner_chat_id = None if owner_chat_id is None else str(owner_chat_id)
        self.poll_timeout = poll_timeout
        self._handlers: Dict[str, _Registration] = {}
        self._callbacks: Dict[str, _Registration] = {}
        self._offset: Optional[int] = None

    def register(
        self,
        command: str,
        handler: Handler,
        *,
        public: bool = False,
        admin_only: bool = False,
    ) -> None:
        self._handlers[command.lstrip("/").lower()] = _Registration(
            handler=handler, public=public, admin_only=admin_only
        )

    def register_callback(
        self, action: str, handler: Handler, *, admin_only: bool = True
    ) -> None:
        """Обработчик нажатия инлайн-кнопки с callback_data вида 'action:arg'.

        По умолчанию кнопки админские: решения о доступе принимает только
        владелец. Кнопки, которые пользователь нажимает в своих же настройках,
        регистрируются с admin_only=False.
        """
        self._callbacks[action.lower()] = _Registration(
            handler=handler, public=False, admin_only=admin_only
        )

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
            "Слушаю команды: %s",
            ", ".join("/" + name for name in self._handlers) or "нет",
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
        if update.get("callback_query"):
            await self._dispatch_callback(update["callback_query"])
            return

        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return

        chat = message.get("chat") or {}
        author = message.get("from") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return

        name, args = self._parse_command(text)
        registration = self._handlers.get(name)
        if registration is None:
            log.info("Неизвестная команда /%s", name)
            return

        access = await self._resolve_access(chat_id, author.get("id"))
        if access is None:
            return
        if not self._permitted(registration, access):
            log.warning("Команда /%s от %s отклонена: нет прав",
                        name, author.get("id") or chat_id)
            return

        ctx = self._context(chat_id, author, args, access)
        log.info("Команда /%s от %s", name, ctx.user_id)
        reply = await self._invoke(registration.handler, ctx, f"/{name}")
        if reply and not await self.sender.send_message(reply, chat_id=ctx.chat_id):
            log.error("Ответ на /%s не доставлен в чат %s", name, ctx.chat_id)

    async def _dispatch_callback(self, query: Dict[str, Any]) -> None:
        """Нажатие инлайн-кнопки: callback_data вида 'approve:12345'."""
        data = (query.get("data") or "").strip()
        author = query.get("from") or {}
        message = query.get("message") or {}
        chat_id = ((message.get("chat") or {}).get("id"))
        callback_id = query.get("id")
        if not data or chat_id is None:
            return

        action, _, args = data.partition(":")
        registration = self._callbacks.get(action.lower())
        if registration is None:
            log.info("Неизвестное действие кнопки %r", action)
            return

        access = await self._resolve_access(chat_id, author.get("id"))
        # Права проверяем по нажавшему, а не по callback_data: сами данные
        # кнопки ничего не доказывают, карточку могли переслать.
        if access is None or not self._permitted(registration, access):
            log.warning("Нажатие %r от %s отклонено: нет прав",
                        action, author.get("id"))
            if callback_id:
                await self.sender.answer_callback_query(
                    callback_id, "Недостаточно прав"
                )
            return
        handler = registration.handler

        ctx = self._context(
            chat_id,
            author,
            args.strip(),
            access,
            callback_id=callback_id,
            message_id=message.get("message_id"),
        )
        log.info("Кнопка %s от %s", action, ctx.user_id)
        reply = await self._invoke(handler, ctx, f"кнопка {action}")
        if reply and not await self.sender.send_message(reply, chat_id=ctx.chat_id):
            log.error("Ответ на кнопку %s не доставлен", action)

    async def _resolve_access(
        self, chat_id: Any, user_id: Any
    ) -> Optional[Access]:
        """Права вызывающего. None — апдейт вообще не обрабатываем."""
        # Чат владельца из .env — полный доступ, как до многопользовательского режима.
        if self.owner_chat_id is not None and str(chat_id) == self.owner_chat_id:
            return Access(allowed=True, is_admin=True)
        if user_id is None:
            return None  # сообщение без автора (пост в канале)
        try:
            return await self._authorize(int(user_id))
        except Exception as exc:  # noqa: BLE001 - сбой БД не должен ронять опрос
            log.exception("Не удалось проверить доступ для %s: %s", user_id, exc)
            return None

    @staticmethod
    def _permitted(registration: _Registration, access: Access) -> bool:
        if registration.admin_only:
            return access.is_admin
        return access.allowed or registration.public

    @staticmethod
    def _context(
        chat_id: Any,
        author: Dict[str, Any],
        args: str,
        access: Access,
        *,
        callback_id: Optional[str] = None,
        message_id: Optional[int] = None,
    ) -> CommandContext:
        full_name = " ".join(
            str(part)
            for part in (author.get("first_name"), author.get("last_name"))
            if part
        )
        return CommandContext(
            user_id=int(author.get("id") or chat_id),
            chat_id=int(chat_id),
            username=str(author.get("username") or ""),
            full_name=full_name,
            args=args,
            is_admin=access.is_admin,
            callback_id=callback_id,
            message_id=message_id,
        )

    async def _invoke(
        self, handler: Handler, ctx: CommandContext, what: str
    ) -> Optional[str]:
        try:
            return await handler(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - команда не должна валить сервис
            log.exception("Ошибка обработчика %s: %s", what, exc)
            # Молчать нельзя: человек не отличит поломку от «так задумано»,
            # а в журнал он не смотрит. Сама команда при этом сервис не валит.
            return ("⚠️ Команда не выполнена: внутренняя ошибка сервиса. "
                    "Подробности — в журнале (journalctl -u telegram-filter).")

    @staticmethod
    def _parse_command(text: str) -> Tuple[str, str]:
        """'/add@my_bot новое слово' -> ('add', 'новое слово')."""
        head, _, rest = text.partition(" ")
        return head[1:].split("@", 1)[0].lower(), rest.strip()


def _backoff(attempt: int) -> float:
    return min(MAX_BACKOFF, 2.0 ** min(attempt, 6)) + random.uniform(0, 1.0)


async def _sleep_or_stop(stop_event: asyncio.Event, delay: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except asyncio.TimeoutError:
        pass
