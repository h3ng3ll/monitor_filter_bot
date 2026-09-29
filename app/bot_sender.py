"""Отправка результата через Telegram Bot API.

Здесь используется ТОЛЬКО Bot API (HTTP) и токен вашего бота.
Никакой связи с Telethon-сессией пользователя: две авторизации не смешиваются.

Модуль отвечает за:
  * форматирование уведомления (:func:`format_message`) в безопасный HTML;
  * HTTP-запрос к api.telegram.org с retry/backoff и обработкой 429;
  * приём апдейтов (getUpdates) для команд бота — см. bot_commands.py.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from typing import Any, Dict, Optional, Sequence, Tuple, Union

import aiohttp

# escape_html и обрезка живут в formatting.py вместе с разбором entities.
# Здесь они пере-экспортируются, чтобы не ломать существующие импорты.
from .formatting import TRUNCATION_SUFFIX, escape_html, render_body

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"

# Жёсткий лимит Telegram на текст одного сообщения.
TELEGRAM_MAX_TEXT = 4096

__all__ = [
    "API_BASE",
    "TELEGRAM_MAX_TEXT",
    "TRUNCATION_SUFFIX",
    "BotApiError",
    "BotBlockedError",
    "BotSender",
    "escape_html",
    "format_message",
    "format_message_with_fallback",
    "format_status",
]


class BotApiError(RuntimeError):
    """Ошибка Bot API, которую нет смысла повторять (неверный токен, chat_id…)."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        description: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.description = description or message


class BotBlockedError(BotApiError):
    """Получатель недоступен навсегда: заблокировал бота, удалён, не нажимал Start."""


# Тексты Telegram, после которых слать в этот чат бессмысленно.
_BLOCKED_MARKERS = (
    "bot was blocked by the user",
    "user is deactivated",
    "chat not found",
    "bot can't initiate conversation with a user",
    "peer_id_invalid",
    "have no rights to send a message",
)

# Признаки того, что Bot API споткнулся именно о разметку, а не о чат или токен.
_MARKUP_ERROR_MARKERS = (
    "can't parse",
    "unsupported start tag",
    "unmatched end tag",
    "unclosed",
    "entity",
    "entities",
    "tg-emoji",
)


def _matches(description: str, markers: Sequence[str]) -> bool:
    lowered = (description or "").lower()
    return any(marker in lowered for marker in markers)


def format_message(
    *,
    source_label: str,
    sent_at: Optional[datetime],
    message_id: int,
    text: str,
    entities: Optional[Sequence[Any]] = None,
    reason: Optional[str] = None,
    link: Optional[str] = None,
    header: str = "🚨 ВАЖНОЕ СООБЩЕНИЕ",
    max_length: int = TELEGRAM_MAX_TEXT,
) -> str:
    """Собирает готовое HTML-уведомление.

    ``entities`` — разметка исходного сообщения (Telethon). Если она передана,
    копия воспроизводит оформление оригинала: ссылки остаются ссылками. Всё,
    что не попало в известные сущности, экранируется, поэтому символы вроде
    ``<`` или ``&`` из исходного сообщения разметку не ломают.
    Без ``entities`` результат такой же, как был до появления этой поддержки.

    Ссылка добавляется, только если она реально существует (``link is not None``) —
    выдуманные ссылки не строятся.
    """
    when = _format_datetime(sent_at)

    head_lines = [
        f"<b>{escape_html(header)}</b>",
        "",
        f"<b>Источник:</b> {escape_html(source_label)}",
        f"<b>Время:</b> {escape_html(when)}",
    ]
    if reason:
        head_lines.append(f"<b>Причина:</b> {escape_html(reason)}")
    head_lines.append("")
    head_lines.append("<b>Текст:</b>")

    tail_lines = ["", f"<b>ID:</b> {message_id}"]
    if link:
        tail_lines.append(f'🔗 <a href="{escape_html(link)}">Открыть оригинал</a>')

    head = "\n".join(head_lines)
    tail = "\n".join(tail_lines)

    body_budget = max_length - len(head) - len(tail) - 2  # 2 переноса строки
    if text:
        body = render_body(text, entities, budget=body_budget)
    else:
        body = escape_html("(без текста)")

    return f"{head}\n{body}\n{tail}"


def format_message_with_fallback(**kwargs: Any) -> Tuple[str, str]:
    """Пара «с разметкой оригинала, без неё».

    Второй вариант нужен на случай, когда Bot API не примет разметку: в нём
    остаётся только наша собственная, заведомо корректная. Лучше уведомление
    без оформления, чем потерянное уведомление.
    """
    rich = format_message(**kwargs)
    if not kwargs.get("entities"):
        return rich, rich
    return rich, format_message(**{**kwargs, "entities": None})


def format_status(
    *,
    header: str,
    fields: Sequence[Tuple[str, str]] = (),
    note: str = "",
    max_length: int = TELEGRAM_MAX_TEXT,
) -> str:
    """Служебное уведомление без текста исходного сообщения.

    Используется, когда пересылать нечего (например, при старте не нашлось
    ни одного совпадения) — тогда важны только сами настройки.
    """
    lines = [f"<b>{escape_html(header)}</b>", ""]
    for label, value in fields:
        lines.append(f"<b>{escape_html(label)}:</b> {escape_html(value)}")
    if note:
        lines.append("")
        lines.append(escape_html(note))
    return "\n".join(lines)[:max_length]


def _format_datetime(value: Optional[datetime]) -> str:
    if value is None:
        return "неизвестно"
    try:
        local = value.astimezone()  # переводим UTC из Telegram в локальное время Pi
    except (ValueError, OSError):
        local = value
    return local.strftime("%Y-%m-%d %H:%M:%S %Z").strip()


# --------------------------------------------------------------------------- #
# Клиент Bot API
# --------------------------------------------------------------------------- #

class BotSender:
    """Асинхронный клиент Telegram Bot API для одного целевого чата."""

    def __init__(
        self,
        token: str,
        chat_id: Union[str, int],
        *,
        timeout: int = 20,
        max_retries: int = 4,
        max_flood_wait: int = 900,
    ) -> None:
        self._token = token
        self.chat_id = chat_id
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_retries = max_retries
        self.max_flood_wait = max_flood_wait
        self._session: Optional[aiohttp.ClientSession] = None
        self.bot_username: Optional[str] = None

    # -- жизненный цикл -------------------------------------------------- #

    async def start(self) -> None:
        """Открывает HTTP-сессию и проверяет токен через getMe."""
        self._session = aiohttp.ClientSession(timeout=self.timeout)
        me = await self._call("getMe", {})
        self.bot_username = me.get("username")
        log.info("Bot API ready: @%s (id=%s)", self.bot_username, me.get("id"))

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            log.info("Bot API session closed")
        self._session = None

    async def __aenter__(self) -> "BotSender":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    # -- отправка --------------------------------------------------------- #

    async def deliver(
        self,
        chat_id: Union[str, int],
        html_text: str,
        *,
        disable_preview: bool = True,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Отправляет готовый HTML в указанный чат.

        Бросает BotBlockedError, если получатель недоступен навсегда, и
        BotApiError на прочих отказах — вызывающий код решает, что делать.
        """
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": html_text,
            "parse_mode": "HTML",
            # Тело содержит ссылки из оригинала: без этого флага Telegram
            # прицепил бы к каждому уведомлению карточку предпросмотра.
            "disable_web_page_preview": disable_preview,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self._call("sendMessage", payload)

    async def send_message(
        self,
        html_text: str,
        *,
        chat_id: Optional[Union[str, int]] = None,
        disable_preview: bool = True,
        reply_markup: Optional[Dict[str, Any]] = None,
        fallback_text: Optional[str] = None,
    ) -> bool:
        """Отправляет готовый HTML. Возвращает True при успехе.

        ``chat_id=None`` — чат по умолчанию из .env (TARGET_CHAT_ID).

        ``fallback_text`` — тот же текст без разметки оригинала. Если Bot API
        не разберёт HTML (чужой эмодзи, пересекающиеся сущности, новый тип
        сущности), уведомление уйдёт в запасном виде: потерять алерт из-за
        оформления нельзя. Повтор ровно один.
        """
        target = self.chat_id if chat_id is None else chat_id
        try:
            await self.deliver(
                target,
                html_text,
                disable_preview=disable_preview,
                reply_markup=reply_markup,
            )
            return True
        except BotApiError as exc:
            if (
                fallback_text is not None
                and fallback_text != html_text
                and _matches(str(exc), _MARKUP_ERROR_MARKERS)
            ):
                log.warning(
                    "Bot API не принял разметку (%s) — повторяю без неё", exc
                )
                return await self.send_message(
                    fallback_text,
                    chat_id=chat_id,
                    disable_preview=disable_preview,
                    reply_markup=reply_markup,
                )
            log.error("Bot API отказал в отправке в %s: %s", target, exc)
            return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.error("Bot API недоступен после всех попыток: %s", exc)
            return False

    async def answer_callback_query(
        self, callback_id: str, text: Optional[str] = None
    ) -> None:
        """Гасит «часики» на нажатой инлайн-кнопке и показывает всплывашку.

        Не критично: если не получилось, кнопка просто повисит — не повод
        ронять обработку нажатия.
        """
        payload: Dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        try:
            await self._call("answerCallbackQuery", payload)
        except (BotApiError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("answerCallbackQuery не прошёл: %s", exc)

    async def edit_message_text(
        self,
        chat_id: Union[str, int],
        message_id: int,
        html_text: str,
        *,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Переписывает уже отправленное сообщение (карточку заявки)."""
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": html_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        # Пустая клавиатура убирает кнопки, чтобы карточку не нажали дважды.
        payload["reply_markup"] = reply_markup or {"inline_keyboard": []}
        try:
            await self._call("editMessageText", payload)
            return True
        except (BotApiError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("Не удалось обновить карточку: %s", exc)
            return False

    # -- приём апдейтов ---------------------------------------------------- #

    async def get_updates(self, offset: Optional[int], timeout: int) -> Any:
        """Long polling getUpdates. Возвращает список апдейтов.

        Слушаем только ``message`` — остальные типы апдейтов Telegram не
        присылает, а значит и не копит их в очереди.
        HTTP-таймаут берём с запасом над long poll, иначе aiohttp оборвёт
        соединение раньше, чем Telegram ответит.
        """
        payload: Dict[str, Any] = {
            "timeout": timeout,
            # callback_query — нажатия инлайн-кнопок на карточках заявок.
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        return await self._call(
            "getUpdates", payload, http_timeout=timeout + self.timeout.total
        )

    async def set_my_commands(
        self,
        commands: Sequence[Tuple[str, str]],
        *,
        scope: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Регистрирует меню команд, чтобы они были видны в интерфейсе.

        ``scope`` позволяет показать расширенный список только админам.
        Не критично: при ошибке только пишем в лог — бот остаётся рабочим.
        """
        payload: Dict[str, Any] = {
            "commands": [
                {"command": name, "description": text}
                for name, text in commands
            ]
        }
        if scope is not None:
            payload["scope"] = scope
        try:
            await self._call("setMyCommands", payload)
            log.info("Меню команд обновлено%s: %s",
                     "" if scope is None else " (адресно)",
                     ", ".join("/" + name for name, _ in commands))
        except (BotApiError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("Не удалось обновить меню команд: %s", exc)

    # -- транспорт --------------------------------------------------------- #

    async def _call(
        self,
        method: str,
        payload: Dict[str, Any],
        *,
        http_timeout: Optional[float] = None,
    ) -> Any:
        """HTTP-вызов метода Bot API с retry/backoff.

        Повторяем: сетевые ошибки, таймауты, 5xx и 429 (по retry_after).
        Не повторяем: остальные 4xx — это ошибка конфигурации, а не сбой.
        """
        if self._session is None or self._session.closed:
            raise BotApiError("HTTP-сессия не открыта — вызовите await sender.start()")

        url = f"{API_BASE}/bot{self._token}/{method}"
        attempt = 0
        last_error: Optional[BaseException] = None

        while attempt <= self.max_retries:
            attempt += 1
            try:
                async with self._session.post(
                    url,
                    json=payload,
                    timeout=(
                        aiohttp.ClientTimeout(total=http_timeout)
                        if http_timeout is not None
                        else self.timeout
                    ),
                ) as response:
                    # Ответ Telegram всегда JSON, но при сбое прокси может прийти HTML.
                    try:
                        data = await response.json(content_type=None)
                    except Exception:  # noqa: BLE001 - тело не JSON
                        data = {}

                    if response.status == 200 and data.get("ok"):
                        return data.get("result", {})

                    description = data.get("description", f"HTTP {response.status}")

                    if response.status == 429:
                        retry_after = int(
                            (data.get("parameters") or {}).get("retry_after", 5)
                        )
                        if retry_after > self.max_flood_wait:
                            raise BotApiError(
                                f"{method}: rate limit {retry_after}s превышает лимит "
                                f"{self.max_flood_wait}s, отправка отменена"
                            )
                        log.warning(
                            "Bot API rate limit на %s: ждём %s c (попытка %s/%s)",
                            method, retry_after, attempt, self.max_retries + 1,
                        )
                        await asyncio.sleep(retry_after)
                        continue

                    if response.status >= 500:
                        last_error = BotApiError(
                            f"{method}: {description}",
                            status=response.status,
                            description=description,
                        )
                        log.warning(
                            "Bot API 5xx на %s: %s (попытка %s/%s)",
                            method, description, attempt, self.max_retries + 1,
                        )
                        await asyncio.sleep(self._backoff(attempt))
                        continue

                    # 400/401/403 — повторять бессмысленно.
                    if response.status in (400, 403) and _matches(
                        description, _BLOCKED_MARKERS
                    ):
                        # Отдельный тип: получателя нет смысла беспокоить снова.
                        raise BotBlockedError(
                            f"{method}: {description}",
                            status=response.status,
                            description=description,
                        )
                    raise BotApiError(
                        f"{method}: {description}",
                        status=response.status,
                        description=description,
                    )

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                delay = self._backoff(attempt)
                log.warning(
                    "Сетевая ошибка при вызове %s: %s. Повтор через %.1f c "
                    "(попытка %s/%s)",
                    method, exc, delay, attempt, self.max_retries + 1,
                )
                await asyncio.sleep(delay)

        if isinstance(last_error, BaseException):
            raise last_error
        raise BotApiError(f"{method}: не удалось выполнить запрос")

    def _backoff(self, attempt: int) -> float:
        """Экспоненциальный backoff с джиттером, максимум 60 секунд."""
        base = min(60.0, 2.0 ** attempt)
        return base + random.uniform(0, 1.0)
