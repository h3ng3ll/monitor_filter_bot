"""Команды бота: доступ, персональные правила, администрирование.

Транспорт (getUpdates, разбор апдейта, проверка прав) живёт в bot_commands.py,
здесь — только смысл команд.

Важно про экранирование: ответы уходят с parse_mode=HTML, поэтому ВСЁ, что
пришло от пользователя — имя, username, ключевое слово, regex — обязано
пройти через escape_html. Иначе /add <b> сломает ответ или подмешает разметку.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

from .bot_commands import CommandContext, CommandListener
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
    "/forget &lt;id&gt; — удалить пользователя целиком\n"
    "/denyall — отклонить все заявки\n"
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

        listener.register("status", self.cmd_status)
        listener.register("keywords", self.cmd_keywords)
        listener.register("add", self.cmd_add)
        listener.register("remove", self.cmd_remove)
        listener.register("exclude", self.cmd_exclude)
        listener.register("unexclude", self.cmd_unexclude)
        listener.register("clear", self.cmd_clear)
        listener.register("regex", self.cmd_regex)
        listener.register("mode", self.cmd_mode)
        listener.register("test", self.cmd_test)
        listener.register("mute", self.cmd_mute)
        listener.register("unmute", self.cmd_unmute)

        listener.register("info", self.cmd_info, admin_only=True)
        listener.register("pending", self.cmd_pending, admin_only=True)
        listener.register("users", self.cmd_users, admin_only=True)
        listener.register("approve", self.cmd_approve, admin_only=True)
        listener.register("deny", self.cmd_deny, admin_only=True)
        listener.register("denyall", self.cmd_deny_all, admin_only=True)
        listener.register("revoke", self.cmd_revoke, admin_only=True)
        listener.register("forget", self.cmd_forget, admin_only=True)

        listener.register_callback("approve", self.cb_approve)
        listener.register_callback("deny", self.cb_deny)
        listener.register_callback("revoke", self.cb_revoke)

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
        return "<b>🔎 МОИ ПРАВИЛА</b>\n\n" + _describe_settings(settings)

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

    async def cmd_mode(self, ctx: CommandContext) -> str:
        try:
            settings = await self.users.set_mode(ctx.user_id, ctx.args)
        except ValueError as exc:
            return f"⚠️ {escape_html(str(exc))}"
        self.app.drop_filter_cache(ctx.user_id)
        explanation = {
            "any": "подходит любое из ключевых слов (или regex)",
            "all": "должны встретиться все ключевые слова",
            "regex": "решает только регулярное выражение",
        }[settings.mode]
        return f"✅ Режим: <b>{settings.mode}</b> — {explanation}"

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
        return await self.app.describe_service()

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

        # Карточки с кнопкой отзыва — только для тех, у кого доступ есть.
        revocable = [
            user for user in everyone
            if user.status == STATUS_APPROVED and not user.is_admin
        ]
        for user in revocable[:MAX_CARDS]:
            await self.sender.send_message(
                f"<b>Доступ открыт</b>\n{escape_html(user.label)}",
                chat_id=ctx.chat_id,
                reply_markup=_keyboard([("⛔ Отозвать", f"revoke:{user.user_id}")]),
            )
        return None

    async def cmd_approve(self, ctx: CommandContext) -> str:
        return await self._decide_by_id(ctx, "approve")

    async def cmd_deny(self, ctx: CommandContext) -> str:
        return await self._decide_by_id(ctx, "deny")

    async def cmd_revoke(self, ctx: CommandContext) -> str:
        return await self._decide_by_id(ctx, "revoke")

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
