"""Минимальные тесты фильтра и форматирования.

Запуск:  .venv/bin/python -m pytest -q
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bot_sender import escape_html, format_message  # noqa: E402
from app.filter import MessageFilter  # noqa: E402


# --------------------------------------------------------------------------- #
# Ключевые слова
# --------------------------------------------------------------------------- #

def test_keyword_match():
    f = MessageFilter(keywords=["важно", "критично"])
    result = f.check("Сервер недоступен, это критично")
    assert result.matched
    assert "критично" in result.reason


def test_keyword_match_is_case_insensitive_by_default():
    f = MessageFilter(keywords=["критично"])
    assert f.check("КРИТИЧНО! Диск заполнен").matched


def test_case_sensitive_mode():
    f = MessageFilter(keywords=["критично"], case_sensitive=True)
    assert not f.check("КРИТИЧНО").matched
    assert f.check("критично").matched


def test_no_keyword_no_match():
    f = MessageFilter(keywords=["важно"])
    result = f.check("Обычное сообщение без ключевых слов")
    assert not result.matched
    assert result.reason == "no matching keywords"


def test_empty_text_does_not_match():
    f = MessageFilter(keywords=["важно"])
    assert not f.check("").matched
    assert not f.check(None).matched


def test_pass_through_when_no_allow_rules():
    f = MessageFilter()
    assert f.check("что угодно").matched


# --------------------------------------------------------------------------- #
# Запрещённые слова: приоритет выше разрешающих
# --------------------------------------------------------------------------- #

def test_exclude_beats_keyword():
    f = MessageFilter(keywords=["важно"], exclude_keywords=["реклама"])
    result = f.check("ВАЖНО! РЕКЛАМА")
    assert not result.matched
    assert "реклама" in result.reason


def test_exclude_applies_without_keywords():
    f = MessageFilter(exclude_keywords=["подпишись"])
    assert not f.check("Подпишись на канал").matched


# --------------------------------------------------------------------------- #
# Regex
# --------------------------------------------------------------------------- #

def test_regex_match():
    f = MessageFilter(regex=re.compile(r"CPU\s+>\s*9\d%", re.IGNORECASE))
    result = f.check("Алерт: CPU > 95% на node-1")
    assert result.matched
    assert "regex" in result.reason


def test_regex_or_keyword():
    f = MessageFilter(keywords=["авария"], regex=re.compile(r"\berror\b", re.I))
    assert f.check("Unhandled ERROR in worker").matched
    assert f.check("Авария на линии").matched
    assert not f.check("всё хорошо").matched


def test_exclude_beats_regex():
    f = MessageFilter(
        exclude_keywords=["тест"], regex=re.compile(r"\berror\b", re.I)
    )
    assert not f.check("ERROR (это тест)").matched


# --------------------------------------------------------------------------- #
# Длина
# --------------------------------------------------------------------------- #

def test_min_length():
    f = MessageFilter(keywords=["ок"], min_length=10)
    assert not f.check("ок").matched
    assert f.check("ок, всё работает нормально").matched


def test_max_length():
    f = MessageFilter(max_length=5)
    assert not f.check("слишком длинное сообщение").matched


# --------------------------------------------------------------------------- #
# Форматирование уведомления
# --------------------------------------------------------------------------- #

def test_escape_html():
    assert escape_html("<b>&</b>") == "&lt;b&gt;&amp;&lt;/b&gt;"


def test_format_message_escapes_user_text():
    out = format_message(
        source_label="@some_bot",
        sent_at=None,
        message_id=12345,
        text="<script>alert(1)</script> & co",
        reason='keyword: "критично"',
    )
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<b>ID:</b> 12345" in out
    assert "@some_bot" in out


def test_format_message_adds_link_only_when_given():
    without = format_message(
        source_label="@some_bot", sent_at=None, message_id=1, text="привет"
    )
    assert "Открыть оригинал" not in without

    with_link = format_message(
        source_label="@some_bot",
        sent_at=None,
        message_id=1,
        text="привет",
        link="https://t.me/c/123/1",
    )
    assert 'href="https://t.me/c/123/1"' in with_link


def test_format_message_fits_telegram_limit():
    out = format_message(
        source_label="@some_bot",
        sent_at=None,
        message_id=1,
        text="A" * 10000,
    )
    assert len(out) <= 4096
    assert "обрезано" in out


def test_format_message_fits_limit_with_escaping_heavy_text():
    out = format_message(
        source_label="@some_bot",
        sent_at=None,
        message_id=1,
        text="<&>" * 4000,
    )
    assert len(out) <= 4096


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
