"""Хранилище состояния: журнал доставок и защита от дубликатов.

Таблица ``deliveries`` с ключом ``(source_chat_id, message_id, recipient)``:
пара «чат + id» однозначно идентифицирует сообщение в Telegram, а получатель
нужен третьим, потому что одно и то же сообщение уходит нескольким людям с
разными правилами.

``processed_messages`` — прежний одноадресный журнал. Он больше не пишется,
но сохранён: при первом запуске его содержимое переносится в ``deliveries``
(см. :meth:`ProcessedMessageStore._migrate`), чтобы после обновления не
пришла заново пачка уже полученных уведомлений.

sqlite3 — синхронная библиотека, поэтому каждый вызов уходит в отдельный
поток через ``asyncio.to_thread``: event loop Telethon не блокируется.
Доступ к соединению защищён ``threading.Lock``.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Версия файла БД. Повышается только когда нужен шаг, который нельзя
# выполнить повторно: создание таблиц обходится CREATE TABLE IF NOT EXISTS.
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_messages (
    chat_id      INTEGER NOT NULL,
    message_id   INTEGER NOT NULL,
    processed_at TEXT    NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_processed_at
    ON processed_messages (processed_at);

-- Журнал доставок. Одно сообщение источника уходит нескольким получателям,
-- поэтому «уже обработано» перестало быть общим признаком: без колонки
-- получателя первая удачная отправка закрыла бы сообщение для всех остальных.
-- recipient — TEXT, потому что TARGET_CHAT_ID может быть и @channelusername.
CREATE TABLE IF NOT EXISTS deliveries (
    source_chat_id INTEGER NOT NULL,
    message_id     INTEGER NOT NULL,
    recipient      TEXT    NOT NULL,
    delivered_at   TEXT    NOT NULL,
    PRIMARY KEY (source_chat_id, message_id, recipient)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_at
    ON deliveries (delivered_at);
CREATE INDEX IF NOT EXISTS idx_deliveries_recipient
    ON deliveries (recipient);
"""


class ProcessedMessageStore:
    """Журнал уже пересланных сообщений."""

    def __init__(
        self,
        db_path: Path,
        retention_days: int = 30,
        *,
        legacy_recipient: Optional[str] = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.retention_days = retention_days
        # Кому уходили сообщения до появления многопользовательского режима:
        # нужен ровно один раз, чтобы перенести старый журнал в deliveries.
        self.legacy_recipient = legacy_recipient
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    # -- жизненный цикл -------------------------------------------------- #

    async def connect(self) -> None:
        await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: соединение используется из пула потоков
        # to_thread, но сериализуется нашим собственным Lock.
        conn = sqlite3.connect(
            self.db_path, check_same_thread=False, timeout=10.0
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        conn.commit()
        self._migrate(conn)
        self._conn = conn
        log.info("SQLite store ready: %s", self.db_path)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Приводит файл БД к SCHEMA_VERSION.

        Новые таблицы создаются через CREATE TABLE IF NOT EXISTS — это
        безопасно и для пустой базы, и для уже работающей, версия для них
        не нужна. user_version нужен только шагу, который нельзя повторять:
        переносу старого журнала в deliveries.
        """
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version >= SCHEMA_VERSION:
            return

        if version < 1 and self.legacy_recipient:
            # До многопользовательского режима журнал был одноадресным:
            # всё, что в нём лежит, ушло в TARGET_CHAT_ID.
            cur = conn.execute(
                "INSERT OR IGNORE INTO deliveries "
                "(source_chat_id, message_id, recipient, delivered_at) "
                "SELECT chat_id, message_id, ?, processed_at FROM processed_messages",
                (self.legacy_recipient,),
            )
            if cur.rowcount:
                log.info("Перенесено записей старого журнала в deliveries: %s",
                         cur.rowcount)

        # PRAGMA не принимает параметры — подставляем константу-целое.
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()

    async def close(self) -> None:
        if self._conn is None:
            return
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        log.info("SQLite connection closed")

    # -- операции --------------------------------------------------------- #

    async def is_delivered(
        self, source_chat_id: int, message_id: int, recipient: str
    ) -> bool:
        return await asyncio.to_thread(
            self._is_delivered_sync, source_chat_id, message_id, recipient
        )

    def _is_delivered_sync(
        self, source_chat_id: int, message_id: int, recipient: str
    ) -> bool:
        with self._lock:
            cur = self._require_conn().execute(
                "SELECT 1 FROM deliveries "
                "WHERE source_chat_id = ? AND message_id = ? AND recipient = ?",
                (source_chat_id, message_id, str(recipient)),
            )
            return cur.fetchone() is not None

    async def mark_delivered(
        self, source_chat_id: int, message_id: int, recipient: str
    ) -> None:
        await asyncio.to_thread(
            self._mark_delivered_sync, source_chat_id, message_id, recipient
        )

    def _mark_delivered_sync(
        self, source_chat_id: int, message_id: int, recipient: str
    ) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            conn = self._require_conn()
            conn.execute(
                "INSERT OR IGNORE INTO deliveries "
                "(source_chat_id, message_id, recipient, delivered_at) "
                "VALUES (?, ?, ?, ?)",
                (source_chat_id, message_id, str(recipient), now),
            )
            conn.commit()

    async def prune(self) -> int:
        """Удаляет записи старше retention_days. Возвращает число удалённых."""
        if not self.retention_days:
            return 0
        return await asyncio.to_thread(self._prune_sync)

    def _prune_sync(self) -> int:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        ).isoformat(timespec="seconds")
        with self._lock:
            conn = self._require_conn()
            removed = 0
            for statement in (
                "DELETE FROM deliveries WHERE delivered_at < ?",
                "DELETE FROM processed_messages WHERE processed_at < ?",
            ):
                cur = conn.execute(statement, (cutoff,))
                removed += cur.rowcount or 0
            conn.commit()
            return removed

    async def count(self) -> int:
        return await asyncio.to_thread(self._count_sync, None)

    async def count_for(self, recipient: str) -> int:
        return await asyncio.to_thread(self._count_sync, str(recipient))

    def _count_sync(self, recipient: Optional[str]) -> int:
        with self._lock:
            conn = self._require_conn()
            if recipient is None:
                cur = conn.execute("SELECT COUNT(*) FROM deliveries")
            else:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM deliveries WHERE recipient = ?",
                    (recipient,),
                )
            return int(cur.fetchone()[0])

    # -- внутреннее -------------------------------------------------------- #

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Store is not connected — вызовите await store.connect()")
        return self._conn
