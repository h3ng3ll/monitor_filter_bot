"""Тесты самопроверки при старте, команд бота и служебного форматирования.

Запуск:  .venv/bin/python -m pytest -q
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bot_commands import Access, CommandListener  # noqa: E402
from app.bot_sender import format_status  # noqa: E402
from app.filter import MessageFilter  # noqa: E402
from app.main import Application  # noqa: E402


# --------------------------------------------------------------------------- #
# Форматирование
# --------------------------------------------------------------------------- #

def test_format_status_renders_fields_and_note():
    out = format_status(
        header="⚙️ СЕРВИС ЗАПУЩЕН",
        fields=[("Источник", "@src")],
        note="совпадений нет",
    )
    assert "<b>⚙️ СЕРВИС ЗАПУЩЕН</b>" in out
    assert "<b>Источник:</b> @src" in out
    assert out.endswith("совпадений нет")


def test_format_status_escapes_html():
    out = format_status(header="h", fields=[("Правила", "a<b> & c")])
    assert "a&lt;b&gt; &amp; c" in out


# --------------------------------------------------------------------------- #
# Поиск последнего совпадения
# --------------------------------------------------------------------------- #

def _probe(texts, **filter_kwargs):
    """Прогоняет _find_last_match на фейковой истории (от новых к старым)."""
    messages = [
        SimpleNamespace(id=100 - i, raw_text=text) for i, text in enumerate(texts)
    ]
    seen = []

    class FakeClient:
        def iter_messages(self, entity, limit=None):
            async def gen():
                for message in messages[:limit]:
                    seen.append(message.id)
                    yield message
            return gen()

    app = SimpleNamespace(
        client=FakeClient(),
        filter=MessageFilter(**filter_kwargs),
        source_entity=object(),
    )
    app.find_recent_matches = (
        lambda *args, **kwargs: Application.find_recent_matches(app, *args, **kwargs)
    )
    found = asyncio.run(Application._find_last_match(app, len(texts)))
    return found, seen


def test_finds_newest_match_and_stops_early():
    found, seen = _probe(
        ["без совпадений", "Team: April", "тоже April"],
        keywords=["April"],
    )
    assert found is not None
    message, result = found
    assert message.id == 99          # второе сообщение — самое свежее совпадение
    assert result.reason == 'keyword: "April"'
    assert seen == [100, 99]         # до третьего не дошли


def test_returns_none_when_nothing_matches():
    found, seen = _probe(["привет", "пока"], keywords=["April"])
    assert found is None
    assert seen == [100, 99]          # просмотрели всю историю


def test_excluded_message_is_not_reported_as_match():
    found, _ = _probe(
        ["April и реклама"],
        keywords=["April"],
        exclude_keywords=["реклама"],
    )
    assert found is None


def test_empty_text_does_not_match_keywords():
    found, _ = _probe([None, "Team: April"], keywords=["April"])
    assert found is not None
    assert found[0].id == 99


# --------------------------------------------------------------------------- #
# Команды бота
# --------------------------------------------------------------------------- #

class FakeSender:
    """Минимальный двойник BotSender: отдаёт заранее заданные апдейты.

    ``await asyncio.sleep(0)`` обязателен: без реального await цикл слушателя
    не отдал бы управление event loop и тест завис бы в tight loop.
    Когда батчи закончились — сам просит слушателя остановиться, поэтому
    тест детерминирован и не зависит от порядка планирования задач.
    """

    bot_username = "bot"

    def __init__(self, batches, stop_event):
        self._batches = list(batches)
        self._stop = stop_event
        self.sent = []          # только тексты — как было раньше
        self.addressed = []     # пары (chat_id, текст)

    async def get_updates(self, offset, timeout):
        await asyncio.sleep(0)
        if self._batches:
            return self._batches.pop(0)
        self._stop.set()
        return []

    async def send_message(self, payload, *, chat_id=None, **kwargs):
        self.sent.append(payload)
        self.addressed.append((chat_id, payload))
        return True

    async def deliver(self, chat_id, payload, **kwargs):
        self.addressed.append((chat_id, payload))

    async def answer_callback_query(self, callback_id, text=None):
        self.addressed.append(("callback", text))


def _msg(update_id, chat_id, text, user_id=None):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "from": {"id": user_id if user_id is not None else chat_id,
                     "first_name": "Tester"},
            "text": text,
        },
    }


async def _reply(text):
    return text


async def _deny_everyone(_user_id):
    """Никого не пускаем: доступ даёт только owner_chat_id."""
    return Access(allowed=False, is_admin=False)


def _run_once(batches, *, allowed_chat_id=42, authorize=_deny_everyone):
    """Прогоняет опрос до исчерпания батчей.

    Первый батч достаётся _skip_backlog (getUpdates с offset=-1),
    остальные — рабочему циклу.
    """
    stop = asyncio.Event()
    sender = FakeSender(batches, stop)
    listener = CommandListener(
        sender, authorize=authorize, owner_chat_id=allowed_chat_id
    )
    listener.register("info", lambda ctx: _reply("ответ /info"))

    asyncio.run(asyncio.wait_for(listener.run(stop), timeout=5))
    return sender, listener


def test_info_command_is_answered():
    sender, _ = _run_once([[], [_msg(10, 42, "/info")]])
    assert sender.sent == ["ответ /info"]


def test_answer_goes_back_to_the_asking_chat():
    sender, _ = _run_once([[], [_msg(10, 42, "/info")]])
    assert sender.addressed == [(42, "ответ /info")]


def test_command_from_foreign_chat_is_ignored():
    sender, _ = _run_once([[], [_msg(10, 999, "/info")]])
    assert sender.sent == []


def test_approved_user_from_own_chat_is_answered():
    async def approve_everyone(_user_id):
        return Access(allowed=True, is_admin=False)

    sender, _ = _run_once(
        [[], [_msg(10, 999, "/info")]], authorize=approve_everyone
    )
    assert sender.addressed == [(999, "ответ /info")]


def test_unknown_command_is_ignored():
    sender, _ = _run_once([[], [_msg(10, 42, "/stats")]])
    assert sender.sent == []


def test_plain_text_is_ignored():
    sender, _ = _run_once([[], [_msg(10, 42, "просто текст")]])
    assert sender.sent == []


def test_offset_advances_past_handled_update():
    _, listener = _run_once([[], [_msg(10, 42, "/info")]])
    assert listener._offset == 11


def test_backlog_is_skipped_on_start():
    # Первый батч — ответ на getUpdates(offset=-1): старую команду не выполняем.
    sender, listener = _run_once([[_msg(7, 42, "/info")], []])
    assert sender.sent == []
    assert listener._offset == 8


def test_command_with_bot_suffix_and_args():
    assert CommandListener._parse_command("/info@my_bot arg") == ("info", "arg")
    assert CommandListener._parse_command("/INFO") == ("info", "")
    assert CommandListener._parse_command("/add@bot новое слово") == (
        "add", "новое слово",
    )


def test_handler_receives_identity_and_args():
    seen = {}

    async def capture(ctx):
        seen["user_id"] = ctx.user_id
        seen["args"] = ctx.args
        seen["is_admin"] = ctx.is_admin
        return None

    stop = asyncio.Event()
    sender = FakeSender([[], [_msg(10, 42, "/add Науменко", user_id=7)]], stop)
    listener = CommandListener(
        sender, authorize=_deny_everyone, owner_chat_id=42
    )
    listener.register("add", capture)
    asyncio.run(asyncio.wait_for(listener.run(stop), timeout=5))

    assert seen == {"user_id": 7, "args": "Науменко", "is_admin": True}


def test_admin_only_command_is_refused_for_plain_user():
    async def approve_everyone(_user_id):
        return Access(allowed=True, is_admin=False)

    stop = asyncio.Event()
    sender = FakeSender([[], [_msg(10, 999, "/approve 5")]], stop)
    listener = CommandListener(
        sender, authorize=approve_everyone, owner_chat_id=42
    )
    listener.register("approve", lambda ctx: _reply("одобрено"), admin_only=True)
    asyncio.run(asyncio.wait_for(listener.run(stop), timeout=5))

    assert sender.sent == []


def test_public_command_reaches_stranger():
    stop = asyncio.Event()
    sender = FakeSender([[], [_msg(10, 999, "/start")]], stop)
    listener = CommandListener(
        sender, authorize=_deny_everyone, owner_chat_id=42
    )
    listener.register("start", lambda ctx: _reply("заявка"), public=True)
    asyncio.run(asyncio.wait_for(listener.run(stop), timeout=5))

    assert sender.addressed == [(999, "заявка")]
