"""Тесты управления доступом: одобрение, отклонение и принудительный отзыв.

Главное здесь — что отзыв закрывает ОБА канала сразу и без перезапуска:
и рассылку, и команды.

Запуск:  .venv/bin/python -m pytest -q
"""

import asyncio
import sys
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
