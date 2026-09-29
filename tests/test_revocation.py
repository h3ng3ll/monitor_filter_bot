"""Тесты управления доступом: одобрение, отклонение и принудительный отзыв.

Главное здесь — что отзыв закрывает ОБА канала сразу и без перезапуска:
и рассылку, и команды.

Запуск:  .venv/bin/python -m pytest -q
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bot_commands import Access, CommandContext, CommandListener  # noqa: E402
from app.bot_handlers import BotHandlers  # noqa: E402
from app.filter import MessageFilter  # noqa: E402
from app.main import Application  # noqa: E402
from app.models import ProcessedMessageStore  # noqa: E402
from app.users import STATUS_APPROVED, STATUS_DENIED, STATUS_PENDING, UserStore  # noqa: E402

OWNER = 1


def _run(coro):
    return asyncio.run(coro)


class FakeSender:
    def __init__(self):
        self.delivered = []
        self.edited = []
        self.toasts = []

    async def deliver(self, chat_id, html_text, **kwargs):
        self.delivered.append((str(chat_id), html_text))

    async def send_message(self, html_text, *, chat_id=None, **kwargs):
        self.delivered.append((str(chat_id), html_text))
        return True

    async def edit_message_text(self, chat_id, message_id, html_text, **kwargs):
        self.edited.append((str(chat_id), message_id, html_text))
        return True

    async def answer_callback_query(self, callback_id, text=None):
        self.toasts.append(text)

    def texts_to(self, chat_id):
        return [text for target, text in self.delivered if target == str(chat_id)]


def _app(tmp_path):
    app = Application.__new__(Application)
    app.config = SimpleNamespace(
        target_chat_id=OWNER,
        legacy_delivery=False,
        multi_user=True,
        log_message_text=False,
        startup_probe=False,
        startup_probe_limit=50,
    )
    app.filter = MessageFilter(keywords=["Науменко"])
    app.store = ProcessedMessageStore(tmp_path / "test.db")
    app.users = UserStore(tmp_path / "test.db")
    app.sender = FakeSender()
    app.source_label = "@src"
    app.owner_id = OWNER
    app._user_filters = {}
    _run(app.store.connect())
    _run(app.users.connect())
    _run(app.users.ensure_admin(OWNER, chat_id=OWNER, label="owner"))
    app.handlers = BotHandlers(app)
    return app


def _ctx(user_id, *, args="", is_admin=False, chat_id=None, callback_id=None,
         message_id=None):
    return CommandContext(
        user_id=user_id,
        chat_id=chat_id if chat_id is not None else user_id,
        username=f"u{user_id}",
        full_name=f"U{user_id}",
        args=args,
        is_admin=is_admin,
        callback_id=callback_id,
        message_id=message_id,
    )


def _admin(args="", **kwargs):
    return _ctx(OWNER, args=args, is_admin=True, **kwargs)


def _join(app, user_id, keywords=("Науменко",)):
    """Проводит человека весь путь: заявка -> одобрение -> свои правила."""
    _run(app.handlers.cmd_start(_ctx(user_id)))
    _run(app.handlers.cmd_approve(_admin(str(user_id))))
    for word in keywords:
        _run(app.handlers.cmd_add(_ctx(user_id, args=word)))


# --------------------------------------------------------------------------- #
# Заявка и одобрение
# --------------------------------------------------------------------------- #

def test_start_creates_request_and_notifies_admin(tmp_path):
    app = _app(tmp_path)
    reply = _run(app.handlers.cmd_start(_ctx(7)))

    assert "Заявка отправлена" in reply
    assert _run(app.users.get(7)).status == STATUS_PENDING
    # Карточка ушла владельцу сессии.
    card = app.sender.texts_to(OWNER)
    assert any("ЗАПРОС ДОСТУПА" in text for text in card)
    assert any("id=" not in text and "<b>ID:</b> 7" in text for text in card)


def test_repeated_start_does_not_spam_admin(tmp_path):
    app = _app(tmp_path)
    _run(app.handlers.cmd_start(_ctx(7)))
    before = len(app.sender.delivered)
    reply = _run(app.handlers.cmd_start(_ctx(7)))

    assert "Заявка уже отправлена" in reply
    assert len(app.sender.delivered) == before


def test_approve_button_updates_card_and_notifies_user(tmp_path):
    app = _app(tmp_path)
    _run(app.handlers.cmd_start(_ctx(7)))
    _run(app.handlers.cb_approve(
        _admin("7", chat_id=OWNER, callback_id="cb1", message_id=55)
    ))

    assert _run(app.users.get(7)).status == STATUS_APPROVED
    # Кнопки с карточки убраны — дважды не нажать.
    assert app.sender.edited and app.sender.edited[0][1] == 55
    assert "Одобрен" in app.sender.edited[0][2]
    assert app.sender.toasts == ["Одобрено"]
    assert any("Доступ открыт" in text for text in app.sender.texts_to(7))


# --------------------------------------------------------------------------- #
# Принудительный отзыв
# --------------------------------------------------------------------------- #

def _recipient_ids(app):
    return [r.user.user_id for r in _run(app.users.active_recipients())]


def test_revoke_stops_delivery_immediately(tmp_path):
    app = _app(tmp_path)
    _join(app, 7)
    assert 7 in _recipient_ids(app)

    _run(app.handlers.cmd_revoke(_admin("7")))

    # Ни перезапуска, ни ручного сброса кеша — получателя больше нет.
    # Владелец (он же админ) в списке остаётся.
    assert 7 not in _recipient_ids(app)
    assert _run(app.users.get(7)).status == STATUS_DENIED


def test_revoke_drops_the_compiled_filter_cache(tmp_path):
    app = _app(tmp_path)
    _join(app, 7)
    # Прогреваем кеш так же, как это делает рассылка.
    recipient = next(
        r for r in _run(app.users.active_recipients()) if r.user.user_id == 7
    )
    app.filter_for(recipient)
    assert 7 in app._user_filters

    _run(app.handlers.cmd_revoke(_admin("7")))
    assert 7 not in app._user_filters


def test_revoked_user_commands_are_refused(tmp_path):
    app = _app(tmp_path)
    _join(app, 7)
    assert (_run(app._authorize(7))).allowed is True

    _run(app.handlers.cmd_revoke(_admin("7")))

    access = _run(app._authorize(7))
    assert access.allowed is False
    assert access.is_admin is False


def test_revoke_notifies_the_user(tmp_path):
    app = _app(tmp_path)
    _join(app, 7)
    _run(app.handlers.cmd_revoke(_admin("7")))
    assert any("отозван" in text for text in app.sender.texts_to(7))


def test_admin_cannot_revoke_themselves(tmp_path):
    app = _app(tmp_path)
    reply = _run(app.handlers.cmd_revoke(_admin(str(OWNER))))
    assert "самого себя" in reply
    assert _run(app.users.get(OWNER)).status == STATUS_APPROVED


def test_revoking_someone_without_access_says_so(tmp_path):
    app = _app(tmp_path)
    _run(app.handlers.cmd_start(_ctx(7)))  # только заявка, доступа нет
    reply = _run(app.handlers.cmd_revoke(_admin("7")))
    assert "доступа и так нет" in reply


def test_revoking_unknown_id_reports_plainly(tmp_path):
    app = _app(tmp_path)
    assert "не найден" in _run(app.handlers.cmd_revoke(_admin("4242")))


def test_revoke_requires_numeric_id(tmp_path):
    app = _app(tmp_path)
    assert "числовой id" in _run(app.handlers.cmd_revoke(_admin("ivan")))


# --------------------------------------------------------------------------- #
# Полное удаление
# --------------------------------------------------------------------------- #

def test_forget_removes_user_and_their_rules(tmp_path):
    app = _app(tmp_path)
    _join(app, 7)
    reply = _run(app.handlers.cmd_forget(_admin("7")))

    assert "Удалён" in reply
    assert _run(app.users.get(7)) is None
    assert 7 not in app._user_filters

    # Снова незнакомец: /start заводит новую заявку, старые правила не всплыли.
    _run(app.handlers.cmd_start(_ctx(7)))
    assert _run(app.users.get(7)).status == STATUS_PENDING
    _run(app.handlers.cmd_approve(_admin("7")))
    assert _run(app.users.settings(7)).keywords == ()


def test_admin_cannot_forget_themselves(tmp_path):
    app = _app(tmp_path)
    reply = _run(app.handlers.cmd_forget(_admin(str(OWNER))))
    assert "самого себя" in reply
    assert _run(app.users.get(OWNER)) is not None


# --------------------------------------------------------------------------- #
# Массовое отклонение
# --------------------------------------------------------------------------- #

def test_deny_all_clears_only_pending(tmp_path):
    app = _app(tmp_path)
    _join(app, 7)                                  # одобрен
    _run(app.handlers.cmd_start(_ctx(8)))          # заявка
    _run(app.handlers.cmd_start(_ctx(9)))          # заявка

    reply = _run(app.handlers.cmd_deny_all(_admin()))

    assert "2" in reply
    assert _run(app.users.get(7)).status == STATUS_APPROVED
    assert _run(app.users.get(8)).status == STATUS_DENIED
    assert _run(app.users.get(9)).status == STATUS_DENIED


# --------------------------------------------------------------------------- #
# Кнопки доступны только админам
# --------------------------------------------------------------------------- #

def test_button_press_from_non_admin_is_rejected(tmp_path):
    """Нажатие проверяется по нажавшему, а не по callback_data.

    Карточку могли переслать, поэтому сами данные кнопки ничего не доказывают.
    """
    app = _app(tmp_path)
    _run(app.handlers.cmd_start(_ctx(7)))

    sender = FakeSender()
    listener = CommandListener(sender, authorize=app._authorize, owner_chat_id=OWNER)
    app.handlers.register_all(listener)

    _run(listener._dispatch({
        "update_id": 1,
        "callback_query": {
            "id": "cb1",
            "from": {"id": 7, "first_name": "U7"},
            "message": {"message_id": 55, "chat": {"id": 7}},
            "data": "approve:7",
        },
    }))

    assert _run(app.users.get(7)).status == STATUS_PENDING
    assert sender.toasts == ["Недостаточно прав"]
    assert sender.edited == []


def test_owner_button_press_is_accepted(tmp_path):
    app = _app(tmp_path)
    _run(app.handlers.cmd_start(_ctx(7)))

    sender = FakeSender()
    app.sender = sender
    app.handlers = BotHandlers(app)
    listener = CommandListener(sender, authorize=app._authorize, owner_chat_id=OWNER)
    app.handlers.register_all(listener)

    _run(listener._dispatch({
        "update_id": 1,
        "callback_query": {
            "id": "cb1",
            "from": {"id": OWNER, "first_name": "Owner"},
            "message": {"message_id": 55, "chat": {"id": OWNER}},
            "data": "approve:7",
        },
    }))

    assert _run(app.users.get(7)).status == STATUS_APPROVED


# --------------------------------------------------------------------------- #
# Возврат доступа — обратное к отзыву
# --------------------------------------------------------------------------- #

def test_revoke_then_unblock_returns_user_to_mailing(tmp_path):
    app = _app(tmp_path)
    _join(app, 7)
    _run(app.handlers.cmd_revoke(_admin("7")))
    assert 7 not in _recipient_ids(app)

    reply = _run(app.handlers.cmd_unblock(_admin("7")))

    assert "возвращён" in reply.lower()
    assert _run(app.users.get(7)).status == STATUS_APPROVED
    assert 7 in _recipient_ids(app)
    assert any("восстановлен" in t for t in app.sender.texts_to(7))


def test_unblock_clears_the_blocked_flag(tmp_path):
    app = _app(tmp_path)
    _join(app, 7)
    # Так сервис помечает того, кто заблокировал бота.
    _run(app.users.set_blocked(7, True))
    assert 7 not in _recipient_ids(app)

    reply = _run(app.handlers.cmd_unblock(_admin("7")))

    assert "блокировк" in reply.lower()
    assert _run(app.users.get(7)).blocked is False
    assert 7 in _recipient_ids(app)


def test_unblock_drops_stale_filter_cache(tmp_path):
    app = _app(tmp_path)
    _join(app, 7)
    _run(app.handlers.cmd_revoke(_admin("7")))
    app._user_filters[7] = (999, None)   # подложный протухший кеш

    _run(app.handlers.cmd_unblock(_admin("7")))
    assert 7 not in app._user_filters


def test_unblock_is_a_noop_when_access_is_already_open(tmp_path):
    app = _app(tmp_path)
    _join(app, 7)
    reply = _run(app.handlers.cmd_unblock(_admin("7")))
    assert "и так открыт" in reply


def test_unblock_warns_when_user_muted_themselves(tmp_path):
    app = _app(tmp_path)
    _join(app, 7)
    _run(app.handlers.cmd_mute(_ctx(7)))
    _run(app.handlers.cmd_revoke(_admin("7")))

    reply = _run(app.handlers.cmd_unblock(_admin("7")))
    # Доступ вернули, но человек всё равно ничего не получит — говорим об этом.
    assert "паузу" in reply


def test_unblock_warns_about_empty_rules(tmp_path):
    app = _app(tmp_path)
    _run(app.handlers.cmd_start(_ctx(7)))
    _run(app.handlers.cmd_approve(_admin("7")))   # одобрен, но слов не добавлял
    _run(app.handlers.cmd_revoke(_admin("7")))

    reply = _run(app.handlers.cmd_unblock(_admin("7")))
    assert "пустые правила" in reply


def test_unblock_reports_unknown_id(tmp_path):
    app = _app(tmp_path)
    assert "не найден" in _run(app.handlers.cmd_unblock(_admin("4242")))


# --------------------------------------------------------------------------- #
# Само-восстановление: человек разблокировал бота
# --------------------------------------------------------------------------- #

def test_blocked_user_is_restored_when_they_write_again(tmp_path):
    """Апдейт от человека доказывает, что бот до него достучится.

    Без этого он навсегда остался бы вне рассылки: на /start получал бы
    приветствие, а сообщений не видел.
    """
    app = _app(tmp_path)
    _join(app, 7)
    _run(app.users.set_blocked(7, True))
    assert 7 not in _recipient_ids(app)

    access = _run(app._authorize(7))

    assert access.allowed is True
    assert _run(app.users.get(7)).blocked is False
    assert 7 in _recipient_ids(app)


def test_self_healing_does_not_resurrect_revoked_access(tmp_path):
    # Снятие blocked не должно возвращать доступ тому, кого отозвал админ.
    app = _app(tmp_path)
    _join(app, 7)
    _run(app.users.set_blocked(7, True))
    _run(app.handlers.cmd_revoke(_admin("7")))

    access = _run(app._authorize(7))

    assert access.allowed is False
    assert 7 not in _recipient_ids(app)


def test_self_healing_drops_filter_cache(tmp_path):
    app = _app(tmp_path)
    _join(app, 7)
    _run(app.users.set_blocked(7, True))
    app._user_filters[7] = (999, None)

    _run(app._authorize(7))
    assert 7 not in app._user_filters


# --------------------------------------------------------------------------- #
# Кнопки в /users симметричны состоянию
# --------------------------------------------------------------------------- #

def _actions_for(app, user_id):
    from app.bot_handlers import _card_actions
    caption, buttons = _card_actions(_run(app.users.get(user_id)))
    return caption, [data for _, data in buttons]


def test_user_card_offers_the_opposite_of_current_state(tmp_path):
    app = _app(tmp_path)

    _run(app.handlers.cmd_start(_ctx(7)))
    assert _actions_for(app, 7) == (
        "Заявка на доступ", ["approve:7", "deny:7"],
    )

    _run(app.handlers.cmd_approve(_admin("7")))
    assert _actions_for(app, 7) == ("Доступ открыт", ["revoke:7"])

    _run(app.handlers.cmd_revoke(_admin("7")))
    assert _actions_for(app, 7) == ("Доступ закрыт", ["unblock:7"])

    _run(app.handlers.cmd_unblock(_admin("7")))
    _run(app.users.set_blocked(7, True))
    assert _actions_for(app, 7) == ("Заблокировал бота", ["unblock:7"])


def test_unblock_button_requires_admin(tmp_path):
    app = _app(tmp_path)
    _join(app, 7)
    _run(app.handlers.cmd_revoke(_admin("7")))

    sender = FakeSender()
    listener = CommandListener(sender, authorize=app._authorize, owner_chat_id=OWNER)
    app.handlers.register_all(listener)

    _run(listener._dispatch({
        "update_id": 1,
        "callback_query": {
            "id": "cb1",
            "from": {"id": 7, "first_name": "U7"},
            "message": {"message_id": 55, "chat": {"id": 7}},
            "data": "unblock:7",
        },
    }))

    # Сам себя вернуть нельзя.
    assert _run(app.users.get(7)).status == STATUS_DENIED
    assert sender.toasts == ["Недостаточно прав"]


# --------------------------------------------------------------------------- #
# Подсказки про конфликт с правилами из .env
# --------------------------------------------------------------------------- #

def _owner_app_with_legacy(tmp_path):
    app = _app(tmp_path)
    app.config.legacy_delivery = True     # целевой чат из .env = личка владельца
    return app


def test_keywords_explains_that_env_rules_are_in_charge(tmp_path):
    app = _owner_app_with_legacy(tmp_path)
    reply = _run(app.handlers.cmd_keywords(_admin(chat_id=OWNER)))
    # Правил нет — значит работают .env, и это должно быть сказано вслух.
    assert "правила оттуда" in reply


def test_keywords_explains_that_personal_rules_win(tmp_path):
    app = _owner_app_with_legacy(tmp_path)
    _run(app.handlers.cmd_add(_admin("Науменко", chat_id=OWNER)))
    reply = _run(app.handlers.cmd_keywords(_admin(chat_id=OWNER)))
    assert "заменяют" in reply


def test_mode_warns_when_there_are_no_rules_to_apply_it_to(tmp_path):
    app = _app(tmp_path)
    _run(app.handlers.cmd_start(_ctx(7)))
    _run(app.handlers.cmd_approve(_admin("7")))   # одобрен, но слов не добавлял
    reply = _run(app.handlers.cmd_mode(_ctx(7, args="all")))
    assert "Режим" in reply and "ни на что не влияет" in reply


def test_personal_commands_need_an_account(tmp_path):
    """TARGET_CHAT_ID может быть группой: право команды даёт чат, а записи нет.

    Без этой проверки запись личных правил падала бы на FOREIGN KEY, и человек
    не получил бы вообще никакого ответа.
    """
    app = _app(tmp_path)
    listener = CommandListener(
        app.sender, authorize=app._authorize, owner_chat_id=OWNER
    )
    app.handlers.register_all(listener)
    handler = listener._handlers["mode"].handler

    reply = _run(handler(_ctx(4242, args="all")))
    assert "Отправьте /start" in reply


def test_mode_is_stored_and_reported(tmp_path):
    app = _app(tmp_path)
    _join(app, 7)
    reply = _run(app.handlers.cmd_mode(_ctx(7, args="all")))

    assert "all" in reply
    assert _run(app.users.settings(7)).mode == "all"
    assert "ни на что не влияет" not in reply


def test_mode_rejects_garbage_without_changing_anything(tmp_path):
    app = _app(tmp_path)
    _join(app, 7)
    _run(app.handlers.cmd_mode(_ctx(7, args="all")))
    reply = _run(app.handlers.cmd_mode(_ctx(7, args="нечто")))

    assert "⚠️" in reply
    assert _run(app.users.settings(7)).mode == "all"


def test_mode_change_is_visible_to_the_fanout_immediately(tmp_path):
    app = _app(tmp_path)
    _join(app, 7, keywords=("Науменко", "отсутствует"))
    recipient = next(
        r for r in _run(app.users.active_recipients()) if r.user.user_id == 7
    )
    assert app.filter_for(recipient).check("White: Науменко").matched is True

    _run(app.handlers.cmd_mode(_ctx(7, args="all")))

    recipient = next(
        r for r in _run(app.users.active_recipients()) if r.user.user_id == 7
    )
    # Кеш фильтров ключуется по rev, поэтому пересборка происходит сама.
    assert app.filter_for(recipient).check("White: Науменко").matched is False


# --------------------------------------------------------------------------- #
# Сбой команды должен быть виден пользователю
# --------------------------------------------------------------------------- #

def test_info_renders_without_exploding(tmp_path):
    """Гоняем /info целиком.

    Раньше describe_service падал на NameError, но ни один тест его не вызывал,
    поэтому поломка жила только в journalctl, а пользователь видел молчание.
    """
    app = _app(tmp_path)
    app.account = "Oleksandr (id=1)"
    app.started_at = datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc)
    app.config.target_chat_id = OWNER

    reply = _run(app.handlers.cmd_info(_admin()))

    assert "СОСТОЯНИЕ СЕРВИСА" in reply
    assert "Работает" in reply
    assert "Пользователей" in reply


def test_uptime_is_human_readable():
    from app.main import _format_uptime
    assert _format_uptime(timedelta(seconds=30)) == "меньше минуты"
    assert _format_uptime(timedelta(days=2, hours=3, minutes=4)) == "2 д 3 ч 4 мин"
    assert _format_uptime(timedelta(hours=5)) == "5 ч"


def test_crashing_handler_answers_instead_of_going_silent(tmp_path):
    app = _app(tmp_path)
    sender = FakeSender()
    listener = CommandListener(sender, authorize=app._authorize, owner_chat_id=OWNER)

    async def boom(ctx):
        raise RuntimeError("что-то отвалилось")

    listener.register("boom", boom)
    _run(listener._dispatch({
        "update_id": 1,
        "message": {"chat": {"id": OWNER},
                    "from": {"id": OWNER, "first_name": "Owner"},
                    "text": "/boom"},
    }))

    assert len(sender.delivered) == 1
    assert "внутренняя ошибка" in sender.delivered[0][1]


def test_handler_returning_none_stays_silent(tmp_path):
    # Отличаем «нечего отвечать» от «упало»: /users сам шлёт свои карточки.
    app = _app(tmp_path)
    sender = FakeSender()
    listener = CommandListener(sender, authorize=app._authorize, owner_chat_id=OWNER)

    async def quiet(ctx):
        return None

    listener.register("quiet", quiet)
    _run(listener._dispatch({
        "update_id": 1,
        "message": {"chat": {"id": OWNER},
                    "from": {"id": OWNER, "first_name": "Owner"},
                    "text": "/quiet"},
    }))

    assert sender.delivered == []


def test_bare_mode_offers_buttons_instead_of_silence(tmp_path):
    """Из меню бота команда приходит без аргумента.

    Набирать значение отдельным сообщением бесполезно — обычный текст
    игнорируется, поэтому показываем кнопки.
    """
    app = _app(tmp_path)
    _join(app, 7)
    reply = _run(app.handlers.cmd_mode(_ctx(7)))

    assert reply is None                       # ответ ушёл отдельным сообщением
    chat, text = app.sender.delivered[-1]
    assert chat == "7"
    assert "Сейчас режим" in text
    assert "/mode all" in text


def test_mode_button_sets_the_pressers_own_mode(tmp_path):
    app = _app(tmp_path)
    _join(app, 7)

    _run(app.handlers.cb_mode(
        _ctx(7, args="all", callback_id="cb1", message_id=42)
    ))

    assert _run(app.users.settings(7)).mode == "all"
    assert app.sender.toasts == ["Режим: all"]
    # Сообщение с кнопками переписано результатом.
    assert app.sender.edited and "all" in app.sender.edited[-1][2]


def test_mode_button_does_not_require_admin(tmp_path):
    """Кнопка режима — настройка самого нажавшего, а не решение о доступе."""
    app = _app(tmp_path)
    _join(app, 7)

    sender = FakeSender()
    app.sender = sender
    app.handlers = BotHandlers(app)
    listener = CommandListener(sender, authorize=app._authorize, owner_chat_id=OWNER)
    app.handlers.register_all(listener)

    _run(listener._dispatch({
        "update_id": 1,
        "callback_query": {
            "id": "cb1",
            "from": {"id": 7, "first_name": "U7"},
            "message": {"message_id": 42, "chat": {"id": 7}},
            "data": "mode:all",
        },
    }))

    assert _run(app.users.settings(7)).mode == "all"
    assert sender.toasts == ["Режим: all"]


def test_access_buttons_still_require_admin(tmp_path):
    # Послабление для кнопки режима не должно ослабить кнопки доступа.
    app = _app(tmp_path)
    _run(app.handlers.cmd_start(_ctx(7)))

    sender = FakeSender()
    listener = CommandListener(sender, authorize=app._authorize, owner_chat_id=OWNER)
    app.handlers.register_all(listener)

    _run(listener._dispatch({
        "update_id": 1,
        "callback_query": {
            "id": "cb1",
            "from": {"id": 7, "first_name": "U7"},
            "message": {"message_id": 42, "chat": {"id": 7}},
            "data": "approve:7",
        },
    }))

    assert _run(app.users.get(7)).status == STATUS_PENDING
    assert sender.toasts == ["Недостаточно прав"]


def test_every_registered_command_answers_something(tmp_path):
    """Страховка от повторения истории с /info.

    Каждая команда должна что-то ответить или осознанно промолчать, но не
    падать — иначе поломка снова окажется видна только в журнале.
    """
    app = _app(tmp_path)
    app.account = "owner"
    app.started_at = datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc)
    _join(app, 7)

    sender = FakeSender()
    app.sender = sender
    app.handlers = BotHandlers(app)
    listener = CommandListener(sender, authorize=app._authorize, owner_chat_id=OWNER)
    app.handlers.register_all(listener)

    args = {"add": "слово", "remove": "слово", "exclude": "стоп",
            "unexclude": "стоп", "regex": "off", "mode": "any",
            "test": "текст", "approve": "7", "deny": "7", "revoke": "7",
            "unblock": "7", "forget": "7"}

    for name in listener.commands:
        reply = _run(listener._handlers[name].handler(
            _admin(args.get(name, ""), chat_id=OWNER)
        ))
        assert reply is None or "внутренняя ошибка" not in reply, name
