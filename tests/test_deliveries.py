"""Тесты журнала доставок и миграции старой базы.

Запуск:  .venv/bin/python -m pytest -q
"""

import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import ProcessedMessageStore  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _store(tmp_path, **kwargs):
    store = ProcessedMessageStore(tmp_path / "test.db", **kwargs)
    _run(store.connect())
    return store


# --------------------------------------------------------------------------- #
# Дедупликация по получателям
# --------------------------------------------------------------------------- #

def test_delivery_is_tracked_per_recipient(tmp_path):
    store = _store(tmp_path)
    assert _run(store.is_delivered(-100, 5, "alice")) is False

    _run(store.mark_delivered(-100, 5, "alice"))
    assert _run(store.is_delivered(-100, 5, "alice")) is True
    # Для другого получателя то же сообщение ещё не доставлено — иначе первая
    # удачная отправка закрывала бы сообщение для всех остальных.
    assert _run(store.is_delivered(-100, 5, "bob")) is False


def test_mark_delivered_is_idempotent(tmp_path):
    store = _store(tmp_path)
    _run(store.mark_delivered(-100, 5, "alice"))
    _run(store.mark_delivered(-100, 5, "alice"))
    assert _run(store.count()) == 1


def test_count_for_recipient(tmp_path):
    store = _store(tmp_path)
    _run(store.mark_delivered(-100, 1, "alice"))
    _run(store.mark_delivered(-100, 2, "alice"))
    _run(store.mark_delivered(-100, 1, "bob"))
    assert _run(store.count()) == 3
    assert _run(store.count_for("alice")) == 2
    assert _run(store.count_for("bob")) == 1


def test_versions_are_tracked_per_text_hash(tmp_path):
    store = _store(tmp_path)
    assert _run(store.is_version_delivered(-100, 5, "alice", "h1")) is False
    _run(store.mark_version_delivered(-100, 5, "alice", "h1"))
    _run(store.mark_version_delivered(-100, 5, "alice", "h1"))
    assert _run(store.is_version_delivered(-100, 5, "alice", "h1")) is True
    assert _run(store.is_version_delivered(-100, 5, "alice", "h2")) is False
    assert _run(store.is_version_delivered(-100, 5, "bob", "h1")) is False
    # Версии не считаются отдельными доставками в /info.
    assert _run(store.count()) == 0


def test_prune_removes_old_versions(tmp_path):
    store = _store(tmp_path, retention_days=1)
    _run(store.mark_version_delivered(-100, 5, "alice", "h1"))
    conn = store._require_conn()
    conn.execute("UPDATE delivered_versions SET delivered_at = '2000-01-01T00:00:00+00:00'")
    conn.commit()
    assert _run(store.prune()) == 1
    assert _run(store.is_version_delivered(-100, 5, "alice", "h1")) is False


def test_recipient_key_accepts_channel_username(tmp_path):
    # TARGET_CHAT_ID может быть строкой @name, а не числом.
    store = _store(tmp_path)
    _run(store.mark_delivered(-100, 5, "@my_channel"))
    assert _run(store.is_delivered(-100, 5, "@my_channel")) is True


def test_prune_clears_both_tables(tmp_path):
    store = _store(tmp_path, retention_days=1)
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO deliveries VALUES (-100, 1, 'alice', '2000-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO processed_messages VALUES (-100, 2, '2000-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    assert _run(store.prune()) == 2
    assert _run(store.count()) == 0


# --------------------------------------------------------------------------- #
# Миграция старой базы
# --------------------------------------------------------------------------- #

def _legacy_db(path, rows):
    """Строит базу в том виде, в каком она была до многопользовательского режима."""
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS processed_messages ("
        " chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,"
        " processed_at TEXT NOT NULL, PRIMARY KEY (chat_id, message_id));"
    )
    conn.executemany(
        "INSERT INTO processed_messages VALUES (?, ?, ?)", rows
    )
    conn.commit()
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 0
    conn.close()


def test_legacy_journal_is_copied_into_deliveries(tmp_path):
    db = tmp_path / "legacy.db"
    _legacy_db(db, [(-100, i, "2026-09-01T00:00:00+00:00") for i in range(48)])

    store = ProcessedMessageStore(db, legacy_recipient="8997424641")
    _run(store.connect())

    # Всё, что лежало в старом журнале, ушло в TARGET_CHAT_ID.
    assert _run(store.count()) == 48
    assert _run(store.is_delivered(-100, 7, "8997424641")) is True
    _run(store.close())

    conn = sqlite3.connect(db)
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 1
    conn.close()


def test_migration_does_not_run_twice(tmp_path):
    db = tmp_path / "legacy.db"
    _legacy_db(db, [(-100, i, "2026-09-01T00:00:00+00:00") for i in range(48)])

    first = ProcessedMessageStore(db, legacy_recipient="8997424641")
    _run(first.connect())
    _run(first.close())

    # Повторное подключение не должно ни удваивать, ни воскрешать записи.
    second = ProcessedMessageStore(db, legacy_recipient="8997424641")
    _run(second.connect())
    assert _run(second.count()) == 48

    _run(second.mark_delivered(-100, 999, "8997424641"))
    _run(second.close())

    third = ProcessedMessageStore(db, legacy_recipient="8997424641")
    _run(third.connect())
    assert _run(third.count()) == 49


def test_fresh_database_is_versioned_without_copying(tmp_path):
    store = ProcessedMessageStore(tmp_path / "new.db", legacy_recipient="42")
    _run(store.connect())
    assert _run(store.count()) == 0
    _run(store.close())

    conn = sqlite3.connect(tmp_path / "new.db")
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 1
    conn.close()
