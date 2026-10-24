"""Общие зависимости бота: конфиг, БД, права доступа, помощники.

Сюда вынесено всё, чем пользуются и main.py (сообщения/кнопки), и adminui.py (инлайн-меню),
чтобы не было циклических импортов.
"""
import io
import logging
import os
import time

import qrcode
from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    BufferedInputFile,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from .db import BotDB
from .panel import PanelDB, PanelError

log = logging.getLogger("tg-vpn-bot")

BOT_TOKEN = open(os.environ["BOT_TOKEN_FILE"]).read().strip()
BOT_NAME = os.environ.get("BOT_NAME", "bot")
SUB_URI = os.environ["SUB_URI"]
XUI_DB = os.environ.get("XUI_DB", "/data/x-ui.db")
BOT_DB = os.environ.get("BOT_DB", "/db/bot.db")
ADMIN_BOOTSTRAP = os.environ.get("ADMIN_BOOTSTRAP", "")
INVITE_TTL_DAYS = int(os.environ.get("INVITE_TTL_DAYS", "7"))

db = BotDB(BOT_DB)
panel = PanelDB(XUI_DB)

_last_cmd: dict[int, float] = {}


def throttled(tg_id: int) -> bool:
    now = time.monotonic()
    if now - _last_cmd.get(tg_id, 0.0) < 1.2:
        return True
    _last_cmd[tg_id] = now
    return False


def fmt_ts(ts: int) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(ts))


async def subid_for(email: str) -> tuple[str | None, str | None]:
    """(subId, None) или (None, текст ошибки для пользователя)."""
    try:
        subs = await panel.client_subids(email)
    except PanelError:
        return None, "Техническая пауза — попробуйте через минуту."
    if not subs:
        return None, "Ваш профиль не найден в панели. Обратитесь к администратору."
    vals = {v for v in subs.values() if v}
    if len(vals) != 1:
        log.warning("subId расходятся для %s: %s", email, subs)
        return None, "С профилем неполадка (ссылки на серверах различаются). Уже сообщено администратору."
    return vals.pop(), None


def qr_photo(link: str) -> BufferedInputFile:
    img = qrcode.make(link, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return BufferedInputFile(buf.getvalue(), filename="vpn.png")


# Меню подсказок при вводе "/" (Bot API setMyCommands). Общий список — всем,
# с /family — главам, полный — только чату админа (scope чата).
USER_COMMANDS = [
    BotCommand(command="link", description="Моя ссылка подписки"),
    BotCommand(command="qr", description="QR-код подписки"),
    BotCommand(command="nodes", description="Ноды и их статус"),
    BotCommand(command="myfamily", description="Моя семья (глава семьи)"),
    BotCommand(command="help", description="Помощь: как подключиться"),
]
HEAD_COMMANDS = USER_COMMANDS + [
    BotCommand(command="family", description="Моя семья: /family <email> <моя семья>|-"),
    BotCommand(command="invite", description="Пригласить члена семьи: /invite <email>"),
]
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="clients", description="Все клиенты панели"),
    BotCommand(command="invite", description="Инвайт: /invite <email>"),
    BotCommand(command="invites", description="Активные инвайты"),
    BotCommand(command="bind", description="Привязка: /bind <tg_id|@user> <email>"),
    BotCommand(command="bindme", description="Привязать себя: /bindme <email>"),
    BotCommand(command="family", description="Семья: /family <email> <семья> [head|member]; «-» убрать"),
    BotCommand(command="families", description="Обзор семей"),
    BotCommand(command="who", description="Кто привязан: /who <email>"),
    BotCommand(command="audit", description="Последние действия"),
    BotCommand(command="sync", description="Синхронизация реестра с панелью"),
]
_LEVELS = {"user": USER_COMMANDS, "head": HEAD_COMMANDS, "admin": ADMIN_COMMANDS}


async def apply_commands(bot: Bot, tg_id: int | None = None, level: str = "user") -> None:
    """Меню подсказок: общее (tg_id=None) или для конкретного чата; level = user|head|admin."""
    try:
        cmds = _LEVELS.get(level, USER_COMMANDS)
        if tg_id is None:
            await bot.set_my_commands(cmds, scope=BotCommandScopeDefault())
        else:
            await bot.set_my_commands(cmds, scope=BotCommandScopeChat(chat_id=tg_id))
    except Exception:
        log.exception("не удалось выставить меню команд")


async def require_admin(m: Message) -> bool:
    u = await db.get_user(m.from_user.id)
    if not u or not u[2]:
        await db.audit(m.from_user.id, "deny_admin_cmd", m.text or "")
        return False
    # идемпотентно выставляем полное меню этого чата (self-heal после пересоздания контейнера)
    await apply_commands(m.bot, m.from_user.id, level="admin")
    return True


async def require_head(m: Message):
    """Строка пользователя (email, username, is_admin, family, head), если он админ или глава семьи."""
    u = await db.get_user(m.from_user.id)
    if not u or not (u[2] or u[4]):
        await db.audit(m.from_user.id, "deny_family_cmd", m.text or "")
        return None
    return u


async def my_email(m: Message) -> str | None:
    """Email текущего пользователя или None (ответ уже отправлен)."""
    u = await db.get_user(m.from_user.id)
    if not u:
        await m.answer("Вы ещё не привязаны. Нужна пригласительная ссылка от администратора.")
        return None
    email = u[0]
    if email == "admin":
        await m.answer("Администратор без своего профиля. Назначьте: /bindme <email>")
        return None
    return email


def user_kb(is_admin: bool, has_family_view: bool) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text="Моя ссылка"), KeyboardButton(text="QR-код")]]
    if has_family_view:
        rows.append([KeyboardButton(text="Моя семья")])
    rows.append([KeyboardButton(text="Ноды и статус"), KeyboardButton(text="Помощь")])
    if is_admin:
        rows.append([KeyboardButton(text="Админка")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


async def greet(u, m: Message) -> None:
    email, _username, is_admin, family, head = u
    kb = user_kb(bool(is_admin), bool(head or (is_admin and family)))
    extra = ""
    if is_admin:
        extra = (
            "\n\nУправление клиентами, семьями и инвайтами — кнопка «Админка» ниже. "
            "Команды тоже работают: /clients, /invite, /family, /bind, /who, /audit, /sync."
        )
    await m.answer(f"Здравствуйте! Ваш профиль: {email}.{extra}", reply_markup=kb)
