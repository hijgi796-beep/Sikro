import asyncio
import html
import logging
import os
import re
import time
import datetime
from aiohttp import web # لإضافة خدمة الويب للـ Mini App

import asyncpg
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from locales import LANGS, t

# ───────────────────────── Settings ─────────────────────────
TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
BINANCE_ID = os.getenv("BINANCE_ID", "0000000")
REF_BONUS = int(os.getenv("REFERRAL_BONUS", "10"))
TASK_REWARD = int(os.getenv("TASK_REWARD", "1"))
COST_PER_MEMBER = int(os.getenv("COST_PER_MEMBER", "2"))
WELCOME_BONUS = int(os.getenv("WELCOME_BONUS", "5")) # مكافأة الترحيب
DAILY_BASE = int(os.getenv("DAILY_BASE", "3"))       # مكافأة اليومية الأساسية
PACKAGES = [(100, 1), (550, 5), (1200, 10), (3200, 25)]
BOT_WAIT_SECONDS = 10

# رابط الـ Mini App (سيتم توليده من Railway أو الدومين الخاص بك)
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-railway-app.up.railway.app/")

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
  joined TIMESTAMPTZ DEFAULT now(), last_active TIMESTAMPTZ DEFAULT now(),
  last_daily TIMESTAMPTZ, streak INT DEFAULT 0);
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
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS mandatory BOOL DEFAULT FALSE;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS descr TEXT;
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
    if target.startswith("http"): return target
    return "https://t.me/" + target.lstrip("@")

def display(target: str) -> str:
    m = re.search(r"t\.me/([^?/]+)", target)
    return "@" + m.group(1) if m else target

def parse_target(text: str, kind: str):
    m = re.match(r"^(?:(?:https?://)?t\.me/|@)?([A-Za-z][A-Za-z0-9_]{4,31})(?:\?start=([A-Za-z0-9_=\-]+))?$", (text or "").strip())
    if not m: return None
    name, start = m.group(1), m.group(2)
    if kind == "bot":
        if not name.lower().endswith("bot"): return None
        return f"https://t.me/{name}?start={start}" if start else "@" + name
    return "@" + name

async def bot_is_admin(target: str) -> bool:
    try:
        mem = await bot.get_chat_member(target, BOT_ID)
        return mem.status in ("administrator", "creator")
    except Exception: return False

async def is_member(target: str, uid: int) -> bool:
    try:
        mem = await bot.get_chat_member(target, uid)
        return mem.status in ("member", "administrator", "creator")
    except Exception: return False

def menu_kb(l, uid):
    rows = [
        [(t(l, "b_follow"), "follow"), (t(l, "b_get"), "get")],
        [(t(l, "b_buy"), "buy"), (t(l, "b_invite"), "invite")],
        [(t(l, "b_daily"), "daily"), (t(l, "b_leaderboard"), "leaderboard")],
        [(t(l, "b_account"), "account"), (t(l, "b_support"), "support")],
        [(t(l, "b_settings"), "settings")],
    ]
    # إضافة زر الميني أب
    rows.insert(0, [("🚀 Open Mini App", WEBAPP_URL)]) 
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
            if photo: await bot.send_photo(a, photo, caption=text, reply_markup=markup)
            else: await bot.send_message(a, text, reply_markup=markup)
        except Exception as e: logging.warning("admin notify failed %s: %s", a, e)

# ─────────────────────── Middleware ───────────────────────
async def give_referral(new_uid, arg):
    try: ref = int(arg)
    except ValueError: return
    if ref == new_uid: return
    ok = await db.fetchval("UPDATE users SET points=points+$2, referrals=referrals+1 WHERE id=$1 RETURNING id", ref, REF_BONUS)
    if ok:
        await db.execute("UPDATE users SET referred_by=$1 WHERE id=$2", ref, new_uid)
        rl = await db.fetchval("SELECT lang FROM users WHERE id=$1", ref)
        try: await bot.send_message(ref, t(rl, "ref_joined", bonus=REF_BONUS))
        except Exception: pass

async def gate_missing(uid):
    return await db.fetch("""SELECT * FROM tasks t WHERE active AND mandatory
        AND NOT EXISTS(SELECT 1 FROM completions c WHERE c.task_id=t.id AND c.user_id=$1)
        ORDER BY id""", uid)

def instr(l, task):
    if task["descr"]: return html.escape(task["descr"])
    return t(l, "instr_channel" if task["type"] == "channel" else "instr_bot")

async def show_gate(ev, l, missing):
    uid = ev.from_user.id
    lines, rows = [], []
    for i, tk in enumerate(missing, 1):
        VIEWED.setdefault((uid, tk["id"]), time.time())
        kind = t(l, "t_channel" if tk["type"] == "channel" else "t_bot")
        lines.append(f"{i}) {kind}: {display(tk['target'])}\n{instr(l, tk)}")
        rows.append([(f"{i}) 🔗 {display(tk['target'])}", link(tk["target"]))])
    rows.append([(t(l, "gate_check"), "gate")])
    await show(ev, t(l, "gate_title") + "\n\n" + "\n\n".join(lines), kb(*rows))

class UserMW(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        data["is_new"] = False
        if u is None: return await handler(event, data)
        row = await db.fetchrow("SELECT banned, lang FROM users WHERE id=$1", u.id)
        if row is None:
            lang = (u.language_code or "en").split("-")[0].lower()
            if lang not in LANGS: lang = "en"
            # إضافة مكافأة الترحيب هنا
            await db.execute("INSERT INTO users(id,username,first_name,lang,points) VALUES($1,$2,$3,$4,$5)",
                             u.id, u.username, u.first_name, lang, WELCOME_BONUS)
            data["is_new"], data["lang"] = True, lang
            if isinstance(event, Message) and (event.text or "").startswith("/start ref_"):
                await give_referral(u.id, event.text.split("ref_", 1)[1].strip())
            # إشعار الترحيب
            if isinstance(event, Message):
                await event.answer(t(lang, "welcome_bonus", bonus=WELCOME_BONUS))
        else:
            if row["banned"] and u.id not in ADMINS:
                if isinstance(event, CallbackQuery): await event.answer(t(row["lang"], "banned"), show_alert=True)
                else: await event.answer(t(row["lang"], "banned"))
                return
            await db.execute("UPDATE users SET last_active=now(), username=$2, first_name=$3 WHERE id=$1", u.id, u.username, u.first_name)
            data["lang"] = row["lang"]
        
        if u.id not in ADMINS and not (isinstance(event, CallbackQuery) and event.data == "gate"):
            missing = await gate_missing(u.id)
            if missing:
                if isinstance(event, CallbackQuery): await event.answer()
                await show_gate(event, data["lang"], missing)
                return
        return await handler(event, data)

# ───────────────────────────────── States ─────────────────────────────────
class Order(StatesGroup):
    target = State()
    qty = State() # تم تحويلها إلى نص لإدخال أي رقم

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
async def start(m: Message, state: FSMContext, lang: str):
    await state.clear()
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

@user_r.callback_query(F.data == "gate")
async def cb_gate(c: CallbackQuery, lang: str):
    uid = c.from_user.id
    for tk in await gate_missing(uid):
        if tk["type"] == "channel": ok = await is_member(tk["target"], uid)
        else:
            seen = VIEWED.setdefault((uid, tk["id"]), time.time())
            ok = time.time() - seen >= BOT_WAIT_SECONDS
        if ok:
            await db.execute("INSERT INTO completions VALUES($1,$2) ON CONFLICT DO NOTHING", tk["id"], uid)
            await db.execute("UPDATE tasks SET done=done+1 WHERE id=$1", tk["id"])
    if await gate_missing(uid): return await c.answer(t(lang, "gate_fail"), show_alert=True)
    await c.answer()
    await send_menu(c, lang, "welcome")

# ─────────────────────────── المكافأة اليومية (Streak) ───────────────────────────
@user_r.callback_query(F.data == "daily")
async def cb_daily(c: CallbackQuery, lang: str):
    uid = c.from_user.id
    u = await db.fetchrow("SELECT last_daily, streak FROM users WHERE id=$1", uid)
    now = datetime.datetime.now(datetime.timezone.utc)
    
    if u["last_daily"] and (now - u["last_daily"]).total_seconds() < 86400:
        return await c.answer(t(lang, "daily_wait"), show_alert=True)
        
    streak = u["streak"] or 0
    if u["last_daily"] and (now - u["last_daily"]).total_seconds() < 172800: # خلال 48 ساعة
        streak += 1
    else:
        streak = 1
        
    reward = DAILY_BASE * streak
    await db.execute("UPDATE users SET points=points+$1, last_daily=$2, streak=$3 WHERE id=$4", reward, now, streak, uid)
    await c.answer(t(lang, "daily_ok", n=reward, streak=streak), show_alert=True)
    await send_menu(c, lang)

# ─────────────────────────── لوحة الصدارة ───────────────────────────
@user_r.callback_query(F.data == "leaderboard")
async def cb_leaderboard(c: CallbackQuery, lang: str):
    # جلب أكثر 10 أشخاص دعوا أصدقاء هذا الأسبوع
    rows = await db.fetch("""
        SELECT username, first_name, referrals FROM users 
        WHERE referred_by IS NOT NULL AND joined >= date_trunc('week', now())
        GROUP BY id ORDER BY referrals DESC LIMIT 10""")
    if not rows:
        txt = "لا يوجد متنافسون بعد هذا الأسبوع. كن الأول!"
    else:
        txt = "\n".join([f"{i+1}. {r['first_name'] or r['username'] or 'User'} - {r['referrals']} دعوة" for i, r in enumerate(rows)])
    await show(c, t(lang, "leaderboard", list=txt), kb([(t(lang, "b_back"), "menu")]))
    await c.answer()

# ─────────────────────────── 1) Follow & earn ───────────────────────────
async def next_task(uid, after=0):
    return await db.fetchrow("""
        SELECT * FROM tasks t WHERE active AND NOT mandatory AND done<total AND id>$2
          AND (owner IS NULL OR owner<>$1)
          AND NOT EXISTS(SELECT 1 FROM completions c WHERE c.task_id=t.id AND c.user_id=$1)
        ORDER BY id LIMIT 1""", uid, after)

async def show_task(ev, l, after=0):
    uid = ev.from_user.id
    row = await next_task(uid, after)
    if not row and after: row = await next_task(uid, 0)
    if not row:
        return await show(ev, t(l, "no_tasks"), kb([(t(l, "b_back"), "menu")]))
    VIEWED[(uid, row["id"])] = time.time()
    kind = t(l, "t_channel" if row["type"] == "channel" else "t_bot")
    await show(ev, t(l, "task", kind=kind, target=display(row["target"]), instr=instr(l, row), reward=row["reward"]), kb(
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

class Full(Exception): pass

@user_r.callback_query(F.data.startswith("done:"))
async def cb_done(c: CallbackQuery, lang: str):
    uid, tid = c.from_user.id, int(c.data.split(":")[1])
    task = await db.fetchrow("SELECT * FROM tasks WHERE id=$1", tid)
    if not task or not task["active"]:
        await c.answer(t(lang, "task_full"), show_alert=True)
        return await show_task(c, lang)
    if task["type"] == "channel":
        ok = await is_member(task["target"], uid)
        if not ok: return await c.answer(t(lang, "not_joined"), show_alert=True)
    elif time.time() - VIEWED.get((uid, tid), 0) < BOT_WAIT_SECONDS:
        return await c.answer(t(lang, "wait"), show_alert=True)

    try:
        async with db.acquire() as con:
            async with con.transaction():
                ins = await con.fetchval("INSERT INTO completions VALUES($1,$2) ON CONFLICT DO NOTHING RETURNING 1", tid, uid)
                if not ins:
                    await c.answer(t(lang, "already"), show_alert=True)
                    return await show_task(c, lang)
                upd = await con.fetchval("UPDATE tasks SET done=done+1, active=(done+1<total) WHERE id=$1 AND done<total RETURNING id", tid)
                if not upd: raise Full()
                pts = await con.fetchval("UPDATE users SET points=points+$2, tasks_done=tasks_done+1 WHERE id=$1 RETURNING points", uid, task["reward"])
    except Full:
        await c.answer(t(lang, "task_full"), show_alert=True)
        return await show_task(c, lang)
    await c.answer(t(lang, "done_ok", n=task["reward"], points=pts))
    await show_task(c, lang)

# ─────────────────────────── 2) Get followers (بدون حد أدنى) ───────────────────────────
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
    if not target: return await m.answer(t(lang, "bad_target"))
    if kind == "channel" and not await bot_is_admin(target): return await m.answer(t(lang, "not_admin"))
    
    await state.update_data(target=target)
    await state.set_state(Order.qty)
    pts = await db.fetchval("SELECT points FROM users WHERE id=$1", m.from_user.id)
    
    # هنا التغيير: نطلب من المستخدم كتابة الرقم يدوياً بدل الأزرار الجاهزة
    await m.answer(t(lang, "order_qty_ask", target=display(target), cost=COST_PER_MEMBER, points=pts), 
                   reply_markup=kb([(t(lang, "b_back"), "menu")]))

@user_r.message(Order.qty, F.text)
async def order_qty(m: Message, state: FSMContext, lang: str):
    try:
        qty = int(m.text)
        if qty < 1: raise ValueError
    except ValueError:
        return await m.answer("❌ Please send a valid number (1 or more).\nأرسل رقماً صحيحاً (1 أو أكثر).")
        
    data = await state.get_data()
    cost = qty * COST_PER_MEMBER
    uid = m.from_user.id
    
    async with db.acquire() as con:
        async with con.transaction():
            left = await con.fetchval("UPDATE users SET points=points-$1 WHERE id=$2 AND points>=$1 RETURNING points", cost, uid)
            if left is None:
                pts = await con.fetchval("SELECT points FROM users WHERE id=$1", uid)
                return await m.answer(t(lang, "no_points", need=cost, points=pts))
            await con.execute("INSERT INTO tasks(owner,type,target,reward,total) VALUES($1,$2,$3,$4,$5)",
                              uid, data["kind"], data["target"], TASK_REWARD, qty)
    await state.clear()
    await m.answer(t(lang, "order_ok", target=display(data["target"]), qty=qty, cost=cost), reply_markup=kb([(t(lang, "b_back"), "menu")]))

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
    await m.answer(t
