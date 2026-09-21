"""Конфигурация сервиса.

Все настройки, которые могут меняться без правки кода, читаются из `.env`
(или из переменных окружения — systemd может передать их напрямую).

Единственная точка входа — :func:`load_config`. Она валидирует значения и
бросает :class:`ConfigError` с понятным текстом, если что-то задано неверно.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Pattern, Tuple, Union

from dotenv import load_dotenv

# Корень проекта: .../telegram_filter/  (на уровень выше пакета app/)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(RuntimeError):
    """Ошибка конфигурации — показывается пользователю без traceback."""


# --------------------------------------------------------------------------- #
# Примитивы чтения переменных окружения
# --------------------------------------------------------------------------- #

def _get_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip()


def _get_required_str(name: str) -> str:
    value = _get_str(name)
    if not value:
        raise ConfigError(
            f"Переменная {name} не задана. Заполните её в .env "
            f"(см. .env.example)."
        )
    return value


def _get_int(name: str, default: int, *, minimum: Optional[int] = None) -> int:
    raw = _get_str(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} должно быть целым числом, получено: {raw!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} должно быть >= {minimum}, получено: {value}")
    return value


def _get_bool(name: str, default: bool = False) -> bool:
    raw = _get_str(name).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on", "y"):
        return True
    if raw in ("0", "false", "no", "off", "n"):
        return False
    raise ConfigError(
        f"{name} должно быть true/false, получено: {raw!r}"
    )


def _get_list(name: str) -> Tuple[str, ...]:
    """Читает список, разделённый запятыми. Пустые элементы отбрасываются."""
    raw = _get_str(name)
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _parse_source(raw: str) -> Union[str, int]:
    """Источник может быть @username, username или числовой id."""
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw if raw.startswith("@") else "@" + raw


# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Config:
    """Иммутабельный снимок настроек сервиса.

    Секреты помечены ``repr=False``, чтобы случайный ``log.debug(config)``
    не записал API_HASH или токен бота в журнал.
    """

    # --- Telegram MTProto (аккаунт пользователя, чтение) ---
    api_id: int = field(repr=False)
    api_hash: str = field(repr=False)
    session_name: str
    session_path: Path

    # --- Источник ---
    source: Union[str, int]

    # --- Telegram Bot API (свой бот, отправка) ---
    target_bot_token: str = field(repr=False)
    target_chat_id: Union[str, int]

    # --- Фильтр ---
    keywords: Tuple[str, ...]
    exclude_keywords: Tuple[str, ...]
    regex: Optional[Pattern[str]]
    case_sensitive: bool
    min_length: int
    max_length: int  # 0 = без ограничения

    # --- Самопроверка при старте ---
    startup_probe: bool          # False = ничего не отправлять при запуске
    startup_probe_limit: int     # сколько последних сообщений просмотреть

    # --- Хранилище обработанных сообщений ---
    db_path: Path
    db_retention_days: int  # 0 = не чистить

    # --- Логирование ---
    log_level: str
    log_message_text: bool

    # --- Сетевые настройки / устойчивость ---
    flood_wait_max_seconds: int
    bot_api_timeout: int
    bot_api_max_retries: int
    reconnect_max_delay: int

    @property
    def source_label(self) -> str:
        """Человекочитаемое имя источника для логов и уведомлений."""
        return str(self.source)


def load_config(env_file: Optional[Path] = None) -> Config:
    """Читает `.env` и переменные окружения, валидирует и возвращает Config.

    Переменные окружения имеют приоритет над `.env` (``override=False``),
    поэтому systemd ``Environment=`` всегда перекрывает файл.
    """
    path = Path(env_file) if env_file else DEFAULT_ENV_FILE
    if path.is_file():
        load_dotenv(path, override=False)

    api_id_raw = _get_required_str("API_ID")
    try:
        api_id = int(api_id_raw)
    except ValueError:
        raise ConfigError(
            f"API_ID должен быть числом (например 123456), получено: {api_id_raw!r}"
        )

    api_hash = _get_required_str("API_HASH")

    session_name = _get_str("SESSION_NAME", "telegram_filter")
    session_dir = Path(_get_str("SESSION_DIR") or PROJECT_ROOT)
    session_path = (session_dir / session_name).expanduser()

    source = _parse_source(_get_required_str("SOURCE_BOT_USERNAME"))

    target_bot_token = _get_required_str("TARGET_BOT_TOKEN")
    target_chat_id_raw = _get_required_str("TARGET_CHAT_ID")
    target_chat_id: Union[str, int]
    if target_chat_id_raw.lstrip("-").isdigit():
        target_chat_id = int(target_chat_id_raw)
    else:
        target_chat_id = target_chat_id_raw  # @channelusername

    case_sensitive = _get_bool("CASE_SENSITIVE", False)

    regex_raw = _get_str("FILTER_REGEX")
    regex: Optional[Pattern[str]] = None
    if regex_raw:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(regex_raw, flags)
        except re.error as exc:
            # Некорректный regex — это ошибка конфигурации, а не падение сервиса.
            raise ConfigError(
                f"FILTER_REGEX содержит некорректное регулярное выражение: {exc}\n"
                f"  Паттерн: {regex_raw}"
            ) from exc

    startup_probe = _get_bool("STARTUP_PROBE", True)
    startup_probe_limit = _get_int("STARTUP_PROBE_LIMIT", 50, minimum=1)

    min_length = _get_int("FILTER_MIN_LENGTH", 0, minimum=0)
    max_length = _get_int("FILTER_MAX_LENGTH", 0, minimum=0)
    if max_length and min_length > max_length:
        raise ConfigError(
            f"FILTER_MIN_LENGTH ({min_length}) больше FILTER_MAX_LENGTH ({max_length})"
        )

    db_path = Path(_get_str("DB_PATH") or (PROJECT_ROOT / "processed_messages.db")).expanduser()

    log_level = _get_str("LOG_LEVEL", "INFO").upper()
    if log_level not in VALID_LOG_LEVELS:
        raise ConfigError(
            f"LOG_LEVEL={log_level!r} недопустим. Разрешено: {', '.join(VALID_LOG_LEVELS)}"
        )

    return Config(
        api_id=api_id,
        api_hash=api_hash,
        session_name=session_name,
        session_path=session_path,
        source=source,
        target_bot_token=target_bot_token,
        target_chat_id=target_chat_id,
        keywords=_get_list("FILTER_KEYWORDS"),
        exclude_keywords=_get_list("FILTER_EXCLUDE_KEYWORDS"),
        regex=regex,
        case_sensitive=case_sensitive,
        min_length=min_length,
        max_length=max_length,
        startup_probe=startup_probe,
        startup_probe_limit=startup_probe_limit,
        db_path=db_path,
        db_retention_days=_get_int("DB_RETENTION_DAYS", 30, minimum=0),
        log_level=log_level,
        log_message_text=_get_bool("LOG_MESSAGE_TEXT", False),
        flood_wait_max_seconds=_get_int("FLOOD_WAIT_MAX_SECONDS", 900, minimum=1),
        bot_api_timeout=_get_int("BOT_API_TIMEOUT", 20, minimum=1),
        bot_api_max_retries=_get_int("BOT_API_MAX_RETRIES", 4, minimum=0),
        reconnect_max_delay=_get_int("RECONNECT_MAX_DELAY", 60, minimum=1),
    )
