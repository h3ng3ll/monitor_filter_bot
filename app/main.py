"""Точка входа сервиса.

Поток работы:

    Telethon NewMessage (от источника)
        -> проверка отправителя
        -> текст сообщения
        -> MessageFilter
        -> проверка дубликата (SQLite)
        -> format_message()
        -> Bot API -> целевой чат
        -> запись в SQLite (только после успешной отправки)

Режимы запуска:
    python -m app.main                      # сервис (systemd)
    python -m app.main --login              # разовая интерактивная авторизация
    python -m app.main --check-filter "…"   # проверить фильтр на строке
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone
from typing import Optional

from telethon import events, utils
from telethon import errors as tg_errors

from .bot_commands import CommandListener
from .bot_sender import BotApiError, BotSender, format_message, format_status
from .config import Config, ConfigError, load_config
from .filter import MessageFilter
from .models import ProcessedMessageStore
from .telegram_client import (
    BotSessionError,
    NotAuthorizedError,
    SourceNotFoundError,
    build_message_link,
    create_client,
    ensure_authorized,
    resolve_source,
    run_until_stopped,
)

log = logging.getLogger("app")

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Ошибки, которые сами не пройдут: неверный .env, сессия бота, отсутствие
# сессии, отвергнутый токен бота. systemd не перезапускает сервис с этим
# кодом (RestartPreventExitStatus=78), поэтому бюджет StartLimitBurst не
# сгорает и после починки обычный `systemctl start` срабатывает сразу.
EXIT_PERMANENT = 78  # EX_CONFIG из sysexits.h


def setup_logging(level: str) -> None:
    """Логи идут в stdout — systemd сам кладёт их в journald."""
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format=LOG_FORMAT,
        datefmt=DATE_FORMAT,
        stream=sys.stdout,
        force=True,
    )
    # Telethon на DEBUG крайне шумный — держим его на уровень выше.
    logging.getLogger("telethon").setLevel(
        logging.INFO if level == "DEBUG" else logging.WARNING
    )
    logging.getLogger("asyncio").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
# Приложение
# --------------------------------------------------------------------------- #

class Application:
    """Связывает конфиг, Telethon, фильтр, SQLite и Bot API."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.filter = MessageFilter.from_config(config)
        self.store = ProcessedMessageStore(config.db_path, config.db_retention_days)
        self.sender = BotSender(
            config.target_bot_token,
            config.target_chat_id,
            timeout=config.bot_api_timeout,
            max_retries=config.bot_api_max_retries,
            max_flood_wait=config.flood_wait_max_seconds,
        )
        self.client = create_client(config)
        self.commands = CommandListener(
            self.sender, allowed_chat_id=config.target_chat_id
        )
        self.commands.register("info", self._cmd_info)
        self.commands.register("start", self._cmd_start)
        self.stop_event = asyncio.Event()
        self.source_entity = None
        self.source_id: Optional[int] = None
        self.source_label: str = config.source_label
        self.started_at = datetime.now(timezone.utc)
        self.account: str = "неизвестен"

    # -- запуск / остановка ------------------------------------------------ #

    async def run(self) -> int:
        self._install_signal_handlers()
        try:
            await self.store.connect()
            removed = await self.store.prune()
            if removed:
                log.info("Очищено старых записей из журнала: %s", removed)

            await self.sender.start()
            await ensure_authorized(self.client, allow_interactive=False)
            self.account = await self._describe_account()

            self.source_entity = await resolve_source(self.client, self.config.source)
            self.source_id = utils.get_peer_id(self.source_entity)
            username = getattr(self.source_entity, "username", None)
            self.source_label = f"@{username}" if username else str(self.config.source)

            self._register_handler()

            log.info("Monitoring %s", self.source_label)
            log.info("Filter rules: %s", self.filter.describe())
            log.info("Target chat: %s", self.config.target_chat_id)

            await self._send_startup_probe()

            listener: Optional[asyncio.Future] = None
            if self.config.bot_commands:
                await self.sender.set_my_commands(
                    [("info", "Настройки и состояние сервиса")]
                )
                listener = asyncio.ensure_future(self.commands.run(self.stop_event))
            else:
                log.info("Команды бота отключены (BOT_COMMANDS=false)")

            try:
                await run_until_stopped(
                    self.client,
                    self.stop_event,
                    max_delay=self.config.reconnect_max_delay,
                )
            finally:
                if listener is not None:
                    listener.cancel()
                    # Ждём завершения, чтобы не оставить висящий запрос к Bot API.
                    await asyncio.gather(listener, return_exceptions=True)
            return 0
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Аккуратно закрывает Telethon, HTTP и SQLite — без traceback."""
        log.info("Завершение работы…")
        try:
            if self.client.is_connected():
                await self.client.disconnect()
                log.info("Telegram client disconnected")
        except Exception as exc:  # noqa: BLE001 - shutdown не должен падать
            log.warning("Ошибка при отключении Telethon: %s", exc)

        try:
            await self.sender.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("Ошибка при закрытии HTTP-сессии: %s", exc)

        try:
            await self.store.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("Ошибка при закрытии SQLite: %s", exc)

        log.info("Сервис остановлен")

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._request_stop, sig.name)
            except NotImplementedError:  # pragma: no cover - не Linux
                signal.signal(sig, lambda *_: self._request_stop(sig.name))

    def _request_stop(self, signal_name: str) -> None:
        if not self.stop_event.is_set():
            log.info("Получен %s — останавливаюсь", signal_name)
            self.stop_event.set()
        # Разбудить run_until_disconnected: отключение вернёт управление.
        asyncio.ensure_future(self._disconnect_quietly())

    async def _disconnect_quietly(self) -> None:
        try:
            if self.client.is_connected():
                await self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    # -- команды бота ------------------------------------------------------- #

    async def _describe_account(self) -> str:
        """Имя и id аккаунта Telethon — главный признак верной настройки."""
        try:
            me = await self.client.get_me()
        except Exception as exc:  # noqa: BLE001 - не повод падать
            log.warning("Не удалось получить аккаунт: %s", exc)
            return "неизвестен"
        return f"{utils.get_display_name(me) or 'unknown'} (id={getattr(me, 'id', '?')})"

    async def _cmd_info(self) -> str:
        """Ответ на /info: настройки и состояние сервиса."""
        probe = (
            f"включена ({self.config.startup_probe_limit} сообщений)"
            if self.config.startup_probe else "выключена"
        )
        try:
            processed = str(await self.store.count())
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось прочитать счётчик журнала: %s", exc)
            processed = "неизвестно"

        return format_status(
            header="ℹ️ СОСТОЯНИЕ СЕРВИСА",
            fields=[
                ("Аккаунт", self.account),
                ("Источник", self.source_label),
                ("Правила", self.filter.describe()),
                ("Целевой чат", str(self.config.target_chat_id)),
                ("Самопроверка при старте", probe),
                ("Переслано сообщений", processed),
                ("Работает", _format_uptime(datetime.now(timezone.utc) - self.started_at)),
            ],
        )

    async def _cmd_start(self) -> str:
        return format_status(
            header="👋 Фильтр App Store Monitor",
            fields=[("Источник", self.source_label)],
            note="Сюда приходят только сообщения, подходящие под правила. "
                 "/info — показать настройки и состояние.",
        )

    # -- самопроверка при старте -------------------------------------------- #

    async def _send_startup_probe(self) -> None:
        """Присылает последнее совпадение из истории источника.

        Смысл — увидеть в целевом чате, что связка «аккаунт -> источник ->
        фильтр -> бот» реально работает, не дожидаясь нового сообщения.
        Историю не помечаем обработанной: это не пересылка, а диагностика,
        и старое сообщение повторно как NewMessage всё равно не придёт.

        Настройки в уведомление не подмешиваем — они доступны по /info.
        Ошибки здесь никогда не роняют сервис: мониторинг важнее самопроверки.
        """
        if not self.config.startup_probe:
            log.info("Startup probe disabled (STARTUP_PROBE=false)")
            return

        limit = self.config.startup_probe_limit

        try:
            found = await self._find_last_match(limit)
        except tg_errors.FloodWaitError as exc:
            log.warning("Startup probe: FloodWait %s c — пропускаю", exc.seconds)
            return
        except (
            ConnectionError,
            OSError,
            asyncio.TimeoutError,
            tg_errors.RPCError,
            ValueError,
        ) as exc:
            log.warning("Startup probe: история недоступна (%s: %s)",
                        type(exc).__name__, exc)
            payload = format_status(
                header="⚠️ СЕРВИС ЗАПУЩЕН — история недоступна",
                fields=[("Источник", self.source_label)],
                note=f"Прочитать историю не удалось: {exc}. Мониторинг новых "
                     f"сообщений при этом работает. /info — настройки.",
            )
            await self._send_probe_payload(payload)
            return

        if found is None:
            payload = format_status(
                header="⚙️ СЕРВИС ЗАПУЩЕН — совпадений нет",
                fields=[
                    ("Источник", self.source_label),
                    ("Просмотрено", f"последние {limit} сообщений"),
                ],
                note="Ни одно из последних сообщений источника не подходит под "
                     "правила. Если это неожиданно — посмотрите /info.",
            )
        else:
            message, result = found
            try:
                chat = await message.get_chat()
                link = build_message_link(chat, message.id)
            except Exception:  # noqa: BLE001 - ссылка необязательна
                link = None
            payload = format_message(
                header="✅ СЕРВИС ЗАПУЩЕН — последнее совпадение",
                source_label=self.source_label,
                sent_at=message.date,
                message_id=message.id,
                text=message.raw_text or "",
                reason=result.reason,
                link=link,
            )

        await self._send_probe_payload(payload)

    async def _send_probe_payload(self, payload: str) -> None:
        if await self.sender.send_message(payload):
            log.info("Startup probe sent to %s", self.config.target_chat_id)
        else:
            log.error(
                "Startup probe НЕ доставлен — проверьте TARGET_CHAT_ID и то, "
                "что вы нажали Start у @%s",
                self.sender.bot_username or "своего бота",
            )

    async def _find_last_match(self, limit: int):
        """Идёт по истории источника от новых к старым до первого совпадения."""
        scanned = 0
        async for message in self.client.iter_messages(
            self.source_entity, limit=limit
        ):
            scanned += 1
            result = self.filter.check(message.raw_text or "")
            if result.matched:
                log.info(
                    "Startup probe: совпадение в сообщении %s (%s), просмотрено %s",
                    message.id, result.reason, scanned,
                )
                return message, result

        log.info("Startup probe: совпадений нет среди последних %s сообщений",
                 scanned)
        return None

    # -- обработка сообщений ------------------------------------------------ #

    def _register_handler(self) -> None:
        """Event handler вместо polling: Telethon сам присылает новые события."""
        self.client.add_event_handler(
            self._on_new_message,
            events.NewMessage(from_users=self.source_entity, incoming=True),
        )

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        """Обрабатывает одно новое сообщение. Никогда не бросает наружу."""
        try:
            await self._handle(event)
        except tg_errors.FloodWaitError as exc:
            wait_for = min(exc.seconds + 5, self.config.flood_wait_max_seconds)
            log.warning("FloodWait %s c при обработке сообщения — жду", exc.seconds)
            await asyncio.sleep(wait_for)
        except Exception as exc:  # noqa: BLE001 - один сбой не должен убить сервис
            log.exception("Необработанная ошибка при обработке сообщения: %s", exc)

    async def _handle(self, event: events.NewMessage.Event) -> None:
        message = event.message

        # Подстраховка: from_users уже отфильтровал, но проверяем явно.
        if self.source_id is not None and message.sender_id != self.source_id:
            log.debug("Message %s пропущен: чужой отправитель", message.id)
            return

        text = message.raw_text or ""
        log.info(
            "Message %s from %s at %s (len=%s)",
            message.id,
            self.source_label,
            message.date.isoformat() if message.date else "?",
            len(text),
        )
        if self.config.log_message_text:
            log.debug("Message %s text: %s", message.id, text)

        result = self.filter.check(text)
        if not result.matched:
            log.info("Message %s ignored (%s)", message.id, result.reason)
            return

        chat_id = message.chat_id
        if await self.store.is_processed(chat_id, message.id):
            log.info("Message %s skipped: already processed", message.id)
            return

        log.info("Message %s matched %s", message.id, result.reason)

        chat = await event.get_chat()
        payload = format_message(
            source_label=self.source_label,
            sent_at=message.date,
            message_id=message.id,
            text=text,
            reason=result.reason,
            link=build_message_link(chat, message.id),
        )

        sent = await self.sender.send_message(payload)
        if not sent:
            # Не помечаем как обработанное: сообщение не доставлено.
            log.error("Message %s NOT forwarded — запись в журнал не добавлена",
                      message.id)
            return

        await self.store.mark_processed(chat_id, message.id)
        log.info("Message %s forwarded successfully", message.id)


def _format_uptime(delta) -> str:
    """'2 д 3 ч 4 мин' — без секунд, они в /info не нужны."""
    total = int(delta.total_seconds())
    if total < 60:
        return "меньше минуты"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Режимы CLI
# --------------------------------------------------------------------------- #

async def run_service(config: Config) -> int:
    app = Application(config)
    return await app.run()


async def run_login(config: Config) -> int:
    """Разовая интерактивная авторизация: телефон -> код -> пароль 2FA."""
    client = create_client(config)
    try:
        await ensure_authorized(client, allow_interactive=True)
        print(f"\nГотово. Session-файл: {config.session_path}.session")
        print("Теперь сервис можно запускать через systemd.")
        return 0
    except tg_errors.PhoneNumberInvalidError:
        print("Неверный номер телефона. Формат: +380XXXXXXXXX", file=sys.stderr)
        return 1
    except tg_errors.SessionPasswordNeededError:
        print("Требуется пароль двухфакторной аутентификации.", file=sys.stderr)
        return 1
    finally:
        if client.is_connected():
            await client.disconnect()


def run_check_filter(config: Config, text: str) -> int:
    """Прогоняет строку через фильтр и печатает решение."""
    message_filter = MessageFilter.from_config(config)
    print(f"Правила: {message_filter.describe()}")
    result = message_filter.check(text)
    print(f"matched = {result.matched}")
    print(f"reason  = {result.reason}")
    if result.matched:
        print("\n--- так будет выглядеть уведомление ---")
        print(
            format_message(
                source_label=config.source_label,
                sent_at=None,
                message_id=0,
                text=text,
                reason=result.reason,
            )
        )
    return 0 if result.matched else 1


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="app.main",
        description="Telegram message filter: читает бота через Telethon, "
                    "фильтрует и пересылает своим ботом.",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Разовая интерактивная авторизация Telethon и выход",
    )
    parser.add_argument(
        "--check-filter",
        metavar="TEXT",
        help="Проверить текущие правила фильтра на указанном тексте и выйти",
    )
    parser.add_argument(
        "--env-file",
        metavar="PATH",
        help="Путь к .env (по умолчанию — .env в корне проекта)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)

    try:
        config = load_config(args.env_file)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return EXIT_PERMANENT

    setup_logging(config.log_level)

    try:
        if args.check_filter is not None:
            return run_check_filter(config, args.check_filter)
        if args.login:
            return asyncio.run(run_login(config))
        return asyncio.run(run_service(config))
    except SourceNotFoundError as exc:
        # Может быть временным (FloodWait, сбой resolve) — пусть systemd повторит.
        log.error("%s", exc)
        return 1
    except (NotAuthorizedError, BotSessionError) as exc:
        log.error("%s", exc)
        return EXIT_PERMANENT
    except BotApiError as exc:
        log.error(
            "Telegram Bot API отклонил запрос: %s\n"
            "  * проверьте TARGET_BOT_TOKEN (токен от @BotFather);\n"
            "  * проверьте TARGET_CHAT_ID и то, что бот добавлен в этот чат.",
            exc,
        )
        return EXIT_PERMANENT
    except KeyboardInterrupt:
        # SIGINT уже обработан внутри, но подстрахуемся от traceback.
        log.info("Прервано пользователем")
        return 0


if __name__ == "__main__":
    sys.exit(main())
