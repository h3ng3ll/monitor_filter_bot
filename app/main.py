"""Точка входа сервиса.

Поток работы:

    Telethon NewMessage (от источника)
        -> проверка отправителя
        -> текст сообщения + разметка (entities)
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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from telethon import events, utils
from telethon import errors as tg_errors

from .bot_commands import Access, CommandListener
from .bot_handlers import PUBLIC_MENU, BotHandlers
from .bot_sender import (
    BotApiError,
    BotBlockedError,
    BotSender,
    format_message,
    format_message_with_fallback,
    format_status,
)
from .formatting import message_entities
from .config import Config, ConfigError, load_config
from .filter import MessageFilter
from .models import ProcessedMessageStore
from .users import Recipient, UserStore
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

# Отправки идут последовательно, и круг до api.telegram.org с Pi занимает
# 80–200 мс, то есть потолок ~5–12 сообщений/с — вдвое-втрое ниже общего
# лимита Bot API. Пауза включается только на больших рассылках.
FANOUT_PAUSE = 0.05
FANOUT_PAUSE_AFTER = 20


@dataclass(frozen=True)
class _Target:
    """Один получатель рассылки."""

    key: str                       # chat_id для Bot API и ключ в журнале доставок
    label: str                     # как назвать в логе
    message_filter: MessageFilter
    user_id: Optional[int] = None  # None — получатель из .env, а не пользователь


def _is_markup_failure(exc: BotApiError) -> bool:
    """Telegram споткнулся о разметку, а не о чат или токен."""
    lowered = str(exc).lower()
    return "parse" in lowered or "entit" in lowered or "tag" in lowered


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
        self.store = ProcessedMessageStore(
            config.db_path,
            config.db_retention_days,
            legacy_recipient=(
                str(config.target_chat_id) if config.legacy_delivery else None
            ),
        )
        self.users = UserStore(
            config.db_path, max_keywords=config.max_keywords_per_user
        )
        self.sender = BotSender(
            config.target_bot_token,
            config.target_chat_id,
            timeout=config.bot_api_timeout,
            max_retries=config.bot_api_max_retries,
            max_flood_wait=config.flood_wait_max_seconds,
        )
        self.client = create_client(config)
        self.commands = CommandListener(
            self.sender,
            authorize=self._authorize,
            owner_chat_id=config.target_chat_id,
        )
        self.handlers = BotHandlers(self)
        self.handlers.register_all(self.commands)
        self.stop_event = asyncio.Event()
        self.source_entity = None
        self.source_id: Optional[int] = None
        self.source_label: str = config.source_label
        self.started_at = datetime.now(timezone.utc)
        self.account: str = "неизвестен"
        # Владелец Telethon-сессии — он же администратор: сообщения приходят
        # именно его аккаунту, ему и решать, кому их пересылать.
        self.owner_id: Optional[int] = None
        # Скомпилированные фильтры пользователей: user_id -> (rev, фильтр).
        # Ключ по rev, поэтому правка правил сама обесценивает запись.
        self._user_filters: Dict[int, Tuple[int, MessageFilter]] = {}

    # -- запуск / остановка ------------------------------------------------ #

    async def run(self) -> int:
        self._install_signal_handlers()
        try:
            await self.store.connect()
            await self.users.connect()
            removed = await self.store.prune()
            if removed:
                log.info("Очищено старых записей из журнала: %s", removed)

            await self.sender.start()
            await ensure_authorized(self.client, allow_interactive=False)
            self.account = await self._describe_account()
            await self._setup_admins()

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
                await self.sender.set_my_commands(PUBLIC_MENU)
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

        try:
            await self.users.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("Ошибка при закрытии SQLite (пользователи): %s", exc)

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
        """Имя и id аккаунта Telethon — главный признак верной настройки.

        Попутно запоминаем id владельца: это и есть администратор сервиса.
        """
        try:
            me = await self.client.get_me()
        except Exception as exc:  # noqa: BLE001 - не повод падать
            log.warning("Не удалось получить аккаунт: %s", exc)
            return "неизвестен"
        owner_id = getattr(me, "id", None)
        if isinstance(owner_id, int):
            self.owner_id = owner_id
        return f"{utils.get_display_name(me) or 'unknown'} (id={owner_id or '?'})"

    async def _setup_admins(self) -> None:
        """Владелец сессии — админ по умолчанию, плюс ADMIN_IDS из .env."""
        if not self.config.multi_user:
            log.info("Многопользовательский режим выключен (MULTI_USER=false)")
            return

        admin_ids: List[int] = []
        if self.owner_id is not None:
            admin_ids.append(self.owner_id)
        else:
            # get_me не ответил: не оставлять же сервис без админа совсем.
            log.warning(
                "Не удалось определить владельца сессии — админом станет только "
                "то, что указано в ADMIN_IDS"
            )
        admin_ids.extend(
            admin_id for admin_id in self.config.admin_ids
            if admin_id not in admin_ids
        )

        if not admin_ids:
            log.error(
                "Админов нет: заявки на доступ будет некому одобрять. "
                "Укажите ADMIN_IDS в .env."
            )
            return

        owner_chat = (
            self.config.target_chat_id
            if isinstance(self.config.target_chat_id, int)
            else None
        )
        for admin_id in admin_ids:
            chat_id = owner_chat if admin_id == self.owner_id else admin_id
            await self.users.ensure_admin(
                admin_id, chat_id=chat_id, label=self.account
            )
        log.info("Администраторы: %s", ", ".join(str(i) for i in admin_ids))

        counts = await self.users.counts()
        log.info(
            "Пользователи: одобрено %s, заявок %s",
            counts.get("approved", 0), counts.get("pending", 0),
        )

    async def admin_chat_ids(self) -> List[int]:
        """Куда слать уведомления админам."""
        chats: List[int] = []
        for admin in await self.users.list_admins():
            chat = admin.target_chat
            if chat and chat not in chats:
                chats.append(chat)
        return chats

    async def _authorize(self, user_id: int) -> Access:
        """Права для CommandListener — из БД, поэтому отзыв срабатывает сразу."""
        user = await self.users.get(user_id)
        if user is None:
            return Access(allowed=False, is_admin=False)

        if user.blocked:
            # Апдейт от человека доказывает, что бот до него достучится, —
            # значит он снял блокировку. Снимаем отметку сами, иначе он
            # навсегда остался бы вне рассылки: приветствие на /start
            # приходило бы, а сообщения — нет.
            log.info("Пользователь %s снова доступен — возвращаю в рассылку",
                     user_id)
            await self.users.set_blocked(user_id, False)
            self.drop_filter_cache(user_id)

        return Access(
            allowed=user.status == "approved", is_admin=bool(user.is_admin)
        )

    # -- персональные фильтры ----------------------------------------------- #

    def build_filter(self, settings) -> MessageFilter:
        """Собирает MessageFilter из сохранённых настроек пользователя."""
        return MessageFilter.from_settings(
            keywords=settings.keywords,
            exclude_keywords=settings.exclude_keywords,
            regex=settings.regex,
            mode=settings.mode,
            case_sensitive=settings.case_sensitive,
            min_length=settings.min_length,
            max_length=settings.max_length,
        )

    def filter_for(self, recipient: Recipient) -> MessageFilter:
        """Фильтр получателя. Кеш обесценивается ростом rev при правке правил."""
        cached = self._user_filters.get(recipient.user.user_id)
        if cached is not None and cached[0] == recipient.settings.rev:
            return cached[1]
        built = self.build_filter(recipient.settings)
        self._user_filters[recipient.user.user_id] = (recipient.settings.rev, built)
        return built

    def drop_filter_cache(self, user_id: int) -> None:
        """Выбрасывает скомпилированный фильтр — после правки правил или отзыва."""
        self._user_filters.pop(user_id, None)

    async def describe_service(self) -> str:
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

        try:
            counts = await self.users.counts()
            people = (f"{counts.get('approved', 0)} одобрено, "
                      f"{counts.get('pending', 0)} заявок")
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось прочитать счётчик пользователей: %s", exc)
            people = "неизвестно"

        return format_status(
            header="ℹ️ СОСТОЯНИЕ СЕРВИСА",
            fields=[
                ("Аккаунт", self.account),
                ("Источник", self.source_label),
                ("Правила (.env)", self.filter.describe()),
                ("Целевой чат", str(self.config.target_chat_id)),
                ("Пользователей", people),
                ("Самопроверка при старте", probe),
                ("Доставлено сообщений", processed),
                ("Работает", _format_uptime(datetime.now(timezone.utc) - self.started_at)),
            ],
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
            payload, fallback = format_message_with_fallback(
                header="✅ СЕРВИС ЗАПУЩЕН — последнее совпадение",
                source_label=self.source_label,
                sent_at=message.date,
                message_id=message.id,
                text=message.raw_text or "",
                entities=message_entities(message),
                reason=result.reason,
                link=link,
            )
            await self._send_probe_payload(payload, fallback)
            return

        await self._send_probe_payload(payload)

    async def _send_probe_payload(
        self, payload: str, fallback: Optional[str] = None
    ) -> None:
        if await self.sender.send_message(payload, fallback_text=fallback):
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

        source_chat_id = message.chat_id
        try:
            link = build_message_link(await event.get_chat(), message.id)
        except Exception:  # noqa: BLE001 - ссылка необязательна
            link = None

        targets = await self._collect_targets()
        if not targets:
            log.info("Message %s: получателей нет", message.id)
            return

        entities = message_entities(message)
        delivered = 0
        for index, target in enumerate(targets):
            if await self._deliver_to(
                target, message, text, entities, source_chat_id, link
            ):
                delivered += 1
            # Пауза нужна только на больших рассылках: отправки идут
            # последовательно, а 429 и так обрабатывается в _call.
            if len(targets) > FANOUT_PAUSE_AFTER and index + 1 < len(targets):
                await asyncio.sleep(FANOUT_PAUSE)

        log.info("Message %s: доставлено %s из %s получателей",
                 message.id, delivered, len(targets))

    async def _collect_targets(self) -> List["_Target"]:
        """Получатель из .env плюс все одобренные пользователи со своими правилами.

        Ключ — chat_id, и он обязан быть уникальным: иначе один чат попал бы в
        список дважды, первый получатель пометил бы сообщение доставленным, а
        правила второго просто не сработали бы. Так и происходит у владельца,
        у которого TARGET_CHAT_ID — его собственная личка.

        Поэтому при совпадении чата личные правила ВЫТЕСНЯЮТ правила из .env:
        команды бота — более позднее и более явное распоряжение, чем файл
        конфигурации. Пока человек не завёл ни одного своего правила, он в
        список пользователей не попадает, и .env продолжает работать как был.
        """
        targets: Dict[str, _Target] = {}

        if self.config.legacy_delivery:
            key = str(self.config.target_chat_id)
            targets[key] = _Target(
                key=key,
                label=f"чат из .env ({self.config.target_chat_id})",
                message_filter=self.filter,
            )

        if not self.config.multi_user:
            return list(targets.values())

        try:
            recipients = await self.users.active_recipients()
        except Exception as exc:  # noqa: BLE001 - сбой БД не должен ломать .env-доставку
            log.exception("Не удалось прочитать список получателей: %s", exc)
            return list(targets.values())

        for recipient in recipients:
            if not recipient.settings.has_rules:
                # Пустые правила = подписки нет. Иначе человек, только что
                # получивший доступ, начал бы получать вообще всё подряд.
                log.debug("Пользователь %s без правил — пропускаю",
                          recipient.user.user_id)
                continue

            key = str(recipient.user.target_chat)
            existing = targets.get(key)
            if existing is not None and existing.user_id is not None:
                # Два пользователя с одним чатом — такого быть не должно,
                # но дублировать доставку всё равно нельзя.
                log.warning("Чат %s уже занят пользователем %s — пропускаю %s",
                            key, existing.user_id, recipient.user.user_id)
                continue
            if existing is not None:
                log.info(
                    "Чат %s: личные правила пользователя %s заменяют правила "
                    "из .env", key, recipient.user.user_id,
                )

            targets[key] = _Target(
                key=key,
                label=recipient.user.label,
                message_filter=self.filter_for(recipient),
                user_id=recipient.user.user_id,
            )

        return list(targets.values())

    async def _deliver_to(
        self,
        target: "_Target",
        message,
        text: str,
        entities,
        source_chat_id: int,
        link: Optional[str],
    ) -> bool:
        """Одна доставка. Никогда не бросает: сбой у одного не касается остальных."""
        result = target.message_filter.check(text)
        if not result.matched:
            log.debug("Message %s -> %s: %s", message.id, target.label, result.reason)
            return False

        if await self.store.is_delivered(source_chat_id, message.id, target.key):
            log.info("Message %s -> %s: уже доставлено", message.id, target.label)
            return False

        payload, fallback = format_message_with_fallback(
            source_label=self.source_label,
            sent_at=message.date,
            message_id=message.id,
            text=text,
            entities=entities,
            reason=result.reason,
            link=link,
        )

        try:
            await self.sender.deliver(target.key, payload)
        except BotBlockedError as exc:
            # Бот заблокирован, аккаунт удалён, Start не нажимали: повторять
            # бессмысленно — снимаем с рассылки, пока человек не вернётся.
            log.warning("Получатель %s недоступен (%s) — отключаю рассылку",
                        target.label, exc)
            if target.user_id is not None:
                await self.users.set_blocked(target.user_id, True)
                self.drop_filter_cache(target.user_id)
            return False
        except BotApiError as exc:
            if _is_markup_failure(exc):
                # Разметка не зашла — отправляем без неё, алерт важнее вида.
                log.warning("Разметка отклонена для %s (%s) — шлю без неё",
                            target.label, exc)
                if not await self.sender.send_message(fallback, chat_id=target.key):
                    return False
            else:
                log.error("Message %s -> %s не доставлено: %s",
                          message.id, target.label, exc)
                return False
        except Exception as exc:  # noqa: BLE001 - один получатель не ломает остальных
            log.error("Message %s -> %s не доставлено: %s",
                      message.id, target.label, exc)
            return False

        await self.store.mark_delivered(source_chat_id, message.id, target.key)
        log.info("Message %s -> %s доставлено (%s)",
                 message.id, target.label, result.reason)
        return True


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
