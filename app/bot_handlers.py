"""Команды бота: доступ, персональные правила, администрирование.

Транспорт (getUpdates, разбор апдейта, проверка прав) живёт в bot_commands.py,
здесь — только смысл команд.

Важно про экранирование: ответы уходят с parse_mode=HTML, поэтому ВСЁ, что
пришло от пользователя — имя, username, ключевое слово, regex — обязано
пройти через escape_html. Иначе /add <b> сломает ответ или подмешает разметку.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

from telethon import errors as tg_errors

from .bot_commands import CommandContext, CommandListener, Handler
from .bot_sender import escape_html, format_status
from .users import (
    STATUS_APPROVED,
    STATUS_DENIED,
    STATUS_PENDING,
    FilterSettings,
    User,
)

if TYPE_CHECKING:  # только для аннотаций — цикла импорта нет
    from .main import Application

log = logging.getLogger(__name__)

# Команды, которые публикуются в меню бота (видны всем).
PUBLIC_MENU = [
    ("start", "Запросить доступ / приветствие"),
    ("help", "Список команд"),
    ("keywords", "Мои правила фильтра"),
    ("add", "Добавить ключевое слово"),
    ("remove", "Убрать ключевое слово"),
    ("mode", "Режим совпадения: any | all | regex"),
    ("test", "Проверить текст на своих правилах"),
    ("mute", "Приостановить уведомления"),
    ("unmute", "Возобновить уведомления"),
    ("status", "Моё состояние"),
]

# Сколько карточек с кнопками отправляем за один /users.
MAX_CARDS = 15

# /last: сколько совпадений можно запросить за раз и сколько истории смотреть.
MAX_LAST = 10
LAST_SCAN_LIMIT = 200

_MODE_EXPLANATION = {
    "any": "подходит любое из ключевых слов (или regex)",
    "all": "должны встретиться все ключевые слова",
    "regex": "решает только регулярное выражение",
}

USER_HELP = (
    "<b>Команды</b>\n\n"
    "/keywords — мои правила\n"
    "/add &lt;слово&gt; — добавить ключевое слово\n"
    "/remove &lt;слово&gt; — убрать ключевое слово\n"
    "/exclude &lt;слово&gt; — добавить стоп-слово\n"
    "/unexclude &lt;слово&gt; — убрать стоп-слово\n"
    "/clear [exclude] — очистить список\n"
    "/regex &lt;паттерн&gt; | /regex off — регулярное выражение\n"
    "/mode any|all|regex — режим совпадения\n"
    "/test &lt;текст&gt; — проверить текст на своих правилах\n"
    "/mute, /unmute — пауза и возобновление\n"
    "/status — моё состояние"
)

ADMIN_HELP = (
    "\n\n<b>Администрирование</b>\n"
    "/pending — заявки на доступ\n"
    "/users — все пользователи\n"
    "/approve &lt;id&gt;, /deny &lt;id&gt; — решение по заявке\n"
    "/revoke &lt;id&gt; — отозвать доступ\n"
    "/unblock &lt;id&gt; — вернуть доступ (обратное к /revoke)\n"
    "/forget &lt;id&gt; — удалить пользователя целиком\n"
    "/denyall — отклонить все заявки\n"
    "/last [N] — прислать N последних совпадений из истории источника\n"
    "/info — настройки и состояние сервиса"
)


def _keyboard(*rows: Sequence[Tuple[str, str]]) -> Dict[str, Any]:
    """Инлайн-клавиатура из пар (подпись, callback_data)."""
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row]
            for row in rows
        ]
    }


def _describe_settings(settings: FilterSettings) -> str:
    """Правила пользователя в читаемом виде. Всё экранировано."""
    lines = [f"<b>Режим:</b> {escape_html(settings.mode)}"]

    if settings.keywords:
        words = ", ".join(escape_html(word) for word in settings.keywords)
        lines.append(f"<b>Ключевые слова ({len(settings.keywords)}):</b> {words}")
    else:
        lines.append("<b>Ключевые слова:</b> нет")

    if settings.exclude_keywords:
        words = ", ".join(escape_html(word) for word in settings.exclude_keywords)
        lines.append(f"<b>Стоп-слова:</b> {words}")

    if settings.regex:
        lines.append(f"<b>Regex:</b> <code>{escape_html(settings.regex)}</code>")

    if not settings.has_rules:
        lines.append("")
        lines.append(
            "⚠️ Правила пусты — вам ничего не пересылается. "
            "Добавьте слово: /add Науменко"
        )
    return "\n".join(lines)


class BotHandlers:
    """Реализации команд. Держит ссылку на приложение, но ничего в нём не ломает."""

    def __init__(self, app: "Application") -> None:
        self.app = app
        self.config = app.config
        self.users = app.users
        self.store = app.store
        self.sender = app.sender

    # -- регистрация -------------------------------------------------------- #

    def register_all(self, listener: CommandListener) -> None:
        listener.register("start", self.cmd_start, public=True)
        listener.register("help", self.cmd_help, public=True)

        # Личные правила требуют записи в users: иначе писать их некуда.
        # Такое бывает, когда TARGET_CHAT_ID — группа: право на команды даёт
        # сам чат, а учётной записи у написавшего может и не быть.
        for name, handler in (
            ("status", self.cmd_status),
            ("keywords", self.cmd_keywords),
            ("add", self.cmd_add),
            ("remove", self.cmd_remove),
            ("exclude", self.cmd_exclude),
            ("unexclude", self.cmd_unexclude),
            ("clear", self.cmd_clear),
            ("regex", self.cmd_regex),
            ("mode", self.cmd_mode),
            ("test", self.cmd_test),
            ("mute", self.cmd_mute),
            ("unmute", self.cmd_unmute),
        ):
            listener.register(name, self._personal(handler))

        listener.register("info", self.cmd_info, admin_only=True)
        listener.register("pending", self.cmd_pending, admin_only=True)
        listener.register("users", self.cmd_users, admin_only=True)
        listener.register("approve", self.cmd_approve, admin_only=True)
        listener.register("deny", self.cmd_deny, admin_only=True)
        listener.register("denyall", self.cmd_deny_all, admin_only=True)
        listener.register("revoke", self.cmd_revoke, admin_only=True)
        listener.register("unblock", self.cmd_unblock, admin_only=True)
        listener.register("forget", self.cmd_forget, admin_only=True)
        listener.register("last", self.cmd_last, admin_only=True)

        listener.register_callback("approve", self.cb_approve)
        listener.register_callback("deny", self.cb_deny)
        listener.register_callback("revoke", self.cb_revoke)
        listener.register_callback("unblock", self.cb_unblock)
        # Это настройка самого нажавшего, админом быть не нужно.
        listener.register_callback("mode", self.cb_mode, admin_only=False)

    def _personal(self, handler: Handler) -> Handler:
        """Не пускает к личным правилам того, у кого нет учётной записи."""

        async def wrapper(ctx: CommandContext) -> Optional[str]:
            if await self.users.get(ctx.user_id) is None:
                return ("Личные правила заводятся после регистрации. "
                        "Отправьте /start.")
            return await handler(ctx)

        return wrapper

    # -- доступ ------------------------------------------------------------- #

    async def cmd_start(self, ctx: CommandContext) -> Optional[str]:
        user = await self.users.get(ctx.user_id)

        if user is not None and user.status == STATUS_APPROVED:
            settings = await self.users.settings(ctx.user_id)
            note = ("Сюда приходят сообщения, подходящие под ВАШИ правила. "
                    "/help — список команд.")
            if not settings.has_rules:
                note = ("Правила пока пусты — уведомления не приходят. "
                        "Добавьте слово: /add Науменко")
            return format_status(
                header="👋 Фильтр App Store Monitor",
                fields=[("Источник", self.app.source_label)],
                note=note,
            )

        if user is not None and user.status == STATUS_DENIED:
            return "⛔️ Доступ закрыт администратором."

        if user is not None and user.status == STATUS_PENDING:
            return "⏳ Заявка уже отправлена, ожидайте решения администратора."

        # Новый человек: заводим заявку и зовём админов.
        user, created = await self.users.request_access(
            ctx.user_id, ctx.chat_id, ctx.username, ctx.full_name
        )
        if created:
            log.info("Новая заявка на доступ: %s", user.label)
            await self.notify_admins_about_request(user)
        return ("📨 Заявка отправлена администратору. Ожидайте — "
                "как только доступ откроют, бот напишет сюда.")

    async def cmd_help(self, ctx: CommandContext) -> str:
        user = await self.users.get(ctx.user_id)
        approved = ctx.is_admin or (user is not None and user.status == STATUS_APPROVED)
        if not approved:
            return ("Этот бот пересылает отфильтрованные уведомления.\n"
                    "Чтобы получить доступ, отправьте /start — заявку увидит "
                    "администратор.")
        return USER_HELP + (ADMIN_HELP if ctx.is_admin else "")

    # -- личные правила ------------------------------------------------------ #

    async def cmd_status(self, ctx: CommandContext) -> str:
        user = await self.users.get(ctx.user_id)
        settings = await self.users.settings(ctx.user_id)
        try:
            delivered = str(await self.store.count_for(str(ctx.chat_id)))
        except Exception as exc:  # noqa: BLE001 - счётчик не критичен
            log.warning("Не удалось прочитать счётчик доставок: %s", exc)
            delivered = "неизвестно"

        fields = [
            ("Статус", user.status if user is not None else "владелец"),
            ("Уведомления", "на паузе" if (user and user.muted) else "включены"),
            ("Режим", settings.mode),
            ("Ключевых слов", str(len(settings.keywords))),
            ("Стоп-слов", str(len(settings.exclude_keywords))),
            ("Regex", settings.regex or "нет"),
            ("Получено сообщений", delivered),
        ]
        return format_status(header="ℹ️ МОЁ СОСТОЯНИЕ", fields=fields)

    async def cmd_keywords(self, ctx: CommandContext) -> str:
        settings = await self.users.settings(ctx.user_id)
        text = "<b>🔎 МОИ ПРАВИЛА</b>\n\n" + _describe_settings(settings)
        note = self._legacy_note(ctx, settings)
        return text + note if note else text

    def _legacy_note(self, ctx: CommandContext, settings: FilterSettings) -> str:
        """Пояснение для чата, который одновременно является целью из .env.

        Обычно это личка владельца: у него TARGET_CHAT_ID — он сам. Без этой
        подсказки непонятно, почему /mode и /remove как будто не работают.
        """
        if not getattr(self.config, "legacy_delivery", False):
            return ""
        if str(ctx.chat_id) != str(self.config.target_chat_id):
            return ""

        if settings.has_rules:
            return ("\n\nℹ️ Это и есть целевой чат из <code>.env</code>. "
                    "Ваши правила выше <b>заменяют</b> <code>FILTER_*</code> "
                    "для него — действуют именно они.")
        return ("\n\nℹ️ Это целевой чат из <code>.env</code>, и сейчас работают "
                "правила оттуда (<code>FILTER_*</code>). Как только вы добавите "
                "своё слово через /add, они заменятся вашими.")

    async def cmd_add(self, ctx: CommandContext) -> str:
        return await self._add_word(ctx, field="keywords", label="Ключевое слово")

    async def cmd_exclude(self, ctx: CommandContext) -> str:
        return await self._add_word(ctx, field="exclude_keywords", label="Стоп-слово")

    async def _add_word(self, ctx: CommandContext, *, field: str, label: str) -> str:
        try:
            settings, added = await self.users.add_keyword(
                ctx.user_id, ctx.args, field=field
            )
        except ValueError as exc:
            return f"⚠️ {escape_html(str(exc))}"
        self.app.drop_filter_cache(ctx.user_id)
        word = escape_html(ctx.args.strip())
        if not added:
            return f"Такое слово уже есть: «{word}»"
        total = len(getattr(settings, field))
        return f"✅ {label} добавлено: «{word}». Всего: {total}"

    async def cmd_remove(self, ctx: CommandContext) -> str:
        return await self._remove_word(ctx, field="keywords", label="Ключевое слово")

    async def cmd_unexclude(self, ctx: CommandContext) -> str:
        return await self._remove_word(ctx, field="exclude_keywords", label="Стоп-слово")

    async def _remove_word(self, ctx: CommandContext, *, field: str, label: str) -> str:
        try:
            settings, removed = await self.users.remove_keyword(
                ctx.user_id, ctx.args, field=field
            )
        except ValueError as exc:
            return f"⚠️ {escape_html(str(exc))}"
        self.app.drop_filter_cache(ctx.user_id)
        word = escape_html(ctx.args.strip())
        if not removed:
            return f"Такого слова нет: «{word}»"
        total = len(getattr(settings, field))
        return f"🗑 {label} убрано: «{word}». Осталось: {total}"

    async def cmd_clear(self, ctx: CommandContext) -> str:
        field = "exclude_keywords" if ctx.args.strip().lower().startswith("exclude") \
            else "keywords"
        await self.users.clear_keywords(ctx.user_id, field=field)
        self.app.drop_filter_cache(ctx.user_id)
        what = "Стоп-слова" if field == "exclude_keywords" else "Ключевые слова"
        return f"🗑 {what} очищены. Проверить: /keywords"

    async def cmd_regex(self, ctx: CommandContext) -> str:
        try:
            settings = await self.users.set_regex(ctx.user_id, ctx.args)
        except ValueError as exc:
            return f"⚠️ {escape_html(str(exc))}"
        self.app.drop_filter_cache(ctx.user_id)
        if not settings.regex:
            return "Regex отключён."
        return f"✅ Regex сохранён: <code>{escape_html(settings.regex)}</code>"

    async def cmd_mode(self, ctx: CommandContext) -> Optional[str]:
        if not ctx.args.strip():
            # Из меню бота команда приходит без аргумента. Набирать значение
            # отдельным сообщением бесполезно — обычный текст игнорируется,
            # поэтому даём кнопки.
            await self._send_mode_picker(ctx)
            return None
        try:
            settings = await self.users.set_mode(ctx.user_id, ctx.args)
        except ValueError as exc:
            return f"⚠️ {escape_html(str(exc))}"
        self.app.drop_filter_cache(ctx.user_id)
        text = (f"✅ Режим: <b>{settings.mode}</b> — "
                f"{_MODE_EXPLANATION[settings.mode]}")
        if not settings.has_rules:
            # Режим сам по себе ничего не делает, если правил нет.
            text += ("\n⚠️ Правил пока нет, поэтому режим ни на что не влияет. "
                     "Добавьте слово: /add Науменко")
        return text + self._legacy_note(ctx, settings)

    async def _send_mode_picker(self, ctx: CommandContext) -> None:
        """Показывает текущий режим и кнопки выбора."""
        current = await self.users.settings(ctx.user_id)
        marks = {name: ("✅ " if name == current.mode else "") + name
                 for name in _MODE_EXPLANATION}
        await self.sender.send_message(
            f"Сейчас режим: <b>{escape_html(current.mode)}</b>\n\n"
            + "\n".join(
                f"<b>{name}</b> — {text}"
                for name, text in _MODE_EXPLANATION.items()
            )
            + "\n\nВыберите кнопкой или командой вида <code>/mode all</code>.",
            chat_id=ctx.chat_id,
            reply_markup=_keyboard([
                (marks["any"], "mode:any"),
                (marks["all"], "mode:all"),
                (marks["regex"], "mode:regex"),
            ]),
        )

    async def cb_mode(self, ctx: CommandContext) -> None:
        """Нажатие кнопки выбора режима — это настройка САМОГО нажавшего."""
        try:
            settings = await self.users.set_mode(ctx.user_id, ctx.args)
        except ValueError as exc:
            if ctx.callback_id:
                await self.sender.answer_callback_query(ctx.callback_id, str(exc))
            return

        self.app.drop_filter_cache(ctx.user_id)
        if ctx.callback_id:
            await self.sender.answer_callback_query(
                ctx.callback_id, f"Режим: {settings.mode}"
            )

        text = (f"✅ Режим: <b>{settings.mode}</b> — "
                f"{_MODE_EXPLANATION[settings.mode]}")
        if not settings.has_rules:
            text += ("\n⚠️ Правил пока нет, поэтому режим ни на что не влияет. "
                     "Добавьте слово: /add Науменко")
        text += self._legacy_note(ctx, settings)

        if ctx.message_id is not None:
            await self.sender.edit_message_text(ctx.chat_id, ctx.message_id, text)
        else:
            await self.sender.send_message(text, chat_id=ctx.chat_id)

    async def cmd_test(self, ctx: CommandContext) -> str:
        if not ctx.args:
            return "Укажите текст: /test Бан на модерації, White: Науменко"
        settings = await self.users.settings(ctx.user_id)
        result = self.app.build_filter(settings).check(ctx.args)
        verdict = "✅ подошло бы" if result.matched else "🚫 не подошло бы"
        return f"{verdict}\n<b>Причина:</b> {escape_html(result.reason)}"

    async def cmd_mute(self, ctx: CommandContext) -> str:
        await self.users.set_muted(ctx.user_id, True)
        return "🔕 Рассылка приостановлена. Возобновить: /unmute"

    async def cmd_unmute(self, ctx: CommandContext) -> str:
        await self.users.set_muted(ctx.user_id, False)
        return "🔔 Рассылка возобновлена."

    # -- администрирование --------------------------------------------------- #

    async def cmd_info(self, ctx: CommandContext) -> str:
        # ctx не нужен, но сигнатура у всех обработчиков одна — так их можно
        # регистрировать единообразно.
        return await self.app.describe_service()

    async def cmd_last(self, ctx: CommandContext) -> str:
        """Проверка фильтра на живых данных: последние совпадения из истории.

        Правила — личные правила админа, а если их нет — правила из .env.
        В журнал доставок ничего не пишется: это диагностика, не рассылка.
        """
        raw = ctx.args.strip()
        if raw and not raw.isdigit():
            return f"Укажите число: /last 3 (от 1 до {MAX_LAST})"
        count = min(max(int(raw or 1), 1), MAX_LAST)

        settings = await self.users.settings(ctx.user_id)
        if settings.has_rules:
            message_filter, rules = self.app.build_filter(settings), "ваши правила"
        else:
            message_filter, rules = self.app.filter, "правила из .env"

        try:
            found = await self.app.find_recent_matches(
                message_filter, count=count, limit=LAST_SCAN_LIMIT
            )
        except tg_errors.FloodWaitError as exc:
            return f"Telegram просит подождать {exc.seconds} c. Повторите позже."
        except (ConnectionError, OSError, asyncio.TimeoutError,
                tg_errors.RPCError, ValueError) as exc:
            log.warning("/last: история недоступна (%s: %s)",
                        type(exc).__name__, exc)
            return f"Не удалось прочитать историю: {escape_html(str(exc))}"

        if not found:
            return (f"Среди последних {LAST_SCAN_LIMIT} сообщений источника "
                    f"совпадений нет ({rules}).")

        sent = 0
        # От старых к новым: самое свежее окажется внизу чата.
        for message, result in reversed(found):
            payload, fallback = await self.app.history_payload(
                message, result, header="🧪 ТЕСТ — совпадение из истории"
            )
            if await self.sender.send_message(
                payload, chat_id=ctx.chat_id, fallback_text=fallback
            ):
                sent += 1
        return (f"Отправлено совпадений: {sent} из {len(found)} "
                f"({rules}, просмотрено до {LAST_SCAN_LIMIT} сообщений).")

    async def cmd_pending(self, ctx: CommandContext) -> Optional[str]:
        pending = await self.users.list_by_status(STATUS_PENDING)
        if not pending:
            return "Заявок нет."
        for user in pending[:MAX_CARDS]:
            await self.sender.send_message(
                self._request_text(user),
                chat_id=ctx.chat_id,
                reply_markup=self._decision_keyboard(user.user_id),
            )
        if len(pending) > MAX_CARDS:
            return f"…и ещё {len(pending) - MAX_CARDS}. Отклонить все: /denyall"
        return None

    async def cmd_users(self, ctx: CommandContext) -> Optional[str]:
        everyone = await self.users.list_all()
        if not everyone:
            return "Пользователей нет."

        lines = ["<b>👥 ПОЛЬЗОВАТЕЛИ</b>", ""]
        for user in everyone:
            marks = []
            if user.is_admin:
                marks.append("админ")
            if user.muted:
                marks.append("пауза")
            if user.blocked:
                marks.append("заблокировал бота")
            suffix = f" — {', '.join(marks)}" if marks else ""
            lines.append(
                f"{_STATUS_ICONS.get(user.status, '•')} "
                f"{escape_html(user.label)}{suffix}"
            )
        await self.sender.send_message("\n".join(lines), chat_id=ctx.chat_id)

        # Карточка с кнопками на каждого, кроме самих админов: что именно
        # предложить, зависит от того, как у человека закрыт доступ.
        cards = [user for user in everyone if not user.is_admin]
        for user in cards[:MAX_CARDS]:
            caption, buttons = _card_actions(user)
            await self.sender.send_message(
                f"<b>{caption}</b>\n{escape_html(user.label)}",
                chat_id=ctx.chat_id,
                reply_markup=_keyboard(buttons),
            )
        if len(cards) > MAX_CARDS:
            return f"…и ещё {len(cards) - MAX_CARDS}."
        return None

    async def cmd_approve(self, ctx: CommandContext) -> str:
        return await self._decide_by_id(ctx, "approve")

    async def cmd_deny(self, ctx: CommandContext) -> str:
        return await self._decide_by_id(ctx, "deny")

    async def cmd_revoke(self, ctx: CommandContext) -> str:
        return await self._decide_by_id(ctx, "revoke")

    async def cmd_unblock(self, ctx: CommandContext) -> str:
        """Обратное к /revoke: вернуть человека в рассылку."""
        return await self._decide_by_id(ctx, "unblock")

    async def cmd_deny_all(self, ctx: CommandContext) -> str:
        denied = await self.users.deny_all_pending(by=ctx.user_id)
        if not denied:
            return "Заявок не было."
        for user in denied:
            self.app.drop_filter_cache(user.user_id)
        log.warning("Админ %s отклонил все заявки (%s)", ctx.user_id, len(denied))
        return f"⛔ Отклонено заявок: {len(denied)}"

    async def cmd_forget(self, ctx: CommandContext) -> str:
        user_id = _parse_user_id(ctx.args)
        if user_id is None:
            return "Укажите числовой id: /forget 12345"
        if user_id == ctx.user_id:
            return "⚠️ Нельзя удалить самого себя."
        user = await self.users.forget(user_id)
        if user is None:
            return f"Пользователь {user_id} не найден."
        self.app.drop_filter_cache(user_id)
        log.warning("Админ %s удалил пользователя %s", ctx.user_id, user.label)
        return (f"🗑 Удалён: {escape_html(user.label)}\n"
                f"Чтобы вернуться, ему нужно снова отправить /start.")

    async def _decide_by_id(self, ctx: CommandContext, action: str) -> str:
        user_id = _parse_user_id(ctx.args)
        if user_id is None:
            return f"Укажите числовой id: /{action} 12345"
        text, _ = await self.apply_decision(action, user_id, by=ctx.user_id)
        return text

    # -- нажатия кнопок ------------------------------------------------------ #

    async def cb_approve(self, ctx: CommandContext) -> None:
        await self._decide_by_button(ctx, "approve")

    async def cb_deny(self, ctx: CommandContext) -> None:
        await self._decide_by_button(ctx, "deny")

    async def cb_revoke(self, ctx: CommandContext) -> None:
        await self._decide_by_button(ctx, "revoke")

    async def cb_unblock(self, ctx: CommandContext) -> None:
        await self._decide_by_button(ctx, "unblock")

    async def _decide_by_button(self, ctx: CommandContext, action: str) -> None:
        user_id = _parse_user_id(ctx.args)
        if user_id is None:
            if ctx.callback_id:
                await self.sender.answer_callback_query(ctx.callback_id, "Неверные данные")
            return

        text, toast = await self.apply_decision(action, user_id, by=ctx.user_id)
        if ctx.callback_id:
            await self.sender.answer_callback_query(ctx.callback_id, toast)
        if ctx.message_id is not None:
            # Переписываем карточку и убираем кнопки: дважды не нажать.
            await self.sender.edit_message_text(ctx.chat_id, ctx.message_id, text)
        else:
            await self.sender.send_message(text, chat_id=ctx.chat_id)

    # -- общая логика решения ------------------------------------------------ #

    async def apply_decision(
        self, action: str, user_id: int, *, by: int
    ) -> Tuple[str, str]:
        """Применяет решение админа. Возвращает (текст для админа, всплывашка)."""
        if action in ("deny", "revoke") and user_id == by:
            return "⚠️ Нельзя отозвать доступ у самого себя.", "Нельзя"

        existing = await self.users.get(user_id)
        if existing is None:
            return f"Пользователь {user_id} не найден.", "Не найден"

        if action == "unblock":
            return await self._unblock(existing, by=by)

        if action == "approve":
            user = await self.users.approve(user_id, by=by)
        elif action == "deny":
            user = await self.users.deny(user_id, by=by)
        else:
            if existing.status != STATUS_APPROVED:
                return (f"У {escape_html(existing.label)} доступа и так нет.",
                        "Доступа нет")
            user = await self.users.revoke(user_id, by=by)

        if user is None:  # pragma: no cover - пользователь исчез между запросами
            return f"Пользователь {user_id} не найден.", "Не найден"

        # Права и правила читаются из БД на каждом сообщении и команде, но
        # скомпилированный фильтр закеширован — его надо выбросить руками.
        self.app.drop_filter_cache(user_id)

        if action == "approve":
            log.info("Админ %s одобрил %s", by, user.label)
            await self._notify_user(
                user,
                "✅ Доступ открыт!\n\nТеперь настройте, что вам пересылать:\n"
                "/add &lt;слово&gt; — ключевое слово\n/keywords — текущие правила\n"
                "/help — все команды",
            )
            return f"✅ Одобрен: {escape_html(user.label)}", "Одобрено"

        verb = "отклонён" if action == "deny" else "отозван"
        log.warning("Админ %s: доступ %s для %s", by, verb, user.label)
        if action == "revoke":
            await self._notify_user(
                user, "⛔️ Доступ к рассылке отозван администратором."
            )
        return (f"⛔ Доступ {verb}: {escape_html(user.label)}",
                "Отклонено" if action == "deny" else "Отозвано")

    async def _unblock(self, user: User, *, by: int) -> Tuple[str, str]:
        """Возвращает человека в рассылку — обратное к /revoke.

        Закрыть доступ могли двумя разными способами, и лечатся они по-разному:
        админ мог отозвать доступ (status=denied), а мог сам пользователь
        заблокировать бота (blocked=1) — тогда сервис снял его с рассылки, чтобы
        не долбиться в закрытую дверь. Команда разбирает оба случая.
        """
        done = []
        if user.status != STATUS_APPROVED:
            # approve заодно снимает blocked.
            user = await self.users.approve(user.user_id, by=by)
            done.append("доступ возвращён")
        elif user.blocked:
            await self.users.set_blocked(user.user_id, False)
            user = await self.users.get(user.user_id)
            done.append("отметка о блокировке снята")

        if not done:
            return (f"У {escape_html(user.label)} доступ и так открыт.",
                    "Уже открыт")

        self.app.drop_filter_cache(user.user_id)
        log.info("Админ %s вернул доступ: %s (%s)", by, user.label, done[0])

        text = f"✅ {done[0].capitalize()}: {escape_html(user.label)}"
        # Честно предупреждаем, почему человек всё ещё может ничего не получать.
        if user.muted:
            text += "\n⚠️ Но он сам поставил рассылку на паузу (/mute)."
        else:
            settings = await self.users.settings(user.user_id)
            if not settings.has_rules:
                text += "\n⚠️ Но у него пустые правила — пока он не добавит " \
                        "слово через /add, сообщений не будет."

        await self._notify_user(
            user, "✅ Доступ к рассылке восстановлен администратором."
        )
        return text, "Возвращён"

    # -- уведомления --------------------------------------------------------- #

    def _request_text(self, user: User) -> str:
        return (
            "<b>🔔 ЗАПРОС ДОСТУПА</b>\n\n"
            f"<b>Пользователь:</b> {escape_html(user.full_name or 'без имени')}\n"
            f"<b>Username:</b> {escape_html('@' + user.username) if user.username else '—'}\n"
            f"<b>ID:</b> {user.user_id}\n"
            f"<b>Время:</b> {escape_html(user.created_at)}"
        )

    @staticmethod
    def _decision_keyboard(user_id: int) -> Dict[str, Any]:
        return _keyboard([
            ("✅ Одобрить", f"approve:{user_id}"),
            ("⛔ Отклонить", f"deny:{user_id}"),
        ])

    async def notify_admins_about_request(self, user: User) -> None:
        """Шлёт карточку заявки всем админам."""
        await self.notify_admins(
            self._request_text(user),
            reply_markup=self._decision_keyboard(user.user_id),
        )

    async def notify_admins(
        self, text: str, *, reply_markup: Optional[Dict[str, Any]] = None
    ) -> None:
        """Недоступный админ не мешает остальным получить уведомление."""
        for chat_id in await self.app.admin_chat_ids():
            try:
                await self.sender.deliver(chat_id, text, reply_markup=reply_markup)
            except Exception as exc:  # noqa: BLE001 - один админ не ломает остальных
                log.warning(
                    "Админ %s не получил уведомление: %s. Если это новый админ — "
                    "он должен сам нажать Start у бота: бот не может написать первым.",
                    chat_id, exc,
                )

    async def _notify_user(self, user: User, text: str) -> None:
        try:
            await self.sender.deliver(user.target_chat, text)
        except Exception as exc:  # noqa: BLE001 - молчание пользователя не наша беда
            log.warning("Не удалось уведомить %s: %s", user.label, exc)


def _card_actions(user: User) -> Tuple[str, List[Tuple[str, str]]]:
    """Подпись и кнопки карточки в /users — по текущему состоянию человека."""
    if user.status == STATUS_PENDING:
        return "Заявка на доступ", [
            ("✅ Одобрить", f"approve:{user.user_id}"),
            ("⛔ Отклонить", f"deny:{user.user_id}"),
        ]
    if user.status == STATUS_DENIED:
        return "Доступ закрыт", [("✅ Вернуть", f"unblock:{user.user_id}")]
    if user.blocked:
        # Человек заблокировал бота; отметка снимется и сама, как только он
        # что-нибудь напишет, но админу нужна возможность не ждать.
        return "Заблокировал бота", [
            ("🔔 Вернуть в рассылку", f"unblock:{user.user_id}")
        ]
    return "Доступ открыт", [("⛔ Отозвать", f"revoke:{user.user_id}")]


_STATUS_ICONS = {
    STATUS_APPROVED: "✅",
    STATUS_PENDING: "⏳",
    STATUS_DENIED: "⛔",
}


def _parse_user_id(raw: str) -> Optional[int]:
    raw = (raw or "").strip().split()[0] if (raw or "").strip() else ""
    try:
        return int(raw)
    except ValueError:
        return None
