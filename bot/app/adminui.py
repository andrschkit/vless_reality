"""Инлайн-интерфейсы: «Админка» и управление семьёй для глав.

Все экраны редактируют одно сообщение (навигация «Назад»), списки длиннее
8 позиций разбиты на страницы. Ручной ввод нужен только в двух местах:
имя новой семьи и контакт для привязки (FSM с кнопкой «Отмена»).

ВАЖНО: рендеры вынесены в функции с ЯВНЫМИ аргументами (email/name/family).
Нельзя вызывать «соседний» хендлер, передавая чужой callback_data —
парсинг адреса экрана из неё даст мусор (баг с «Назад» после перемещения).
"""
import re
import secrets

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
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


async def _render_client_card(cq: CallbackQuery, email: str) -> None:
    cl = await db.get_client(email)
    if not cl:
        await _edit(cq, "Нет в реестре.", kb([[btn("« К списку", "adm:cl:0")]]))
        await _answer(cq)
        return
    _e, family, head = cl
    bound = await db.get_user_by_email(email)
    subid, _err = await subid_for(email)
    fam_txt = f"{family} (глава)" if family and head else (family or "—")
    tg_txt = f"@{bound[1]} (tg_id {bound[0]})" if bound else "не привязан"
    text = (
        f"{email}\n"
        f"subId: {subid or '—'}\n"
        f"Семья: {fam_txt}\n"
        f"Telegram: {tg_txt}"
    )
    markup = kb(
        [
            [btn("Пригласить", f"adm:inv:{email}"), btn("Семья…", f"adm:cf:{email}")],
            [btn("Привязать…", f"adm:bd:{email}")],
            [btn("« К списку", "adm:cl:0")],
        ]
    )
    await _edit(cq, text, markup)
    await _answer(cq)


@router.callback_query(F.data.regexp(r"^adm:c:[^:]+$"))
async def ui_client_card(cq: CallbackQuery) -> None:
    if not await _admin(cq):
        return
    await _render_client_card(cq, cq.data.split(":", 2)[2])


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
        kb([[btn("« К карточке", f"adm:c:{email}")]]),
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

@router.callback_query(F.data == "adm:fms")
async def ui_families(cq: CallbackQuery) -> None:
    if not await _admin(cq):
        return
    fams = await db.family_names()
    if not fams:
        await _edit(
            cq,
            "Семей пока нет. Создать: карточка клиента → «Семья…» → «Новая семья…».",
            kb([[btn("Назад", "adm")]]),
        )
        await _answer(cq)
        return
    buttons = [[btn(f"{name} ({count})", f"adm:fm:{name}")] for name, count in fams]
    buttons.append([btn("Назад", "adm")])
    await _edit(cq, "Семьи:", kb(buttons))
    await _answer(cq)


async def _render_family_card(cq: CallbackQuery, name: str) -> None:
    members = await db.family_members(name)
    if not members:
        await _edit(cq, f"Семья «{name}» пуста или не существует.", kb([[btn("Назад", "adm:fms")]]))
        await _answer(cq)
        return
    lines, buttons = [], []
    for email, head, tg_id, _un in members:
        tag = " (глава)" if head else ""
        tag += "" if tg_id else " — не привязан"
        lines.append(email + tag)
        buttons.append(
            [
                btn("снять главу" if head else "сделать главой", f"adm:fm:{name}:h:{email}"),
                btn("убрать", f"adm:fm:{name}:r:{email}"),
            ]
        )
    buttons.append([btn("Добавить члена", f"adm:fa:{name}:0")])
    buttons.append([btn("Назад", "adm:fms")])
    await _edit(cq, f"Семья «{name}»:\n" + "\n".join(lines), kb(buttons))
    await _answer(cq)


@router.callback_query(F.data.regexp(r"^adm:fm:[A-Za-z0-9_-]+$"))
async def ui_family_card(cq: CallbackQuery) -> None:
    if not await _admin(cq):
        return
    await _render_family_card(cq, cq.data.split(":", 2)[2])


@router.callback_query(F.data.regexp(r"^adm:fm:[A-Za-z0-9_-]+:[hr]:[^:]+\Z"))
async def ui_family_member_op(cq: CallbackQuery) -> None:
    if not await _admin(cq):
        return
    parts = cq.data.split(":")
    name = parts[2]
    op = parts[3]
    email = parts[4]
    if op == "h":
        cur = await db.get_client(email)
        await db.set_family(email, name, head=not (cur and cur[2]))
        await db.audit(cq.from_user.id, "family_head_toggle", f"{email} in {name}")
    else:
        await db.clear_family(email)
        await db.audit(cq.from_user.id, "family_clear", f"{email} <- {name}")
    await cq.answer("Готово.")
    await _render_family_card(cq, name)


async def _render_family_add(cq: CallbackQuery, name: str, page: int) -> None:
    free = await db.unassigned_clients()
    part, page, pages = _paginate(free, page)
    buttons = [[btn(e, f"adm:fa:{name}:a:{e}")] for e in part]
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
    buttons.append([btn("« К карточке", f"adm:c:{email}")])
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
    if not FAMILY_RE.match(name):
        await m.answer(
            "Имя семьи: латиница, цифры, _ и -, до 24 символов. Попробуйте ещё раз через карточку клиента."
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
    members = await db.family_members(family)
    lines, buttons = [], []
    for email, head, tg_id, _un in members:
        tag = " (глава)" if head else ""
        tag += "" if tg_id else " — не привязан"
        lines.append(email + tag)
        if email != viewer_email:
            buttons.append([btn(f"{email}: ссылка", f"fl:{email}"), btn("QR", f"fq:{email}")])
            if not is_admin and not head:
                buttons.append([btn(f"✕ убрать {email}", f"hd:r:{email}")])
    buttons.append([btn("➕ Добавить члена", "hd:a:0")])
    footer = ""
    if not is_admin:
        footer = (
            "\n\nВы глава: «Добавить члена» — из числа нераспределённых; "
            "✕ — убрать из семьи. Командами: /family <email> <моя семья>|-."
        )
    return f"Семья «{family}»:\n" + "\n".join(lines) + footer, kb(buttons)


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


@router.callback_query(F.data.regexp(r"^hd:r:[^:]+\Z"))
async def ui_head_remove(cq: CallbackQuery) -> None:
    u = await _head(cq)
    if not u:
        return
    me, _un, is_admin, my_family, _hd = u
    email = cq.data.split(":", 2)[2]
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
