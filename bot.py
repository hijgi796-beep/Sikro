import asyncio
import html
import logging
import os
import re
import time

import asyncpg
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from locales import LANGS, t

# ───────────────────────── Settings (Railway variables) ─────────────────────────
TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
BINANCE_ID = os.getenv("BINANCE_ID", "0000000")
REF_BONUS = int(os.getenv("REFERRAL_BONUS", "10"))      # points per invited friend
TASK_REWARD = int(os.getenv("TASK_REWARD", "1"))         # points a user earns per follow
COST_PER_MEMBER = int(os.getenv("COST_PER_MEMBER", "2")) # points a user pays per member
PACKAGES = [(100, 1), (550, 5), (1200, 10), (3200, 25)]  # (points, price in USD)
QTYS = [10, 25, 50, 100]
BOT_WAIT_SECONDS = 8  # bot-start tasks can't be verified, so we enforce a small delay

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
db: asyncpg.Pool = None
BOT_ID = 0
BOT_USERNAME = ""
VIEWED: dict = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id BIGINT PRIMARY KEY, username TEXT, first_name TEXT, lang TEXT DEFAULT 'en',
  points INT DEFAULT 0, referred_by BIGINT, referrals INT DEFAULT 0, banned BOOL DEFAULT FALSE,
  purchases INT DEFAULT 0, tasks_done INT DEFAULT 0,
  joined TIMESTAMPTZ DEFAULT now(), last_active TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS tasks(
  id SERIAL PRIMARY KEY, owner BIGINT, type TEXT, target TEXT, reward INT,
  total INT, done INT DEFAULT 0, active BOOL DEFAULT TRUE, created TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS completions(
  task_id INT, user_id BIGINT, PRIMARY KEY(task_id, user_id));
CREATE TABLE IF NOT EXISTS payments(
  id SERIAL PRIMARY KEY, user_id BIGINT, points INT, price INT, file_id TEXT,
  status TEXT DEFAULT 'pending', created TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS tickets(
  id SERIAL PRIMARY KEY, user_id BIGINT, text TEXT, status TEXT DEFAULT 'open',
  created TIMESTAMPTZ DEFAULT now());
"""

# ───────────────────────────────── Helpers ─────────────────────────────────


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=a, **({"url": b} if b.startswith("http") else {"callback_data": b}))
         for a, b in row] for row in rows])


async def show(ev, text, markup=None):
    msg = ev.message if isinstance(ev, CallbackQuery) else ev
    try:
        if isinstance(ev, CallbackQuery):
            await msg.edit_text(text, reply_markup=markup)
        else:
            await msg.answer(text, reply_markup=markup)
    except TelegramBadRequest:
        await msg.answer(text, reply_markup=markup)


def link(target: str) -> str:
    return "https://t.me/" + target.lstrip("@")


def parse_target(text: str, kind: str):
    m = re.match(r"^(?:https?://t\.me/|@)?([A-Za-z][A-Za-z0-9_]{4,31})$", (text or "").strip())
    if not m:
        return None
    name = m.group(1)
    if kind == "bot" and not name.lower().endswith("bot"):
        return None
    return "@" + name


def menu_kb(l, uid):
    rows = [
        [(t(l, "b_follow"), "follow"), (t(l, "b_get"), "get")],
        [(t(l, "b_buy"), "buy"), (t(l, "b_invite"), "invite")],
        [(t(l, "b_account"), "account"), (t(l, "b_support"), "support")],
        [(t(l, "b_settings"), "settings")],
    ]
    if uid in ADMINS:
        rows.append([(t(l, "b_admin"), "admin")])
    return kb(*rows)


async def send_menu(ev, l, key="menu"):
    uid = ev.from_user.id
    pts = await db.fetchval("SELECT points FROM users WHERE id=$1", uid)
    await show(ev, t(l, key, points=pts), menu_kb(l, uid))


async def notify_admins(text, markup=None, photo=None):
    for a in ADMINS:
        try:
            if photo:
                await bot.send_photo(a, photo, caption=text, reply_markup=markup)
            else:
                await bot.send_message(a, text, reply_markup=markup)
        except Exception as e:
            logging.warning("admin notify failed %s: %s", a, e)


# ─────────────────────── Middleware: register, ban, activity ───────────────────────
class UserMW(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        data["is_new"] = False
        if u is None:
            return await handler(event, data)
        row = await db.fetchrow("SELECT banned, lang FROM users WHERE id=$1", u.id)
        if row is None:
            lang = (u.language_code or "en").split("-")[0].lower()
            if lang not in LANGS:
                lang = "en"
            await db.execute("INSERT INTO users(id,username,first_name,lang) VALUES($1,$2,$3,$4)",
                             u.id, u.username, u.first_name, lang)
            data["is_new"], data["lang"] = True, lang
        else:
            if row["banned"] and u.id not in ADMINS:
                if isinstance(event, CallbackQuery):
                    await event.answer(t(row["lang"], "banned"), show_alert=True)
                else:
                    await event.answer(t(row["lang"], "banned"))
                return
            await db.execute("UPDATE users SET last_active=now(), username=$2, first_name=$3 WHERE id=$1",
                             u.id, u.username, u.first_name)
            data["lang"] = row["lang"]
        return await handler(event, data)


# ───────────────────────────────── States ─────────────────────────────────
class Order(StatesGroup):
    target = State()
    qty = State()


class Buy(StatesGroup):
    shot = State()


class Support(StatesGroup):
    text = State()


class Adm(StatesGroup):
    input = State()
    reply = State()


user_r = Router()
admin_r = Router()
admin_r.message.filter(F.from_user.id.in_(ADMINS))
admin_r.callback_query.filter(F.from_user.id.in_(ADMINS))


# ───────────────────────────── Start / menu / cancel ─────────────────────────────
@user_r.message(CommandStart())
async def start(m: Message, command: CommandObject, state: FSMContext, lang: str, is_new: bool):
    await state.clear()
    if is_new and command.args and command.args.startswith("ref_"):
        try:
            ref = int(command.args[4:])
        except ValueError:
            ref = 0
        if ref and ref != m.from_user.id:
            ok = await db.fetchval(
                "UPDATE users SET points=points+$2, referrals=referrals+1 WHERE id=$1 RETURNING id", ref, REF_BONUS)
            if ok:
                await db.execute("UPDATE users SET referred_by=$1 WHERE id=$2", ref, m.from_user.id)
                rl = await db.fetchval("SELECT lang FROM users WHERE id=$1", ref)
                try:
                    await bot.send_message(ref, t(rl, "ref_joined", bonus=REF_BONUS))
                except Exception:
                    pass
    await send_menu(m, lang, "welcome")


@user_r.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext, lang: str):
    await state.clear()
    await m.answer(t(lang, "cancelled"))
    await send_menu(m, lang)


@user_r.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery, state: FSMContext, lang: str):
    await state.clear()
    await send_menu(c, lang)
    await c.answer()


# ─────────────────────────── 1) Follow & earn ───────────────────────────
async def next_task(uid, after=0):
    return await db.fetchrow("""
        SELECT * FROM tasks t WHERE active AND done<total AND id>$2
          AND (owner IS NULL OR owner<>$1)
          AND NOT EXISTS(SELECT 1 FROM completions c WHERE c.task_id=t.id AND c.user_id=$1)
        ORDER BY id LIMIT 1""", uid, after)


async def show_task(ev, l, after=0):
    uid = ev.from_user.id
    row = await next_task(uid, after)
    if not row and after:
        row = await next_task(uid, 0)
    if not row:
        return await show(ev, t(l, "no_tasks"), kb([(t(l, "b_back"), "menu")]))
    VIEWED[(uid, row["id"])] = time.time()
    kind = t(l, "t_channel" if row["type"] == "channel" else "t_bot")
    await show(ev, t(l, "task", kind=kind, target=row["target"], reward=row["reward"]), kb(
        [(t(l, "b_open"), link(row["target"]))],
        [(t(l, "b_done"), f"done:{row['id']}"), (t(l, "b_skip"), f"skip:{row['id']}")],
        [(t(l, "b_back"), "menu")]))


@user_r.callback_query(F.data == "follow")
async def cb_follow(c: CallbackQuery, lang: str):
    await show_task(c, lang)
    await c.answer()


@user_r.callback_query(F.data.startswith("skip:"))
async def cb_skip(c: CallbackQuery, lang: str):
    await show_task(c, lang, int(c.data.split(":")[1]))
    await c.answer()


class Full(Exception):
    pass


@user_r.callback_query(F.data.startswith("done:"))
async def cb_done(c: CallbackQuery, lang: str):
    uid, tid = c.from_user.id, int(c.data.split(":")[1])
    task = await db.fetchrow("SELECT * FROM tasks WHERE id=$1", tid)
    if not task or not task["active"]:
        await c.answer(t(lang, "task_full"), show_alert=True)
        return await show_task(c, lang)
    if task["type"] == "channel":
        try:
            mem = await bot.get_chat_member(task["target"], uid)
            ok = mem.status in ("member", "administrator", "creator")
        except Exception:
            ok = False
        if not ok:
            return await c.answer(t(lang, "not_joined"), show_alert=True)
    elif time.time() - VIEWED.get((uid, tid), 0) < BOT_WAIT_SECONDS:
        return await c.answer(t(lang, "wait"), show_alert=True)

    try:
        async with db.acquire() as con:
            async with con.transaction():
                ins = await con.fetchval(
                    "INSERT INTO completions VALUES($1,$2) ON CONFLICT DO NOTHING RETURNING 1", tid, uid)
                if not ins:
                    await c.answer(t(lang, "already"), show_alert=True)
                    return await show_task(c, lang)
                upd = await con.fetchval(
                    "UPDATE tasks SET done=done+1, active=(done+1<total) WHERE id=$1 AND done<total RETURNING id", tid)
                if not upd:
                    raise Full()
                pts = await con.fetchval(
                    "UPDATE users SET points=points+$2, tasks_done=tasks_done+1 WHERE id=$1 RETURNING points",
                    uid, task["reward"])
    except Full:
        await c.answer(t(lang, "task_full"), show_alert=True)
        return await show_task(c, lang)
    await c.answer(t(lang, "done_ok", n=task["reward"], points=pts))
    await show_task(c, lang)


# ─────────────────────────── 2) Get followers ───────────────────────────
@user_r.callback_query(F.data == "get")
async def cb_get(c: CallbackQuery, lang: str, state: FSMContext):
    await state.clear()
    await show(c, t(lang, "get_pick"), kb(
        [(t(lang, "t_channel"), "gt:channel"), (t(lang, "t_bot"), "gt:bot")],
        [(t(lang, "b_back"), "menu")]))
    await c.answer()


@user_r.callback_query(F.data.startswith("gt:"))
async def cb_gt(c: CallbackQuery, lang: str, state: FSMContext):
    kind = c.data.split(":")[1]
    await state.set_state(Order.target)
    await state.update_data(kind=kind)
    await show(c, t(lang, "get_channel" if kind == "channel" else "get_bot"))
    await c.answer()


@user_r.message(Order.target, F.text)
async def order_target(m: Message, state: FSMContext, lang: str):
    kind = (await state.get_data())["kind"]
    target = parse_target(m.text, kind)
    if not target:
        return await m.answer(t(lang, "bad_target"))
    if kind == "channel":
        try:
            mem = await bot.get_chat_member(target, BOT_ID)
            ok = mem.status in ("administrator", "creator")
        except Exception:
            ok = False
        if not ok:
            return await m.answer(t(lang, "not_admin"))
    await state.update_data(target=target)
    await state.set_state(Order.qty)
    pts = await db.fetchval("SELECT points FROM users WHERE id=$1", m.from_user.id)
    rows = [[(f"{q} • {q * COST_PER_MEMBER}", f"gq:{q}") for q in QTYS[:2]],
            [(f"{q} • {q * COST_PER_MEMBER}", f"gq:{q}") for q in QTYS[2:]],
            [(t(lang, "b_back"), "menu")]]
    await m.answer(t(lang, "get_qty", target=target, cost=COST_PER_MEMBER, points=pts), reply_markup=kb(*rows))


@user_r.callback_query(Order.qty, F.data.startswith("gq:"))
async def order_qty(c: CallbackQuery, state: FSMContext, lang: str):
    qty = int(c.data.split(":")[1])
    if qty not in QTYS:
        return await c.answer()
    data = await state.get_data()
    cost = qty * COST_PER_MEMBER
    uid = c.from_user.id
    async with db.acquire() as con:
        async with con.transaction():
            left = await con.fetchval(
                "UPDATE users SET points=points-$1 WHERE id=$2 AND points>=$1 RETURNING points", cost, uid)
            if left is None:
                pts = await con.fetchval("SELECT points FROM users WHERE id=$1", uid)
                return await c.answer(t(lang, "no_points", need=cost, points=pts), show_alert=True)
            await con.execute(
                "INSERT INTO tasks(owner,type,target,reward,total) VALUES($1,$2,$3,$4,$5)",
                uid, data["kind"], data["target"], TASK_REWARD, qty)
    await state.clear()
    await show(c, t(lang, "order_ok", target=data["target"], qty=qty, cost=cost), kb([(t(lang, "b_back"), "menu")]))
    await c.answer()


# ─────────────────────────── 3) Buy points ───────────────────────────
@user_r.callback_query(F.data == "buy")
async def cb_buy(c: CallbackQuery, lang: str, state: FSMContext):
    await state.clear()
    rows = [[(f"{p} ⭐ — ${pr}", f"bp:{i}")] for i, (p, pr) in enumerate(PACKAGES)]
    rows.append([(t(lang, "b_back"), "menu")])
    await show(c, t(lang, "buy_title"), kb(*rows))
    await c.answer()


@user_r.callback_query(F.data.startswith("bp:"))
async def cb_bp(c: CallbackQuery, lang: str, state: FSMContext):
    i = int(c.data.split(":")[1])
    pts, price = PACKAGES[i]
    await state.set_state(Buy.shot)
    await state.update_data(pts=pts, price=price)
    await show(c, t(lang, "buy_info", pts=pts, price=price, binance=BINANCE_ID))
    await c.answer()


@user_r.message(Buy.shot, F.photo)
async def buy_photo(m: Message, state: FSMContext, lang: str):
    d = await state.get_data()
    await state.clear()
    pid = await db.fetchval("INSERT INTO payments(user_id,points,price,file_id) VALUES($1,$2,$3,$4) RETURNING id",
                            m.from_user.id, d["pts"], d["price"], m.photo[-1].file_id)
    u = await db.fetchrow("SELECT * FROM users WHERE id=$1", m.from_user.id)
    caption = (f"💳 <b>طلب شحن #{pid}</b>\n\n🆔 <code>{u['id']}</code>\n👤 @{html.escape(u['username'] or '-')} "
               f"({html.escape(u['first_name'] or '')})\n📦 {d['pts']} نقطة — ${d['price']}\n"
               f"🛒 مرات الشحن السابقة: {u['purchases']}\n💰 رصيده الحالي: {u['points']}\n"
               f"📅 انضم: {u['joined']:%Y-%m-%d}")
    await notify_admins(caption, kb([("✅ موافقة", f"pok:{pid}"), ("❌ رفض", f"pno:{pid}")]), photo=m.photo[-1].file_id)
    await m.answer(t(lang, "buy_got"), reply_markup=kb([(t(lang, "b_back"), "menu")]))


@user_r.message(Buy.shot)
async def buy_nophoto(m: Message, lang: str):
    await m.answer(t(lang, "buy_photo"))


@admin_r.callback_query(F.data.startswith(("pok:", "pno:")))
async def pay_decide(c: CallbackQuery):
    action, pid = c.data.split(":")
    pid = int(pid)
    status = "approved" if action == "pok" else "rejected"
    p = await db.fetchrow("UPDATE payments SET status=$2 WHERE id=$1 AND status='pending' RETURNING *", pid, status)
    if not p:
        return await c.answer("تمت معالجة هذا الطلب مسبقاً", show_alert=True)
    ul = await db.fetchval("SELECT lang FROM users WHERE id=$1", p["user_id"])
    if status == "approved":
        await db.execute("UPDATE users SET points=points+$2, purchases=purchases+1 WHERE id=$1", p["user_id"], p["points"])
        msg = t(ul, "pay_ok", pts=p["points"])
    else:
        msg = t(ul, "pay_no")
    try:
        await bot.send_message(p["user_id"], msg)
    except Exception:
        pass
    label = "✅ تمت الموافقة" if status == "approved" else "❌ تم الرفض"
    try:
        await c.message.edit_caption(caption=(c.message.caption or "") + f"\n\n{label} بواسطة {c.from_user.id}")
    except TelegramBadRequest:
        pass
    await c.answer(label)


# ─────────────────────────── 4) Support ───────────────────────────
@user_r.callback_query(F.data == "support")
async def cb_support(c: CallbackQuery, lang: str, state: FSMContext):
    await state.set_state(Support.text)
    await show(c, t(lang, "sup_prompt"))
    await c.answer()


@user_r.message(Support.text, F.text)
async def support_msg(m: Message, state: FSMContext, lang: str):
    await state.clear()
    tid = await db.fetchval("INSERT INTO tickets(user_id,text) VALUES($1,$2) RETURNING id", m.from_user.id, m.text)
    await notify_admins(
        f"🆘 <b>تذكرة دعم #{tid}</b>\n🆔 <code>{m.from_user.id}</code> @{html.escape(m.from_user.username or '-')}\n\n{html.escape(m.text)}",
        kb([("💬 رد", f"tk:{tid}")]))
    await m.answer(t(lang, "sup_sent"), reply_markup=kb([(t(lang, "b_back"), "menu")]))


@admin_r.callback_query(F.data.startswith("tk:"))
async def ticket_reply_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.reply)
    await state.update_data(tid=int(c.data.split(":")[1]))
    await c.message.answer("✍️ اكتب ردك الآن (أو /cancel):")
    await c.answer()


@admin_r.message(Adm.reply, F.text, ~F.text.startswith("/"))
async def ticket_reply_send(m: Message, state: FSMContext):
    tid = (await state.get_data())["tid"]
    await state.clear()
    row = await db.fetchrow("UPDATE tickets SET status='closed' WHERE id=$1 RETURNING user_id", tid)
    if not row:
        return await m.answer("التذكرة غير موجودة")
    ul = await db.fetchval("SELECT lang FROM users WHERE id=$1", row["user_id"])
    try:
        await bot.send_message(row["user_id"], t(ul, "sup_reply", text=html.escape(m.text)))
        await m.answer("✅ تم إرسال الرد")
    except Exception:
        await m.answer("⚠️ تعذر الإرسال (ربما حظر المستخدم البوت)")


# ─────────────────── Account / Invite / Settings ───────────────────
@user_r.callback_query(F.data == "account")
async def cb_account(c: CallbackQuery, lang: str):
    u = await db.fetchrow("SELECT * FROM users WHERE id=$1", c.from_user.id)
    await show(c, t(lang, "account", id=u["id"], username="@" + u["username"] if u["username"] else "-",
                    points=u["points"], refs=u["referrals"], tasks=u["tasks_done"], buys=u["purchases"],
                    lang=LANGS.get(u["lang"], u["lang"]), joined=f"{u['joined']:%Y-%m-%d}"),
               kb([(t(lang, "b_back"), "menu")]))
    await c.answer()


@user_r.callback_query(F.data == "invite")
async def cb_invite(c: CallbackQuery, lang: str):
    refs = await db.fetchval("SELECT referrals FROM users WHERE id=$1", c.from_user.id)
    await show(c, t(lang, "invite", bonus=REF_BONUS, refs=refs,
                    link=f"https://t.me/{BOT_USERNAME}?start=ref_{c.from_user.id}"),
               kb([(t(lang, "b_back"), "menu")]))
    await c.answer()


@user_r.callback_query(F.data == "settings")
async def cb_settings(c: CallbackQuery, lang: str):
    codes = list(LANGS)
    rows = [[(LANGS[x], f"sl:{x}") for x in codes[i:i + 3]] for i in range(0, len(codes), 3)]
    rows.append([(t(lang, "b_back"), "menu")])
    await show(c, t(lang, "settings"), kb(*rows))
    await c.answer()


@user_r.callback_query(F.data.startswith("sl:"))
async def cb_setlang
