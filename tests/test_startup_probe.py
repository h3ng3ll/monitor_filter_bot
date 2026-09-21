"""Тесты самопроверки при старте и служебного форматирования.

Запуск:  .venv/bin/python -m pytest -q
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bot_sender import TELEGRAM_MAX_TEXT, format_message, format_status  # noqa: E402
from app.filter import MessageFilter  # noqa: E402
from app.main import Application  # noqa: E402


# --------------------------------------------------------------------------- #
# Форматирование
# --------------------------------------------------------------------------- #

def test_extra_fields_appear_in_header():
    out = format_message(
        source_label="@src",
        sent_at=None,
        message_id=7,
        text="тело",
        extra_fields=[("Аккаунт", "Вася (id=1)"), ("Целевой чат", "42")],
    )
    assert "<b>Аккаунт:</b> Вася (id=1)" in out
    assert "<b>Целевой чат:</b> 42" in out


def test_extra_fields_do_not_break_telegram_limit():
    # Длинная шапка должна урезать тело, а не переполнить сообщение.
    out = format_message(
        source_label="@src",
        sent_at=None,
        message_id=7,
        text="я" * 5000,
        extra_fields=[("Правила", "к" * 500)],
    )
    assert len(out) <= TELEGRAM_MAX_TEXT


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
