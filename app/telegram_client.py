"""Telethon-клиент: авторизация пользователем, источник, переподключение.

Telethon здесь используется ТОЛЬКО для чтения сообщений от исходного бота
(MTProto, сессия вашего аккаунта). Отправка результата живёт в bot_sender.py.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional, Union

from telethon import TelegramClient, utils
from telethon import errors as tg_errors
from telethon.tl import types

from .config import Config

log = logging.getLogger(__name__)


class SourceNotFoundError(RuntimeError):
    """Источник (бот) не найден или недоступен для этого аккаунта."""


class NotAuthorizedError(RuntimeError):
    """Нет валидной сессии, а интерактивный вход невозможен."""


class BotSessionError(RuntimeError):
    """Сессия принадлежит боту, а не пользователю — читать нечего."""


# --------------------------------------------------------------------------- #
# Создание клиента
# --------------------------------------------------------------------------- #

def create_client(config: Config) -> TelegramClient:
    """Собирает TelegramClient с настройками устойчивости к сети.

    auto_reconnect + connection_retries=infinite — Telethon сам поднимает
    соединение после потери интернета, перезагрузки роутера и т.п.
    flood_sleep_threshold — короткие FloodWait библиотека пережидает сама.
    """
    return TelegramClient(
        str(config.session_path),
        config.api_id,
        config.api_hash,
        auto_reconnect=True,
        connection_retries=None,      # None = повторять бесконечно, но с паузами
        retry_delay=5,
        request_retries=5,
        timeout=15,
        flood_sleep_threshold=60,
        # Логи Telethon уходят в отдельный logger, чтобы их можно было
        # приглушить, не трогая логи приложения.
        base_logger="telethon",
    )


def _ensure_user_account(me, session_path: str) -> None:
    """Отклоняет сессию бота.

    Telethon ``start()`` спрашивает «phone (or bot token)», и если в ответ
    вставить токен @BotFather, получится валидная сессия — но БОТА. Такой
    клиент не получает сообщения от других ботов (Telegram это запрещает),
    поэтому handler молчит, а в логах вообще ничего нет. Ловим это на старте.
    """
    if not getattr(me, "bot", False):
        return
    raise BotSessionError(
        f"Telethon-сессия авторизована как БОТ @{getattr(me, 'username', '?')} "
        f"(id={getattr(me, 'id', '?')}), а нужен ваш пользовательский аккаунт.\n"
        f"Боты не могут получать сообщения от других ботов, поэтому источник "
        f"читаться не будет и в логах не появится ни одного события.\n"
        f"Починить:\n"
        f"  1) sudo systemctl stop telegram-filter\n"
        f"  2) rm {session_path}\n"
        f"  3) python -m app.main --login   # ввести НОМЕР ТЕЛЕФОНА, не токен бота\n"
        f"  4) sudo systemctl start telegram-filter"
    )


async def ensure_authorized(client: TelegramClient, *, allow_interactive: bool) -> None:
    """Подключается и проверяет сессию.

    Если сессии нет:
      * в интерактивном режиме (TTY) — запускает обычный вход Telethon
        (телефон -> код -> пароль 2FA);
      * под systemd — падает с понятной ошибкой, не пытаясь читать stdin.
    """
    if not client.is_connected():
        await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        _ensure_user_account(me, str(getattr(client.session, "filename", "") or "<session>"))
        log.info(
            "Telegram client connected as %s (id=%s)",
            utils.get_display_name(me) or "unknown",
            getattr(me, "id", "?"),
        )
        return

    if not allow_interactive:
        raise NotAuthorizedError(
            "Нет активной Telegram-сессии, а интерактивный вход недоступен "
            "(нет терминала).\n"
            "Выполните один раз вручную:  python -m app.main --login\n"
            "После этого рядом появится .session-файл и сервис будет "
            "стартовать без вопросов."
        )

    log.info("Сессия не найдена — запускаю интерактивную авторизацию Telethon")
    # client.start() спросит код из Telegram и пароль 2FA сам, а номер берём
    # своим промптом: штатный принимает и токен бота, что даёт нерабочую сессию.
    await client.start(phone=_prompt_phone)
    me = await client.get_me()
    _ensure_user_account(me, str(getattr(client.session, "filename", "") or "<session>"))
    log.info(
        "Авторизация успешна: %s (id=%s)",
        utils.get_display_name(me) or "unknown",
        getattr(me, "id", "?"),
    )


def _prompt_phone() -> str:
    """Спрашивает номер телефона и не даёт войти под ботом."""
    while True:
        value = input("Номер телефона вашего аккаунта (+380XXXXXXXXX): ").strip()
        if ":" in value:
            print(
                "Это токен бота, а не номер телефона. Сервис читает сообщения "
                "от чужого бота, а боты не видят сообщений друг друга — "
                "нужен ваш личный аккаунт."
            )
            continue
        if value:
            return value


# --------------------------------------------------------------------------- #
# Источник сообщений
# --------------------------------------------------------------------------- #

async def resolve_source(client: TelegramClient, source: Union[str, int]):
    """Находит entity исходного бота и проверяет, что он существует.

    Бросает SourceNotFoundError с понятным текстом, если источник недоступен.
    """
    try:
        entity = await client.get_entity(source)
    except (
        ValueError,
        TypeError,
        tg_errors.UsernameNotOccupiedError,
        tg_errors.UsernameInvalidError,
    ) as exc:
        raise SourceNotFoundError(
            f"Источник {source!r} не найден.\n"
            f"  * проверьте SOURCE_BOT_USERNAME в .env (формат: @some_bot);\n"
            f"  * у вашего аккаунта должен быть диалог с этим ботом "
            f"(откройте его и нажмите Start);\n"
            f"  * причина: {exc}"
        ) from exc
    except tg_errors.FloodWaitError as exc:
        raise SourceNotFoundError(
            f"Telegram просит подождать {exc.seconds} c перед следующим запросом "
            f"(FloodWait). Запустите сервис позже."
        ) from exc

    if isinstance(entity, types.User) and not entity.bot:
        log.warning(
            "Источник %s — обычный пользователь, а не бот. "
            "Сервис будет работать, но проверьте настройку.",
            source,
        )

    log.info(
        "Источник найден: %s (id=%s, username=@%s)",
        utils.get_display_name(entity) or source,
        utils.get_peer_id(entity),
        getattr(entity, "username", None),
    )
    return entity


def build_message_link(chat, message_id: int) -> Optional[str]:
    """Возвращает ссылку на оригинальное сообщение — или None.

    Ссылку можно построить только для каналов и супергрупп. У приватной
    переписки с ботом публичного URL не существует, и выдумывать его нельзя,
    поэтому возвращается None и строка со ссылкой просто не добавляется.
    """
    if isinstance(chat, types.Channel):
        username = getattr(chat, "username", None)
        if username:
            return f"https://t.me/{username}/{message_id}"
        return f"https://t.me/c/{chat.id}/{message_id}"
    return None


# --------------------------------------------------------------------------- #
# Переподключение
# --------------------------------------------------------------------------- #

async def run_until_stopped(
    client: TelegramClient,
    stop_event: asyncio.Event,
    *,
    max_delay: int = 60,
) -> None:
    """Держит клиент живым, пока не попросят остановиться.

    Telethon переподключается сам, но если он всё-таки отвалился совсем
    (например, интернета не было очень долго), внешний цикл поднимает
    соединение заново с экспоненциальной паузой — без tight loop.
    """
    attempt = 0

    while not stop_event.is_set():
        try:
            if not client.is_connected():
                log.info("Подключаюсь к Telegram…")
                await client.connect()
                if not await client.is_user_authorized():
                    raise NotAuthorizedError(
                        "Сессия перестала быть валидной (возможно, вы завершили "
                        "её в настройках Telegram). Выполните: python -m app.main --login"
                    )
                log.info("Соединение восстановлено")

            attempt = 0  # успешное подключение сбрасывает backoff

            disconnected = asyncio.ensure_future(client.run_until_disconnected())
            stopping = asyncio.ensure_future(stop_event.wait())
            done, pending = await asyncio.wait(
                {disconnected, stopping}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done:
                # Пробрасываем исключение, из-за которого клиент отключился.
                exc = task.exception()
                if exc is not None:
                    raise exc

            if stop_event.is_set():
                return

            log.warning("Соединение с Telegram потеряно — переподключаюсь")

        except NotAuthorizedError:
            raise
        except asyncio.CancelledError:
            raise
        except tg_errors.FloodWaitError as exc:
            wait_for = min(exc.seconds + 5, max_delay * 10)
            log.warning("FloodWait %s c — жду прежде чем продолжить", exc.seconds)
            await _sleep_or_stop(stop_event, wait_for)
        except (
            ConnectionError,
            OSError,
            asyncio.TimeoutError,
            tg_errors.RPCError,
        ) as exc:
            attempt += 1
            delay = _backoff(attempt, max_delay)
            log.warning(
                "Ошибка соединения с Telegram (%s: %s). Повтор через %.1f c",
                type(exc).__name__, exc, delay,
            )
            await _sleep_or_stop(stop_event, delay)
        else:
            attempt += 1
            await _sleep_or_stop(stop_event, _backoff(attempt, max_delay))


def _backoff(attempt: int, max_delay: int) -> float:
    return min(float(max_delay), 2.0 ** min(attempt, 6)) + random.uniform(0, 1.0)


async def _sleep_or_stop(stop_event: asyncio.Event, delay: float) -> None:
    """Спит, но просыпается немедленно, если пришёл сигнал остановки."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except asyncio.TimeoutError:
        pass
