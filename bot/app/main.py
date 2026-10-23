"""Телеграм-бот семейного VPN: свои ссылка/QR + статус нод + семейные ссылки.

MVP — только чтение (sqlite панели read-only). Мутации клиентов — фаза 2.
"""
import asyncio
import io
import logging
import os
import secrets
import time

import qrcode
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from .db import BotDB
from .panel import PanelDB, PanelError, ping_nodes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tg-vpn-bot")

BOT_TOKEN = open(os.environ["BOT_TOKEN_FILE"]).read().strip()
BOT_NAME = os.environ.get("BOT_NAME", "bot")
SUB_URI = os.environ["SUB_URI"]
XUI_DB = os.environ.get("XUI_DB", "/data/x-ui.db")
BOT_DB = os.environ.get("BOT_DB", "/db/bot.db")
ADMIN_BOOTSTRAP = os.environ.get("ADMIN_BOOTSTRAP", "")
INVITE_TTL_DAYS = int(os.environ.get("INVITE_TTL_DAYS", "7"))

HELP_TEXT = """\
Этот бот выдаёт персональную ссылку на семейный VPN.

Как подключиться (Happ на iPhone):
1. Скопируй свою ссылку кнопкой «Моя ссылка».
2. Happ → + → «Импорт из буфера» (или вставь в поле подписки).
3. Включи переключатель — готово.

v2rayNG (Android): настройки → «Импорт из буфера обмена», потом обновить подписку.

В ссылку уже включены все серверы сразу — выбирать ноду вручную не нужно.
Приложение само покажет пинг каждого сервера.

Важно: ссылка — это доступ к VPN. Не пересылай её посторонним.
"""

db = BotDB(BOT_DB)
panel = PanelDB(XUI_DB)
router = Router()

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


def user_kb(is_admin: bool, has_family_view: bool) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text="Моя ссылка"), KeyboardButton(text="QR-код")]]
    if has_family_view:
        rows.append([KeyboardButton(text="Моя семья")])
    rows.append([KeyboardButton(text="Ноды и статус"), KeyboardButton(text="Помощь")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


async def greet(u, m: Message) -> None:
    email, _username, is_admin, family, head = u
    kb = user_kb(bool(is_admin), bool(head or (is_admin and family)))
    extra = ""
    if is_admin:
        extra = "\n\nКоманды администратора: /clients, /invite, /invites, /bind, /bindme, /family, /families, /who, /audit, /sync"
    await m.answer(f"Здравствуйте! Ваш профиль: {email}.{extra}", reply_markup=kb)


@router.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject) -> None:
    if throttled(m.from_user.id):
        return
    arg = (command.args or "").strip()

    u = await db.get_user(m.from_user.id)
    if u:
        await greet(u, m)
        return

    # автопривязка по ожидающей привязке от админа (/bind @username email)
    email = await db.take_pending_bind(m.from_user.username)
    if email and not arg:
        await db.bind_user(m.from_user.id, email, m.from_user.username)
        await db.audit(m.from_user.id, "bind_pending", email)
        u = await db.get_user(m.from_user.id)
        await m.answer(f"Готово, вы привязаны к профилю {email}.")
        await greet(u, m)
        return

    if arg and arg == ADMIN_BOOTSTRAP and ADMIN_BOOTSTRAP:
        await db.bind_user(m.from_user.id, "admin", m.from_user.username, is_admin=True)
        await db.audit(m.from_user.id, "bootstrap_admin", m.from_user.username or "")
        u = await db.get_user(m.from_user.id)
        await m.answer("Вы — администратор бота.")
        await greet(u, m)
        return

    if arg:
        email = await db.take_invite(arg, m.from_user.id)
        if email:
            await db.bind_user(m.from_user.id, email, m.from_user.username)
            await db.audit(m.from_user.id, "bind_invite", email)
            u = await db.get_user(m.from_user.id)
            await m.answer(f"Готово, вы привязаны к профилю {email}.")
            await greet(u, m)
            return
        await db.audit(m.from_user.id, "invite_bad", arg)
        await m.answer("Пригласительная ссылка недействительна или уже использована. Попросите новую у администратора.")
        return

    await m.answer("Я бот семейного VPN. Для начала работы нужна пригласительная ссылка — попросите её у администратора.")


async def require_admin(m: Message) -> bool:
    u = await db.get_user(m.from_user.id)
    if not u or not u[2]:
        await db.audit(m.from_user.id, "deny_admin_cmd", m.text or "")
        return False
    return True


@router.message(Command("invite"))
async def cmd_invite(m: Message, command: CommandObject) -> None:
    if throttled(m.from_user.id) or not await require_admin(m):
        return
    email = (command.args or "").strip().lower()
    if not email:
        await m.answer("Формат: /invite <email профиля в панели>")
        return
    try:
        subs = await panel.client_subids(email)
    except PanelError:
        await m.answer("БД панели недоступна, попробуйте позже.")
        return
    if not subs:
        await m.answer(f"Клиента {email} нет в панели. Сначала создайте его в 3x-ui (или проверьте email).")
        return
    code = secrets.token_urlsafe(9)
    await db.create_invite(code, email, m.from_user.id, INVITE_TTL_DAYS)
    await db.audit(m.from_user.id, "invite_create", email)
    await m.answer(
        f"Инвайт для {email} (одноразовый, живёт {INVITE_TTL_DAYS} дн.):\n"
        f"https://t.me/{BOT_NAME}?start={code}"
    )


@router.message(Command("invites"))
async def cmd_invites(m: Message) -> None:
    if throttled(m.from_user.id) or not await require_admin(m):
        return
    rows = await db.active_invites()
    if not rows:
        await m.answer("Активных инвайтов нет.")
        return
    lines = [
        f"`{code}` → {email}, до {fmt_ts(exp)}" for code, email, exp in rows
    ]
    await m.answer("Активные инвайты:\n" + "\n".join(lines))


@router.message(Command("bind"))
async def cmd_bind(m: Message, command: CommandObject) -> None:
    if throttled(m.from_user.id) or not await require_admin(m):
        return
    parts = (command.args or "").split()
    if len(parts) != 2:
        await m.answer("Формат: /bind <tg_id или @username> <email>. При первом сообщении человека бот его привяжет.")
        return
    who, email = parts[0], parts[1].lower()
    try:
        subs = await panel.client_subids(email)
    except PanelError:
        await m.answer("БД панели недоступна, попробуйте позже.")
        return
    if not subs:
        await m.answer(f"Клиента {email} нет в панели.")
        return
    if who.lstrip("@").isdigit():
        await db.bind_user(int(who), email, None)
        await db.audit(m.from_user.id, "bind_direct", f"{who}->{email}")
        await m.answer(f"Привязал tg_id {who} к {email}.")
    else:
        await db.add_pending_bind(who, email, m.from_user.id)
        await db.audit(m.from_user.id, "bind_pending_create", f"{who}->{email}")
        await m.answer(f"Запомнил: @{who.lstrip('@')} привяжется к {email}, когда напишет боту.")


@router.message(Command("bindme"))
async def cmd_bindme(m: Message, command: CommandObject) -> None:
    if throttled(m.from_user.id) or not await require_admin(m):
        return
    email = (command.args or "").strip().lower()
    if not email:
        await m.answer("Формат: /bindme <ваш email в панели>")
        return
    try:
        subs = await panel.client_subids(email)
    except PanelError:
        await m.answer("БД панели недоступна, попробуйте позже.")
        return
    if not subs:
        await m.answer(f"Клиента {email} нет в панели.")
        return
    await db.bind_user(m.from_user.id, email, m.from_user.username, is_admin=True)
    await db.audit(m.from_user.id, "bindme", email)
    u = await db.get_user(m.from_user.id)
    await m.answer(f"Готово, администратор привязан к {email}.")
    await greet(u, m)


@router.message(Command("family"))
async def cmd_family(m: Message, command: CommandObject) -> None:
    if throttled(m.from_user.id) or not await require_admin(m):
        return
    parts = (command.args or "").split()
    if len(parts) < 2:
        await m.answer("Формат: /family <email> <семья> [head] — зачислить в семью; head = назначить главой.")
        return
    email, fam = parts[0].lower(), parts[1]
    head = len(parts) > 2 and parts[2] == "head"
    await db.set_family(email, fam, True if head else None)
    await db.audit(m.from_user.id, "family_set", f"{email} -> {fam} head={head}")
    await m.answer(f"{email} → семья «{fam}»" + (" (глава)" if head else ""))


@router.message(Command("families"))
async def cmd_families(m: Message) -> None:
    if throttled(m.from_user.id) or not await require_admin(m):
        return
    rows = await db.all_families()
    if not rows:
        await m.answer("Семьи пока не назначены. Формат: /family <email> <семья> [head]")
        return
    out: dict[str, list[str]] = {}
    for fam, email, head, bound in rows:
        tag = " (глава)" if head else ""
        tag += " — привязан" if bound else " — не привязан"
        out.setdefault(fam, []).append(email + tag)
    text = "\n\n".join(f"{fam}:\n" + "\n".join("  " + x for x in ms) for fam, ms in sorted(out.items()))
    await m.answer("Семьи:\n\n" + text)


@router.message(Command("clients"))
async def cmd_clients(m: Message) -> None:
    if throttled(m.from_user.id) or not await require_admin(m):
        return
    note = ""
    try:
        for email in await panel.all_client_emails():
            await db.ensure_client(email)
    except PanelError:
        note = "\n(панель недоступна — список может быть неполным)"
    rows = [r for r in await db.all_clients() if r[0] != "admin"]
    if not rows:
        await m.answer("Реестр пуст, а панель недоступна. Попробуйте позже или сначала /sync.")
        return
    lines = []
    for email, family, head, bound in rows:
        tag = f" | семья «{family}»" + (" (глава)" if head else "") if family else ""
        tag += " | привязан" if bound else " | не привязан"
        lines.append(email + tag)
    await m.answer("Клиенты панели:\n" + "\n".join(lines) + note)


@router.message(Command("who"))
async def cmd_who(m: Message, command: CommandObject) -> None:
    if throttled(m.from_user.id) or not await require_admin(m):
        return
    email = (command.args or "").strip().lower()
    if not email:
        await m.answer("Формат: /who <email>")
        return
    cl = await db.get_client(email)
    if not cl:
        await m.answer(f"{email} не встречается в реестре бота.")
        return
    u = await db.get_user_by_email(email)
    fam = f"семья «{cl[1]}»" + (" (глава)" if cl[2] else "") if cl[1] else "без семьи"
    bound = f"привязан: tg_id {u[0]}, @{u[1] or '—'}" if u else "не привязан к telegram"
    await m.answer(f"{email}: {fam}, {bound}.")


@router.message(Command("audit"))
async def cmd_audit(m: Message) -> None:
    if throttled(m.from_user.id) or not await require_admin(m):
        return
    rows = await db.recent_audit(20)
    if not rows:
        await m.answer("Аудит пуст.")
        return
    lines = [f"{fmt_ts(ts)}  tg={tg_id}  {action} {detail}" for ts, tg_id, action, detail in rows]
    await m.answer("Последние действия:\n" + "\n".join(lines))


@router.message(Command("sync"))
async def cmd_sync(m: Message) -> None:
    if throttled(m.from_user.id) or not await require_admin(m):
        return
    try:
        emails = await panel.all_client_emails()
    except PanelError:
        await m.answer("БД панели недоступна, попробуйте позже.")
        return
    for e in sorted(emails):
        await db.ensure_client(e)
    await db.audit(m.from_user.id, "sync", f"{len(emails)} клиентов")
    await m.answer(f"Реестр синхронизирован: {len(emails)} клиентов из панели.")


# ---------- кнопки ----------

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


@router.message(F.text == "Моя ссылка")
async def btn_link(m: Message) -> None:
    if throttled(m.from_user.id):
        return
    email = await my_email(m)
    if not email:
        return
    subid, err = await subid_for(email)
    if err:
        await m.answer(err)
        return
    await db.audit(m.from_user.id, "get_link", email)
    await m.answer(
        f"Ваша ссылка подписки — все сервера сразу:\n\n{SUB_URI}{subid}\n\n"
        "Импортируй её в приложение кнопкой «Помощь». Ссылку не пересылайте посторонним."
    )


@router.message(F.text == "QR-код")
async def btn_qr(m: Message) -> None:
    if throttled(m.from_user.id):
        return
    email = await my_email(m)
    if not email:
        return
    subid, err = await subid_for(email)
    if err:
        await m.answer(err)
        return
    await db.audit(m.from_user.id, "get_qr", email)
    link = SUB_URI + subid
    await m.answer_photo(qr_photo(link), caption=f"QR вашей подписки ({email}). Сканируйте приложением.")


@router.message(F.text == "Ноды и статус")
async def btn_nodes(m: Message) -> None:
    if throttled(m.from_user.id):
        return
    try:
        nodes = await panel.nodes()
        pings = await ping_nodes(nodes)
    except PanelError:
        await m.answer("Техническая пауза — попробуйте через минуту.")
        return
    lines = []
    for remark, addr, ms in pings:
        lines.append(f"{remark:<9} {addr:<16} {f'{ms} мс' if ms is not None else 'недоступна'}")
    await m.answer(
        "Серверы (TCP-проверка живости с мастера — это НЕ скорость с вашего телефона;\n"
        "приложение покажет реальный пинг после импорта):\n\n" + "\n".join(lines)
    )


@router.message(F.text == "Помощь")
async def btn_help(m: Message) -> None:
    if throttled(m.from_user.id):
        return
    await m.answer(HELP_TEXT)


@router.message(F.text == "Моя семья")
async def btn_family(m: Message) -> None:
    if throttled(m.from_user.id):
        return
    u = await db.get_user(m.from_user.id)
    if not u:
        await m.answer("Вы ещё не привязаны. Нужна пригласительная ссылка от администратора.")
        return
    email, _username, is_admin, family, head = u
    if not family or not (head or is_admin):
        await m.answer("Вы не глава семьи. Ссылки членов семьи доступны главе; своя — кнопками выше.")
        return
    members = await db.family_members(family)
    if not members:
        await m.answer("В семье пока никого. Администратор добавляет: /family <email> " + (family or "<семья>"))
        return
    lines, kb_rows = [], []
    for mem_email, mem_head, tg_id, username in members:
        tag = " (глава)" if mem_head else ""
        tag += "" if tg_id else " — не привязан к боту"
        lines.append(mem_email + tag)
        if mem_email != email:
            kb_rows.append([
                InlineKeyboardButton(text=f"{mem_email}: ссылка", callback_data=f"fl:{mem_email}"),
                InlineKeyboardButton(text="QR", callback_data=f"fq:{mem_email}"),
            ])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
    await m.answer(f"Семья «{family}»:\n" + "\n".join(lines), reply_markup=kb)


@router.callback_query(F.data.regexp(r"^f[ql]:"))
async def fam_callback(cq: CallbackQuery) -> None:
    kind, email = cq.data[1], cq.data[3:]
    rq = await db.get_user(cq.from_user.id)
    target = await db.get_client(email)
    allowed = bool(rq and target and (rq[2] or (rq[3] and rq[4] and rq[3] == target[1])))
    if not allowed:
        await db.audit(cq.from_user.id, "deny_family", email)
        await cq.answer("Нет доступа к этому профилю.", show_alert=True)
        return
    subid, err = await subid_for(email)
    if err:
        await cq.answer(err, show_alert=True)
        return
    await db.audit(cq.from_user.id, f"fam_{'link' if kind == 'l' else 'qr'}", email)
    link = SUB_URI + subid
    if kind == "l":
        await cq.message.answer(f"Ссылка для {email}:\n\n{link}\n\nПередайте лично и попросите не пересылать дальше.")
    else:
        await cq.message.answer_photo(qr_photo(link), caption=f"QR подписки для {email}.")
    await cq.answer()


# ---------- остальное ----------

@router.message()
async def fallback(m: Message) -> None:
    if throttled(m.from_user.id) or not m.from_user:
        return
    u = await db.get_user(m.from_user.id)
    if not u:
        email = await db.take_pending_bind(m.from_user.username)
        if email:
            await db.bind_user(m.from_user.id, email, m.from_user.username)
            await db.audit(m.from_user.id, "bind_pending", email)
            u = await db.get_user(m.from_user.id)
            await m.answer(f"Готово, вы привязаны к профилю {email}.")
            await greet(u, m)
            return
        await m.answer("Я бот семейного VPN. Для начала работы нужна пригласительная ссылка — попросите её у администратора.")
        return
    await db.touch_username(m.from_user.id, m.from_user.username)
    await m.answer("Не понял. Используйте кнопки внизу экрана.")


async def main() -> None:
    await db.open()
    await panel.open()
    bot = Bot(token=BOT_TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)
    dp = Dispatcher()
    dp.include_router(router)
    log.info("старт бота; БД бота: %s, БД панели: %s", BOT_DB, XUI_DB)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
