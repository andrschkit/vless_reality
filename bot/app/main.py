"""Телеграм-бот семейного VPN: сообщения, reply-кнопки, точка входа.

Вся инлайн-навигация (Админка, семейные экраны) — в adminui.py;
общие зависимости (БД, права, помощники) — в common.py.
"""
import asyncio
import logging
import re
import secrets

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from .adminui import ROOT_TEXT, render_family_view, root_markup, router as adminui_router
from .common import (
    ADMIN_BOOTSTRAP,
    BOT_NAME,
    BOT_TOKEN,
    INVITE_TTL_DAYS,
    SUB_URI,
    apply_commands,
    db,
    fmt_ts,
    greet,
    log,
    my_email,
    panel,
    qr_photo,
    require_admin,
    require_head,
    subid_for,
    throttled,
    user_kb,
)
from .panel import PanelError, ping_nodes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

HELP_TEXT = """\
Этот бот выдаёт персональную ссылку на семейный VPN.

Happ на iPhone:
1. Скопируй свою ссылку кнопкой «Моя ссылка».
2. Happ → + → «Импорт из буфера» (или вставь в поле подписки).
3. Включи переключатель — готово.

Happ на Android:
1. Установи Happ (Google Play) и открой его.
2. Скопируй свою ссылку кнопкой «Моя ссылка».
3. Happ → + → «Импорт из буфера обмена».
4. Включи профиль — готово.

ВАЖНО для Happ (и iPhone, и Android): настройка «Фрагментация» должна
быть ВЫКЛЮЧЕНА. С включённой фрагментацией серверы Reality не работают
(пинг есть, а интернета нет).

v2rayNG (Android): настройки → «Импорт из буфера обмена», потом на главном
экране обнови подписку.

В ссылку уже включены все серверы сразу — выбирать ноду вручную не нужно.
Приложение само покажет пинг каждого сервера.

Важно: ссылка — это доступ к VPN. Не пересылай её посторонним и не отправляй
через российские сервисы и мессенджеры (ВКонтакте, Одноклассники и т.п.) —
там её увидят, и наш домен могут заблокировать. Лучше лично или через Telegram.
"""

FAMILY_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")

router = Router()


async def fresh_kb(tg_id: int):
    """Актуальная reply-клавиатура пользователя (обновляем на каждом ответе без инлайна)."""
    u = await db.get_user(tg_id)
    if not u:
        return None
    return user_kb(bool(u[2]), bool(u[4] or (u[2] and u[3])))


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
        await apply_commands(m.bot, m.from_user.id, level="admin")
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


# ---------- команды администратора (текстовый fallback, полный UI — «Админка») ----------

@router.message(Command("invite"))
async def cmd_invite(m: Message, command: CommandObject) -> None:
    if throttled(m.from_user.id):
        return
    u = await require_head(m)
    if not u:
        return
    _me, _un, is_admin, my_family, _hd = u
    email = (command.args or "").strip().lower()
    if not email:
        await m.answer(
            "Формат: /invite <email профиля> — у админа любой, у главы — только член его семьи. "
            "Проще: «Моя семья» → человек → «Пригласить»."
        )
        return
    try:
        subs = await panel.client_subids(email)
    except PanelError:
        await m.answer("БД панели недоступна, попробуйте позже.")
        return
    if not subs:
        await m.answer(f"Клиента {email} нет в панели. Сначала создайте его в 3x-ui (или проверьте email).")
        return
    if not is_admin:
        cl = await db.get_client(email)
        if not cl or cl[1] != my_family:
            await m.answer(f"Вы можете приглашать только членов своей семьи «{my_family}».")
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
    lines = [f"`{code}` → {email}, до {fmt_ts(exp)}" for code, email, exp in rows]
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
    await apply_commands(m.bot, m.from_user.id, level="admin")
    u = await db.get_user(m.from_user.id)
    await m.answer(f"Готово, администратор привязан к {email}.")
    await greet(u, m)


@router.message(Command("family"))
async def cmd_family(m: Message, command: CommandObject) -> None:
    if throttled(m.from_user.id):
        return
    u = await require_head(m)
    if not u:
        return
    _me, _un, is_admin, my_family, _is_head = u
    parts = (command.args or "").split()
    if len(parts) < 2:
        await m.answer(
            "Формат: /family <email> <семья> [head|member] — зачислить или переместить; "
            "/family <email> - — убрать из семьи.\n"
            "Глава управляет только своей семьёй; глав назначает админ. "
            "Проще: кнопка «Админка» или «Моя семья»."
        )
        return
    email, fam = parts[0].lower(), parts[1]
    if email == "admin":
        await m.answer("«admin» — служебный профиль бота, его нельзя зачислять в семью.")
        return
    target = await db.get_client(email)

    if fam == "-":
        if not target or not target[1]:
            await m.answer(f"{email} и так не состоит в семье.")
            return
        if not is_admin:
            if target[1] != my_family:
                await m.answer("Глава может убирать только из СВОЕЙ семьи.")
                return
            if target[2]:
                await m.answer("Главу семьи может убирать только админ.")
                return
        await db.clear_family(email)
        await db.audit(m.from_user.id, "family_clear", f"{email} <- {target[1]}")
        await m.answer(f"{email} убран из семьи «{target[1]}».")
        return

    if not FAMILY_RE.match(fam) or fam == "fnone":
        await m.answer("Имя семьи: латиница, цифры, _ и -, до 24 символов (имя «fnone» зарезервировано).")
        return

    mode = parts[2].lower() if len(parts) > 2 else ""
    if mode == "head":
        if not is_admin:
            await m.answer("Глав назначает только админ.")
            return
        head_val: bool | None = True
    elif mode == "member":
        head_val = False
    elif mode == "":
        head_val = None  # статус главы не меняем (чистое перемещение)
    else:
        await m.answer("Третий аргумент, если есть: head или member.")
        return

    if not is_admin:
        if fam != my_family:
            await m.answer(f"Глава может зачислять только в свою семью «{my_family}».")
            return
        if target and target[2] and target[1] != my_family:
            await m.answer(f"{email} — глава другой семьи; перемещать глав может только админ.")
            return

    await db.set_family(email, fam, head_val)
    await db.audit(m.from_user.id, "family_set", f"{email} -> {fam} head={head_val}")
    moved = target is not None and target[1] is not None and target[1] != fam
    verb = "перемещён" if moved else "зачислен"
    await m.answer(f"{email} {verb} в семью «{fam}»" + (" (глава)" if head_val else ""))


@router.message(Command("families"))
async def cmd_families(m: Message) -> None:
    if throttled(m.from_user.id) or not await require_admin(m):
        return
    rows = await db.all_families()
    if not rows:
        await m.answer("Семьи пока не назначены. Проще через «Админка» → «Семьи».")
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


# ---------- reply-кнопки ----------

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
        "Импортируй её в приложение (кнопка «Помощь»).\n"
        "Не пересылайте ссылку посторонним и не отправляйте её через российские сервисы "
        "и мессенджеры (ВКонтакте, Одноклассники и т.п.) — там её увидят, и наш домен "
        "могут заблокировать. Лучше передать лично или через Telegram.",
        reply_markup=await fresh_kb(m.from_user.id),
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
    await m.answer_photo(
        qr_photo(link),
        caption=(
            f"QR вашей подписки ({email}). Сканируйте приложением.\n"
            "Не отправляйте его через российские сервисы и мессенджеры "
            "(ВКонтакте, Одноклассники и т.п.) — только лично или через Telegram."
        ),
        reply_markup=await fresh_kb(m.from_user.id),
    )


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
        "приложение покажет реальный пинг после импорта):\n\n" + "\n".join(lines),
        reply_markup=await fresh_kb(m.from_user.id),
    )


@router.message(F.text == "Помощь")
async def btn_help(m: Message) -> None:
    if throttled(m.from_user.id):
        return
    await m.answer(HELP_TEXT, reply_markup=await fresh_kb(m.from_user.id))


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
    if head and not is_admin:
        await apply_commands(m.bot, m.from_user.id, level="head")
    text, markup = await render_family_view(family, email, bool(is_admin))
    await m.answer(text, reply_markup=markup)


@router.message(F.text == "Админка")
async def btn_admin(m: Message) -> None:
    if throttled(m.from_user.id):
        return
    if not await require_admin(m):
        return
    await m.answer(ROOT_TEXT, reply_markup=root_markup())


# ---------- слэш-алиасы к кнопкам ----------

@router.message(Command("link"))
async def cmd_link_alias(m: Message) -> None:
    await btn_link(m)


@router.message(Command("qr"))
async def cmd_qr_alias(m: Message) -> None:
    await btn_qr(m)


@router.message(Command("nodes"))
async def cmd_nodes_alias(m: Message) -> None:
    await btn_nodes(m)


@router.message(Command("help"))
async def cmd_help_alias(m: Message) -> None:
    await btn_help(m)


@router.message(Command("myfamily"))
async def cmd_myfamily_alias(m: Message) -> None:
    await btn_family(m)


# ---------- ссылки/QR членов семьи ----------

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
        await cq.message.answer(
            f"Ссылка для {email}:\n\n{link}\n\n"
            "Передайте её лично или через Telegram, не через российские сервисы "
            "и мессенджеры (ВКонтакте, Одноклассники и т.п.) — там её увидят, и наш домен "
            "могут заблокировать. Попросите не пересылать дальше."
        )
    else:
        await cq.message.answer_photo(
            qr_photo(link),
            caption=f"QR подписки для {email}.\nПередайте лично или через Telegram, не через российские сервисы и мессенджеры.",
        )
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
    await apply_commands(bot)
    dp = Dispatcher()
    dp.include_router(adminui_router)
    dp.include_router(router)
    log.info("старт бота; БД бота: /db/bot.db, БД панели: /data/x-ui.db")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
