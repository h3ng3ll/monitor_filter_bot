"""Тесты рассылки: у каждого свои правила, сбой одного не касается остальных.

Запуск:  .venv/bin/python -m pytest -q
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bot_sender import BotApiError, BotBlockedError  # noqa: E402
from app.filter import MessageFilter  # noqa: E402
from app.main import Application  # noqa: E402
from app.models import ProcessedMessageStore  # noqa: E402
from app.users import UserStore  # noqa: E402

SOURCE_CHAT = -100123


def _run(coro):
    return asyncio.run(coro)


class FakeSender:
    """Двойник BotSender: запоминает доставки, умеет падать для конкретных чатов."""

    def __init__(self, failures=None):
        self.delivered = []          # пары (chat_id, текст)
        self.failures = failures or {}

    async def deliver(self, chat_id, html_text, **kwargs):
        error = self.failures.get(str(chat_id))
        if error is not None:
            raise error
        self.delivered.append((str(chat_id), html_text))

    async def send_message(self, html_text, *, chat_id=None, **kwargs):
        self.delivered.append((str(chat_id), html_text))
        return True

    @property
    def recipients(self):
        return [chat_id for chat_id, _ in self.delivered]


def _message(message_id=5, text="Бан. White: Олександр Науменко"):
    return SimpleNamespace(
        id=message_id,
        chat_id=SOURCE_CHAT,
        sender_id=777,
        date=None,
        raw_text=text,
        message=text,
        entities=None,
    )


class _FakeEvent:
    def __init__(self, message):
        self.message = message

    async def get_chat(self):
        return SimpleNamespace(username=None, id=abs(SOURCE_CHAT))


def _app(tmp_path, *, sender=None, legacy=True, multi_user=True,
         legacy_keywords=("Науменко",)):
    """Собирает Application без сети: только БД, фильтры и двойник отправителя."""
    app = Application.__new__(Application)
    app.config = SimpleNamespace(
        target_chat_id=999,
        legacy_delivery=legacy,
        multi_user=multi_user,
        log_message_text=False,
    )
    app.filter = MessageFilter(keywords=list(legacy_keywords))
    app.store = ProcessedMessageStore(tmp_path / "test.db")
    app.users = UserStore(tmp_path / "test.db")
    app.sender = sender or FakeSender()
    app.source_id = 777
    app.source_label = "@src"
    app._user_filters = {}
    _run(app.store.connect())
    _run(app.users.connect())
    return app


def _add_user(app, user_id, keywords, *, mode="any"):
    _run(app.users.request_access(user_id, user_id, f"u{user_id}", f"U{user_id}"))
    _run(app.users.approve(user_id, by=1))
    for word in keywords:
        _run(app.users.add_keyword(user_id, word))
    if mode != "any":
        _run(app.users.set_mode(user_id, mode))


def _handle(app, message=None):
    _run(app._handle(_FakeEvent(message or _message())))


# --------------------------------------------------------------------------- #
# Персональные правила
# --------------------------------------------------------------------------- #

def test_each_user_gets_only_their_own_match(tmp_path):
    app = _app(tmp_path, legacy=False)
    _add_user(app, 1, ["Науменко"])
    _add_user(app, 2, ["Natalie"])
    _handle(app)
    assert app.sender.recipients == ["1"]


def test_legacy_target_is_served_from_env_filter(tmp_path):
    app = _app(tmp_path, legacy=True)
    _add_user(app, 1, ["Науменко"])
    _handle(app)
    assert sorted(app.sender.recipients) == ["1", "999"]


def test_user_without_rules_receives_nothing(tmp_path):
    app = _app(tmp_path, legacy=False)
    # Доступ есть, правил нет — иначе человек получал бы всё подряд.
    _run(app.users.request_access(1, 1, "u1", "U1"))
    _run(app.users.approve(1, by=99))
    _handle(app)
    assert app.sender.recipients == []


def test_muted_user_is_skipped(tmp_path):
    app = _app(tmp_path, legacy=False)
    _add_user(app, 1, ["Науменко"])
    _run(app.users.set_muted(1, True))
    _handle(app)
    assert app.sender.recipients == []


def test_all_mode_requires_every_keyword(tmp_path):
    app = _app(tmp_path, legacy=False)
    _add_user(app, 1, ["Науменко", "Sky"], mode="all")
    _add_user(app, 2, ["Науменко", "отсутствует"], mode="all")
    _handle(app, _message(text="Бан X Sky Whisper. White: Науменко"))
    assert app.sender.recipients == ["1"]


def test_multi_user_disabled_leaves_only_legacy(tmp_path):
    app = _app(tmp_path, multi_user=False)
    _add_user(app, 1, ["Науменко"])
    _handle(app)
    assert app.sender.recipients == ["999"]


# --------------------------------------------------------------------------- #
# Дедупликация
# --------------------------------------------------------------------------- #

def test_repeated_event_is_not_delivered_twice(tmp_path):
    app = _app(tmp_path, legacy=False)
    _add_user(app, 1, ["Науменко"])
    _handle(app)
    _handle(app)
    assert app.sender.recipients == ["1"]


def test_new_user_still_gets_an_already_delivered_message(tmp_path):
    app = _app(tmp_path, legacy=False)
    _add_user(app, 1, ["Науменко"])
    _handle(app)
    _add_user(app, 2, ["Науменко"])
    _handle(app)
    # Первому повторно не ушло, второму — ушло.
    assert app.sender.recipients == ["1", "2"]


# --------------------------------------------------------------------------- #
# Сбои доставки
# --------------------------------------------------------------------------- #

def test_blocked_user_is_muted_and_others_still_receive(tmp_path):
    sender = FakeSender(failures={
        "1": BotBlockedError("sendMessage: Forbidden: bot was blocked by the user",
                             status=403),
    })
    app = _app(tmp_path, sender=sender, legacy=False)
    _add_user(app, 1, ["Науменко"])
    _add_user(app, 2, ["Науменко"])
    _handle(app)

    assert sender.recipients == ["2"]
    assert _run(app.users.get(1)).blocked is True
    # И больше его в рассылке нет.
    assert [r.user.user_id for r in _run(app.users.active_recipients())] == [2]


def test_arbitrary_error_does_not_abort_the_loop(tmp_path):
    sender = FakeSender(failures={"1": RuntimeError("что-то пошло не так")})
    app = _app(tmp_path, sender=sender, legacy=False)
    _add_user(app, 1, ["Науменко"])
    _add_user(app, 2, ["Науменко"])
    _handle(app)

    assert sender.recipients == ["2"]
    # Неудачная доставка не помечается как выполненная — и не мьютит человека.
    assert _run(app.store.is_delivered(SOURCE_CHAT, 5, "1")) is False
    assert _run(app.users.get(1)).blocked is False


def test_failed_delivery_is_retried_on_a_later_event(tmp_path):
    sender = FakeSender(failures={"1": RuntimeError("временный сбой")})
    app = _app(tmp_path, sender=sender, legacy=False)
    _add_user(app, 1, ["Науменко"])
    _handle(app)
    assert sender.recipients == []

    sender.failures.clear()
    _handle(app)
    assert sender.recipients == ["1"]


def test_foreign_sender_is_ignored(tmp_path):
    app = _app(tmp_path, legacy=False)
    _add_user(app, 1, ["Науменко"])
    message = _message()
    message.sender_id = 12345  # не источник
    _handle(app, message)
    assert app.sender.recipients == []


# --------------------------------------------------------------------------- #
# Кеш фильтров
# --------------------------------------------------------------------------- #

def test_editing_keywords_takes_effect_without_cache_flush(tmp_path):
    app = _app(tmp_path, legacy=False)
    _add_user(app, 1, ["Natalie"])
    _handle(app)
    assert app.sender.recipients == []

    # Правку делает команда бота; кеш ключуется по rev, поэтому сбрасывать
    # его вручную не обязательно.
    _run(app.users.add_keyword(1, "Науменко"))
    _handle(app, _message(message_id=6))
    assert app.sender.recipients == ["1"]
