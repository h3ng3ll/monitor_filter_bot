"""Пользователи сервиса и их персональные правила фильтрации.

Модель доступа: посторонний пишет боту /start, получает статус ``pending``
и ждёт решения администратора. Администратор — владелец Telethon-сессии,
то есть тот самый аккаунт, которому сообщения приходят на самом деле; он и
решает, кому ещё их пересылать.

Правила у каждого пользователя свои и живут здесь же, в SQLite: .env
перечитать на ходу нельзя, а списки слов правятся командами бота.

Работа с sqlite3 повторяет models.py: библиотека синхронная, поэтому каждый
вызов уходит в отдельный поток через ``asyncio.to_thread``, а соединение
защищено ``threading.Lock``. Соединение своё, к тому же файлу: WAL разрешает
одновременных читателей и сериализует единственного писателя, а ``timeout``
гасит редкие SQLITE_BUSY.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"

MODES = ("any", "all", "regex")

# Списки слов хранятся строкой через запятую — как FILTER_KEYWORDS в .env.
_LIST_SEPARATOR = ","

# Колонки user_filters, которые вообще разрешено править командами.
# Имя колонки подставляется в SQL, поэтому берётся только отсюда.
_FILTER_COLUMNS = (
    "keywords",
    "exclude_keywords",
    "regex",
    "mode",
    "case_sensitive",
    "min_length",
    "max_length",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    chat_id    INTEGER NOT NULL,
    username   TEXT    NOT NULL DEFAULT '',
    full_name  TEXT    NOT NULL DEFAULT '',
    status     TEXT    NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'approved', 'denied')),
    is_admin   INTEGER NOT NULL DEFAULT 0,
    muted      INTEGER NOT NULL DEFAULT 0,
    blocked    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL,
    decided_at TEXT,
    decided_by INTEGER
);
CREATE INDEX IF NOT EXISTS idx_users_status ON users (status);

CREATE TABLE IF NOT EXISTS user_filters (
    user_id          INTEGER PRIMARY KEY
                     REFERENCES users (user_id) ON DELETE CASCADE,
    keywords         TEXT    NOT NULL DEFAULT '',
    exclude_keywords TEXT    NOT NULL DEFAULT '',
    regex            TEXT    NOT NULL DEFAULT '',
    mode             TEXT    NOT NULL DEFAULT 'any'
                     CHECK (mode IN ('any', 'all', 'regex')),
    case_sensitive   INTEGER NOT NULL DEFAULT 0,
    min_length       INTEGER NOT NULL DEFAULT 0,
    max_length       INTEGER NOT NULL DEFAULT 0,
    rev              INTEGER NOT NULL DEFAULT 1,
    updated_at       TEXT    NOT NULL
);
"""


# --------------------------------------------------------------------------- #
# Модели
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class User:
    user_id: int
    chat_id: int
    username: str
    full_name: str
    status: str
    is_admin: bool
    muted: bool
    blocked: bool
    created_at: str

    @property
    def label(self) -> str:
        """Человекочитаемое имя для списков и логов."""
        name = self.full_name or "без имени"
        handle = f"@{self.username}" if self.username else "без username"
        return f"{name} ({handle}, id={self.user_id})"

    @property
    def target_chat(self) -> int:
        """Куда слать. chat_id обычно равен user_id, но полагаться не будем."""
        return self.chat_id or self.user_id


@dataclass(frozen=True)
class FilterSettings:
    keywords: Tuple[str, ...]
    exclude_keywords: Tuple[str, ...]
    regex: str
    mode: str
    case_sensitive: bool
    min_length: int
    max_length: int
    rev: int

    @property
    def has_rules(self) -> bool:
        """Пустые правила = подписки нет.

        Иначе человек, только что получивший доступ, начал бы получать вообще
        всё подряд — это не то, чего он просил.
        """
        if self.mode == "regex":
            return bool(self.regex)
        return bool(self.keywords) or bool(self.regex)


@dataclass(frozen=True)
class Recipient:
    user: User
    settings: FilterSettings


# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #

def split_list(value: str) -> Tuple[str, ...]:
    return tuple(
        part.strip() for part in (value or "").split(_LIST_SEPARATOR) if part.strip()
    )


def join_list(values: Sequence[str]) -> str:
    return _LIST_SEPARATOR.join(values)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _user_from_row(row: sqlite3.Row) -> User:
    return User(
        user_id=int(row["user_id"]),
        chat_id=int(row["chat_id"]),
        username=row["username"] or "",
        full_name=row["full_name"] or "",
        status=row["status"],
        is_admin=bool(row["is_admin"]),
        muted=bool(row["muted"]),
        blocked=bool(row["blocked"]),
        created_at=row["created_at"],
    )


def _settings_from_row(row: sqlite3.Row) -> FilterSettings:
    return FilterSettings(
        keywords=split_list(row["keywords"]),
        exclude_keywords=split_list(row["exclude_keywords"]),
        regex=row["regex"] or "",
        mode=row["mode"],
        case_sensitive=bool(row["case_sensitive"]),
        min_length=int(row["min_length"]),
        max_length=int(row["max_length"]),
        rev=int(row["rev"]),
    )


class UserStore:
    """Хранилище пользователей и их правил."""

    def __init__(self, db_path: Path, *, max_keywords: int = 50) -> None:
        self.db_path = Path(db_path)
        self.max_keywords = max_keywords
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    # -- жизненный цикл -------------------------------------------------- #

    async def connect(self) -> None:
        await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # Без этого ON DELETE CASCADE у user_filters не сработает.
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        conn.commit()
        self._conn = conn
        log.info("SQLite users store ready: %s", self.db_path)

    async def close(self) -> None:
        if self._conn is None:
            return
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("UserStore не подключён — вызовите await users.connect()")
        return self._conn

    # -- заявки и решения -------------------------------------------------- #

    async def request_access(
        self, user_id: int, chat_id: int, username: str, full_name: str
    ) -> Tuple[User, bool]:
        """Заводит заявку. Возвращает (пользователь, это_новая_заявка)."""
        return await asyncio.to_thread(
            self._request_access_sync, user_id, chat_id, username, full_name
        )

    def _request_access_sync(
        self, user_id: int, chat_id: int, username: str, full_name: str
    ) -> Tuple[User, bool]:
        with self._lock:
            conn = self._require_conn()
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is not None:
                # Контакты могли измениться — обновляем, статус не трогаем.
                conn.execute(
                    "UPDATE users SET chat_id = ?, username = ?, full_name = ? "
                    "WHERE user_id = ?",
                    (chat_id, username, full_name, user_id),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM users WHERE user_id = ?", (user_id,)
                ).fetchone()
                return _user_from_row(row), False

            conn.execute(
                "INSERT INTO users (user_id, chat_id, username, full_name, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, chat_id, username, full_name, STATUS_PENDING, _now()),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            return _user_from_row(row), True

    async def get(self, user_id: int) -> Optional[User]:
        return await asyncio.to_thread(self._get_sync, user_id)

    def _get_sync(self, user_id: int) -> Optional[User]:
        with self._lock:
            row = self._require_conn().execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            return _user_from_row(row) if row is not None else None

    async def approve(self, user_id: int, *, by: int) -> Optional[User]:
        return await asyncio.to_thread(
            self._decide_sync, user_id, STATUS_APPROVED, by
        )

    async def deny(self, user_id: int, *, by: int) -> Optional[User]:
        return await asyncio.to_thread(self._decide_sync, user_id, STATUS_DENIED, by)

    async def revoke(self, user_id: int, *, by: int) -> Optional[User]:
        """Отзыв доступа у одобренного: то же состояние, что и отказ."""
        return await asyncio.to_thread(self._decide_sync, user_id, STATUS_DENIED, by)

    def _decide_sync(self, user_id: int, status: str, by: int) -> Optional[User]:
        with self._lock:
            conn = self._require_conn()
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE users SET status = ?, decided_at = ?, decided_by = ?, "
                # Снимаем blocked: человека одобрили заново, пусть попробует ещё раз.
                "blocked = CASE WHEN ? = 'approved' THEN 0 ELSE blocked END "
                "WHERE user_id = ?",
                (status, _now(), by, status, user_id),
            )
            if status == STATUS_APPROVED:
                conn.execute(
                    "INSERT OR IGNORE INTO user_filters (user_id, updated_at) "
                    "VALUES (?, ?)",
                    (user_id, _now()),
                )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            return _user_from_row(row)

    async def deny_all_pending(self, *, by: int) -> Tuple[User, ...]:
        """Отклоняет все ожидающие заявки разом. Возвращает отклонённых."""
        return await asyncio.to_thread(self._deny_all_sync, by)

    def _deny_all_sync(self, by: int) -> Tuple[User, ...]:
        with self._lock:
            conn = self._require_conn()
            rows = conn.execute(
                "SELECT * FROM users WHERE status = ?", (STATUS_PENDING,)
            ).fetchall()
            if not rows:
                return ()
            conn.execute(
                "UPDATE users SET status = ?, decided_at = ?, decided_by = ? "
                "WHERE status = ?",
                (STATUS_DENIED, _now(), by, STATUS_PENDING),
            )
            conn.commit()
            return tuple(_user_from_row(row) for row in rows)

    async def forget(self, user_id: int) -> Optional[User]:
        """Полностью удаляет пользователя вместе с его правилами."""
        return await asyncio.to_thread(self._forget_sync, user_id)

    def _forget_sync(self, user_id: int) -> Optional[User]:
        with self._lock:
            conn = self._require_conn()
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            # Подстраховка на случай, если foreign_keys вдруг выключены.
            conn.execute("DELETE FROM user_filters WHERE user_id = ?", (user_id,))
            conn.commit()
            return _user_from_row(row)

    async def ensure_admin(
        self, user_id: int, *, chat_id: Optional[int] = None, label: str = ""
    ) -> None:
        """Делает пользователя одобренным админом. Идемпотентно."""
        await asyncio.to_thread(self._ensure_admin_sync, user_id, chat_id, label)

    def _ensure_admin_sync(
        self, user_id: int, chat_id: Optional[int], label: str
    ) -> None:
        with self._lock:
            conn = self._require_conn()
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO users (user_id, chat_id, username, full_name, "
                    "status, is_admin, created_at, decided_at) "
                    "VALUES (?, ?, '', ?, ?, 1, ?, ?)",
                    (
                        user_id,
                        chat_id if chat_id is not None else user_id,
                        label,
                        STATUS_APPROVED,
                        _now(),
                        _now(),
                    ),
                )
            else:
                conn.execute(
                    "UPDATE users SET is_admin = 1, status = ?, blocked = 0 "
                    "WHERE user_id = ?",
                    (STATUS_APPROVED, user_id),
                )
            conn.execute(
                "INSERT OR IGNORE INTO user_filters (user_id, updated_at) "
                "VALUES (?, ?)",
                (user_id, _now()),
            )
            conn.commit()

    # -- состояние получателя ---------------------------------------------- #

    async def set_muted(self, user_id: int, muted: bool) -> None:
        await asyncio.to_thread(self._set_flag_sync, "muted", user_id, muted)

    async def set_blocked(self, user_id: int, blocked: bool) -> None:
        await asyncio.to_thread(self._set_flag_sync, "blocked", user_id, blocked)

    def _set_flag_sync(self, column: str, user_id: int, value: bool) -> None:
        assert column in ("muted", "blocked")  # имя колонки не из пользовательского ввода
        with self._lock:
            conn = self._require_conn()
            conn.execute(
                f"UPDATE users SET {column} = ? WHERE user_id = ?",
                (1 if value else 0, user_id),
            )
            conn.commit()

    # -- списки ------------------------------------------------------------ #

    async def list_by_status(self, status: str) -> Tuple[User, ...]:
        return await asyncio.to_thread(self._list_sync, status)

    async def list_all(self) -> Tuple[User, ...]:
        return await asyncio.to_thread(self._list_sync, None)

    def _list_sync(self, status: Optional[str]) -> Tuple[User, ...]:
        with self._lock:
            conn = self._require_conn()
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM users ORDER BY created_at, user_id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM users WHERE status = ? ORDER BY created_at, user_id",
                    (status,),
                ).fetchall()
            return tuple(_user_from_row(row) for row in rows)

    async def list_admins(self) -> Tuple[User, ...]:
        return await asyncio.to_thread(self._list_admins_sync)

    def _list_admins_sync(self) -> Tuple[User, ...]:
        with self._lock:
            rows = self._require_conn().execute(
                "SELECT * FROM users WHERE is_admin = 1 ORDER BY user_id"
            ).fetchall()
            return tuple(_user_from_row(row) for row in rows)

    async def counts(self) -> dict:
        return await asyncio.to_thread(self._counts_sync)

    def _counts_sync(self) -> dict:
        with self._lock:
            rows = self._require_conn().execute(
                "SELECT status, COUNT(*) AS n FROM users GROUP BY status"
            ).fetchall()
            return {row["status"]: int(row["n"]) for row in rows}

    async def active_recipients(self) -> Tuple[Recipient, ...]:
        """Одобренные, не замьютившие себя и не заблокировавшие бота."""
        return await asyncio.to_thread(self._active_recipients_sync)

    def _active_recipients_sync(self) -> Tuple[Recipient, ...]:
        with self._lock:
            rows = self._require_conn().execute(
                "SELECT u.*, f.keywords, f.exclude_keywords, f.regex, f.mode, "
                "       f.case_sensitive, f.min_length, f.max_length, f.rev "
                "  FROM users AS u "
                "  JOIN user_filters AS f ON f.user_id = u.user_id "
                " WHERE u.status = ? AND u.muted = 0 AND u.blocked = 0 "
                " ORDER BY u.user_id",
                (STATUS_APPROVED,),
            ).fetchall()
            return tuple(
                Recipient(user=_user_from_row(row), settings=_settings_from_row(row))
                for row in rows
            )

    # -- правила ------------------------------------------------------------ #

    async def settings(self, user_id: int) -> FilterSettings:
        return await asyncio.to_thread(self._settings_sync, user_id)

    def _settings_sync(self, user_id: int) -> FilterSettings:
        with self._lock:
            conn = self._require_conn()
            row = conn.execute(
                "SELECT * FROM user_filters WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT OR IGNORE INTO user_filters (user_id, updated_at) "
                    "VALUES (?, ?)",
                    (user_id, _now()),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM user_filters WHERE user_id = ?", (user_id,)
                ).fetchone()
            return _settings_from_row(row)

    def _write_sync(self, user_id: int, column: str, value: Any) -> FilterSettings:
        """Пишет одно поле правил и поднимает rev — это ключ кеша фильтров."""
        if column not in _FILTER_COLUMNS:
            raise ValueError(f"Неизвестное поле правил: {column}")
        with self._lock:
            conn = self._require_conn()
            conn.execute(
                "INSERT OR IGNORE INTO user_filters (user_id, updated_at) "
                "VALUES (?, ?)",
                (user_id, _now()),
            )
            conn.execute(
                f"UPDATE user_filters SET {column} = ?, rev = rev + 1, "
                f"updated_at = ? WHERE user_id = ?",
                (value, _now(), user_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM user_filters WHERE user_id = ?", (user_id,)
            ).fetchone()
            return _settings_from_row(row)

    async def add_keyword(
        self, user_id: int, word: str, *, field: str = "keywords"
    ) -> Tuple[FilterSettings, bool]:
        """Добавляет слово. Возвращает (правила, было_ли_добавлено)."""
        word = (word or "").strip()
        if not word:
            raise ValueError("Укажите слово: /add Науменко")
        if _LIST_SEPARATOR in word:
            raise ValueError("Запятая в слове не поддерживается — добавляйте по одному.")

        current = await self.settings(user_id)
        existing = getattr(current, field)
        if any(word.lower() == item.lower() for item in existing):
            return current, False
        if len(existing) >= self.max_keywords:
            raise ValueError(
                f"Достигнут предел в {self.max_keywords} слов. "
                f"Удалите лишние: /remove <слово>"
            )
        updated = join_list(tuple(existing) + (word,))
        return await asyncio.to_thread(self._write_sync, user_id, field, updated), True

    async def remove_keyword(
        self, user_id: int, word: str, *, field: str = "keywords"
    ) -> Tuple[FilterSettings, bool]:
        word = (word or "").strip()
        if not word:
            raise ValueError("Укажите слово: /remove Науменко")
        current = await self.settings(user_id)
        existing = getattr(current, field)
        kept = tuple(item for item in existing if item.lower() != word.lower())
        if len(kept) == len(existing):
            return current, False
        settings = await asyncio.to_thread(
            self._write_sync, user_id, field, join_list(kept)
        )
        return settings, True

    async def clear_keywords(
        self, user_id: int, *, field: str = "keywords"
    ) -> FilterSettings:
        return await asyncio.to_thread(self._write_sync, user_id, field, "")

    async def set_regex(self, user_id: int, pattern: str) -> FilterSettings:
        """Сохраняет regex, предварительно его скомпилировав.

        Битый паттерн до базы не доходит: иначе пользователь молча перестал бы
        получать сообщения и не понял почему.
        """
        pattern = (pattern or "").strip()
        if pattern.lower() in ("", "off", "выкл", "-"):
            return await asyncio.to_thread(self._write_sync, user_id, "regex", "")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"Некорректное регулярное выражение: {exc}") from exc
        return await asyncio.to_thread(self._write_sync, user_id, "regex", pattern)

    async def set_mode(self, user_id: int, mode: str) -> FilterSettings:
        mode = (mode or "").strip().lower()
        if mode not in MODES:
            raise ValueError(
                "Режим должен быть одним из: " + ", ".join(MODES)
            )
        return await asyncio.to_thread(self._write_sync, user_id, "mode", mode)

    async def set_case_sensitive(self, user_id: int, value: bool) -> FilterSettings:
        return await asyncio.to_thread(
            self._write_sync, user_id, "case_sensitive", 1 if value else 0
        )
