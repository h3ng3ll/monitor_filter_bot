"""Тесты хранилища пользователей и персональных правил.

Запуск:  .venv/bin/python -m pytest -q
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.users import (  # noqa: E402
    STATUS_APPROVED,
    STATUS_DENIED,
    STATUS_PENDING,
    UserStore,
)


def _store(tmp_path, **kwargs):
    store = UserStore(tmp_path / "test.db", **kwargs)
    asyncio.run(store.connect())
    return store


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Заявки и решения
# --------------------------------------------------------------------------- #

def test_first_start_creates_pending_request(tmp_path):
    store = _store(tmp_path)
    user, created = _run(store.request_access(7, 7, "ivan", "Ivan Petrov"))
    assert created is True
    assert user.status == STATUS_PENDING
    assert user.label == "Ivan Petrov (@ivan, id=7)"


def test_second_start_does_not_duplicate_request(tmp_path):
    store = _store(tmp_path)
    _run(store.request_access(7, 7, "ivan", "Ivan"))
    user, created = _run(store.request_access(7, 7, "ivan_new", "Ivan"))
    assert created is False
    # Контакты обновились, статус не сбросился.
    assert user.username == "ivan_new"
    assert user.status == STATUS_PENDING


def test_approve_creates_filter_row_and_recipient(tmp_path):
    store = _store(tmp_path)
    _run(store.request_access(7, 7, "ivan", "Ivan"))
    _run(store.approve(7, by=1))
    _run(store.add_keyword(7, "Науменко"))

    recipients = _run(store.active_recipients())
    assert [r.user.user_id for r in recipients] == [7]
    assert recipients[0].settings.keywords == ("Науменко",)


def test_deny_all_pending_leaves_approved_alone(tmp_path):
    store = _store(tmp_path)
    _run(store.request_access(7, 7, "a", "A"))
    _run(store.request_access(8, 8, "b", "B"))
    _run(store.approve(7, by=1))

    denied = _run(store.deny_all_pending(by=1))
    assert [u.user_id for u in denied] == [8]
    assert _run(store.get(7)).status == STATUS_APPROVED
    assert _run(store.get(8)).status == STATUS_DENIED


def test_ensure_admin_is_idempotent(tmp_path):
    store = _store(tmp_path)
    _run(store.ensure_admin(1, chat_id=1, label="owner"))
    _run(store.ensure_admin(1, chat_id=1, label="owner"))
    admins = _run(store.list_admins())
    assert [a.user_id for a in admins] == [1]
    assert admins[0].status == STATUS_APPROVED


def test_ensure_admin_promotes_existing_user(tmp_path):
    store = _store(tmp_path)
    _run(store.request_access(7, 7, "ivan", "Ivan"))
    _run(store.ensure_admin(7))
    user = _run(store.get(7))
    assert user.is_admin is True
    assert user.status == STATUS_APPROVED


# --------------------------------------------------------------------------- #
# Правила
# --------------------------------------------------------------------------- #

def _approved(tmp_path, **kwargs):
    store = _store(tmp_path, **kwargs)
    _run(store.request_access(7, 7, "ivan", "Ivan"))
    _run(store.approve(7, by=1))
    return store


def test_add_keyword_bumps_rev(tmp_path):
    store = _approved(tmp_path)
    before = _run(store.settings(7)).rev
    settings, added = _run(store.add_keyword(7, "April"))
    assert added is True
    assert settings.rev > before


def test_add_keyword_is_idempotent(tmp_path):
    store = _approved(tmp_path)
    _run(store.add_keyword(7, "April"))
    settings = _run(store.settings(7))
    again, added = _run(store.add_keyword(7, "april"))
    assert added is False
    # Повтор ничего не меняет, значит и кеш фильтров сбрасывать незачем.
    assert again.rev == settings.rev
    assert again.keywords == ("April",)


def test_add_keyword_rejects_comma_and_empty(tmp_path):
    store = _approved(tmp_path)
    for bad in ("a,b", "", "   "):
        try:
            _run(store.add_keyword(7, bad))
        except ValueError:
            continue
        raise AssertionError(f"ожидался ValueError для {bad!r}")


def test_add_keyword_respects_limit(tmp_path):
    store = _approved(tmp_path, max_keywords=2)
    _run(store.add_keyword(7, "a"))
    _run(store.add_keyword(7, "b"))
    try:
        _run(store.add_keyword(7, "c"))
    except ValueError as exc:
        assert "предел" in str(exc)
    else:
        raise AssertionError("ожидался ValueError при переполнении списка")


def test_remove_keyword_reports_missing(tmp_path):
    store = _approved(tmp_path)
    _run(store.add_keyword(7, "April"))
    _, removed = _run(store.remove_keyword(7, "нет такого"))
    assert removed is False
    settings, removed = _run(store.remove_keyword(7, "april"))
    assert removed is True
    assert settings.keywords == ()


def test_set_regex_rejects_broken_pattern(tmp_path):
    store = _approved(tmp_path)
    try:
        _run(store.set_regex(7, "[unclosed"))
    except ValueError as exc:
        assert "регулярное" in str(exc)
    else:
        raise AssertionError("битый паттерн не должен сохраняться")
    assert _run(store.settings(7)).regex == ""


def test_set_regex_off_clears_it(tmp_path):
    store = _approved(tmp_path)
    _run(store.set_regex(7, "Науменк"))
    assert _run(store.settings(7)).regex == "Науменк"
    assert _run(store.set_regex(7, "off")).regex == ""


def test_set_mode_validates(tmp_path):
    store = _approved(tmp_path)
    assert _run(store.set_mode(7, "all")).mode == "all"
    try:
        _run(store.set_mode(7, "нечто"))
    except ValueError:
        pass
    else:
        raise AssertionError("ожидался ValueError для неизвестного режима")


# --------------------------------------------------------------------------- #
# Кто попадает в рассылку
# --------------------------------------------------------------------------- #

def test_recipients_exclude_pending_denied_muted_and_blocked(tmp_path):
    store = _store(tmp_path)
    for user_id in (1, 2, 3, 4, 5):
        _run(store.request_access(user_id, user_id, f"u{user_id}", f"U{user_id}"))
        _run(store.approve(user_id, by=99))
        _run(store.add_keyword(user_id, "April"))

    _run(store.deny(2, by=99))
    _run(store.set_muted(3, True))
    _run(store.set_blocked(4, True))
    _run(store.request_access(6, 6, "u6", "U6"))  # так и остался pending

    assert [r.user.user_id for r in _run(store.active_recipients())] == [1, 5]


def test_recipient_without_rules_still_listed_but_flagged(tmp_path):
    # Фильтрацию по has_rules делает приложение — хранилище отдаёт как есть.
    store = _approved(tmp_path)
    recipients = _run(store.active_recipients())
    assert len(recipients) == 1
    assert recipients[0].settings.has_rules is False


def test_regex_mode_without_pattern_has_no_rules(tmp_path):
    store = _approved(tmp_path)
    _run(store.add_keyword(7, "April"))
    _run(store.set_mode(7, "regex"))
    settings = _run(store.settings(7))
    # В режиме regex ключевые слова не участвуют — правил фактически нет.
    assert settings.has_rules is False


# --------------------------------------------------------------------------- #
# Отзыв и удаление
# --------------------------------------------------------------------------- #

def test_revoke_removes_from_recipients_immediately(tmp_path):
    store = _approved(tmp_path)
    _run(store.add_keyword(7, "April"))
    assert len(_run(store.active_recipients())) == 1

    _run(store.revoke(7, by=1))
    assert _run(store.active_recipients()) == ()
    assert _run(store.get(7)).status == STATUS_DENIED


def test_forget_deletes_user_and_cascades_filters(tmp_path):
    store = _approved(tmp_path)
    _run(store.add_keyword(7, "April"))

    removed = _run(store.forget(7))
    assert removed is not None
    assert _run(store.get(7)) is None
    assert _run(store.active_recipients()) == ()

    # Человек снова становится незнакомцем: заявка заводится с нуля.
    user, created = _run(store.request_access(7, 7, "ivan", "Ivan"))
    assert created is True
    assert user.status == STATUS_PENDING
    # Старые правила не воскресли.
    _run(store.approve(7, by=1))
    assert _run(store.settings(7)).keywords == ()


def test_reapproving_clears_blocked_flag(tmp_path):
    store = _approved(tmp_path)
    _run(store.set_blocked(7, True))
    _run(store.approve(7, by=1))
    assert _run(store.get(7)).blocked is False
