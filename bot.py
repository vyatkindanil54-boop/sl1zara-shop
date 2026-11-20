"""
Telegram-магазин с ручной выдачей товара.
aiogram 3.x + SQLite.

Возможности:
  Покупатель: каталог, карточка товара, заказ, «Я оплатил», мои заказы, поддержка.
  Админ (/admin): добавить / изменить / скрыть / удалить товар, заказы на проверке,
  подтверждение оплаты и ручная выдача (текст, файл, фото — что угодно),
  отклонение заказа, реквизиты оплаты, рассылка, статистика.
"""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from html import escape

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "")  # без @
CURRENCY = os.getenv("CURRENCY", "₽")
DB_PATH = os.getenv("DB_PATH", "shop.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ─────────────────────────── База данных ───────────────────────────


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                price REAL NOT NULL,
                active INTEGER DEFAULT 1,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id INTEGER,
                product_name TEXT,
                price REAL,
                status TEXT DEFAULT 'new',
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def get_setting(key: str, default: str = "") -> str:
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def upsert_user(m: Message) -> None:
    u = m.from_user
    with db() as c:
        c.execute(
            "INSERT INTO users(id, username, first_name, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
            (u.id, u.username, u.first_name, now()),
        )


def fmt_price(p: float) -> str:
    return f"{int(p) if float(p).is_integer() else p} {CURRENCY}"


STATUS_LABELS = {
    "new": "🕓 Ожидает оплаты",
    "paid": "🔎 Проверяем оплату",
    "done": "✅ Выдан",
    "rejected": "❌ Отклонён",
    "cancelled": "🚫 Отменён",
}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ─────────────────────────── Состояния ───────────────────────────


class AddProduct(StatesGroup):
    name = State()
    description = State()
    price = State()


class EditProduct(StatesGroup):
    value = State()


class Deliver(StatesGroup):
    content = State()


class RejectOrder(StatesGroup):
    reason = State()


class Broadcast(StatesGroup):
    content = State()
    confirm = State()


class PaymentInfo(StatesGroup):
    text = State()


# ─────────────────────────── Клавиатуры ───────────────────────────

BTN_CATALOG = "🛍 Каталог"
BTN_ORDERS = "📦 Мои заказы"
BTN_SUPPORT = "💬 Поддержка"
BTN_ADMIN = "⚙️ Админ-панель"


def main_kb(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_CATALOG)],
        [KeyboardButton(text=BTN_ORDERS), KeyboardButton(text=BTN_SUPPORT)],
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def admin_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить товар", callback_data="adm:add")
    b.button(text="📋 Товары", callback_data="adm:products")
    b.button(text="🔎 Заказы на проверке", callback_data="adm:pending")
    b.button(text="💳 Реквизиты оплаты", callback_data="adm:payinfo")
    b.button(text="📣 Рассылка", callback_data="adm:broadcast")
    b.button(text="📊 Статистика", callback_data="adm:stats")
    b.adjust(1, 2, 1, 2)
    return b.as_markup()


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="adm:cancel")]]
    )


def admin_order_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Оплата есть — выдать", callback_data=f"ord:deliver:{order_id}")],
            [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ord:reject:{order_id}")],
        ]
    )


# ─────────────────────────── Покупатель ───────────────────────────

user_router = Router()


@user_router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    upsert_user(m)
    welcome = get_setting(
        "welcome",
        "👋 Добро пожаловать в магазин!\n\nВыберите товар в каталоге, оформите заказ и оплатите — "
        "после проверки оплаты товар придёт сюда, в этот чат.",
    )
    await m.answer(welcome, reply_markup=main_kb(m.from_user.id))


@user_router.message(F.text == BTN_CATALOG)
@user_router.message(Command("catalog"))
async def show_catalog(m: Message):
    await send_catalog(m)


async def send_catalog(target: Message, edit: bool = False):
    with db() as c:
        items = c.execute("SELECT * FROM products WHERE active=1 ORDER BY id").fetchall()
    if not items:
        text, kb = "Каталог пока пуст. Загляните позже 🙂", None
    else:
        b = InlineKeyboardBuilder()
        for p in items:
            b.button(text=f"{p['name']} — {fmt_price(p['price'])}", callback_data=f"prod:{p['id']}")
        b.adjust(1)
        text, kb = "🛍 <b>Каталог</b>\n\nВыберите товар:", b.as_markup()
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@user_router.callback_query(F.data == "catalog")
async def cb_catalog(cq: CallbackQuery):
    await send_catalog(cq.message, edit=True)
    await cq.answer()


@user_router.callback_query(F.data.startswith("prod:"))
async def cb_product(cq: CallbackQuery):
    pid = int(cq.data.split(":")[1])
    with db() as c:
        p = c.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        await cq.answer("Товар недоступен", show_alert=True)
        return
    text = f"<b>{escape(p['name'])}</b>\n\n"
    if p["description"]:
        text += f"{escape(p['description'])}\n\n"
    text += f"💰 Цена: <b>{fmt_price(p['price'])}</b>"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Купить", callback_data=f"buy:{pid}")],
            [InlineKeyboardButton(text="« Назад", callback_data="catalog")],
        ]
    )
    await cq.message.edit_text(text, reply_markup=kb)
    await cq.answer()


@user_router.callback_query(F.data.startswith("buy:"))
async def cb_buy(cq: CallbackQuery):
    pid = int(cq.data.split(":")[1])
    with db() as c:
        p = c.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
        if not p:
            await cq.answer("Товар недоступен", show_alert=True)
            return
        cur = c.execute(
            "INSERT INTO orders(user_id, product_id, product_name, price, status, created_at, updated_at) "
            "VALUES(?,?,?,?, 'new', ?, ?)",
            (cq.from_user.id, pid, p["name"], p["price"], now(), now()),
        )
        oid = cur.lastrowid
    pay = get_setting("payment_info", "Реквизиты для оплаты ещё не указаны — напишите в поддержку.")
    text = (
        f"🧾 <b>Заказ #{oid}</b>\n\n"
        f"Товар: {escape(p['name'])}\n"
        f"К оплате: <b>{fmt_price(p['price'])}</b>\n\n"
        f"<b>Как оплатить:</b>\n{escape(pay)}\n\n"
        f"В комментарии к переводу укажите номер заказа: <code>{oid}</code>\n"
        f"После оплаты нажмите «Я оплатил»."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paid:{oid}")],
            [InlineKeyboardButton(text="🚫 Отменить заказ", callback_data=f"cancel:{oid}")],
        ]
    )
    await cq.message.edit_text(text, reply_markup=kb)
    await cq.answer()


@user_router.callback_query(F.data.startswith("paid:"))
async def cb_paid(cq: CallbackQuery, bot: Bot):
    oid = int(cq.data.split(":")[1])
    with db() as c:
        o = c.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, cq.from_user.id)).fetchone()
        if not o or o["status"] != "new":
            await cq.answer("Этот заказ уже обработан", show_alert=True)
            return
        c.execute("UPDATE orders SET status='paid', updated_at=? WHERE id=?", (now(), oid))
    await cq.message.edit_text(
        f"🔎 <b>Заказ #{oid}</b> передан на проверку.\n\n"
        "Как только оплата подтвердится, товар придёт в этот чат. Обычно это занимает немного времени."
    )
    u = cq.from_user
    who = f"@{u.username}" if u.username else escape(u.full_name)
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(
                admin,
                f"💸 <b>Новая оплата — заказ #{oid}</b>\n\n"
                f"Покупатель: {who} (<code>{u.id}</code>)\n"
                f"Товар: {escape(o['product_name'])}\n"
                f"Сумма: <b>{fmt_price(o['price'])}</b>\n\n"
                "Проверьте поступление и выдайте товар.",
                reply_markup=admin_order_kb(oid),
            )
        except Exception as e:  # админ не запускал бота и т.п.
            logging.warning("Не удалось уведомить админа %s: %s", admin, e)
    await cq.answer()


@user_router.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(cq: CallbackQuery):
    oid = int(cq.data.split(":")[1])
    with db() as c:
        c.execute(
            "UPDATE orders SET status='cancelled', updated_at=? WHERE id=? AND user_id=? AND status='new'",
            (now(), oid, cq.from_user.id),
        )
    await cq.message.edit_text(f"🚫 Заказ #{oid} отменён.")
    await cq.answer()


@user_router.message(F.text == BTN_ORDERS)
async def my_orders(m: Message):
    with db() as c:
        rows = c.execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 15", (m.from_user.id,)
        ).fetchall()
    if not rows:
        await m.answer("У вас пока нет заказов.")
        return
    lines = ["📦 <b>Ваши заказы</b>\n"]
    for o in rows:
        lines.append(
            f"#{o['id']} · {escape(o['product_name'])} · {fmt_price(o['price'])}\n"
            f"   {STATUS_LABELS.get(o['status'], o['status'])} · {o['created_at']}"
        )
    await m.answer("\n".join(lines))


@user_router.message(F.text == BTN_SUPPORT)
async def support(m: Message):
    if SUPPORT_USERNAME:
        await m.answer(f"💬 По любым вопросам пишите: @{SUPPORT_USERNAME}")
    else:
        await m.answer("💬 Поддержка: напишите администратору магазина.")


# ─────────────────────────── Админка ───────────────────────────

admin_router = Router()
admin_router.message.filter(lambda m: is_admin(m.from_user.id))
admin_router.callback_query.filter(lambda cq: is_admin(cq.from_user.id))


@admin_router.message(Command("admin"))
@admin_router.message(F.text == BTN_ADMIN)
async def admin_panel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("⚙️ <b>Админ-панель</b>", reply_markup=admin_kb())


@admin_router.callback_query(F.data == "adm:menu")
async def adm_menu(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await cq.message.edit_text("⚙️ <b>Админ-панель</b>", reply_markup=admin_kb())
    await cq.answer()


@admin_router.callback_query(F.data == "adm:cancel")
async def adm_cancel(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await cq.message.edit_text("Отменено.", reply_markup=admin_kb())
    await cq.answer()


# ── Добавление товара ──


@admin_router.callback_query(F.data == "adm:add")
async def add_start(cq: CallbackQuery, state: FSMContext):
    await state.set_state(AddProduct.name)
    await cq.message.edit_text("➕ <b>Новый товар</b>\n\nШаг 1/3. Введите <b>название</b>:", reply_markup=cancel_kb())
    await cq.answer()


@admin_router.message(AddProduct.name, F.text)
async def add_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip()[:100])
    await state.set_state(AddProduct.description)
    await m.answer(
        "Шаг 2/3. Введите <b>описание</b> (или отправьте <code>-</code>, чтобы пропустить):",
        reply_markup=cancel_kb(),
    )


@admin_router.message(AddProduct.description, F.text)
async def add_desc(m: Message, state: FSMContext):
    desc = "" if m.text.strip() == "-" else m.text.strip()[:1000]
    await state.update_data(description=desc)
    await state.set_state(AddProduct.price)
    await m.answer(f"Шаг 3/3. Введите <b>цену</b> в {CURRENCY} (только число, например 299):", reply_markup=cancel_kb())


def parse_price(text: str) -> float | None:
    try:
        v = float(text.replace(",", ".").replace(" ", ""))
        return v if v > 0 else None
    except ValueError:
        return None


@admin_router.message(AddProduct.price, F.text)
async def add_price(m: Message, state: FSMContext):
    price = parse_price(m.text)
    if price is None:
        await m.answer("Нужна положительная цифра, например <code>299</code>. Попробуйте ещё раз:")
        return
    data = await state.get_data()
    with db() as c:
        cur = c.execute(
            "INSERT INTO products(name, description, price, created_at) VALUES(?,?,?,?)",
            (data["name"], data["description"], price, now()),
        )
        pid = cur.lastrowid
    await state.clear()
    await m.answer(
        f"✅ Товар добавлен (ID {pid}):\n\n<b>{escape(data['name'])}</b> — {fmt_price(price)}",
        reply_markup=admin_kb(),
    )


# ── Список и редактирование товаров ──


@admin_router.callback_query(F.data == "adm:products")
async def adm_products(cq: CallbackQuery):
    with db() as c:
        items = c.execute("SELECT * FROM products ORDER BY id").fetchall()
    b = InlineKeyboardBuilder()
    for p in items:
        mark = "🟢" if p["active"] else "⚪️"
        b.button(text=f"{mark} {p['name']} — {fmt_price(p['price'])}", callback_data=f"ap:{p['id']}")
    b.button(text="➕ Добавить товар", callback_data="adm:add")
    b.button(text="« Назад", callback_data="adm:menu")
    b.adjust(1)
    text = "📋 <b>Товары</b>\n🟢 — в каталоге, ⚪️ — скрыт" if items else "Товаров пока нет."
    await cq.message.edit_text(text, reply_markup=b.as_markup())
    await cq.answer()


async def show_admin_product(msg: Message, pid: int, edit: bool = True):
    with db() as c:
        p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        sold = c.execute(
            "SELECT COUNT(*) n FROM orders WHERE product_id=? AND status='done'", (pid,)
        ).fetchone()["n"]
    if not p:
        await msg.answer("Товар не найден.")
        return
    text = (
        f"<b>{escape(p['name'])}</b> (ID {p['id']})\n\n"
        f"Описание: {escape(p['description']) or '—'}\n"
        f"Цена: <b>{fmt_price(p['price'])}</b>\n"
        f"Статус: {'🟢 в каталоге' if p['active'] else '⚪️ скрыт'}\n"
        f"Продано: {sold}"
    )
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Название", callback_data=f"ape:name:{pid}")
    b.button(text="✏️ Описание", callback_data=f"ape:description:{pid}")
    b.button(text="✏️ Цена", callback_data=f"ape:price:{pid}")
    b.button(text="🙈 Скрыть" if p["active"] else "👁 Показать", callback_data=f"apt:{pid}")
    b.button(text="🗑 Удалить", callback_data=f"apd:{pid}")
    b.button(text="« К списку", callback_data="adm:products")
    b.adjust(3, 2, 1)
    if edit:
        await msg.edit_text(text, reply_markup=b.as_markup())
    else:
        await msg.answer(text, reply_markup=b.as_markup())


@admin_router.callback_query(F.data.startswith("ap:"))
async def adm_product(cq: CallbackQuery):
    await show_admin_product(cq.message, int(cq.data.split(":")[1]))
    await cq.answer()


@admin_router.callback_query(F.data.startswith("apt:"))
async def adm_toggle(cq: CallbackQuery):
    pid = int(cq.data.split(":")[1])
    with db() as c:
        c.execute("UPDATE products SET active = 1 - active WHERE id=?", (pid,))
    await show_admin_product(cq.message, pid)
    await cq.answer("Готово")


@admin_router.callback_query(F.data.startswith("apd:"))
async def adm_delete_ask(cq: CallbackQuery):
    pid = int(cq.data.split(":")[1])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"apdy:{pid}")],
            [InlineKeyboardButton(text="« Нет", callback_data=f"ap:{pid}")],
        ]
    )
    await cq.message.edit_text("Удалить товар? История заказов сохранится.", reply_markup=kb)
    await cq.answer()


@admin_router.callback_query(F.data.startswith("apdy:"))
async def adm_delete(cq: CallbackQuery):
    pid = int(cq.data.split(":")[1])
    with db() as c:
        c.execute("DELETE FROM products WHERE id=?", (pid,))
    await cq.answer("Удалено")
    await adm_products(cq)


@admin_router.callback_query(F.data.startswith("ape:"))
async def adm_edit_start(cq: CallbackQuery, state: FSMContext):
    _, field, pid = cq.data.split(":")
    await state.set_state(EditProduct.value)
    await state.update_data(field=field, pid=int(pid))
    names = {"name": "новое название", "description": "новое описание (или - чтобы очистить)", "price": "новую цену"}
    await cq.message.edit_text(f"Введите {names[field]}:", reply_markup=cancel_kb())
    await cq.answer()


@admin_router.message(EditProduct.value, F.text)
async def adm_edit_save(m: Message, state: FSMContext):
    data = await state.get_data()
    field, pid = data["field"], data["pid"]
    if field == "price":
        value = parse_price(m.text)
        if value is None:
            await m.answer("Нужна положительная цифра. Ещё раз:")
            return
    elif field == "description":
        value = "" if m.text.strip() == "-" else m.text.strip()[:1000]
    else:
        value = m.text.strip()[:100]
    with db() as c:
        c.execute(f"UPDATE products SET {field}=? WHERE id=?", (value, pid))  # field из белого списка
    await state.clear()
    await m.answer("✅ Сохранено")
    await show_admin_product(m, pid, edit=False)


# ── Заказы и ручная выдача ──


@admin_router.callback_query(F.data == "adm:pending")
async def adm_pending(cq: CallbackQuery):
    with db() as c:
        rows = c.execute("SELECT * FROM orders WHERE status='paid' ORDER BY id").fetchall()
    if not rows:
        await cq.answer("Нет заказов на проверке 👌", show_alert=True)
        return
    await cq.answer()
    for o in rows:
        await cq.message.answer(
            f"🔎 <b>Заказ #{o['id']}</b>\n"
            f"Покупатель: <code>{o['user_id']}</code>\n"
            f"Товар: {escape(o['product_name'])}\n"
            f"Сумма: <b>{fmt_price(o['price'])}</b>\n"
            f"Создан: {o['created_at']}",
            reply_markup=admin_order_kb(o["id"]),
        )


@admin_router.callback_query(F.data.startswith("ord:deliver:"))
async def ord_deliver_start(cq: CallbackQuery, state: FSMContext):
    oid = int(cq.data.split(":")[2])
    with db() as c:
        o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o or o["status"] != "paid":
        await cq.answer("Заказ уже обработан", show_alert=True)
        return
    await state.set_state(Deliver.content)
    await state.update_data(oid=oid)
    await cq.message.answer(
        f"📤 Заказ #{oid}: отправьте сюда то, что нужно выдать покупателю — "
        "текст, файл, фото, что угодно. Я перешлю это ему одним сообщением.",
        reply_markup=cancel_kb(),
    )
    await cq.answer()


@admin_router.message(Deliver.content)
async def ord_deliver(m: Message, state: FSMContext, bot: Bot):
    oid = (await state.get_data())["oid"]
    with db() as c:
        o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o or o["status"] != "paid":
        await state.clear()
        await m.answer("Заказ уже обработан другим админом.")
        return
    try:
        await bot.send_message(
            o["user_id"], f"🎉 <b>Заказ #{oid} оплачен!</b>\nВаш товар «{escape(o['product_name'])}»:"
        )
        await m.copy_to(o["user_id"])
        await bot.send_message(o["user_id"], "Спасибо за покупку! Если что-то не так — напишите в поддержку.")
    except Exception as e:
        await m.answer(f"⚠️ Не удалось отправить покупателю: {escape(str(e))}\nЗаказ не закрыт.")
        return
    with db() as c:
        c.execute("UPDATE orders SET status='done', updated_at=? WHERE id=?", (now(), oid))
    await state.clear()
    await m.answer(f"✅ Заказ #{oid} выдан.", reply_markup=admin_kb())


@admin_router.callback_query(F.data.startswith("ord:reject:"))
async def ord_reject_start(cq: CallbackQuery, state: FSMContext):
    oid = int(cq.data.split(":")[2])
    await state.set_state(RejectOrder.reason)
    await state.update_data(oid=oid)
    await cq.message.answer(
        f"Причина отклонения заказа #{oid} (её увидит покупатель), или <code>-</code> без причины:",
        reply_markup=cancel_kb(),
    )
    await cq.answer()


@admin_router.message(RejectOrder.reason, F.text)
async def ord_reject(m: Message, state: FSMContext, bot: Bot):
    oid = (await state.get_data())["oid"]
    reason = "" if m.text.strip() == "-" else m.text.strip()
    with db() as c:
        o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o or o["status"] != "paid":
            await state.clear()
            await m.answer("Заказ уже обработан.")
            return
        c.execute("UPDATE orders SET status='rejected', updated_at=? WHERE id=?", (now(), oid))
    text = f"❌ Заказ #{oid} отклонён: оплата не найдена."
    if reason:
        text += f"\nПричина: {escape(reason)}"
    text += "\nЕсли вы уверены, что оплатили, напишите в поддержку."
    try:
        await bot.send_message(o["user_id"], text)
    except Exception:
        pass
    await state.clear()
    await m.answer(f"Заказ #{oid} отклонён.", reply_markup=admin_kb())


# ── Реквизиты ──


@admin_router.callback_query(F.data == "adm:payinfo")
async def payinfo_start(cq: CallbackQuery, state: FSMContext):
    current = get_setting("payment_info", "— не указаны —")
    await state.set_state(PaymentInfo.text)
    await cq.message.edit_text(
        f"💳 Текущие реквизиты:\n\n{escape(current)}\n\nОтправьте новый текст с реквизитами и инструкцией по оплате:",
        reply_markup=cancel_kb(),
    )
    await cq.answer()


@admin_router.message(PaymentInfo.text, F.text)
async def payinfo_save(m: Message, state: FSMContext):
    set_setting("payment_info", m.text.strip())
    await state.clear()
    await m.answer("✅ Реквизиты сохранены.", reply_markup=admin_kb())


# ── Рассылка ──


@admin_router.callback_query(F.data == "adm:broadcast")
async def bc_start(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Broadcast.content)
    await cq.message.edit_text(
        "📣 Отправьте сообщение для рассылки (текст, фото, видео — что угодно):", reply_markup=cancel_kb()
    )
    await cq.answer()


@admin_router.message(Broadcast.content)
async def bc_preview(m: Message, state: FSMContext):
    await state.update_data(chat_id=m.chat.id, msg_id=m.message_id)
    await state.set_state(Broadcast.confirm)
    with db() as c:
        n = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🚀 Отправить ({n} польз.)", callback_data="bc:go")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="adm:cancel")],
        ]
    )
    await m.answer("Отправить это сообщение всем пользователям?", reply_markup=kb)


@admin_router.callback_query(Broadcast.confirm, F.data == "bc:go")
async def bc_go(cq: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    await cq.message.edit_text("⏳ Рассылка идёт…")
    await cq.answer()
    with db() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM users").fetchall()]
    ok = fail = 0
    for uid in ids:
        try:
            await bot.copy_message(uid, data["chat_id"], data["msg_id"])
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)  # ~20 сообщений/сек, в пределах лимитов Telegram
    await cq.message.answer(f"📣 Рассылка завершена: ✅ {ok}, ❌ {fail}", reply_markup=admin_kb())


# ── Статистика ──


@admin_router.callback_query(F.data == "adm:stats")
async def adm_stats(cq: CallbackQuery):
    today = datetime.now().strftime("%Y-%m-%d")
    with db() as c:
        users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        done = c.execute("SELECT COUNT(*) n, COALESCE(SUM(price),0) s FROM orders WHERE status='done'").fetchone()
        done_today = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(price),0) s FROM orders WHERE status='done' AND updated_at LIKE ?",
            (today + "%",),
        ).fetchone()
        pending = c.execute("SELECT COUNT(*) n FROM orders WHERE status='paid'").fetchone()["n"]
        top = c.execute(
            "SELECT product_name, COUNT(*) n FROM orders WHERE status='done' "
            "GROUP BY product_name ORDER BY n DESC LIMIT 5"
        ).fetchall()
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: {users}\n"
        f"🔎 Ждут проверки: {pending}\n\n"
        f"Сегодня: {done_today['n']} продаж на {fmt_price(done_today['s'])}\n"
        f"Всего: {done['n']} продаж на {fmt_price(done['s'])}"
    )
    if top:
        text += "\n\n🏆 <b>Топ товаров:</b>\n" + "\n".join(f"• {escape(t['product_name'])} — {t['n']}" for t in top)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Назад", callback_data="adm:menu")]])
    await cq.message.edit_text(text, reply_markup=kb)
    await cq.answer()


# Если админ в режиме ввода прислал не то (например, стикер вместо текста)
@admin_router.message(StateFilter(AddProduct, EditProduct, RejectOrder, PaymentInfo))
async def wrong_input(m: Message):
    await m.answer("Нужен текст. Отправьте текстом или нажмите «Отмена».")


# ─────────────────────────── Запуск ───────────────────────────


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Укажите BOT_TOKEN в файле .env")
    if not ADMIN_IDS:
        logging.warning("ADMIN_IDS пуст — админ-панель никому не будет доступна")
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin_router)  # админ раньше, чтобы его состояния ловились первыми
    dp.include_router(user_router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
