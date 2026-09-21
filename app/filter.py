"""Фильтрация текста сообщений.

Модуль намеренно не зависит ни от Telethon, ни от сети — это чистая функция
«текст -> решение», поэтому его легко тестировать (см. tests/test_filter.py).

Порядок проверок (приоритет сверху вниз):

1. ``exclude`` — запрещённые слова. Срабатывает раньше всего: сообщение
   отбрасывается, даже если содержит разрешённое слово.
2. ``length`` — минимальная/максимальная длина текста.
3. ``keywords`` / ``regex`` — разрешающие правила, объединяются по ИЛИ.

Если не задано ни одного разрешающего правила (нет keywords и нет regex),
фильтр пропускает всё, что не отсеяно шагами 1–2.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Pattern, Sequence

from .config import Config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilterResult:
    """Результат проверки сообщения.

    matched: пропустить сообщение дальше (True) или отбросить (False).
    reason:  короткая причина решения — попадает в лог.
    """

    matched: bool
    reason: str

    def __bool__(self) -> bool:  # позволяет писать `if result:`
        return self.matched


class MessageFilter:
    """Применяет набор правил к тексту сообщения.

    Расширение: чтобы добавить новое правило (тип сообщения, отправитель,
    время суток и т.п.), добавьте метод ``_check_<name>`` и вызовите его
    в :meth:`check` в нужном месте цепочки — контракт FilterResult не меняется.
    """

    def __init__(
        self,
        keywords: Sequence[str] = (),
        exclude_keywords: Sequence[str] = (),
        regex: Optional[Pattern[str]] = None,
        case_sensitive: bool = False,
        min_length: int = 0,
        max_length: int = 0,
    ) -> None:
        self.case_sensitive = case_sensitive
        self.regex = regex
        self.min_length = min_length
        self.max_length = max_length

        # Ключевые слова сравниваем в едином регистре, чтобы не звать
        # .lower() на каждое слово для каждого сообщения.
        self._keywords = tuple(self._normalize(k) for k in keywords)
        self._exclude = tuple(self._normalize(k) for k in exclude_keywords)

        # Оригинальные написания нужны только для текста причины в логе.
        self._keywords_display = tuple(keywords)
        self._exclude_display = tuple(exclude_keywords)

    # -- публичный API ------------------------------------------------- #

    @classmethod
    def from_config(cls, config: Config) -> "MessageFilter":
        return cls(
            keywords=config.keywords,
            exclude_keywords=config.exclude_keywords,
            regex=config.regex,
            case_sensitive=config.case_sensitive,
            min_length=config.min_length,
            max_length=config.max_length,
        )

    @property
    def has_allow_rules(self) -> bool:
        return bool(self._keywords) or self.regex is not None

    def describe(self) -> str:
        """Однострочное описание правил — пишется в лог при старте."""
        parts = []
        parts.append(f"keywords={list(self._keywords_display) or 'any'}")
        if self._exclude_display:
            parts.append(f"exclude={list(self._exclude_display)}")
        if self.regex is not None:
            parts.append(f"regex={self.regex.pattern!r}")
        if self.min_length:
            parts.append(f"min_length={self.min_length}")
        if self.max_length:
            parts.append(f"max_length={self.max_length}")
        parts.append(f"case_sensitive={self.case_sensitive}")
        return ", ".join(parts)

    def check(self, text: Optional[str]) -> FilterResult:
        """Главный метод: решает, пропускать ли сообщение."""
        text = text or ""

        excluded = self._check_exclude(text)
        if excluded is not None:
            return excluded

        length_problem = self._check_length(text)
        if length_problem is not None:
            return length_problem

        if not self.has_allow_rules:
            return FilterResult(True, "no allow rules configured: pass-through")

        hit = self._first_keyword_hit(text)
        if hit is not None:
            return FilterResult(True, f'keyword: "{hit}"')

        if self.regex is not None and self.regex.search(text):
            return FilterResult(True, f"regex: {self.regex.pattern!r}")

        return FilterResult(False, "no matching keywords")

    # -- правила -------------------------------------------------------- #

    def _check_exclude(self, text: str) -> Optional[FilterResult]:
        if not self._exclude:
            return None
        haystack = self._normalize(text)
        for needle, display in zip(self._exclude, self._exclude_display):
            if needle in haystack:
                return FilterResult(False, f'excluded keyword: "{display}"')
        return None

    def _check_length(self, text: str) -> Optional[FilterResult]:
        length = len(text)
        if self.min_length and length < self.min_length:
            return FilterResult(False, f"too short: {length} < {self.min_length}")
        if self.max_length and length > self.max_length:
            return FilterResult(False, f"too long: {length} > {self.max_length}")
        return None

    def _first_keyword_hit(self, text: str) -> Optional[str]:
        if not self._keywords:
            return None
        haystack = self._normalize(text)
        for needle, display in zip(self._keywords, self._keywords_display):
            if needle in haystack:
                return display
        return None

    # -- утилиты -------------------------------------------------------- #

    def _normalize(self, value: str) -> str:
        return value if self.case_sensitive else value.lower()
