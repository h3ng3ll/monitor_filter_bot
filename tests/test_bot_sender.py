"""Тесты отправки: запасной вариант без разметки и классификация ошибок.

Запуск:  .venv/bin/python -m pytest -q
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon.tl import types as tl  # noqa: E402

from app.bot_sender import (  # noqa: E402
    BotApiError,
    BotBlockedError,
    BotSender,
    format_message,
    format_message_with_fallback,
)


def _sender_with_transport(errors):
    """BotSender с подменённым транспортом: сети в тестах нет.

    errors — что вернуть/бросить на N-й вызов (None = успех).
    """
    sender = BotSender("token", 42)
    sent = []

    async def fake_call(method, payload, **kwargs):
        sent.append(payload)
        index = len(sent) - 1
        if index < len(errors) and errors[index] is not None:
            raise errors[index]
        return {}

    sender._call = fake_call
    return sender, sent


# --------------------------------------------------------------------------- #
# Запасная отправка без разметки
# --------------------------------------------------------------------------- #

def test_markup_error_falls_back_to_plain():
    error = BotApiError(
        "sendMessage: Bad Request: can't parse entities: "
        'unsupported start tag "tg-emoji"',
        status=400,
        description="Bad Request: can't parse entities",
    )
    sender, sent = _sender_with_transport([error])
    assert asyncio.run(sender.send_message("<b>x</b>", fallback_text="plain")) is True
    assert [p["text"] for p in sent] == ["<b>x</b>", "plain"]


def test_fallback_is_attempted_only_once():
    error = BotApiError("sendMessage: can't parse entities", status=400)
    # Запасной вариант тоже отвергнут — третьей попытки быть не должно.
    sender, sent = _sender_with_transport([error, error])
    assert asyncio.run(sender.send_message("<b>x</b>", fallback_text="plain")) is False
    assert len(sent) == 2


def test_other_errors_do_not_retry():
    error = BotApiError("sendMessage: Bad Request: chat not found", status=400)
    sender, sent = _sender_with_transport([error])
    assert asyncio.run(sender.send_message("<b>x</b>", fallback_text="plain")) is False
    assert len(sent) == 1


def test_identical_fallback_is_not_retried():
    error = BotApiError("sendMessage: can't parse entities", status=400)
    sender, sent = _sender_with_transport([error])
    assert asyncio.run(sender.send_message("same", fallback_text="same")) is False
    assert len(sent) == 1


def test_missing_fallback_is_not_retried():
    error = BotApiError("sendMessage: can't parse entities", status=400)
    sender, sent = _sender_with_transport([error])
    assert asyncio.run(sender.send_message("<b>x</b>")) is False
    assert len(sent) == 1


# --------------------------------------------------------------------------- #
# Адресация
# --------------------------------------------------------------------------- #

def test_chat_id_defaults_to_configured_target():
    sender, sent = _sender_with_transport([])
    asyncio.run(sender.send_message("hi"))
    assert sent[0]["chat_id"] == 42


def test_explicit_chat_id_overrides_target():
    sender, sent = _sender_with_transport([])
    asyncio.run(sender.send_message("hi", chat_id=777))
    assert sent[0]["chat_id"] == 777
    assert sent[0]["disable_web_page_preview"] is True


def test_deliver_raises_instead_of_returning_false():
    error = BotBlockedError("sendMessage: Forbidden: bot was blocked by the user",
                            status=403)
    sender, _ = _sender_with_transport([error])
    try:
        asyncio.run(sender.deliver(5, "hi"))
    except BotBlockedError:
        pass
    else:
        raise AssertionError("BotBlockedError должен пробрасываться наружу")


def test_blocked_error_is_a_bot_api_error():
    # Обработчики ловят BotApiError — BotBlockedError не должен утечь мимо.
    assert issubclass(BotBlockedError, BotApiError)


# --------------------------------------------------------------------------- #
# Пара «с разметкой / без разметки»
# --------------------------------------------------------------------------- #

def _kwargs(**extra):
    base = dict(
        source_label="@src",
        sent_at=None,
        message_id=7,
        text="Open link here",
    )
    base.update(extra)
    return base


def test_fallback_pair_differs_only_when_entities_given():
    entities = [tl.MessageEntityTextUrl(offset=5, length=4, url="https://x.example")]
    rich, plain = format_message_with_fallback(**_kwargs(entities=entities))
    assert 'href="https://x.example"' in rich
    assert "<a" not in plain
    assert "Open link here" in plain


def test_fallback_pair_is_identical_without_entities():
    rich, plain = format_message_with_fallback(**_kwargs())
    assert rich == plain


def test_format_message_without_entities_is_unchanged():
    # Замок на обратную совместимость: старый вызов даёт прежний результат.
    assert format_message(**_kwargs()) == format_message(**_kwargs(entities=None))
    assert format_message(**_kwargs()) == format_message(**_kwargs(entities=[]))


def test_format_message_with_entities_fits_telegram_limit():
    long_text = "A" * 10000
    entities = [tl.MessageEntityBold(offset=0, length=10000)]
    out = format_message(**_kwargs(text=long_text, entities=entities))
    assert len(out) <= 4096
    assert out.count("<b>") == out.count("</b>")


def test_format_message_preserves_entities_and_envelope():
    entities = [tl.MessageEntityTextUrl(offset=5, length=4, url="https://x.example")]
    out = format_message(**_kwargs(entities=entities, reason='keyword: "x"'))
    assert 'href="https://x.example"' in out
    assert "<b>ID:</b> 7" in out
