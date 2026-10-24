"""Инлайн-интерфейсы: «Админка» и управление семьёй для глав.

Все экраны редактируют одно сообщение (навигация «Назад»), списки длиннее
8 позиций разбиты на страницы. Ручной ввод нужен только в двух местах:
имя новой семьи и контакт для привязки (FSM с кнопкой «Отмена»).

ВАЖНО: рендеры вынесены в функции с ЯВНЫМИ аргументами (email/name/family).
Нельзя вызывать «соседний» хендлер, передавая чужой callback_data —
парсинг адреса экрана из неё даст мусор (баг с «Назад» после перемещения).
"""
import asyncio
import re
import secrets

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .common import BOT_NAME, INVITE_TTL_DAYS, db, fmt_ts, log, panel, subid_for
from .panel import PanelError

router = Router()

PAGE = 8
FAMILY_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")


class UI(StatesGroup):
    family_name = State()   # data: email клиента, которого зачисляем в новую семью
    bind_contact = State()  # data: email клиента, к которому привязываем контакт
    broadcast = State()     # data: bc_chat, bc_msg — сообщение для рассылки всем


def btn(text: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb)


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_kb() -> InlineKeyboardMarkup:
    return kb([[btn("Отмена", "adm:cancel")]])


async def _edit(cq: CallbackQuery, text: str, markup=None) -> None:
    try:
        await cq.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            pass
        else:
            raise


async def _answer(cq: CallbackQuery) -> None:
    try:
        await cq.answer()
    except Exception:
        pass  # повторный answer() того же callback не страшен


async def _admin(cq: CallbackQuery) -> bool:
    u = await db.get_user(cq.from_user.id)
    if not u or not u[2]:
        await db.audit(cq.from_user.id, "deny_admin_ui", cq.data or "")
        await cq.answer("Нет доступа.", show_alert=True)
        return False
    return True


async def _head(cq: CallbackQuery):
    """Строка пользователя, если он админ или глава семьи."""
    u = await db.get_user(cq.from_user.id)
    if not u or not (u[2] or u[4]):
        await db.audit(cq.from_user.id, "deny_head_ui", cq.data or "")
        await cq.answer("Нет доступа.", show_alert=True)
        return None
    return u


def _paginate(items, page: int, per: int = PAGE):
    pages = max(1, (len(items) + per - 1) // per)
    page = min(max(0, page), pages - 1)
    return items[page * per:(page + 1) * per], page, pages


def _nav(prefix: str, page: int, pages: int) -> list:
    row = []
    if pages > 1:
        if page > 0:
            row.append(btn("«", f"{prefix}{page - 1}"))
        row.append(btn(f"{page + 1}/{pages}", f"{prefix}{page}"))
        if page < pages - 1:
            row.append(btn("»", f"{prefix}{page + 1}"))
    return row


# ---------- корень админки ----------

def root_markup() -> InlineKeyboardMarkup:
    return kb(
        [
            [btn("Клиенты", "adm:cl:0"), btn("Семьи", "adm:fms")],
            [btn("Инвайты", "adm:iv"), btn("Аудит", "adm:au")],
            [btn("Рассылка всем", "adm:bc")],
            [btn("Синхронизировать с панелью", "adm:sy")],
        ]
    )


ROOT_TEXT = "Админка. Выбирай раздел — всё управление здесь, команды необязательны."


@router.callback_query(F.data == "adm")
async def ui_root(cq: CallbackQuery) -> None:
    if not await _admin(cq):
        return
    await _edit(cq, ROOT_TEXT, root_markup())
    await _answer(cq)


# ---------- клиенты ----------

@router.callback_query(F.data.regexp(r"^adm:cl:\d+$"))
async def ui_clients(cq: CallbackQuery) -> None:
    if not await _admin(cq):
        return
    page = int(cq.data.split(":")[2])
    rows_all = await db.all_clients()
    part, page, pages = _paginate(rows_all, page)
    lines, buttons = [], []
    for email, family, head, bound in part:
        tag = ""
        if family:
            tag += f" [{family}]" + ("★" if head else "")
        if bound:
            tag += " ✓"
        lines.append(email + tag)
        buttons.append([btn(email, f"adm:c:{email}")])
    nav = _nav("adm:cl:", page, pages)
    if nav:
        buttons.append(nav)
    buttons.append([btn("Назад", "adm")])
    header = "Клиенты панели (✓ — привязан к боту, ★ — глава семьи):\n" + "\n".join(lines)
    await _edit(cq, header, kb(buttons))
    await _answer(cq)


async def _render_client_card(cq: CallbackQuery, email: str, from_family: bool = False) -> None:
    cl = await db.get_client(email)
    if not cl:
        await _edit(cq, "Нет в реестре.", kb([[btn("« К списку", "adm:cl:0")]]))
        await _answer(cq)
        return
    _e, family, head = cl
    bound = await db.get_user_by_email(email)
    subid, _err = await subid_for(email)
    fam_txt = f"{family} (глава)" if family and head else (family or "Без семьи")
    if not bound:
        tg_txt = "не привязан"
    elif bound[1]:
        tg_txt = f"@{bound[1]} (tg_id {bound[0]})"
    else:
        tg_txt = f"tg_id {bound[0]} (без @username)"
    text = (
        f"{email}\n"
        f"subId: {subid or '—'}\n"
        f"Семья: {fam_txt}\n"
        f"Telegram: {tg_txt}"
    )
    if from_family:
        back_cb = f"adm:fm:{family}" if family else NONE_CB
        back_label = "« К семье"
    else:
        back_cb, back_label = "adm:cl:0", "« К списку"
    rows = [
        [btn("Пригласить", f"adm:inv:{email}"), btn("Семья…", f"adm:cf:{email}")],
        [btn("Привязать…", f"adm:bd:{email}")],
    ]
    if family:
        rows.append([btn("Снять главу" if head else "Сделать главой", f"adm:fh:{email}"),
                     btn("Убрать из семьи", f"adm:fr:{email}")])
    rows.append([btn(back_label, back_cb)])
    await _edit(cq, text, kb(rows))
    await _answer(cq)


@router.callback_query(F.data.regexp(r"^adm:c:[^:]+$"))
async def ui_client_card(cq: CallbackQuery) -> None:
    if not await _admin(cq):
        return
    await _render_client_card(cq, cq.data.split(":", 2)[2], from_family=False)


# ---------- инвайты ----------

@router.callback_query(F.data.regexp(r"^adm:inv:[^:]+$"))
async def ui_invite(cq: CallbackQuery) -> None:
    if not await _admin(cq):
        return
    email = cq.data.split(":", 2)[2]
    code = secrets.token_urlsafe(9)
    await db.create_invite(code, email, cq.from_user.id, INVITE_TTL_DAYS)
    await db.audit(cq.from_user.id, "invite_create", email)
    await cq.answer("Инвайт создан.")
    await _edit(
        cq,
        f"Инвайт для {email} (одноразовый, живёт {INVITE_TTL_DAYS} дн.):\n\n"
        f"https://t.me/{BOT_NAME}?start={code}\n\n"
        "Перешли человеку эту ссылку — лучше через Telegram.",
        kb([[btn("« К карточке", f"adm:fc:{email}")]]),
    )
    await _answer(cq)


async def _render_invites(cq: CallbackQuery) -> None:
    rows = await db.active_invites()
    if not rows:
        await _edit(cq, "Активных инвайтов нет. Создать: карточка клиента → «Пригласить».",
                    kb([[btn("Назад", "adm")]]))
        await _answer(cq)
        return
    buttons = []
    for code, email, exp in rows:
        buttons.append([btn(f"{email} (до {fmt_ts(exp)})", "adm:noop")])
        buttons.append([btn("Отозвать", f"adm:rv:{code}")])
    buttons.append([btn("Назад", "adm")])
    await _edit(cq, "Активные инвайты:", kb(buttons))
    await _answer(cq)


@router.callback_query(F.data == "adm:iv")
async def ui_invites(cq: CallbackQuery) -> None:
    if not await _admin(cq):
        return
    await _render_invites(cq)


@router.callback_query(F.data == "adm:noop")
async def ui_noop(cq: CallbackQuery) -> None:
    await _answer(cq)


@router.callback_query(F.data.regexp(r"^adm:rv:[A-Za-z0-9_-]+$"))
async def ui_revoke(cq: CallbackQuery) -> None:
    if not await _admin(cq):
        return
    code = cq.data.split(":", 2)[2]
    await db.delete_invite(code)
    await db.audit(cq.from_user.id, "invite_revoke", code)
    await cq.answer("Инвайт отозван.")
    await _render_invites(cq)


# ---------- семьи (админ) ----------

NONE_CB = "adm:fnone"  # виртуальная группа «Без семьи» (зарезервировано, не имя семьи)


@router.callback_query(F.data == "adm:fms")
async def ui_families(cq: CallbackQuery) -> None:
    if not await _admin(cq):
        return
    fams = await db.family_names()
    free_n = len(await db.unassigned_clients())
    buttons = [[btn(f"{name} ({count})", f"adm:fm:{name}")] for name, count in fams]
    buttons.append([btn(f"Без семьи ({free_n})", NONE_CB)])
    buttons.append([btn("Назад", "adm")])
    await _edit(cq, "Семьи (открывай — внутри люди кнопками):", kb(buttons))
    await _answer(cq)


async def _render_family_card(cq: CallbackQuery, name: str) -> None:
    """Состав семьи: люди — кнопками, действия — в карточке человека."""
    members = await db.family_members(name)
    if not members:
        await _edit(cq, f"Семья «{name}» пуста.", kb([[btn("Назад", "adm:fms")]]))
        await _answer(cq)
        return
    buttons = []
    for email, head, tg_id, _un in members:
        label = email + (" ★" if head else "") + (" ✓" if tg_id else "")
        buttons.append([btn(label, f"adm:fc:{email}")])
    buttons.append([btn("Добавить члена", f"adm:fa:{name}:0")])
    buttons.append([btn("Назад", "adm:fms")])
    await _edit(cq, f"Семья «{name}» ({len(members)}):", kb(buttons))
    await _answer(cq)


@router.callback_query(F.data.regexp(r"^adm:fm:[A-Za-z0-9_-]+$"))
async def ui_family_card(cq: CallbackQuery) -> None:
    if not await _admin(cq):
        return
    await _render_family_card(cq, cq.data.split(":", 2)[2])


async def _render_unassigned(cq: CallbackQuery, page: int) -> None:
    free = await db.unassigned_clients_info()
    part, page, pages = _paginate(free, page)
    buttons = [[btn(e + (" ✓" if bound else ""), f"adm:fc:{e}")] for e, bound in part]
    nav = _nav("adm:fnone:", page, pages)
    if nav:
        buttons.append(nav)
    buttons.append([btn("Назад", "adm:fms")])
    await _edit(cq, f"Без семьи ({len(free)}) — сюда попадают новые и убранные из семей:", kb(buttons))
    await _answer(cq)


@router.callback_query(F.data.regexp(r"^adm:fnone(:\d+)?$"))
async def ui_unassigned(cq: CallbackQuery) -> None:
    if not await _admin(cq):
        return
    page = int(cq.data.split(":")[-1]) if cq.data != NONE_CB else 0
    await _render_unassigned(cq, page)


async def _render_family_add(cq: CallbackQuery, name: str, page: int) -> None:
    free = await db.unassigned_clients_info()
    part, page, pages = _paginate(free, page)
    buttons = [[btn(e + (" ✓" if bound else ""), f"adm:fa:{name}:a:{e}")] for e, bound in part]
    nav = _nav(f"adm:fa:{name}:", page, pages)
    if nav:
        buttons.append(nav)
    buttons.append([btn("Назад", f"adm:fm:{name}")])
    await _edit(cq, f"Кого зачислить в «{name}»? (без семьи: {len(free)})", kb(buttons))
    await _answer(cq)


@router.callback_query(F.data.regexp(r"^adm:fa:[A-Za-z0-9_-]+:\d+$"))
async def ui_family_add_list(cq: CallbackQuery) -> None:
    if not await _admin(cq):
        return
    parts = cq.data.split(":")
    await _render_family_add(cq, parts[2], int(parts[3]))


@router.callback_query(F.data.regexp(r"^adm:fa:[A-Za-z0-9_-]+:a:[^:]+\Z"))
async def ui_family_add(cq: CallbackQuery) -> None:
    if not await _admin(cq):
        return
    parts = cq.data.split(":")
    name = parts[2]
    email = parts[4]
    await db.set_family(email, name, head=None)
    await db.audit(cq.from_user.id, "family_set", f"{email} -> {name} (ui)")
    await cq.answer(f"{email} зачислен в «{name}».")
    await _render_family_add(cq, name, 0)


# ---------- карточка человека из семьи (adm:fc:) ----------

@router.callback_query(F.data.regexp(r"^adm:fc:[^:]+\Z"))
async def ui_family_member_card(cq: CallbackQuery) -> None:
    """Карточка человека с контекстом семьи: «Назад» ведёт в его текущую семью."""
    if not await _admin(cq):
        return
    await _render_client_card(cq, cq.data.split(":", 2)[2], from_family=True)


@router.callback_query(F.data.regexp(r"^adm:fh:[^:]+\Z"))
async def ui_toggle_head(cq: CallbackQuery) -> None:
    if not await _admin(cq):
        return
    email = cq.data.split(":", 2)[2]
    cl = await db.get_client(email)
    if not cl or not cl[1]:
        await cq.answer("Сначала зачислите человека в семью.", show_alert=True)
        return
    await db.set_family(email, cl[1], head=not cl[2])
    await db.audit(cq.from_user.id, "family_head_toggle", f"{email} in {cl[1]}")
    await cq.answer("Готово.")
    await _render_client_card(cq, email, from_family=True)


@router.callback_query(F.data.regexp(r"^adm:fr:[^:]+\Z"))
async def ui_remove_from_family(cq: CallbackQuery) -> None:
    if not await _admin(cq):
        return
    email = cq.data.split(":", 2)[2]
    cl = await db.get_client(email)
    if not cl or not cl[1]:
        await cq.answer("Человек и так без семьи.")
        return
    await db.clear_family(email)
    await db.audit(cq.from_user.id, "family_clear", f"{email} <- {cl[1]}")
    await cq.answer(f"{email} убран в «Без семьи».")
    await _render_client_card(cq, email, from_family=True)


# ---------- назначение семьи из карточки клиента ----------

async def _render_client_family(cq: CallbackQuery, email: str) -> None:
    cl = await db.get_client(email)
    fams = await db.family_names()
    cur = cl[1] if cl else None
    buttons = []
    for name, count in fams:
        mark = "✓ " if name == cur else ""
        buttons.append([btn(f"{mark}{name} ({count})", f"adm:cf:{email}:{name}")])
    buttons.append([btn("Убрать из семьи", f"adm:cf:{email}:-")])
    buttons.append([btn("Новая семья…", f"adm:fn:{email}")])
    buttons.append([btn("« К карточке", f"adm:fc:{email}")])
    await _edit(cq, f"Семья для {email} (сейчас: {cur or '—'}):", kb(buttons))
    await _answer(cq)


@router.callback_query(F.data.regexp(r"^adm:cf:[^:]+\Z"))
async def ui_client_family(cq: CallbackQuery) -> None:
    if not await _admin(cq):
        return
    await _render_client_family(cq, cq.data.split(":", 2)[2])


@router.callback_query(F.data.regexp(r"^adm:cf:[^:]+:[A-Za-z0-9_-]+\Z"))
async def ui_client_family_set(cq: CallbackQuery) -> None:
    if not await _admin(cq):
        return
    parts = cq.data.split(":")
    email = parts[2]
    fam = ":".join(parts[3:])
    if fam == "-":
        await db.clear_family(email)
        await db.audit(cq.from_user.id, "family_clear", f"{email} (ui)")
        await cq.answer(f"{email} убран из семьи.")
    else:
        await db.set_family(email, fam, head=None)
        await db.audit(cq.from_user.id, "family_set", f"{email} -> {fam} (ui)")
        await cq.answer(f"{email} → «{fam}».")
    await _render_client_family(cq, email)


@router.callback_query(F.data.regexp(r"^adm:fn:[^:]+\Z"))
async def ui_new_family_prompt(cq: CallbackQuery, state: FSMContext) -> None:
    if not await _admin(cq):
        return
    email = cq.data.split(":", 2)[2]
    await state.set_state(UI.family_name)
    await state.update_data(email=email)
    await _edit(
        cq,
        f"Введите имя новой семьи для {email} — латиница, цифры, _ и - (до 24 символов).",
        cancel_kb(),
    )
    await _answer(cq)


@router.message(StateFilter(UI.family_name))
async def ui_new_family_input(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    email = data.get("email", "")
    name = (m.text or "").strip()
    if not FAMILY_RE.match(name) or name == "fnone":
        await m.answer(
            "Имя семьи: латиница, цифры, _ и -, до 24 символов (имя «fnone» зарезервировано). "
            "Попробуйте ещё раз через карточку клиента."
        )
        return
    await db.set_family(email, name, head=None)
    await db.audit(m.from_user.id, "family_set", f"{email} -> {name} (ui,new)")
    await m.answer(f"Создана семья «{name}», {email} зачислен.")


# ---------- привязка телеграма из карточки ----------

@router.callback_query(F.data.regexp(r"^adm:bd:[^:]+\Z"))
async def ui_bind_prompt(cq: CallbackQuery, state: FSMContext) -> None:
    if not await _admin(cq):
        return
    email = cq.data.split(":", 2)[2]
    await state.set_state(UI.bind_contact)
    await state.update_data(email=email)
    await _edit(
        cq,
        f"Привязка к {email}. Ответьте сообщением:\n"
        "@username — бот привяжет при первом сообщении человека;\n"
        "или числовой tg_id (узнаётся у @userinfobot) — привязка сразу.",
        cancel_kb(),
    )
    await _answer(cq)


@router.message(StateFilter(UI.bind_contact))
async def ui_bind_input(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    email = data.get("email", "")
    contact = (m.text or "").strip()
    if not contact:
        await m.answer("Пусто. Попробуйте снова через карточку клиента.")
        return
    if contact.lstrip("@").isdigit():
        await db.bind_user(int(contact.lstrip("@")), email, None)
        await db.audit(m.from_user.id, "bind_direct", f"{contact}->{email} (ui)")
        await m.answer(f"Привязан tg_id {contact} к {email}.")
    else:
        await db.add_pending_bind(contact, email, m.from_user.id)
        await db.audit(m.from_user.id, "bind_pending_create", f"{contact}->{email} (ui)")
        await m.answer(f"Запомнил: @{contact.lstrip('@')} привяжется к {email}, когда напишет боту.")


# ---------- массовая рассылка (только админ) ----------

@router.callback_query(F.data == "adm:bc")
async def ui_broadcast_prompt(cq: CallbackQuery, state: FSMContext) -> None:
    if not await _admin(cq):
        return
    n = len(await db.all_bound_tg_ids())
    await state.set_state(UI.broadcast)
    await _edit(
        cq,
        f"Рассылка всем привязанным пользователям ({n} человек).\n"
        "Пришлите сообщение для рассылки — текст или фото с подписью.",
        cancel_kb(),
    )
    await _answer(cq)


@router.message(StateFilter(UI.broadcast), F.text | F.photo)
async def ui_broadcast_hold(m: Message, state: FSMContext) -> None:
    n = len(await db.all_bound_tg_ids())
    await state.update_data(bc_chat=m.chat.id, bc_msg=m.message_id)
    await m.answer(
        f"Принято. Разослать это сообщение {n} пользователям?",
        reply_markup=kb([[btn("Разослать", "adm:bcsend")], [btn("Отмена", "adm:cancel")]]),
    )


@router.message(StateFilter(UI.broadcast))
async def ui_broadcast_bad(m: Message) -> None:
    await m.answer("Поддерживаются текст и фото с подписью. Пришлите ещё раз — или отмените кнопкой выше.")


@router.callback_query(F.data == "adm:bcsend")
async def ui_broadcast_send(cq: CallbackQuery, state: FSMContext) -> None:
    if not await _admin(cq):
        return
    data = await state.get_data()
    await state.clear()
    bc_chat, bc_msg = data.get("bc_chat"), data.get("bc_msg")
    if not bc_chat or not bc_msg:
        await _edit(cq, "Сообщение потерялось — начните рассылку заново.", root_markup())
        await _answer(cq)
        return
    ids = await db.all_bound_tg_ids()
    sent, failed = 0, []
    for tg_id in ids:
        try:
            await cq.bot.copy_message(chat_id=tg_id, from_chat_id=bc_chat, message_id=bc_msg)
            sent += 1
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await cq.bot.copy_message(chat_id=tg_id, from_chat_id=bc_chat, message_id=bc_msg)
                sent += 1
            except Exception:
                failed.append(tg_id)
        except Exception:
            failed.append(tg_id)
    await db.audit(cq.from_user.id, "broadcast", f"sent={sent} failed={len(failed)}")
    text = f"Рассылка завершена: доставлено {sent} из {len(ids)}."
    if failed:
        text += "\nНе доставлено (вероятно, заблокировали бота): " + ", ".join(str(i) for i in failed[:10])
    await _edit(cq, text, root_markup())
    await _answer(cq)


# ---------- аудит и синхронизация ----------

@router.callback_query(F.data == "adm:au")
async def ui_audit(cq: CallbackQuery) -> None:
    if not await _admin(cq):
        return
    rows = await db.recent_audit(20)
    if not rows:
        text = "Аудит пуст."
    else:
        text = "Последние действия:\n" + "\n".join(
            f"{fmt_ts(ts)}  tg={tg}  {action} {detail}" for ts, tg, action, detail in rows
        )
    await _edit(cq, text, kb([[btn("Назад", "adm")]]))
    await _answer(cq)


@router.callback_query(F.data == "adm:sy")
async def ui_sync(cq: CallbackQuery) -> None:
    if not await _admin(cq):
        return
    try:
        emails = await panel.all_client_emails()
    except PanelError:
        await _edit(cq, "Панель недоступна, попробуйте позже.", kb([[btn("Назад", "adm")]]))
        await _answer(cq)
        return
    for e in sorted(emails):
        await db.ensure_client(e)
    await db.audit(cq.from_user.id, "sync", f"{len(emails)} клиентов (ui)")
    await cq.answer(f"Синхронизировано: {len(emails)} клиентов.")
    await _edit(cq, f"Синхронизировано: {len(emails)} клиентов из панели.", kb([[btn("Назад", "adm")]]))
    await _answer(cq)


# ---------- отмена любого FSM-ввода ----------

@router.callback_query(F.data == "adm:cancel")
async def ui_cancel(cq: CallbackQuery, state: FSMContext) -> None:
    cur = await state.get_state()
    await state.clear()
    if cur:
        if await _admin(cq):
            await _edit(cq, "Отменено.", root_markup())
        else:
            await _edit(cq, "Отменено.")
    await _answer(cq)


# ---------- семейный вид для «Моя семья» (общий с main.py) ----------

async def render_family_view(family: str, viewer_email: str, is_admin: bool) -> tuple[str, InlineKeyboardMarkup]:
    """«Моя семья»: люди — кнопками, действия — в карточке человека (hfc:/adm:fc:)."""
    members = await db.family_members(family)
    buttons = []
    for email, head, tg_id, _un in members:
        label = email + (" ★" if head else "") + (" ✓" if tg_id else "")
        target = f"adm:fc:{email}" if is_admin else f"hfc:{email}"
        buttons.append([btn(label, target)])
    buttons.append([btn("➕ Добавить члена", "hd:a:0")])
    footer = ""
    if not is_admin:
        footer = (
            "\n\nТапни человека — в карточке: ссылка, QR, пригласить, убрать из семьи. "
            "«Добавить члена» — из числа нераспределённых."
        )
    return f"Семья «{family}» ({len(members)}):", kb(buttons)


# ---------- управление семьёй для глав (hd:) ----------

async def _render_head_add(cq: CallbackQuery, my_family: str, page: int) -> None:
    free = await db.unassigned_clients()
    part, page, pages = _paginate(free, page)
    buttons = [[btn(e, f"hd:a:{e}")] for e in part]
    nav = _nav("hd:a:", page, pages)
    if nav:
        buttons.append(nav)
    buttons.append([btn("« К семье", "hd:view")])
    await _edit(cq, f"Кого зачислить в «{my_family}»? (без семьи: {len(free)})", kb(buttons))
    await _answer(cq)


@router.callback_query(F.data.regexp(r"^hd:a:\d+$"))
async def ui_head_add_list(cq: CallbackQuery) -> None:
    u = await _head(cq)
    if not u:
        return
    _me, _un, is_admin, my_family, _hd = u
    if not my_family:
        await cq.answer("Вам не назначена семья.", show_alert=True)
        return
    await _render_head_add(cq, my_family, int(cq.data.split(":")[2]))


@router.callback_query(F.data.regexp(r"^hd:a:[^:]+\Z"))
async def ui_head_add(cq: CallbackQuery) -> None:
    u = await _head(cq)
    if not u:
        return
    _me, _un, is_admin, my_family, _hd = u
    email = cq.data.split(":", 2)[2]
    if not my_family:
        await cq.answer("Вам не назначена семья.", show_alert=True)
        return
    cl = await db.get_client(email)
    if cl and cl[2] and cl[1] != my_family:
        await cq.answer("Это глава другой семьи — перемещать глав может только админ.", show_alert=True)
        return
    await db.set_family(email, my_family, head=None)
    await db.audit(cq.from_user.id, "family_set", f"{email} -> {my_family} (head-ui)")
    await cq.answer(f"{email} зачислен в «{my_family}».")
    await _render_head_add(cq, my_family, 0)


@router.callback_query(F.data.regexp(r"^hfc:[^:]+\Z"))
async def ui_head_member_card(cq: CallbackQuery) -> None:
    """Карточка члена семьи для главы: ссылка, QR, убрать (своей семьи, не главу, не себя)."""
    u = await _head(cq)
    if not u:
        return
    me, _un, is_admin, my_family, _hd = u
    email = cq.data.split(":")[1]
    cl = await db.get_client(email)
    if not cl or cl[1] != my_family:
        await cq.answer("Это не член вашей семьи.", show_alert=True)
        return
    bound = await db.get_user_by_email(email)
    subid, _err = await subid_for(email)
    lines = [email, "★ глава семьи" if cl[2] else "член семьи",
             "привязан к боту" if bound else "не привязан к боту"]
    if subid:
        lines.append(f"subId: {subid}")
    rows = [[btn("Ссылка", f"fl:{email}"), btn("QR", f"fq:{email}")]]
    rows.append([btn("Пригласить", f"hinv:{email}")])
    if email != me and not cl[2]:
        rows.append([btn("Убрать из семьи", f"hdrm:{email}")])
    rows.append([btn("« К моей семье", "hd:view")])
    await _edit(cq, "\n".join(lines), kb(rows))
    await _answer(cq)


@router.callback_query(F.data.regexp(r"^hinv:[^:]+\Z"))
async def ui_head_invite(cq: CallbackQuery) -> None:
    """Глава создаёт инвайт — только для члена своей семьи."""
    u = await _head(cq)
    if not u:
        return
    _me, _un, is_admin, my_family, _hd = u
    email = cq.data.split(":")[1]
    cl = await db.get_client(email)
    if not cl or cl[1] != my_family:
        await cq.answer("Можно приглашать только членов своей семьи.", show_alert=True)
        return
    code = secrets.token_urlsafe(9)
    await db.create_invite(code, email, cq.from_user.id, INVITE_TTL_DAYS)
    await db.audit(cq.from_user.id, "invite_create", f"{email} (head)")
    await cq.answer("Инвайт создан.")
    await _edit(
        cq,
        f"Инвайт для {email} (одноразовый, живёт {INVITE_TTL_DAYS} дн.):\n\n"
        f"https://t.me/{BOT_NAME}?start={code}\n\n"
        "Перешли человеку эту ссылку — лучше через Telegram.",
        kb([[btn("« К карточке", f"hfc:{email}")]]),
    )
    await _answer(cq)


@router.callback_query(F.data.regexp(r"^hd:r:[^:]+\Z"))
@router.callback_query(F.data.regexp(r"^hdrm:[^:]+\Z"))
async def ui_head_remove(cq: CallbackQuery) -> None:
    u = await _head(cq)
    if not u:
        return
    me, _un, is_admin, my_family, _hd = u
    email = cq.data.split(":")[-1]
    if not my_family:
        await cq.answer("Вам не назначена семья.", show_alert=True)
        return
    cl = await db.get_client(email)
    if not cl or cl[1] != my_family:
        await cq.answer("Можно убирать только из своей семьи.", show_alert=True)
        return
    if cl[2]:
        await cq.answer("Главу может убирать только админ.", show_alert=True)
        return
    await db.clear_family(email)
    await db.audit(cq.from_user.id, "family_clear", f"{email} <- {my_family} (head-ui)")
    await cq.answer(f"{email} убран из «{my_family}».")
    text, markup = await render_family_view(my_family, me, is_admin)
    await _edit(cq, text, markup)
    await _answer(cq)


@router.callback_query(F.data == "hd:view")
async def ui_head_view(cq: CallbackQuery) -> None:
    u = await _head(cq)
    if not u:
        return
    me, _un, is_admin, my_family, _hd = u
    if not my_family:
        await cq.answer("Вам не назначена семья.", show_alert=True)
        return
    text, markup = await render_family_view(my_family, me, is_admin)
    await _edit(cq, text, markup)
    await _answer(cq)
