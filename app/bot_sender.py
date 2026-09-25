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
import html
import logging
import random
from datetime import datetime
from typing import Any, Dict, Optional, Sequence, Tuple, Union

import aiohttp

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"

# Жёсткий лимит Telegram на текст одного сообщения.
TELEGRAM_MAX_TEXT = 4096
# Запас под служебные строки (заголовок, источник, время, ID, ссылка).
TRUNCATION_SUFFIX = "\n…[сообщение обрезано]"


class BotApiError(RuntimeError):
    """Ошибка Bot API, которую нет смысла повторять (неверный токен, chat_id…)."""


# --------------------------------------------------------------------------- #
# Форматирование
# --------------------------------------------------------------------------- #

def escape_html(text: str) -> str:
    """Экранирует текст для Telegram HTML parse mode.

    Telegram требует экранировать ``&``, ``<`` и ``>``; кавычки трогать не нужно,
    поэтому ``quote=False`` — так текст остаётся читаемым.
    """
    return html.escape(text or "", quote=False)


def format_message(
    *,
    source_label: str,
    sent_at: Optional[datetime],
    message_id: int,
    text: str,
    reason: Optional[str] = None,
    link: Optional[str] = None,
    header: str = "🚨 ВАЖНОЕ СООБЩЕНИЕ",
    max_length: int = TELEGRAM_MAX_TEXT,
) -> str:
    """Собирает готовое HTML-уведомление.

    Весь пользовательский текст экранируется, поэтому символы вроде ``<``
    или ``&`` из исходного сообщения не ломают разметку.
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
    body_raw = text.strip() if text else "(без текста)"
    body = _truncate(body_raw, body_budget)

    return f"{head}\n{escape_html(body)}\n{tail}"


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


def _truncate(text: str, budget: int) -> str:
    """Обрезает текст так, чтобы экранированная версия влезла в бюджет."""
    if budget <= 0:
        return "…"
    # Экранирование может удлинить строку (& -> &amp;), поэтому проверяем
    # результат и уменьшаем срез, пока не влезем.
    if len(escape_html(text)) <= budget:
        return text
    limit = budget - len(TRUNCATION_SUFFIX)
    while limit > 0:
        candidate = text[:limit] + TRUNCATION_SUFFIX
        if len(escape_html(candidate)) <= budget:
            return candidate
        limit -= max(1, (len(escape_html(candidate)) - budget))
    return "…"


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

    async def send_message(self, html_text: str, *, disable_preview: bool = True) -> bool:
        """Отправляет готовый HTML в целевой чат.

        Возвращает True при успехе. При постоянной ошибке (неверный chat_id,
        забаненный бот и т.п.) возвращает False — вызывающий код не должен
        помечать сообщение как обработанное.
        """
        payload = {
            "chat_id": self.chat_id,
            "text": html_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }
        try:
            await self._call("sendMessage", payload)
            return True
        except BotApiError as exc:
            log.error("Bot API отказал в отправке: %s", exc)
            return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.error("Bot API недоступен после всех попыток: %s", exc)
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
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        return await self._call(
            "getUpdates", payload, http_timeout=timeout + self.timeout.total
        )

    async def set_my_commands(self, commands: Sequence[Tuple[str, str]]) -> None:
        """Регистрирует меню команд, чтобы /info был виден в интерфейсе.

        Не критично: при ошибке только пишем в лог — бот остаётся рабочим.
        """
        try:
            await self._call(
                "setMyCommands",
                {"commands": [
                    {"command": name, "description": text}
                    for name, text in commands
                ]},
            )
            log.info("Меню команд обновлено: %s",
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
                        last_error = BotApiError(f"{method}: {description}")
                        log.warning(
                            "Bot API 5xx на %s: %s (попытка %s/%s)",
                            method, description, attempt, self.max_retries + 1,
                        )
                        await asyncio.sleep(self._backoff(attempt))
                        continue

                    # 400/401/403 — повторять бессмысленно.
                    raise BotApiError(f"{method}: {description}")

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
