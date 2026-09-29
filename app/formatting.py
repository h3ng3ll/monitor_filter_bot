"""Преобразование сообщения Telegram (текст + entities) в HTML для Bot API.

Сообщение приходит парой «текст + entities»: сами теги в тексте не хранятся,
вместо них лежит список сущностей со смещениями. Смещения считаются в кодовых
единицах UTF-16, поэтому эмодзи занимает две позиции, а не одну — отсюда вся
возня с суррогатными парами.

Модуль умеет две вещи:
  * :func:`render_html` — собрать из пары безопасный HTML;
  * :func:`render_body` — то же, но с обрезкой под лимит Telegram так, чтобы
    не разорвать тег и не оставить его незакрытым.

Алгоритм расстановки тегов повторяет ``telethon.extensions.html.unparse``:
он корректно обходит суррогатные пары и экранирует только тот текст, который
ещё не тронут. Своя копия нужна из-за двух дефектов оригинала (проверено на
Telethon 1.45.0):

  * он не знает ``MessageEntitySpoiler`` и молча теряет скрытый текст;
  * в ``<pre>`` он подставляет литеральные ``{}`` и лишние отступы, потому что
    ``.format()`` в его таблице применяется только к открывающему тегу.

Зависимость от Telethon — только арифметика суррогатов из ``telethon.helpers``.
Сами сущности различаются по имени класса, поэтому импортировать ``tl.types``
не нужно, а таблица форматтеров остаётся декларативной.
"""

from __future__ import annotations

import copy
import html
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from telethon.helpers import (
    add_surrogate,
    del_surrogate,
    strip_text,
    within_surrogate,
)

log = logging.getLogger(__name__)

# Приписка к обрезанному тексту. Совпадает с прежней в bot_sender.py.
TRUNCATION_SUFFIX = "\n…[сообщение обрезано]"

# Схемы, которые Bot API принимает в href. Всё остальное (javascript:, data:)
# отбрасываем вместе с сущностью — видимый текст ссылки при этом остаётся.
ALLOWED_URL_SCHEMES = ("http://", "https://", "tg://", "mailto:")

# Сколько раз пытаемся ужать тело под бюджет, прежде чем сдаться.
_MAX_SHRINK_STEPS = 24

# Язык подсветки приходит из чужого сообщения — оставляем только безопасное.
_LANGUAGE_RE = re.compile(r"[^A-Za-z0-9_+#.\-]")

Formatter = Callable[[Any, str], Tuple[str, str]]


def escape_html(text: Optional[str]) -> str:
    """Экранирует текст для Telegram HTML parse mode.

    Telegram требует экранировать ``&``, ``<`` и ``>``; кавычки трогать не нужно,
    поэтому ``quote=False`` — так текст остаётся читаемым.
    """
    return html.escape(text or "", quote=False)


def message_entities(message: Any) -> List[Any]:
    """Сущности сообщения или пустой список.

    Через getattr, а не ``message.entities``: у служебных сообщений и у
    тестовых двойников атрибута может не быть вовсе.
    """
    return list(getattr(message, "entities", None) or [])


# --------------------------------------------------------------------------- #
# Таблица форматтеров = политика: что вообще переносим в копию
# --------------------------------------------------------------------------- #

def _pre(entity: Any, _: str) -> Tuple[str, str]:
    """<pre> с подсветкой, если язык указан и прошёл очистку."""
    language = _LANGUAGE_RE.sub("", getattr(entity, "language", "") or "")[:32]
    if language:
        return f'<pre><code class="language-{language}">', "</code></pre>"
    return "<pre>", "</pre>"


def _text_url(entity: Any, _: str) -> Tuple[str, str]:
    # В атрибуте кавычки экранировать обязательно, иначе href можно разорвать.
    return f'<a href="{html.escape(entity.url, quote=True)}">', "</a>"


def _blockquote(entity: Any, _: str) -> Tuple[str, str]:
    if getattr(entity, "collapsed", False):
        return "<blockquote expandable>", "</blockquote>"
    return "<blockquote>", "</blockquote>"


# Рендерим только то, что Bot API точно примет от бота. Остальное намеренно
# отсутствует:
#   * CustomEmoji — <tg-emoji> чужого эмодзи бот отправить не может, и весь
#     sendMessage упадёт с 400, то есть уведомление потеряется целиком;
#   * MentionName — ссылка tg://user?id= от бота проходит не всегда;
#   * Url/Email/Mention/Hashtag/Cashtag/BotCommand/Phone/BankCard — Telegram
#     сам заново распознаёт их в тексте, размечать не требуется.
# Текст таких сущностей не теряется: пропадает только оформление.
_FORMATTERS: Dict[str, Formatter] = {
    "MessageEntityBold": lambda e, t: ("<b>", "</b>"),
    "MessageEntityItalic": lambda e, t: ("<i>", "</i>"),
    "MessageEntityUnderline": lambda e, t: ("<u>", "</u>"),
    "MessageEntityStrike": lambda e, t: ("<s>", "</s>"),
    "MessageEntitySpoiler": lambda e, t: ("<tg-spoiler>", "</tg-spoiler>"),
    "MessageEntityCode": lambda e, t: ("<code>", "</code>"),
    "MessageEntityPre": _pre,
    "MessageEntityBlockquote": _blockquote,
    "MessageEntityTextUrl": _text_url,
}


def _renderable(surrogate: str, entities: Optional[Sequence[Any]]) -> List[Any]:
    """Отбирает поддерживаемые сущности с корректными границами.

    Сортировка по ``(offset, -length)`` гарантирует, что при одинаковом начале
    внешний тег откроется раньше внутреннего: ``<b><i>ab</i>cdef</b>``.
    """
    if not entities:
        return []

    limit = len(surrogate)
    usable: List[Any] = []
    for entity in entities:
        if type(entity).__name__ not in _FORMATTERS:
            continue

        offset = getattr(entity, "offset", None)
        length = getattr(entity, "length", None)
        if not isinstance(offset, int) or not isinstance(length, int):
            continue
        if offset < 0 or length <= 0 or offset + length > limit:
            continue  # битые границы ломают разметку — проще пропустить

        if type(entity).__name__ == "MessageEntityTextUrl":
            url = (getattr(entity, "url", "") or "").strip().lower()
            if not url.startswith(ALLOWED_URL_SCHEMES):
                log.debug("Ссылка со схемой вне белого списка отброшена")
                continue

        usable.append(entity)

    usable.sort(key=lambda e: (e.offset, -e.length))
    return usable


# --------------------------------------------------------------------------- #
# Рендеринг
# --------------------------------------------------------------------------- #

def render_html(text: str, entities: Optional[Sequence[Any]] = None) -> str:
    """Собирает HTML из пары «текст + entities».

    Пересекающиеся (не вложенные) сущности Telegram-клиенты не создают, и
    отдельно они здесь не разбираются: в теории такая пара дала бы
    ``<b>ab<i>cd</b>ef</i>``. На этот случай в bot_sender есть запасная
    отправка без разметки.
    """
    if not text:
        return ""

    surrogate = add_surrogate(text)
    usable = _renderable(surrogate, entities)
    if not usable:
        return escape_html(text)

    insert_at: List[Tuple[int, int, str]] = []
    for index, entity in enumerate(usable):
        start = entity.offset
        end = start + entity.length
        open_tag, close_tag = _FORMATTERS[type(entity).__name__](
            entity, surrogate[start:end]
        )
        insert_at.append((start, index, open_tag))
        insert_at.append((end, -index, close_tag))
    insert_at.sort(key=lambda item: (item[0], item[1]))

    # Идём справа налево и экранируем только ещё не тронутый кусок текста,
    # иначе экранировались бы уже вставленные теги.
    bound = len(surrogate)
    while insert_at:
        at, _, what = insert_at.pop()
        while within_surrogate(surrogate, at):
            at += 1
        surrogate = (
            surrogate[:at]
            + what
            + escape_html(surrogate[at:bound])
            + surrogate[bound:]
        )
        bound = at

    return del_surrogate(escape_html(surrogate[:bound]) + surrogate[bound:])


def _prepare(
    text: str, entities: Optional[Sequence[Any]]
) -> Tuple[str, List[Any]]:
    """``text.strip()`` со сдвигом смещений сущностей.

    strip_text умеет и то и другое, но работает с суррогатным текстом и правит
    сущности на месте — поэтому копируем: они принадлежат объекту сообщения
    Telethon, менять их нельзя.
    """
    surrogate = add_surrogate(text or "")
    usable = [copy.copy(entity) for entity in _renderable(surrogate, entities)]
    stripped = strip_text(surrogate, usable)
    return stripped, usable


def _cut(
    surrogate: str, entities: Sequence[Any], limit: int
) -> Tuple[str, List[Any]]:
    """Режет суррогатный текст по limit и подрезает сущности под срез."""
    if limit >= len(surrogate):
        return surrogate, list(entities)

    while limit > 0 and within_surrogate(surrogate, limit):
        limit -= 1  # не разрываем суррогатную пару пополам (эмодзи)

    clipped: List[Any] = []
    for entity in entities:
        if entity.offset >= limit:
            continue  # сущность целиком за срезом
        if entity.offset + entity.length <= limit:
            clipped.append(entity)
            continue
        shortened = copy.copy(entity)
        shortened.length = limit - entity.offset
        clipped.append(shortened)

    return surrogate[:limit], clipped


def render_body(
    text: str,
    entities: Optional[Sequence[Any]] = None,
    *,
    budget: int,
) -> str:
    """HTML тела уведомления, гарантированно не длиннее budget символов.

    Режем не готовый HTML, а исходную пару «текст + сущности»: тогда разметка
    получается корректной по построению — тег невозможно разрубить пополам или
    оставить незакрытым.

    Бюджет считаем по длине готового HTML. Это строже, чем требует Telegram
    (он меряет текст уже после разбора тегов), зато итог проверяется одним len().
    """
    if budget <= 0:
        return "…"

    stripped, usable = _prepare(text, entities)
    plain = del_surrogate(stripped)
    rendered = render_html(plain, usable)
    if len(rendered) <= budget:
        return rendered

    suffix = escape_html(TRUNCATION_SUFFIX)
    payload_budget = budget - len(suffix)
    if payload_budget <= 0:
        return "…"

    limit = min(len(stripped), payload_budget)
    for _ in range(_MAX_SHRINK_STEPS):
        if limit <= 0:
            break
        head, clipped = _cut(stripped, usable, limit)
        candidate = render_html(del_surrogate(head), clipped) + suffix
        if len(candidate) <= budget:
            return candidate
        # Теги и экранирование раздувают текст, поэтому ужимаем срез
        # пропорционально перерасходу: иначе на строке из «<&>» пришлось бы
        # делать сотни шагов по одному символу.
        overhead = max(1, len(candidate) - len(suffix))
        limit = min(limit - 1, max(1, limit * payload_budget // overhead))

    return "…"
