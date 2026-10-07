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
    out = format_message(text="<script>alert(1)</script> & co")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_format_message_is_plain_copy_without_envelope():
    out = format_message(text="привет")
    assert out == "привет"
    for noise in ("ВАЖНОЕ", "Источник", "Время", "Причина", "ID:"):
        assert noise not in out


def test_format_message_header_only_when_given():
    out = format_message(text="привет", header="✅ СЕРВИС ЗАПУЩЕН")
    assert out == "<b>✅ СЕРВИС ЗАПУЩЕН</b>\n\nпривет"


def test_format_message_fits_telegram_limit():
    out = format_message(text="A" * 10000)
    assert len(out) <= 4096
    assert "обрезано" in out


def test_format_message_fits_limit_with_escaping_heavy_text():
    out = format_message(text="<&>" * 4000)
    assert len(out) <= 4096


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# --------------------------------------------------------------------------- #
# Режимы совпадения
# --------------------------------------------------------------------------- #

def test_any_mode_is_the_default_and_unchanged():
    f = MessageFilter(keywords=["April", "Natalie"])
    assert f.mode == "any"
    assert f.check("Team: April").matched is True


def test_all_mode_requires_every_keyword():
    f = MessageFilter(keywords=["April", "Sky"], mode="all")
    assert f.check("Team: April, X Sky Whisper").matched is True

    result = f.check("Team: April")
    assert result.matched is False
    assert 'missing keyword: "Sky"' == result.reason


def test_all_mode_still_respects_exclude():
    f = MessageFilter(keywords=["April"], exclude_keywords=["реклама"], mode="all")
    assert f.check("April и реклама").matched is False


def test_regex_mode_ignores_keywords():
    f = MessageFilter.from_settings(
        keywords=["April"], regex="Науменк", mode="regex"
    )
    assert f.check("Team: April").matched is False
    assert f.check("White: Олександр Науменко").matched is True


def test_regex_mode_without_pattern_matches_nothing():
    f = MessageFilter(keywords=["April"], mode="regex")
    result = f.check("Team: April")
    assert result.matched is False
    assert result.reason == "regex mode: pattern not set"


def test_from_settings_ignores_broken_pattern():
    # Битый паттерн не должен ронять рассылку остальным правилам.
    f = MessageFilter.from_settings(keywords=["April"], regex="[unclosed")
    assert f.regex is None
    assert f.check("Team: April").matched is True


def test_from_settings_regex_is_case_insensitive_by_default():
    f = MessageFilter.from_settings(regex="науменк", mode="regex")
    assert f.check("НАУМЕНКО").matched is True
    sensitive = MessageFilter.from_settings(
        regex="науменк", mode="regex", case_sensitive=True
    )
    assert sensitive.check("НАУМЕНКО").matched is False


def test_unknown_mode_falls_back_to_any():
    f = MessageFilter(keywords=["April"], mode="нечто")
    assert f.mode == "any"
    assert f.check("Team: April").matched is True


def test_describe_mentions_non_default_mode():
    assert "mode=all" in MessageFilter(keywords=["a"], mode="all").describe()
    assert "mode=" not in MessageFilter(keywords=["a"]).describe()
