"""Тесты самопроверки при старте, команд бота и служебного форматирования.

Запуск:  .venv/bin/python -m pytest -q
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bot_commands import CommandListener  # noqa: E402
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
        self.sent = []

    async def get_updates(self, offset, timeout):
        await asyncio.sleep(0)
        if self._batches:
            return self._batches.pop(0)
        self._stop.set()
        return []

    async def send_message(self, payload):
        self.sent.append(payload)
        return True


def _msg(update_id, chat_id, text):
    return {"update_id": update_id,
            "message": {"chat": {"id": chat_id}, "text": text}}


async def _reply(text):
    return text


def _run_once(batches, *, allowed_chat_id=42):
    """Прогоняет опрос до исчерпания батчей.

    Первый батч достаётся _skip_backlog (getUpdates с offset=-1),
    остальные — рабочему циклу.
    """
    stop = asyncio.Event()
    sender = FakeSender(batches, stop)
    listener = CommandListener(sender, allowed_chat_id=allowed_chat_id)
    listener.register("info", lambda: _reply("ответ /info"))

    asyncio.run(asyncio.wait_for(listener.run(stop), timeout=5))
    return sender, listener


def test_info_command_is_answered():
    sender, _ = _run_once([[], [_msg(10, 42, "/info")]])
    assert sender.sent == ["ответ /info"]


def test_command_from_foreign_chat_is_ignored():
    sender, _ = _run_once([[], [_msg(10, 999, "/info")]])
    assert sender.sent == []


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
    assert CommandListener._parse_command("/info@my_bot arg") == "info"
    assert CommandListener._parse_command("/INFO") == "info"
