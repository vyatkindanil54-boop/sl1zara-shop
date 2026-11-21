"""
Telegram-магазин с ручной выдачей товара.
aiogram 3.x + SQLite.

Покупатель:  приветствие с баннером, каталог с фото, промокоды, заказы, профиль,
             «О магазине», поддержка, глубокие ссылки на товары.
Админ:       /admin — товары (с фото), заказы и ручная выдача, промокоды, пользователи
             (поиск, бан, личные сообщения), канал (посты, публикация товаров, проверка
             подключения), рассылка, настройки магазина прямо из Telegram, статистика.
"""

import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime
from html import escape

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
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

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
SHOP_NAME_DEFAULT = os.getenv("SHOP_NAME", "SL1ZARA SHOP")
DEFAULT_CHANNEL = os.getenv("CHANNEL", "@sl1zara")
SUPPORT_DEFAULT = os.getenv("SUPPORT_USERNAME", "")  # без @
CURRENCY = os.getenv("CURRENCY", "₽")
MAX_PRICE = float(os.getenv("MAX_PRICE", "10000"))  # лимит платёжного провайдера на одну транзакцию
DB_PATH = os.getenv("DB_PATH", "shop.db")

BOT_USERNAME = ""  # заполняется при старте

DIV = "━━━━━━━━━━━━━━━"

DEFAULT_WELCOME = (
    "Добро пожаловать в {shop}!\n\n"
    "🛍 Выбирайте товар в каталоге\n"
    "💳 Оплачивайте удобным способом\n"
    "⚡️ Получайте заказ прямо в этом чате\n\n"
    "Нажмите кнопку ниже 👇"
)
DEFAULT_ABOUT = (
    "Мы работаем быстро и честно. Все заказы выдаются вручную после проверки оплаты, "
    "обычно это занимает несколько минут.\n\n"
    "Остались вопросы? Нажмите «Поддержка»."
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ─────────────────────────── База данных ───────────────────────────


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _add_column(c: sqlite3.Connection, table: str, col: str, ddl: str) -> None:
    cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def init_db() -> None:
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                banned INTEGER DEFAULT 0,
                ref_by INTEGER,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                price REAL NOT NULL,
                photo_id TEXT,
                active INTEGER DEFAULT 1,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id INTEGER,
                product_name TEXT,
                price REAL,
                orig_price REAL,
                promo TEXT,
                status TEXT DEFAULT 'new',
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS promos (
                code TEXT PRIMARY KEY,
                percent INTEGER NOT NULL,
                max_uses INTEGER DEFAULT 0,
                used INTEGER DEFAULT 0,
                active INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        # миграции для баз, созданных старой версией бота
        _add_column(c, "users", "banned", "INTEGER DEFAULT 0")
        _add_column(c, "users", "ref_by", "INTEGER")
        _add_column(c, "products", "photo_id", "TEXT")
        _add_column(c, "orders", "orig_price", "REAL")
        _add_column(c, "orders", "promo", "TEXT")
        _add_column(c, "orders", "proof_id", "TEXT")
        _add_column(c, "orders", "proof_type", "TEXT")


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def get_setting(key: str, default: str = "") -> str:
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row and row["value"] != "" else default


def set_setting(key: str, value: str) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def shop_name() -> str:
    return get_setting("shop_name", SHOP_NAME_DEFAULT)


def support_username() -> str:
    return get_setting("support", SUPPORT_DEFAULT)


def channel_id():
    ch = get_setting("channel", DEFAULT_CHANNEL).strip()
    if ch.lstrip("-").isdigit():
        return int(ch)
    return ch


def channel_link() -> str:
    ch = channel_id()
    if isinstance(ch, str) and ch.startswith("@"):
        return f"https://t.me/{ch[1:]}"
    return get_setting("channel_url", "")


def upsert_user(u) -> bool:
    """Возвращает True, если пользователь новый."""
    with db() as c:
        exists = c.execute("SELECT 1 FROM users WHERE id=?", (u.id,)).fetchone()
        if exists:
            c.execute("UPDATE users SET username=?, first_name=? WHERE id=?", (u.username, u.first_name, u.id))
            return False
        c.execute(
            "INSERT INTO users(id, username, first_name, created_at) VALUES(?,?,?,?)",
            (u.id, u.username, u.first_name, now()),
        )
        return True


def is_banned(user_id: int) -> bool:
    with db() as c:
        row = c.execute("SELECT banned FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(row and row["banned"])


def fmt_price(p: float) -> str:
    p = float(p)
    return f"{int(p)} {CURRENCY}" if p.is_integer() else f"{p:.2f} {CURRENCY}"


STATUS_LABELS = {
    "new": "🕓 Ожидает оплаты",
    "paid": "🔎 Проверяем оплату",
    "done": "✅ Выдан",
    "rejected": "❌ Отклонён",
    "cancelled": "🚫 Отменён",
}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ─────────────────────────── Помощники ───────────────────────────


async def safe_edit(msg: Message, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    """Редактирует сообщение; если оно с медиа или редактирование невозможно — шлёт новое."""
    if msg.photo or msg.video or msg.document or msg.animation:
        try:
            await msg.delete()
        except Exception:
            pass
        await msg.answer(text, reply_markup=kb)
        return
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            await msg.answer(text, reply_markup=kb)


def url_button(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


DOCS = {
    "terms": "📄 Пользовательское соглашение",
    "privacy": "🔐 Политика конфиденциальности",
}


def is_url(v: str) -> bool:
    return v.startswith("http://") or v.startswith("https://")


def doc_button(key: str) -> InlineKeyboardButton:
    value = get_setting(key)
    if is_url(value):
        return url_button(DOCS[key], value)
    return InlineKeyboardButton(text=DOCS[key], callback_data=f"doc:{key}")


def docs_rows() -> list[list[InlineKeyboardButton]]:
    return [[doc_button(k)] for k in DOCS]


def support_email() -> str:
    return get_setting("support_email", "")


def support_lines() -> str:
    lines = []
    if support_username():
        lines.append(f"✈️ Telegram: @{escape(support_username())}")
    if support_email():
        lines.append(f"📧 Почта: {escape(support_email())}")
    return "\n".join(lines)


def product_text(p) -> str:
    text = f"🔥 <b>{escape(p['name'])}</b>\n{DIV}\n"
    if p["description"]:
        text += f"{escape(p['description'])}\n\n"
    text += f"💰 Цена: <b>{fmt_price(p['price'])}</b>"
    return text


def product_kb(pid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Купить", callback_data=f"buy:{pid}")],
            [InlineKeyboardButton(text="« В каталог", callback_data="catalog")],
        ]
    )


async def show_product(msg: Message, pid: int, from_callback: bool) -> bool:
    with db() as c:
        p = c.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        return False
    text, kb = product_text(p), product_kb(pid)
    if p["photo_id"]:
        if from_callback:
            try:
                await msg.delete()
            except Exception:
                pass
        await msg.answer_photo(p["photo_id"], caption=text, reply_markup=kb)
    elif from_callback:
        await safe_edit(msg, text, kb)
    else:
        await msg.answer(text, reply_markup=kb)
    return True


def order_screen(o) -> tuple[str, InlineKeyboardMarkup]:
    pay = get_setting("payment_info", "Реквизиты для оплаты ещё не указаны — напишите в поддержку.")
    orig = o["orig_price"] or o["price"]
    if o["promo"]:
        price_line = (
            f"<s>{fmt_price(orig)}</s> → <b>{fmt_price(o['price'])}</b>\n"
            f"🎟 Промокод: <code>{escape(o['promo'])}</code>"
        )
    else:
        price_line = f"<b>{fmt_price(o['price'])}</b>"
    text = (
        f"🧾 <b>Заказ #{o['id']}</b>\n{DIV}\n"
        f"🛍 Товар: {escape(o['product_name'])}\n"
        f"💰 К оплате: {price_line}\n\n"
        f"💳 <b>Как оплатить</b>\n{escape(pay)}\n\n"
        f"✍️ В комментарии к переводу укажите номер заказа: <code>{o['id']}</code>\n"
        f"После оплаты нажмите «Я оплатил».\n\n"
        f"<i>Оплачивая заказ, вы принимаете Пользовательское соглашение и Политику конфиденциальности.</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paid:{o['id']}")],
            [InlineKeyboardButton(text="🎟 Ввести промокод", callback_data=f"promo:{o['id']}")],
            *docs_rows(),
            [InlineKeyboardButton(text="🚫 Отменить заказ", callback_data=f"cancel:{o['id']}")],
        ]
    )
    return text, kb


# ─────────────────────────── Состояния ───────────────────────────


class AddProduct(StatesGroup):
    name = State()
    description = State()
    price = State()
    photo = State()


class EditProduct(StatesGroup):
    value = State()


class Deliver(StatesGroup):
    content = State()


class RejectOrder(StatesGroup):
    reason = State()


class Broadcast(StatesGroup):
    content = State()
    confirm = State()


class SettingEdit(StatesGroup):
    value = State()


class ChannelPost(StatesGroup):
    content = State()
    confirm = State()


class PromoCreate(StatesGroup):
    code = State()
    percent = State()
    limit = State()


class UserSearch(StatesGroup):
    query = State()


class WriteUser(StatesGroup):
    text = State()


class PromoInput(StatesGroup):
    code = State()


class PaymentProof(StatesGroup):
    screenshot = State()


# ─────────────────────────── Клавиатуры ───────────────────────────

BTN_CATALOG = "🛍 Каталог"
BTN_ORDERS = "📦 Мои заказы"
BTN_PROFILE = "👤 Профиль"
BTN_ABOUT = "ℹ️ О магазине"
BTN_SUPPORT = "💬 Поддержка"
BTN_PRICES = "💰 Цены и тарифы"
BTN_DOCS = "📄 Документы"
BTN_ADMIN = "⚙️ Админ-панель"


def main_kb(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_CATALOG)],
        [KeyboardButton(text=BTN_ORDERS), KeyboardButton(text=BTN_PROFILE)],
        [KeyboardButton(text=BTN_PRICES), KeyboardButton(text=BTN_DOCS)],
        [KeyboardButton(text=BTN_ABOUT), KeyboardButton(text=BTN_SUPPORT)],
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def admin_kb() -> InlineKeyboardMarkup:
    with db() as c:
        pending = c.execute("SELECT COUNT(*) n FROM orders WHERE status='paid'").fetchone()["n"]
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить товар", callback_data="adm:add")
    b.button(text="📋 Товары", callback_data="adm:products")
    b.button(text=f"🔎 Оплаты ({pending})", callback_data="adm:pending")
    b.button(text="📜 Заказы", callback_data="adm:orders")
    b.button(text="📢 Канал", callback_data="adm:channel")
    b.button(text="📣 Рассылка", callback_data="adm:broadcast")
    b.button(text="🎟 Промокоды", callback_data="adm:promos")
    b.button(text="👥 Пользователи", callback_data="adm:users")
    b.button(text="⚙️ Настройки", callback_data="adm:settings")
    b.button(text="📊 Статистика", callback_data="adm:stats")
    b.adjust(1, 2, 2, 2, 2, 1)
    return b.as_markup()


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="adm:cancel")]]
    )


def back_kb(to: str = "adm:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Назад", callback_data=to)]])


def admin_order_caption(o, who: str | None = None) -> str:
    who = who or f"<code>{o['user_id']}</code>"
    promo = f"\n🎟 Промокод: <code>{escape(o['promo'])}</code>" if o["promo"] else ""
    return (
        f"💸 <b>Оплата на проверку — заказ #{o['id']}</b>\n{DIV}\n"
        f"👤 Покупатель: {who} (<code>{o['user_id']}</code>)\n"
        f"🛍 Товар: {escape(o['product_name'])}\n"
        f"💰 Сумма: <b>{fmt_price(o['price'])}</b>{promo}\n"
        f"🕒 {o['updated_at']}\n\n"
        "Сверьте скриншот с поступлением и выдайте товар."
    )


async def send_proof(bot: Bot, chat_id: int, o, caption: str) -> None:
    kb = admin_order_kb(o["id"])
    if o["proof_type"] == "photo":
        await bot.send_photo(chat_id, o["proof_id"], caption=caption, reply_markup=kb)
    elif o["proof_type"] == "document":
        await bot.send_document(chat_id, o["proof_id"], caption=caption, reply_markup=kb)
    else:
        await bot.send_message(chat_id, caption, reply_markup=kb)


def admin_order_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Оплата есть — выдать", callback_data=f"ord:deliver:{order_id}")],
            [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ord:reject:{order_id}")],
        ]
    )


# ─────────────────────────── Подписка на канал и баны ───────────────────────────


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(channel_id(), user_id)
        return member.status not in ("left", "kicked")
    except Exception as e:
        logging.warning("Проверка подписки не удалась (%s) — пропускаю пользователя", e)
        return True  # если бот не админ канала, не блокируем покупателей


class GateMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or is_admin(user.id):
            return await handler(event, data)

        if is_banned(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("⛔️ Доступ ограничен", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("⛔️ Доступ к магазину ограничен.")
            return None

        if get_setting("force_sub") == "1":
            # документы, цены и поддержка доступны всем, даже без подписки (требование платёжной системы)
            if isinstance(event, CallbackQuery) and (event.data in ("chk_sub", "docs") or event.data.startswith("doc:")):
                return await handler(event, data)
            if isinstance(event, Message) and event.text in (BTN_DOCS, BTN_PRICES, BTN_SUPPORT):
                return await handler(event, data)
            if not await is_subscribed(data["bot"], user.id):
                rows = []
                link = channel_link()
                if link:
                    rows.append([url_button("📢 Подписаться на канал", link)])
                rows.append([InlineKeyboardButton(text="✅ Я подписался", callback_data="chk_sub")])
                text = (
                    f"🔒 <b>{escape(shop_name())}</b>\n{DIV}\n"
                    "Чтобы пользоваться магазином, подпишитесь на наш канал, затем нажмите «Я подписался»."
                )
                target = event if isinstance(event, Message) else event.message
                await target.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
                if isinstance(event, CallbackQuery):
                    await event.answer()
                return None
        return await handler(event, data)


# ─────────────────────────── Покупатель ───────────────────────────

user_router = Router()


async def send_welcome(target: Message, user_id: int) -> None:
    shop = shop_name()
    body = escape(get_setting("welcome", DEFAULT_WELCOME)).replace("{shop}", f"<b>{escape(shop)}</b>")
    text = f"✨ <b>{escape(shop)}</b> ✨\n{DIV}\n{body}"
    rows = [[InlineKeyboardButton(text="🛍 Открыть каталог", callback_data="catalog")]]
    link = channel_link()
    if link:
        rows.append([url_button("📢 Наш канал", link)])
    rows.append([InlineKeyboardButton(text="📄 Документы", callback_data="docs")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    banner = get_setting("banner")
    if banner:
        try:
            await target.answer_photo(banner, caption=text, reply_markup=kb)
        except TelegramBadRequest:
            await target.answer(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)
    await target.answer("Главное меню внизу 👇", reply_markup=main_kb(user_id))


@user_router.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    arg = command.args or ""
    upsert_user(m.from_user)
    if arg.startswith("prod_") and arg[5:].isdigit():
        await m.answer(f"👋 Добро пожаловать в <b>{escape(shop_name())}</b>!", reply_markup=main_kb(m.from_user.id))
        if await show_product(m, int(arg[5:]), from_callback=False):
            return
        await m.answer("Этот товар сейчас недоступен. Загляните в каталог 👇")
        await send_catalog(m)
        return
    await send_welcome(m, m.from_user.id)


@user_router.callback_query(F.data == "chk_sub")
async def cb_check_sub(cq: CallbackQuery, bot: Bot):
    if await is_subscribed(bot, cq.from_user.id):
        await cq.answer("✅ Спасибо за подписку!")
        try:
            await cq.message.delete()
        except Exception:
            pass
        upsert_user(cq.from_user)
        await send_welcome(cq.message, cq.from_user.id)
    else:
        await cq.answer("Вы ещё не подписались на канал", show_alert=True)


@user_router.message(F.text == BTN_CATALOG)
@user_router.message(Command("catalog"))
async def show_catalog(m: Message, state: FSMContext):
    await state.clear()
    upsert_user(m.from_user)
    await send_catalog(m)


async def send_catalog(target: Message, edit: bool = False):
    with db() as c:
        items = c.execute("SELECT * FROM products WHERE active=1 ORDER BY id").fetchall()
    if not items:
        text, kb = "🛍 <b>Каталог</b>\n\nПока здесь пусто. Загляните позже 🙂", None
    else:
        b = InlineKeyboardBuilder()
        for p in items:
            b.button(text=f"🔹 {p['name']} · {fmt_price(p['price'])}", callback_data=f"prod:{p['id']}")
        b.adjust(1)
        text, kb = f"🛍 <b>Каталог</b>\n{DIV}\nВыберите товар:", b.as_markup()
    if edit:
        await safe_edit(target, text, kb)
    else:
        await target.answer(text, reply_markup=kb)


@user_router.callback_query(F.data == "catalog")
async def cb_catalog(cq: CallbackQuery):
    await send_catalog(cq.message, edit=True)
    await cq.answer()


@user_router.callback_query(F.data.startswith("prod:"))
async def cb_product(cq: CallbackQuery):
    if not await show_product(cq.message, int(cq.data.split(":")[1]), from_callback=True):
        await cq.answer("Товар недоступен", show_alert=True)
        return
    await cq.answer()


@user_router.callback_query(F.data.startswith("buy:"))
async def cb_buy(cq: CallbackQuery):
    pid = int(cq.data.split(":")[1])
    with db() as c:
        p = c.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
        if not p:
            await cq.answer("Товар недоступен", show_alert=True)
            return
        if p["price"] > MAX_PRICE:
            await cq.answer(f"Сумма превышает лимит {fmt_price(MAX_PRICE)}. Напишите в поддержку.", show_alert=True)
            return
        cur = c.execute(
            "INSERT INTO orders(user_id, product_id, product_name, price, orig_price, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?, 'new', ?, ?)",
            (cq.from_user.id, pid, p["name"], p["price"], p["price"], now(), now()),
        )
        o = c.execute("SELECT * FROM orders WHERE id=?", (cur.lastrowid,)).fetchone()
    text, kb = order_screen(o)
    await safe_edit(cq.message, text, kb)
    await cq.answer()


@user_router.callback_query(F.data.startswith("ordv:"))
async def cb_order_view(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    oid = int(cq.data.split(":")[1])
    with db() as c:
        o = c.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, cq.from_user.id)).fetchone()
    if not o or o["status"] != "new":
        await cq.answer("Заказ уже обработан", show_alert=True)
        return
    text, kb = order_screen(o)
    await safe_edit(cq.message, text, kb)
    await cq.answer()


@user_router.callback_query(F.data.startswith("promo:"))
async def cb_promo_ask(cq: CallbackQuery, state: FSMContext):
    oid = int(cq.data.split(":")[1])
    await state.set_state(PromoInput.code)
    await state.update_data(oid=oid)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="« Назад к заказу", callback_data=f"ordv:{oid}")]]
    )
    await safe_edit(cq.message, "🎟 Отправьте промокод одним сообщением:", kb)
    await cq.answer()


@user_router.message(PromoInput.code, F.text)
async def promo_apply(m: Message, state: FSMContext):
    oid = (await state.get_data())["oid"]
    code = m.text.strip().upper()
    with db() as c:
        o = c.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, m.from_user.id)).fetchone()
        if not o or o["status"] != "new":
            await state.clear()
            await m.answer("Заказ уже обработан.")
            return
        pr = c.execute("SELECT * FROM promos WHERE code=? AND active=1", (code,)).fetchone()
        if not pr or (pr["max_uses"] and pr["used"] >= pr["max_uses"]):
            await m.answer("❌ Такого промокода нет или он уже недействителен. Попробуйте другой или вернитесь к заказу.")
            return
        orig = o["orig_price"] or o["price"]
        new_price = max(round(orig * (100 - pr["percent"]) / 100, 2), 1)
        c.execute("UPDATE orders SET price=?, orig_price=?, promo=? WHERE id=?", (new_price, orig, code, oid))
        o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    await state.clear()
    await m.answer(f"✅ Промокод применён: скидка {pr['percent']}%")
    text, kb = order_screen(o)
    await m.answer(text, reply_markup=kb)


@user_router.callback_query(F.data.startswith("paid:"))
async def cb_paid(cq: CallbackQuery, state: FSMContext):
    oid = int(cq.data.split(":")[1])
    with db() as c:
        o = c.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, cq.from_user.id)).fetchone()
    if not o or o["status"] != "new":
        await cq.answer("Этот заказ уже обработан", show_alert=True)
        return
    await state.set_state(PaymentProof.screenshot)
    await state.update_data(oid=oid)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="« Назад к заказу", callback_data=f"ordv:{oid}")]]
    )
    await safe_edit(
        cq.message,
        f"📸 <b>Заказ #{oid}</b>\n{DIV}\n"
        "Отправьте сюда <b>скриншот или чек оплаты</b>: фото или файл (PDF).\n\n"
        "На скриншоте должны быть видны сумма, дата и получатель.",
        kb,
    )
    await cq.answer()


@user_router.message(PaymentProof.screenshot, F.photo | F.document)
async def payment_proof(m: Message, state: FSMContext, bot: Bot):
    oid = (await state.get_data())["oid"]
    if m.photo:
        proof_id, proof_type = m.photo[-1].file_id, "photo"
    else:
        proof_id, proof_type = m.document.file_id, "document"
    with db() as c:
        o = c.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, m.from_user.id)).fetchone()
        if not o or o["status"] != "new":
            await state.clear()
            await m.answer("Этот заказ уже обработан.")
            return
        c.execute(
            "UPDATE orders SET status='paid', proof_id=?, proof_type=?, updated_at=? WHERE id=?",
            (proof_id, proof_type, now(), oid),
        )
        o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    await state.clear()
    await m.answer(
        f"🔎 <b>Заказ #{oid}</b> передан на проверку.\n\n"
        "Как только оплата подтвердится, товар придёт в этот чат. Обычно это занимает немного времени.",
        reply_markup=main_kb(m.from_user.id),
    )
    u = m.from_user
    who = f"@{u.username}" if u.username else escape(u.full_name)
    caption = admin_order_caption(o, who)
    for admin in ADMIN_IDS:
        try:
            await send_proof(bot, admin, o, caption)
        except Exception as e:
            logging.warning("Не удалось уведомить админа %s: %s", admin, e)


@user_router.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(cq: CallbackQuery):
    oid = int(cq.data.split(":")[1])
    with db() as c:
        c.execute(
            "UPDATE orders SET status='cancelled', updated_at=? WHERE id=? AND user_id=? AND status='new'",
            (now(), oid, cq.from_user.id),
        )
    await safe_edit(cq.message, f"🚫 Заказ #{oid} отменён.")
    await cq.answer()


@user_router.message(F.text == BTN_ORDERS)
async def my_orders(m: Message, state: FSMContext):
    await state.clear()
    with db() as c:
        rows = c.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 15", (m.from_user.id,)).fetchall()
    if not rows:
        await m.answer("📦 У вас пока нет заказов.\n\nЗагляните в каталог 👇", reply_markup=main_kb(m.from_user.id))
        return
    lines = [f"📦 <b>Ваши заказы</b>\n{DIV}"]
    for o in rows:
        lines.append(
            f"\n<b>#{o['id']}</b> · {escape(o['product_name'])} · {fmt_price(o['price'])}\n"
            f"{STATUS_LABELS.get(o['status'], o['status'])} · {o['created_at']}"
        )
    await m.answer("\n".join(lines))


@user_router.message(F.text == BTN_PROFILE)
async def profile(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    upsert_user(m.from_user)
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        st = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(price),0) s FROM orders WHERE user_id=? AND status='done'", (uid,)
        ).fetchone()
    await m.answer(
        f"👤 <b>Ваш профиль</b>\n{DIV}\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"📅 В магазине с: {u['created_at']}\n"
        f"🛒 Покупок: <b>{st['n']}</b> на {fmt_price(st['s'])}"
    )


@user_router.message(F.text == BTN_ABOUT)
async def about(m: Message, state: FSMContext):
    await state.clear()
    text = f"ℹ️ <b>О магазине {escape(shop_name())}</b>\n{DIV}\n{escape(get_setting('about', DEFAULT_ABOUT))}"
    if support_lines():
        text += f"\n\n💬 <b>Поддержка</b>\n{support_lines()}"
    rows = []
    link = channel_link()
    if link:
        rows.append([url_button("📢 Наш канал", link)])
    if support_username():
        rows.append([url_button("💬 Поддержка", f"https://t.me/{support_username()}")])
    rows += docs_rows()
    await m.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@user_router.message(F.text == BTN_SUPPORT)
async def support(m: Message, state: FSMContext):
    await state.clear()
    if not support_lines():
        await m.answer("💬 Поддержка: напишите администратору магазина.")
        return
    rows = []
    if support_username():
        rows.append([url_button("✉️ Написать в Telegram", f"https://t.me/{support_username()}")])
    await m.answer(
        f"💬 <b>Поддержка {escape(shop_name())}</b>\n{DIV}\n"
        f"Остались вопросы по заказу или оплате? Мы на связи:\n\n{support_lines()}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
    )


@user_router.message(F.text == BTN_PRICES)
async def prices(m: Message, state: FSMContext):
    await state.clear()
    with db() as c:
        items = c.execute("SELECT * FROM products WHERE active=1 ORDER BY price").fetchall()
    if not items:
        await m.answer("💰 <b>Цены и тарифы</b>\n\nТовары скоро появятся 🙂")
        return
    lines = [f"💰 <b>Цены и тарифы {escape(shop_name())}</b>\n{DIV}"]
    for p in items:
        line = f"\n🔹 <b>{escape(p['name'])}</b> — {fmt_price(p['price'])}"
        if p["description"]:
            desc = p["description"] if len(p["description"]) <= 200 else p["description"][:200] + "…"
            line += f"\n<i>{escape(desc)}</i>"
        lines.append(line)
    lines.append(
        f"\n{DIV}\nЦены указаны в рублях за единицу товара. Итоговая сумма показывается перед оплатой. "
        "Оплачивая заказ, вы принимаете Пользовательское соглашение."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🛍 Открыть каталог", callback_data="catalog")], *docs_rows()]
    )
    await m.answer("\n".join(lines), reply_markup=kb)


@user_router.message(F.text == BTN_DOCS)
async def docs_menu(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(
        f"📄 <b>Документы {escape(shop_name())}</b>\n{DIV}\nВыберите документ:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=docs_rows()),
    )


@user_router.callback_query(F.data == "docs")
async def cb_docs(cq: CallbackQuery):
    await cq.message.answer(
        f"📄 <b>Документы {escape(shop_name())}</b>\n{DIV}\nВыберите документ:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=docs_rows()),
    )
    await cq.answer()


@user_router.callback_query(F.data.startswith("doc:"))
async def cb_doc(cq: CallbackQuery):
    key = cq.data.split(":")[1]
    if key not in DOCS:
        await cq.answer()
        return
    value = get_setting(key)
    if not value:
        await cq.answer("Документ скоро появится", show_alert=True)
        return
    if is_url(value):
        await cq.message.answer(DOCS[key], reply_markup=InlineKeyboardMarkup(inline_keyboard=[[url_button("Открыть", value)]]))
    else:
        title = f"<b>{DOCS[key]}</b>\n{DIV}\n"
        body = escape(value)
        chunks = [body[i : i + 3800] for i in range(0, len(body), 3800)]
        for i, chunk in enumerate(chunks):
            await cq.message.answer((title if i == 0 else "") + chunk)
    await cq.answer()


@user_router.message(PaymentProof.screenshot)
async def payment_proof_wrong(m: Message):
    await m.answer("📸 Пришлите, пожалуйста, скриншот оплаты (фото) или чек файлом.")


# ─────────────────────────── Админка ───────────────────────────

admin_router = Router()
admin_router.message.filter(lambda m: is_admin(m.from_user.id))
admin_router.callback_query.filter(lambda cq: is_admin(cq.from_user.id))

ADMIN_TITLE = "⚙️ <b>Админ-панель</b>"


@admin_router.message(Command("admin"))
@admin_router.message(F.text == BTN_ADMIN)
async def admin_panel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(ADMIN_TITLE, reply_markup=admin_kb())


@admin_router.callback_query(F.data == "adm:menu")
async def adm_menu(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(cq.message, ADMIN_TITLE, admin_kb())
    await cq.answer()


@admin_router.callback_query(F.data == "adm:cancel")
async def adm_cancel(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(cq.message, "Отменено.\n\n" + ADMIN_TITLE, admin_kb())
    await cq.answer()


def parse_price(text: str) -> float | None:
    try:
        v = float(text.replace(",", ".").replace(" ", ""))
        return v if 0 < v <= MAX_PRICE else None
    except ValueError:
        return None


PRICE_ERROR = f"Нужно число больше 0 и не больше {int(MAX_PRICE)} (лимит платёжной системы). Ещё раз:"


# ── Добавление товара ──


@admin_router.callback_query(F.data == "adm:add")
async def add_start(cq: CallbackQuery, state: FSMContext):
    await state.set_state(AddProduct.name)
    await safe_edit(cq.message, "➕ <b>Новый товар</b>\n\nШаг 1/4. Введите <b>название</b>:", cancel_kb())
    await cq.answer()


@admin_router.message(AddProduct.name, F.text)
async def add_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip()[:100])
    await state.set_state(AddProduct.description)
    await m.answer("Шаг 2/4. Введите <b>описание</b> (или <code>-</code>, чтобы пропустить):", reply_markup=cancel_kb())


@admin_router.message(AddProduct.description, F.text)
async def add_desc(m: Message, state: FSMContext):
    desc = "" if m.text.strip() == "-" else m.text.strip()[:700]
    await state.update_data(description=desc)
    await state.set_state(AddProduct.price)
    await m.answer(
        f"Шаг 3/4. Введите <b>цену</b> в {CURRENCY} (число, например 299, максимум {int(MAX_PRICE)}):",
        reply_markup=cancel_kb(),
    )


@admin_router.message(AddProduct.price, F.text)
async def add_price(m: Message, state: FSMContext):
    price = parse_price(m.text)
    if price is None:
        await m.answer(PRICE_ERROR)
        return
    await state.update_data(price=price)
    await state.set_state(AddProduct.photo)
    await m.answer("Шаг 4/4. Отправьте <b>фото</b> товара (или <code>-</code>, чтобы без фото):", reply_markup=cancel_kb())


@admin_router.message(AddProduct.photo)
async def add_photo(m: Message, state: FSMContext):
    if m.photo:
        photo_id = m.photo[-1].file_id
    elif m.text and m.text.strip() == "-":
        photo_id = None
    else:
        await m.answer("Отправьте фото или <code>-</code>.")
        return
    data = await state.get_data()
    with db() as c:
        cur = c.execute(
            "INSERT INTO products(name, description, price, photo_id, created_at) VALUES(?,?,?,?,?)",
            (data["name"], data["description"], data["price"], photo_id, now()),
        )
        pid = cur.lastrowid
    await state.clear()
    await m.answer(
        f"✅ Товар добавлен (ID {pid}):\n\n<b>{escape(data['name'])}</b> — {fmt_price(data['price'])}",
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
    text = "📋 <b>Товары</b>\n🟢 — в каталоге, ⚪️ — скрыт" if items else "📋 Товаров пока нет."
    await safe_edit(cq.message, text, b.as_markup())
    await cq.answer()


async def show_admin_product(msg: Message, pid: int, edit: bool = True):
    with db() as c:
        p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        sold = c.execute("SELECT COUNT(*) n FROM orders WHERE product_id=? AND status='done'", (pid,)).fetchone()["n"]
    if not p:
        await msg.answer("Товар не найден.")
        return
    text = (
        f"<b>{escape(p['name'])}</b> (ID {p['id']})\n{DIV}\n"
        f"Описание: {escape(p['description']) or '—'}\n"
        f"Цена: <b>{fmt_price(p['price'])}</b>\n"
        f"Фото: {'есть 🖼' if p['photo_id'] else 'нет'}\n"
        f"Статус: {'🟢 в каталоге' if p['active'] else '⚪️ скрыт'}\n"
        f"Продано: {sold}"
    )
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Название", callback_data=f"ape:name:{pid}")
    b.button(text="✏️ Описание", callback_data=f"ape:description:{pid}")
    b.button(text="✏️ Цена", callback_data=f"ape:price:{pid}")
    b.button(text="🖼 Фото", callback_data=f"ape:photo:{pid}")
    b.button(text="🙈 Скрыть" if p["active"] else "👁 Показать", callback_data=f"apt:{pid}")
    b.button(text="🗑 Удалить", callback_data=f"apd:{pid}")
    b.button(text="« К списку", callback_data="adm:products")
    b.adjust(2, 2, 2, 1)
    if edit:
        await safe_edit(msg, text, b.as_markup())
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
    await safe_edit(cq.message, "Удалить товар? История заказов сохранится.", kb)
    await cq.answer()


@admin_router.callback_query(F.data.startswith("apdy:"))
async def adm_delete(cq: CallbackQuery):
    with db() as c:
        c.execute("DELETE FROM products WHERE id=?", (int(cq.data.split(":")[1]),))
    await cq.answer("Удалено")
    await adm_products(cq)


@admin_router.callback_query(F.data.startswith("ape:"))
async def adm_edit_start(cq: CallbackQuery, state: FSMContext):
    _, field, pid = cq.data.split(":")
    await state.set_state(EditProduct.value)
    await state.update_data(field=field, pid=int(pid))
    prompts = {
        "name": "Введите новое название:",
        "description": "Введите новое описание (или <code>-</code>, чтобы очистить):",
        "price": "Введите новую цену:",
        "photo": "Отправьте новое фото (или <code>-</code>, чтобы убрать):",
    }
    await safe_edit(cq.message, prompts[field], cancel_kb())
    await cq.answer()


@admin_router.message(EditProduct.value)
async def adm_edit_save(m: Message, state: FSMContext):
    data = await state.get_data()
    field, pid = data["field"], data["pid"]
    text = m.text.strip() if m.text else ""
    if field == "photo":
        if m.photo:
            value = m.photo[-1].file_id
        elif text == "-":
            value = None
        else:
            await m.answer("Отправьте фото или <code>-</code>.")
            return
    else:
        if not text:
            await m.answer("Нужен текст.")
            return
        if field == "price":
            value = parse_price(text)
            if value is None:
                await m.answer(PRICE_ERROR)
                return
        elif field == "description":
            value = "" if text == "-" else text[:700]
        else:
            value = text[:100]
    column = {"name": "name", "description": "description", "price": "price", "photo": "photo_id"}[field]
    with db() as c:
        c.execute(f"UPDATE products SET {column}=? WHERE id=?", (value, pid))
    await state.clear()
    await m.answer("✅ Сохранено")
    await show_admin_product(m, pid, edit=False)


# ── Заказы и ручная выдача ──


@admin_router.callback_query(F.data == "adm:pending")
async def adm_pending(cq: CallbackQuery):
    with db() as c:
        rows = c.execute("SELECT * FROM orders WHERE status='paid' ORDER BY id").fetchall()
    if not rows:
        await cq.answer("Нет оплат на проверке 👌", show_alert=True)
        return
    await cq.answer()
    for o in rows:
        await send_proof(cq.bot, cq.from_user.id, o, admin_order_caption(o))


@admin_router.callback_query(F.data == "adm:orders")
async def adm_orders(cq: CallbackQuery):
    with db() as c:
        rows = c.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 20").fetchall()
    if not rows:
        await safe_edit(cq.message, "📜 Заказов пока нет.", back_kb())
    else:
        lines = [f"📜 <b>Последние заказы</b>\n{DIV}"]
        for o in rows:
            lines.append(
                f"<b>#{o['id']}</b> {STATUS_LABELS.get(o['status'], o['status'])}\n"
                f"   {escape(o['product_name'])} · {fmt_price(o['price'])} · <code>{o['user_id']}</code>"
            )
        await safe_edit(cq.message, "\n".join(lines), back_kb())
    await cq.answer()


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
        f"📤 Заказ #{oid}: отправьте сюда то, что нужно выдать покупателю: текст, файл, фото, что угодно. "
        "Я перешлю это ему.",
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
        await m.answer("Заказ уже обработан.")
        return
    try:
        await bot.send_message(
            o["user_id"], f"🎉 <b>Заказ #{oid} оплачен!</b>\n{DIV}\nВаш товар «{escape(o['product_name'])}»:"
        )
        await m.copy_to(o["user_id"])
        await bot.send_message(o["user_id"], "Спасибо за покупку! 💛 Если что-то не так, напишите в поддержку.")
    except Exception as e:
        await m.answer(f"⚠️ Не удалось отправить покупателю: {escape(str(e))}\nЗаказ не закрыт.")
        return
    with db() as c:
        c.execute("UPDATE orders SET status='done', updated_at=? WHERE id=?", (now(), oid))
        if o["promo"]:
            c.execute("UPDATE promos SET used = used + 1 WHERE code=?", (o["promo"],))
    await state.clear()
    await m.answer(f"✅ Заказ #{oid} выдан.", reply_markup=admin_kb())


@admin_router.callback_query(F.data.startswith("ord:reject:"))
async def ord_reject_start(cq: CallbackQuery, state: FSMContext):
    oid = int(cq.data.split(":")[2])
    await state.set_state(RejectOrder.reason)
    await state.update_data(oid=oid)
    await cq.message.answer(
        f"Причина отклонения заказа #{oid} (её увидит покупатель) или <code>-</code> без причины:",
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


# ── Настройки магазина ──

SETTINGS = {
    "shop_name": ("🏪 Название магазина", "Введите новое название магазина:"),
    "welcome": (
        "👋 Текст приветствия",
        "Введите текст приветствия (до 800 символов). Слово <code>{shop}</code> заменится на название магазина.",
    ),
    "banner": ("🖼 Баннер приветствия", "Отправьте фото-баннер для приветствия или <code>-</code>, чтобы убрать."),
    "about": ("ℹ️ О магазине", "Введите текст раздела «О магазине» (до 2000 символов):"),
    "payment_info": ("💳 Реквизиты оплаты", "Введите реквизиты и инструкцию по оплате:"),
    "support": ("💬 Поддержка: Telegram", "Введите ник поддержки (без @) или <code>-</code>, чтобы убрать:"),
    "support_email": ("📧 Поддержка: почта", "Введите почту поддержки или <code>-</code>, чтобы убрать:"),
    "terms": (
        "📄 Соглашение",
        "Отправьте <b>ссылку</b> на Пользовательское соглашение (например, статью telegra.ph) "
        "или <b>сам текст</b> одним сообщением (до 4000 символов). <code>-</code> — удалить.",
    ),
    "privacy": (
        "🔐 Политика",
        "Отправьте <b>ссылку</b> на Политику конфиденциальности (например, статью telegra.ph) "
        "или <b>сам текст</b> одним сообщением (до 4000 символов). <code>-</code> — удалить.",
    ),
    "bot_description": (
        "📝 Описание бота",
        "Введите описание бота (до 512 символов). Его видят все, кто открывает бота до нажатия «Старт». "
        "Сюда можно добавить кодовое слово для проверки платёжной системы. <code>-</code> — очистить.",
    ),
    "bot_short": (
        "🏷 Краткое описание",
        "Введите краткое описание (до 120 символов). Оно показывается в профиле бота. <code>-</code> — очистить.",
    ),
}


def settings_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, (title, _) in SETTINGS.items():
        b.button(text=title, callback_data=f"set:{key}")
    sub = "🟢 включена" if get_setting("force_sub") == "1" else "⚪️ выключена"
    b.button(text=f"🔒 Обязательная подписка: {sub}", callback_data="adm:forcesub")
    b.button(text="« Назад", callback_data="adm:menu")
    b.adjust(2, 2, 2, 2, 2, 2, 1, 1)
    return b.as_markup()


def doc_status(key: str) -> str:
    v = get_setting(key)
    if not v:
        return "❌ не добавлено"
    return "🔗 ссылка" if is_url(v) else f"📝 текст ({len(v)} симв.)"


def settings_text() -> str:
    return (
        f"⚙️ <b>Настройки магазина</b>\n{DIV}\n"
        f"🏪 Название: <b>{escape(shop_name())}</b>\n"
        f"📢 Канал: <code>{escape(str(channel_id()))}</code>\n"
        f"💬 Поддержка: {('@' + escape(support_username())) if support_username() else '—'}"
        f" / {escape(support_email()) or '—'}\n"
        f"🖼 Баннер: {'есть' if get_setting('banner') else 'нет'}\n"
        f"📄 Соглашение: {doc_status('terms')}\n"
        f"🔐 Политика: {doc_status('privacy')}\n"
        f"📝 Описание бота: {'есть' if get_setting('bot_description') else 'нет'}\n"
        f"💰 Лимит цены: {fmt_price(MAX_PRICE)}\n\n"
        "Выберите, что изменить:"
    )


@admin_router.callback_query(F.data == "adm:settings")
async def adm_settings(cq: CallbackQuery):
    await safe_edit(cq.message, settings_text(), settings_kb())
    await cq.answer()


@admin_router.callback_query(F.data == "adm:forcesub")
async def adm_forcesub(cq: CallbackQuery, bot: Bot):
    if get_setting("force_sub") == "1":
        set_setting("force_sub", "0")
        await cq.answer("Подписка отключена")
    else:
        ok, info = await channel_status(bot)
        if not ok:
            await cq.answer("Сначала подключите канал: бот должен быть админом канала. Проверьте в разделе «Канал».", show_alert=True)
            return
        set_setting("force_sub", "1")
        await cq.answer("Подписка включена")
    await safe_edit(cq.message, settings_text(), settings_kb())


@admin_router.callback_query(F.data.startswith("set:"))
async def setting_start(cq: CallbackQuery, state: FSMContext):
    key = cq.data.split(":")[1]
    if key == "channel":
        prompt = "Отправьте @username канала (например <code>@sl1zara</code>) или числовой ID:"
    else:
        prompt = SETTINGS[key][1]
    await state.set_state(SettingEdit.value)
    await state.update_data(key=key)
    await safe_edit(cq.message, prompt, cancel_kb())
    await cq.answer()


@admin_router.message(SettingEdit.value)
async def setting_save(m: Message, state: FSMContext, bot: Bot):
    key = (await state.get_data())["key"]
    text = m.text.strip() if m.text else ""
    if key == "banner":
        if m.photo:
            set_setting("banner", m.photo[-1].file_id)
        elif text == "-":
            set_setting("banner", "")
        else:
            await m.answer("Отправьте фото или <code>-</code>.")
            return
    else:
        if not text:
            await m.answer("Нужен текст.")
            return
        value = "" if text == "-" else text
        if key == "channel":
            value = value.replace("https://t.me/", "").replace("t.me/", "")
            if value and not value.lstrip("-").isdigit() and not value.startswith("@"):
                value = "@" + value
        elif key == "support":
            value = value.lstrip("@")
        elif key == "welcome":
            value = value[:800]
        elif key == "about":
            value = value[:2000]
        elif key == "shop_name":
            value = value[:50]
        elif key == "support_email":
            if value and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
                await m.answer("Это не похоже на почту. Пример: <code>support@gmail.com</code>. Ещё раз:")
                return
        elif key in ("terms", "privacy"):
            if not is_url(value):
                value = (m.text or "").strip() if value else ""
                value = value[:4000]
        elif key == "bot_description":
            if len(value) > 512:
                await m.answer(f"Слишком длинно: {len(value)} символов, можно до 512. Сократите:")
                return
            try:
                await bot.set_my_description(description=value)
            except Exception as e:
                await m.answer(f"⚠️ Telegram не принял описание: {escape(str(e))}")
                return
        elif key == "bot_short":
            if len(value) > 120:
                await m.answer(f"Слишком длинно: {len(value)} символов, можно до 120. Сократите:")
                return
            try:
                await bot.set_my_short_description(short_description=value)
            except Exception as e:
                await m.answer(f"⚠️ Telegram не принял описание: {escape(str(e))}")
                return
        set_setting(key, value)
    await state.clear()
    await m.answer("✅ Сохранено.")
    if key == "channel":
        ok, info = await channel_status(bot)
        await m.answer(info)
    await m.answer(settings_text(), reply_markup=settings_kb())


# ── Канал ──


async def channel_status(bot: Bot) -> tuple[bool, str]:
    ch = channel_id()
    if not ch:
        return False, "❌ Канал не задан."
    try:
        chat = await bot.get_chat(ch)
    except Exception as e:
        return False, (
            f"❌ Не вижу канал <code>{escape(str(ch))}</code>.\n"
            "Добавьте бота в канал администратором и проверьте название.\n"
            f"<i>{escape(str(e))}</i>"
        )
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
    except Exception as e:
        return False, f"❌ Не удалось проверить права бота: <i>{escape(str(e))}</i>"
    if me.status == "creator":
        return True, f"✅ Канал «{escape(chat.title or str(ch))}» подключён."
    if me.status != "administrator":
        return False, f"❌ Бот в канале «{escape(chat.title or str(ch))}», но не админ. Назначьте его администратором."
    if getattr(me, "can_post_messages", True) is False:
        return False, "❌ У бота нет права «Публикация сообщений». Включите его в правах администратора."
    return True, f"✅ Канал «{escape(chat.title or str(ch))}» подключён, бот может публиковать."


def channel_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✍️ Написать пост", callback_data="ch:post")
    b.button(text="🛍 Опубликовать товар", callback_data="ch:prod")
    b.button(text="🔗 Проверить подключение", callback_data="ch:check")
    b.button(text="✏️ Сменить канал", callback_data="set:channel")
    b.button(text="« Назад", callback_data="adm:menu")
    b.adjust(1, 1, 1, 1, 1)
    return b.as_markup()


@admin_router.callback_query(F.data == "adm:channel")
async def adm_channel(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        f"📢 <b>Канал</b>\n{DIV}\n"
        f"Подключён: <code>{escape(str(channel_id()))}</code>\n\n"
        "Чтобы бот мог писать в канал, добавьте его туда администратором с правом «Публикация сообщений», "
        "затем нажмите «Проверить подключение»."
    )
    await safe_edit(cq.message, text, channel_menu_kb())
    await cq.answer()


@admin_router.callback_query(F.data == "ch:check")
async def ch_check(cq: CallbackQuery, bot: Bot):
    ok, info = await channel_status(bot)
    await cq.message.answer(info)
    await cq.answer()


@admin_router.callback_query(F.data == "ch:post")
async def ch_post_start(cq: CallbackQuery, state: FSMContext):
    await state.set_state(ChannelPost.content)
    await safe_edit(
        cq.message,
        "✍️ Отправьте сообщение для канала: текст, фото, видео или файл. Форматирование сохранится.",
        cancel_kb(),
    )
    await cq.answer()


@admin_router.message(ChannelPost.content)
async def ch_post_preview(m: Message, state: FSMContext):
    await state.update_data(chat_id=m.chat.id, msg_id=m.message_id)
    await state.set_state(ChannelPost.confirm)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 Опубликовать", callback_data="chp:plain")],
            [InlineKeyboardButton(text="📤 С кнопкой «Открыть магазин»", callback_data="chp:btn")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="adm:cancel")],
        ]
    )
    await m.answer(f"Опубликовать это сообщение в канале <code>{escape(str(channel_id()))}</code>?", reply_markup=kb)


@admin_router.callback_query(ChannelPost.confirm, F.data.startswith("chp:"))
async def ch_post_send(cq: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    markup = None
    if cq.data == "chp:btn":
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[url_button(f"🛍 Открыть {shop_name()}", f"https://t.me/{BOT_USERNAME}")]]
        )
    try:
        await bot.copy_message(channel_id(), data["chat_id"], data["msg_id"], reply_markup=markup)
    except Exception as e:
        await safe_edit(cq.message, f"⚠️ Не получилось опубликовать: {escape(str(e))}\n\nПроверьте подключение канала.", channel_menu_kb())
        await state.clear()
        await cq.answer()
        return
    await state.clear()
    await safe_edit(cq.message, "✅ Опубликовано в канале.", channel_menu_kb())
    await cq.answer()


@admin_router.callback_query(F.data == "ch:prod")
async def ch_prod_list(cq: CallbackQuery):
    with db() as c:
        items = c.execute("SELECT * FROM products WHERE active=1 ORDER BY id").fetchall()
    if not items:
        await cq.answer("Нет товаров в каталоге", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for p in items:
        b.button(text=f"{p['name']} — {fmt_price(p['price'])}", callback_data=f"chpp:{p['id']}")
    b.button(text="« Назад", callback_data="adm:channel")
    b.adjust(1)
    await safe_edit(cq.message, "Какой товар опубликовать в канале?", b.as_markup())
    await cq.answer()


@admin_router.callback_query(F.data.startswith("chpp:"))
async def ch_prod_confirm(cq: CallbackQuery):
    pid = int(cq.data.split(":")[1])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 Опубликовать", callback_data=f"chpy:{pid}")],
            [InlineKeyboardButton(text="« Назад", callback_data="ch:prod")],
        ]
    )
    await safe_edit(cq.message, "Опубликовать карточку товара в канале с кнопкой «Купить»?", kb)
    await cq.answer()


@admin_router.callback_query(F.data.startswith("chpy:"))
async def ch_prod_send(cq: CallbackQuery, bot: Bot):
    pid = int(cq.data.split(":")[1])
    with db() as c:
        p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p:
        await cq.answer("Товар не найден", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[url_button("🛒 Купить в боте", f"https://t.me/{BOT_USERNAME}?start=prod_{pid}")]]
    )
    text = product_text(p) + "\n\n👇 Заказать можно в боте"
    try:
        if p["photo_id"]:
            await bot.send_photo(channel_id(), p["photo_id"], caption=text, reply_markup=kb)
        else:
            await bot.send_message(channel_id(), text, reply_markup=kb)
    except Exception as e:
        await safe_edit(cq.message, f"⚠️ Не получилось опубликовать: {escape(str(e))}", channel_menu_kb())
        await cq.answer()
        return
    await safe_edit(cq.message, "✅ Товар опубликован в канале.", channel_menu_kb())
    await cq.answer()


# ── Промокоды ──


@admin_router.callback_query(F.data == "adm:promos")
async def adm_promos(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    with db() as c:
        rows = c.execute("SELECT * FROM promos ORDER BY code").fetchall()
    b = InlineKeyboardBuilder()
    for p in rows:
        mark = "🟢" if p["active"] else "⚪️"
        lim = f"{p['used']}/{p['max_uses']}" if p["max_uses"] else f"{p['used']}/∞"
        b.button(text=f"{mark} {p['code']} −{p['percent']}% ({lim})", callback_data=f"prv:{p['code']}")
    b.button(text="➕ Создать промокод", callback_data="prn")
    b.button(text="« Назад", callback_data="adm:menu")
    b.adjust(1)
    await safe_edit(cq.message, f"🎟 <b>Промокоды</b>\n{DIV}\nИспользовано / лимит в скобках.", b.as_markup())
    await cq.answer()


@admin_router.callback_query(F.data == "prn")
async def promo_new(cq: CallbackQuery, state: FSMContext):
    await state.set_state(PromoCreate.code)
    await safe_edit(cq.message, "Введите код промокода (латиница, цифры, до 20 символов), например <code>SALE10</code>:", cancel_kb())
    await cq.answer()


@admin_router.message(PromoCreate.code, F.text)
async def promo_code(m: Message, state: FSMContext):
    code = m.text.strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{2,20}", code):
        await m.answer("Только латиница, цифры, - и _, от 2 до 20 символов. Ещё раз:")
        return
    with db() as c:
        if c.execute("SELECT 1 FROM promos WHERE code=?", (code,)).fetchone():
            await m.answer("Такой промокод уже есть. Введите другой:")
            return
    await state.update_data(code=code)
    await state.set_state(PromoCreate.percent)
    await m.answer("Размер скидки в процентах (1–99):", reply_markup=cancel_kb())


@admin_router.message(PromoCreate.percent, F.text)
async def promo_percent(m: Message, state: FSMContext):
    if not m.text.strip().isdigit() or not 1 <= int(m.text.strip()) <= 99:
        await m.answer("Введите целое число от 1 до 99:")
        return
    await state.update_data(percent=int(m.text.strip()))
    await state.set_state(PromoCreate.limit)
    await m.answer("Сколько раз можно использовать? Число или <code>0</code> — без лимита:", reply_markup=cancel_kb())


@admin_router.message(PromoCreate.limit, F.text)
async def promo_limit(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        await m.answer("Введите число (0 — без лимита):")
        return
    data = await state.get_data()
    with db() as c:
        c.execute(
            "INSERT INTO promos(code, percent, max_uses) VALUES(?,?,?)",
            (data["code"], data["percent"], int(m.text.strip())),
        )
    await state.clear()
    await m.answer(f"✅ Промокод <code>{data['code']}</code> создан: −{data['percent']}%", reply_markup=admin_kb())


@admin_router.callback_query(F.data.startswith("prv:"))
async def promo_view(cq: CallbackQuery, code: str | None = None):
    code = code or cq.data.split(":", 1)[1]
    with db() as c:
        p = c.execute("SELECT * FROM promos WHERE code=?", (code,)).fetchone()
    if not p:
        await cq.answer("Не найден", show_alert=True)
        return
    text = (
        f"🎟 <b>{escape(p['code'])}</b>\n{DIV}\n"
        f"Скидка: −{p['percent']}%\n"
        f"Использовано: {p['used']} из {p['max_uses'] or '∞'}\n"
        f"Статус: {'🟢 активен' if p['active'] else '⚪️ выключен'}"
    )
    b = InlineKeyboardBuilder()
    b.button(text="🙈 Выключить" if p["active"] else "👁 Включить", callback_data=f"prt:{code}")
    b.button(text="🗑 Удалить", callback_data=f"prd:{code}")
    b.button(text="« К списку", callback_data="adm:promos")
    b.adjust(2, 1)
    await safe_edit(cq.message, text, b.as_markup())
    await cq.answer()


@admin_router.callback_query(F.data.startswith("prt:"))
async def promo_toggle(cq: CallbackQuery):
    with db() as c:
        c.execute("UPDATE promos SET active = 1 - active WHERE code=?", (cq.data.split(":", 1)[1],))
    await promo_view(cq, code=cq.data.split(":", 1)[1])


@admin_router.callback_query(F.data.startswith("prd:"))
async def promo_delete(cq: CallbackQuery, state: FSMContext):
    with db() as c:
        c.execute("DELETE FROM promos WHERE code=?", (cq.data.split(":", 1)[1],))
    await adm_promos(cq, state)


# ── Пользователи ──


@admin_router.callback_query(F.data == "adm:users")
async def adm_users(cq: CallbackQuery, state: FSMContext):
    with db() as c:
        total = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        banned = c.execute("SELECT COUNT(*) n FROM users WHERE banned=1").fetchone()["n"]
    await state.set_state(UserSearch.query)
    await safe_edit(
        cq.message,
        f"👥 <b>Пользователи</b>\n{DIV}\nВсего: {total}, в бане: {banned}\n\n"
        "Отправьте ID или @username, чтобы найти человека:",
        cancel_kb(),
    )
    await cq.answer()


async def show_user_card(msg: Message, uid: int, edit: bool = False):
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        st = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(price),0) s FROM orders WHERE user_id=? AND status='done'", (uid,)
        ).fetchone()
    uname = f"@{escape(u['username'])}" if u["username"] else "—"
    text = (
        f"👤 <b>{escape(u['first_name'] or 'Без имени')}</b> {uname}\n{DIV}\n"
        f"🆔 <code>{uid}</code>\n"
        f"📅 Регистрация: {u['created_at']}\n"
        f"🛒 Покупок: {st['n']} на {fmt_price(st['s'])}\n"
        f"Статус: {'⛔️ в бане' if u['banned'] else '✅ активен'}"
    )
    b = InlineKeyboardBuilder()
    b.button(text="✅ Разбанить" if u["banned"] else "⛔️ Забанить", callback_data=f"ub:{uid}")
    b.button(text="✉️ Написать", callback_data=f"uw:{uid}")
    b.button(text="« Меню", callback_data="adm:menu")
    b.adjust(2, 1)
    if edit:
        await safe_edit(msg, text, b.as_markup())
    else:
        await msg.answer(text, reply_markup=b.as_markup())


@admin_router.message(UserSearch.query, F.text)
async def user_search(m: Message, state: FSMContext):
    q = m.text.strip().lstrip("@")
    with db() as c:
        if q.isdigit():
            u = c.execute("SELECT id FROM users WHERE id=?", (int(q),)).fetchone()
        else:
            u = c.execute("SELECT id FROM users WHERE lower(username)=lower(?)", (q,)).fetchone()
    if not u:
        await m.answer("Не нашёл такого пользователя. Он должен хотя бы раз запустить бота. Попробуйте ещё раз:")
        return
    await state.clear()
    await show_user_card(m, u["id"])


@admin_router.callback_query(F.data.startswith("ub:"))
async def user_ban(cq: CallbackQuery):
    uid = int(cq.data.split(":")[1])
    if is_admin(uid):
        await cq.answer("Админа банить нельзя", show_alert=True)
        return
    with db() as c:
        c.execute("UPDATE users SET banned = 1 - banned WHERE id=?", (uid,))
    await show_user_card(cq.message, uid, edit=True)
    await cq.answer("Готово")


@admin_router.callback_query(F.data.startswith("uw:"))
async def user_write_start(cq: CallbackQuery, state: FSMContext):
    await state.set_state(WriteUser.text)
    await state.update_data(uid=int(cq.data.split(":")[1]))
    await cq.message.answer("✉️ Отправьте сообщение для пользователя (текст, фото, файл):", reply_markup=cancel_kb())
    await cq.answer()


@admin_router.message(WriteUser.text)
async def user_write_send(m: Message, state: FSMContext, bot: Bot):
    uid = (await state.get_data())["uid"]
    try:
        await bot.send_message(uid, f"✉️ <b>Сообщение от {escape(shop_name())}:</b>")
        await m.copy_to(uid)
    except Exception as e:
        await m.answer(f"⚠️ Не удалось отправить: {escape(str(e))}")
        return
    await state.clear()
    await m.answer("✅ Отправлено.", reply_markup=admin_kb())


# ── Рассылка ──


@admin_router.callback_query(F.data == "adm:broadcast")
async def bc_start(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Broadcast.content)
    await safe_edit(cq.message, "📣 Отправьте сообщение для рассылки (текст, фото, видео — что угодно):", cancel_kb())
    await cq.answer()


@admin_router.message(Broadcast.content)
async def bc_preview(m: Message, state: FSMContext):
    await state.update_data(chat_id=m.chat.id, msg_id=m.message_id)
    await state.set_state(Broadcast.confirm)
    with db() as c:
        n = c.execute("SELECT COUNT(*) n FROM users WHERE banned=0").fetchone()["n"]
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
    await safe_edit(cq.message, "⏳ Рассылка идёт…")
    await cq.answer()
    with db() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM users WHERE banned=0").fetchall()]
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
async def adm_stats(cq: CallbackQuery, bot: Bot):
    today = datetime.now().strftime("%Y-%m-%d")
    with db() as c:
        users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        users_today = c.execute("SELECT COUNT(*) n FROM users WHERE created_at LIKE ?", (today + "%",)).fetchone()["n"]
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
    subs = ""
    try:
        subs = f"\n📢 Подписчиков канала: {await bot.get_chat_member_count(channel_id())}"
    except Exception:
        pass
    text = (
        f"📊 <b>Статистика</b>\n{DIV}\n"
        f"👥 Пользователей: {users} (сегодня +{users_today}){subs}\n"
        f"🔎 Ждут проверки: {pending}\n\n"
        f"💰 Сегодня: {done_today['n']} продаж на {fmt_price(done_today['s'])}\n"
        f"💰 Всего: {done['n']} продаж на {fmt_price(done['s'])}"
    )
    if top:
        text += "\n\n🏆 <b>Топ товаров:</b>\n" + "\n".join(f"• {escape(t['product_name'])} — {t['n']}" for t in top)
    await safe_edit(cq.message, text, back_kb())
    await cq.answer()


# Админ прислал не текст там, где ждём текст
@admin_router.message(
    StateFilter(
        AddProduct.name,
        AddProduct.description,
        AddProduct.price,
        RejectOrder.reason,
        PromoCreate.code,
        PromoCreate.percent,
        PromoCreate.limit,
        UserSearch.query,
    )
)
async def wrong_input(m: Message):
    await m.answer("Нужен текст. Отправьте текстом или нажмите «Отмена».")


# ─────────────────────────── Запуск ───────────────────────────


async def main():
    global BOT_USERNAME
    if not BOT_TOKEN:
        raise SystemExit("Укажите BOT_TOKEN в переменных окружения")
    if not ADMIN_IDS:
        logging.warning("ADMIN_IDS пуст — админ-панель никому не будет доступна")
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT_USERNAME = (await bot.get_me()).username
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(GateMiddleware())
    dp.callback_query.outer_middleware(GateMiddleware())
    # user_router первым: кнопки меню работают из любого состояния и сбрасывают его
    dp.include_router(user_router)
    dp.include_router(admin_router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
