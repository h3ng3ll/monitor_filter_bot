"""Тесты переноса разметки оригинала (entities) в копию.

Запуск:  .venv/bin/python -m pytest -q
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon.tl import types as tl  # noqa: E402

from app.formatting import (  # noqa: E402
    escape_html,
    message_entities,
    render_body,
    render_html,
)

# Реальный текст из задачи: две инлайн-ссылки и жирный заголовок.
SAMPLE = (
    "Бан на модерації банера!\n"
    "X Sky Whisper (6921) has been UNPUBLISHED from the App Store.\n"
    "Open in App Store (FR) | Open in App Store (US)"
)
_FR = SAMPLE.index("Open in App Store (FR)")


def _sample_entities():
    return [
        tl.MessageEntityBold(offset=0, length=24),
        tl.MessageEntityTextUrl(
            offset=_FR, length=22, url="https://apps.apple.com/fr/app/id6921"
        ),
        tl.MessageEntityTextUrl(
            offset=_FR + 25, length=22, url="https://apps.apple.com/us/app/id6921"
        ),
    ]


# --------------------------------------------------------------------------- #
# Что переносится
# --------------------------------------------------------------------------- #

def test_inline_links_and_bold_survive():
    out = render_html(SAMPLE, _sample_entities())
    assert '<a href="https://apps.apple.com/fr/app/id6921">Open in App Store (FR)</a>' in out
    assert '<a href="https://apps.apple.com/us/app/id6921">Open in App Store (US)</a>' in out
    assert "<b>Бан на модерації банера!</b>" in out
    # Видимый текст не потерян.
    assert "X Sky Whisper (6921)" in out


def test_spoiler_is_rendered():
    # Telethon свой unparse теряет спойлеры — наша таблица их знает.
    out = render_html("secret", [tl.MessageEntitySpoiler(offset=0, length=6)])
    assert out == "<tg-spoiler>secret</tg-spoiler>"


def test_pre_is_compact_and_has_no_literal_braces():
    # Оригинальный unparse вставляет сюда литеральные "{}" и отступы.
    assert render_html("abc", [tl.MessageEntityPre(offset=0, length=3, language="")]) == "<pre>abc</pre>"
    out = render_html("abc", [tl.MessageEntityPre(offset=0, length=3, language="python")])
    assert out == '<pre><code class="language-python">abc</code></pre>'
    assert "{}" not in out


def test_pre_language_is_sanitized():
    out = render_html("abc", [tl.MessageEntityPre(offset=0, length=3, language="py'\"><x")])
    assert out == '<pre><code class="language-pyx">abc</code></pre>'


def test_nested_entities_open_outer_first():
    out = render_html("abcd", [
        tl.MessageEntityBold(offset=0, length=4),
        tl.MessageEntityItalic(offset=0, length=2),
    ])
    assert out == "<b><i>ab</i>cd</b>"


# --------------------------------------------------------------------------- #
# Что отбрасывается — но текст остаётся
# --------------------------------------------------------------------------- #

def test_custom_emoji_is_dropped_but_text_kept():
    # <tg-emoji> чужого эмодзи уронил бы весь sendMessage с 400.
    out = render_html("hi", [tl.MessageEntityCustomEmoji(offset=0, length=2, document_id=5)])
    assert out == "hi"
    assert "tg-emoji" not in out


def test_mention_name_is_dropped():
    out = render_html("Ivan", [tl.MessageEntityMentionName(offset=0, length=4, user_id=1)])
    assert out == "Ivan"
    assert "tg://user" not in out


def test_plain_url_entity_needs_no_markup():
    # Telegram сам распознаёт ссылки в тексте — размечать не нужно.
    out = render_html("https://x.example", [tl.MessageEntityUrl(offset=0, length=17)])
    assert "<a" not in out


def test_javascript_url_is_dropped():
    out = render_html("click", [
        tl.MessageEntityTextUrl(offset=0, length=5, url="javascript:alert(1)")
    ])
    assert out == "click"


def test_out_of_bounds_entity_is_skipped():
    out = render_html("ab", [tl.MessageEntityBold(offset=0, length=99)])
    assert out == "ab"


# --------------------------------------------------------------------------- #
# Экранирование и UTF-16
# --------------------------------------------------------------------------- #

def test_text_is_escaped_around_entity_spans():
    out = render_html('a<b> & link', [
        tl.MessageEntityTextUrl(offset=7, length=4, url="https://x/?a=1&b=2")
    ])
    assert "a&lt;b&gt; &amp; " in out
    assert 'href="https://x/?a=1&amp;b=2"' in out


def test_offsets_are_counted_in_utf16():
    # Эмодзи занимает 2 кодовые единицы: смещение 3, а не 2.
    text = "🚨 bold tail"
    out = render_html(text, [tl.MessageEntityBold(offset=3, length=4)])
    assert out == "🚨 <b>bold</b> tail"


def test_no_entities_matches_plain_escape():
    assert render_html("a<b>&c") == escape_html("a<b>&c")


# --------------------------------------------------------------------------- #
# Обрезка
# --------------------------------------------------------------------------- #

def test_truncation_never_leaves_open_tag():
    out = render_body("A" * 500, [tl.MessageEntityBold(offset=0, length=500)], budget=200)
    assert out.count("<b>") == out.count("</b>") == 1
    assert len(out) <= 200
    assert "обрезано" in out


def test_truncation_drops_entities_past_the_cut():
    entities = [
        tl.MessageEntityBold(offset=0, length=5),
        tl.MessageEntityTextUrl(offset=300, length=5, url="https://x.example"),
    ]
    out = render_body("A" * 400, entities, budget=120)
    assert "<b>AAAAA</b>" in out
    assert "x.example" not in out
    assert len(out) <= 120


def test_truncation_does_not_split_surrogate_pair():
    out = render_body("🚨" * 100, None, budget=50)
    # Уцелевшая половинка суррогата сломала бы кодирование.
    assert out.encode("utf-16", "surrogatepass").decode("utf-16") == out
    assert len(out) <= 50


def test_escape_heavy_text_is_not_collapsed():
    # Прежний _truncate схлопывал такой текст в одно многоточие.
    out = render_body("<&>" * 4000, None, budget=4000)
    assert len(out) > 1000
    assert out != "…"
    assert len(out) <= 4000


def test_input_entities_are_not_mutated():
    entities = _sample_entities()
    before = [(e.offset, e.length) for e in entities]
    render_body("  " + SAMPLE + "  ", entities, budget=80)
    assert [(e.offset, e.length) for e in entities] == before


def test_leading_whitespace_is_stripped_without_shifting_entities():
    out = render_body("   bold tail", [tl.MessageEntityBold(offset=3, length=4)], budget=200)
    assert out == "<b>bold</b> tail"


def test_zero_budget_returns_ellipsis():
    assert render_body("text", None, budget=0) == "…"


# --------------------------------------------------------------------------- #
# Доступ к сущностям
# --------------------------------------------------------------------------- #

def test_message_entities_tolerates_objects_without_attribute():
    assert message_entities(SimpleNamespace(id=1, raw_text="x")) == []
    assert message_entities(SimpleNamespace(entities=None)) == []
    assert len(message_entities(SimpleNamespace(entities=_sample_entities()))) == 3
