"""Хранилище состояния: защита от дубликатов.

Одна таблица ``processed_messages`` с составным первичным ключом
``(chat_id, message_id)`` — именно эта пара однозначно идентифицирует
сообщение в Telegram.

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

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_messages (
    chat_id      INTEGER NOT NULL,
    message_id   INTEGER NOT NULL,
    processed_at TEXT    NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_processed_at
    ON processed_messages (processed_at);
"""


class ProcessedMessageStore:
    """Журнал уже пересланных сообщений."""

    def __init__(self, db_path: Path, retention_days: int = 30) -> None:
        self.db_path = Path(db_path)
        self.retention_days = retention_days
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
        self._conn = conn
        log.info("SQLite store ready: %s", self.db_path)

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

    async def is_processed(self, chat_id: int, message_id: int) -> bool:
        return await asyncio.to_thread(self._is_processed_sync, chat_id, message_id)

    def _is_processed_sync(self, chat_id: int, message_id: int) -> bool:
        with self._lock:
            cur = self._require_conn().execute(
                "SELECT 1 FROM processed_messages WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            )
            return cur.fetchone() is not None

    async def mark_processed(self, chat_id: int, message_id: int) -> None:
        await asyncio.to_thread(self._mark_processed_sync, chat_id, message_id)

    def _mark_processed_sync(self, chat_id: int, message_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            conn = self._require_conn()
            conn.execute(
                "INSERT OR IGNORE INTO processed_messages "
                "(chat_id, message_id, processed_at) VALUES (?, ?, ?)",
                (chat_id, message_id, now),
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
            cur = conn.execute(
                "DELETE FROM processed_messages WHERE processed_at < ?", (cutoff,)
            )
            conn.commit()
            return cur.rowcount or 0

    async def count(self) -> int:
        return await asyncio.to_thread(self._count_sync)

    def _count_sync(self) -> int:
        with self._lock:
            cur = self._require_conn().execute(
                "SELECT COUNT(*) FROM processed_messages"
            )
            return int(cur.fetchone()[0])

    # -- внутреннее -------------------------------------------------------- #

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Store is not connected — вызовите await store.connect()")
        return self._conn
