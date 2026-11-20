import asyncio
import random
import sys
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
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Message, WebAppInfo

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
DAILY_STEP = int(os.getenv("DAILY_STEP", "1"))         # زيادة المكافأة لكل يوم متتالي
DAILY_MAX_STREAK = int(os.getenv("DAILY_MAX_STREAK", "7"))  # سقف الأيام المتتالية
AD_INTERVAL = int(os.getenv("AD_INTERVAL", "600"))     # ثواني بين كل إعلان وآخر (الإعلان يظهر بعد هذه المدة)
AD_WATCH_SECONDS = int(os.getenv("AD_WATCH_SECONDS", "15"))  # مدة المشاهدة المطلوبة
AD_DAILY_LIMIT = int(os.getenv("AD_DAILY_LIMIT", "20"))      # أقصى عدد إعلانات يومياً للمستخدم
AD_DEFAULT_REWARD = int(os.getenv("AD_DEFAULT_REWARD", "2"))
MAX_ORDER = int(os.getenv("MAX_ORDER", "100000"))
# عجلة الحظ: (الجائزة بالنقاط, الوزن)
WHEEL = [(0, 22), (1, 24), (2, 20), (3, 12), (5, 9), (10, 7), (20, 4), (50, 2)]
# المستويات: (الحد الأدنى من XP, الأيقونة, الاسم, نسبة بونص اليومية %)
LEVELS = [(0, "🥉", "Bronze", 0), (100, "🥈", "Silver", 10), (400, "🥇", "Gold", 25), (1000, "💎", "Diamond", 50)]
PACKAGES = [(100, 1), (550, 5), (1200, 10), (3200, 25)]
BOT_WAIT_SECONDS = 10

# رابط الـ Mini App (سيتم توليده من Railway أو الدومين الخاص بك)
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-railway-app.up.railway.app/")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
db: asyncpg.Pool = None
BOT_ID = 0
BOT_USERNAME = ""
VIEWED: dict = {}
BG: list = []

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
ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS lang TEXT DEFAULT 'en';
ALTER TABLE users ADD COLUMN IF NOT EXISTS points INT DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by BIGINT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS referrals INT DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS banned BOOL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS purchases INT DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS tasks_done INT DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS joined TIMESTAMPTZ DEFAULT now();
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active TIMESTAMPTZ DEFAULT now();
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_daily TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS streak INT DEFAULT 0;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS owner BIGINT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS reward INT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS total INT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS done INT DEFAULT 0;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS active BOOL DEFAULT TRUE;
UPDATE users SET points=0 WHERE points IS NULL;
UPDATE users SET referrals=0 WHERE referrals IS NULL;
UPDATE users SET tasks_done=0 WHERE tasks_done IS NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_spin TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS spins INT DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS ads_watched INT DEFAULT 0;
CREATE TABLE IF NOT EXISTS ads(
  id SERIAL PRIMARY KEY, title TEXT, url TEXT, reward INT DEFAULT 2,
  active BOOL DEFAULT TRUE, views INT DEFAULT 0, created TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS ad_views(
  id SERIAL PRIMARY KEY, user_id BIGINT, ad_id INT, started TIMESTAMPTZ DEFAULT now(),
  claimed BOOL DEFAULT FALSE);
CREATE INDEX IF NOT EXISTS ad_views_user ON ad_views(user_id, started);
CREATE INDEX IF NOT EXISTS users_ref ON users(referred_by);
UPDATE users SET spins=0 WHERE spins IS NULL;
UPDATE users SET ads_watched=0 WHERE ads_watched IS NULL;
"""

# ───────────────────────────────── Helpers ─────────────────────────────────
def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=a, **(
            {"web_app": WebAppInfo(url=b[7:])} if b.startswith("webapp:")
            else {"url": b} if b.startswith("http") else {"callback_data": b}))
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

async def member_state(target: str, uid: int):
    """True/False، أو None إذا تعذر التحقق (البوت ليس أدمن، خطأ شبكة...)."""
    try:
        mem = await bot.get_chat_member(target, uid)
        if mem.status == "restricted": return bool(getattr(mem, "is_member", False))
        return mem.status in ("member", "administrator", "creator")
    except Exception: return None

async def is_member(target: str, uid: int) -> bool:
    return (await member_state(target, uid)) is True

def menu_kb(l, uid):
    rows = [
        [(t(l, "b_follow"), "follow"), (t(l, "b_get"), "get")],
        [(t(l, "b_buy"), "buy"), (t(l, "b_invite"), "invite")],
        [(t(l, "b_daily"), "daily"), (t(l, "b_leaderboard"), "leaderboard")],
        [(t(l, "b_account"), "account"), (t(l, "b_support"), "support")],
        [(t(l, "b_settings"), "settings")],
    ]
    # إضافة زر الميني أب
    if WEBAPP_URL.startswith("https://"):
        rows.insert(0, [("🚀 Mini App", "webapp:" + WEBAPP_URL)])
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
    ok = await db.fetchval("UPDATE users SET points=points+$2, referrals=referrals+1, spins=spins+1 WHERE id=$1 RETURNING id", ref, REF_BONUS)
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

async def gate_verify(uid):
    for tk in await gate_missing(uid):
        if tk["type"] == "channel": ok = await is_member(tk["target"], uid)
        else:
            seen = VIEWED.setdefault((uid, tk["id"]), time.time())
            ok = time.time() - seen >= BOT_WAIT_SECONDS
        if ok:
            ins = await db.fetchval("INSERT INTO completions VALUES($1,$2) ON CONFLICT DO NOTHING RETURNING 1", tk["id"], uid)
            if ins: await db.execute("UPDATE tasks SET done=done+1 WHERE id=$1", tk["id"])
    return await gate_missing(uid)

@user_r.callback_query(F.data == "gate")
async def cb_gate(c: CallbackQuery, lang: str):
    if await gate_verify(c.from_user.id): return await c.answer(t(lang, "gate_fail"), show_alert=True)
    await c.answer()
    await send_menu(c, lang, "welcome")

# ─────────────────────────── المكافأة اليومية / لوحة الصدارة ───────────────────────────
def fmt_wait(sec: int) -> str:
    h, m = sec // 3600, (sec % 3600) // 60
    return f"{h}h {m}m"

@user_r.callback_query(F.data == "daily")
async def cb_daily(c: CallbackQuery, lang: str):
    r = await claim_daily(c.from_user.id)
    if not r["ok"]:
        return await c.answer(t(lang, "daily_wait") + f"\n⏳ {fmt_wait(r['wait'])}", show_alert=True)
    await c.answer(t(lang, "daily_ok", n=r["reward"], streak=r["streak"]), show_alert=True)
    await send_menu(c, lang)

@user_r.callback_query(F.data == "leaderboard")
async def cb_leaderboard(c: CallbackQuery, lang: str):
    rows = await weekly_top(10)
    if not rows: txt = "—"
    else: txt = "\n".join(f"{i+1}. {html.escape(r['name'] or 'User')} — {r['n']} 👥" for i, r in enumerate(rows))
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

@user_r.callback_query(F.data.startswith("done:"))
async def cb_done(c: CallbackQuery, lang: str):
    uid, tid = c.from_user.id, int(c.data.split(":")[1])
    r = await complete_task(uid, tid)
    code = r["code"]
    if code == "not_joined": return await c.answer(t(lang, "not_joined"), show_alert=True)
    if code == "wait": return await c.answer(t(lang, "wait"), show_alert=True)
    if code in ("gone", "full", "already"):
        await c.answer(t(lang, "already" if code == "already" else "task_full"), show_alert=True)
        return await show_task(c, lang)
    await c.answer(t(lang, "done_ok", n=r["reward"], points=r["points"]))
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
        if qty < 1 or qty > MAX_ORDER: raise ValueError
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
    if not p: return await c.answer("تمت معالجة هذا الطلب مسبقاً", show_alert=True)
    ul = await db.fetchval("SELECT lang FROM users WHERE id=$1", p["user_id"])
    if status == "approved":
        await db.execute("UPDATE users SET points=points+$2, purchases=purchases+1 WHERE id=$1", p["user_id"], p["points"])
        msg = t(ul, "pay_ok", pts=p["points"])
    else: msg = t(ul, "pay_no")
    try: await bot.send_message(p["user_id"], msg)
    except Exception: pass
    label = "✅ تمت الموافقة" if status == "approved" else "❌ تم الرفض"
    try: await c.message.edit_caption(caption=(c.message.caption or "") + f"\n\n{label} بواسطة {c.from_user.id}")
    except TelegramBadRequest: pass
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
    if not row: return await m.answer("التذكرة غير موجودة")
    ul = await db.fetchval("SELECT lang FROM users WHERE id=$1", row["user_id"])
    try:
        await bot.send_message(row["user_id"], t(ul, "sup_reply", text=html.escape(m.text)))
        await m.answer("✅ تم إرسال الرد")
    except Exception: await m.answer("⚠️ تعذر الإرسال (ربما حظر المستخدم البوت)")

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
async def cb_setlang(c: CallbackQuery):
    code = c.data.split(":")[1]
    if code in LANGS:
        await db.execute("UPDATE users SET lang=$1 WHERE id=$2", code, c.from_user.id)
        await c.answer(t(code, "lang_ok"))
        await send_menu(c, code)

# ───────────────────────────── 5) Admin panel ─────────────────────────────
class AdmTask(StatesGroup):
    link = State()
    descr = State()
    nums = State()

def admin_kb():
    return kb(
        [("📊 الإحصائيات", "adm:stats"), ("👥 الدعوات", "adm:ref")],
        [("💰 إضافة رصيد", "adm:bal")],
        [("➕ مهمة: قناة", "adm:task:channel"), ("➕ مهمة: بوت", "adm:task:bot")],
        [("🔒 إجباري: قناة", "adm:mand:channel"), ("🔒 إجباري: بوت", "adm:mand:bot")],
        [("🗂 إدارة المهام", "adm:tl"), ("📺 الإعلانات", "adm:ads")],
        [("📣 إذاعة", "adm:bc")],
        [("🚫 حظر", "adm:ban"), ("✅ فك الحظر", "adm:unban")],
        [("⬅️ رجوع", "menu")])

@admin_r.callback_query(F.data == "admin")
async def cb_admin(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, "🛠 <b>لوحة الأدمن</b>", admin_kb())
    await c.answer()

@admin_r.callback_query(F.data == "adm:stats")
async def adm_stats(c: CallbackQuery):
    q = lambda w: db.fetchval(f"SELECT count(*) FROM users WHERE {w}")
    now = await q("last_active > now()-interval '5 minutes'")
    hour = await q("last_active > now()-interval '1 hour'")
    day = await q("last_active > now()-interval '1 day'")
    total = await q("TRUE")
    pending = await db.fetchval("SELECT count(*) FROM payments WHERE status='pending'")
    await show(c, f"📊 <b>المستخدمون النشطون</b>\n\n🟢 الآن (5 د): {now}\n🕐 آخر ساعة: {hour}\n📅 آخر 24 ساعة: {day}\n👥 الإجمالي: {total}\n\n⏳ دفعات معلقة: {pending}",
               kb([("⬅️ رجوع", "admin")]))
    await c.answer()

@admin_r.callback_query(F.data == "adm:ref")
async def adm_ref(c: CallbackQuery):
    today = await db.fetchval("SELECT count(*) FROM users WHERE referred_by IS NOT NULL AND joined >= date_trunc('day', now())")
    total = await db.fetchval("SELECT count(*) FROM users WHERE referred_by IS NOT NULL")
    await show(c, f"👥 <b>الدعوات</b>\n\nاليوم: {today}\nالإجمالي: {total}", kb([("⬅️ رجوع", "admin")]))
    await c.answer()

PROMPTS = {
    "bal": "💰 أرسل: <code>ID المبلغ</code>\nمثال: <code>123456789 50</code> (سالب للخصم)",
    "ban": "🚫 أرسل ID المستخدم لحظره",
    "unban": "✅ أرسل ID المستخدم لفك الحظر",
}

@admin_r.callback_query(F.data.regexp(r"^adm:(bal|ban|unban)$"))
async def adm_prompt(c: CallbackQuery, state: FSMContext):
    action = c.data.split(":")[1]
    await state.set_state(Adm.input)
    await state.update_data(action=action)
    await c.message.answer(PROMPTS[action] + "\n\n/cancel للإلغاء")
    await c.answer()

@admin_r.message(Adm.input, F.text, ~F.text.startswith("/"))
async def adm_input(m: Message, state: FSMContext):
    a, p = (await state.get_data())["action"], m.text.split()
    try:
        if a == "bal":
            uid, amt = int(p[0]), int(p[1])
            pts = await db.fetchval("UPDATE users SET points=points+$2 WHERE id=$1 RETURNING points", uid, amt)
            res = f"✅ الرصيد الجديد: {pts}" if pts is not None else "❌ المستخدم غير موجود"
        else:
            uid = int(p[0])
            ok = await db.fetchval("UPDATE users SET banned=$2 WHERE id=$1 RETURNING id", uid, a == "ban")
            res = ("✅ تم الحظر" if a == "ban" else "✅ تم فك الحظر") if ok else "❌ المستخدم غير موجود"
    except (ValueError, IndexError):
        return await m.answer("❌ صيغة خاطئة، أعد المحاولة أو /cancel")
    await state.clear()
    await m.answer(res, reply_markup=kb([("⬅️ لوحة الأدمن", "admin")]))

# ── Add task (normal or mandatory): link → campaign name → reward/count ──
@admin_r.callback_query(F.data.regexp(r"^adm:(task|mand):(channel|bot)$"))
async def adm_task_start(c: CallbackQuery, state: FSMContext):
    _, mode, kind = c.data.split(":")
    await state.set_state(AdmTask.link)
    await state.update_data(mode=mode, kind=kind)
    if kind == "bot": txt = "🔗 أرسل رابط الإحالة الخاص بالبوت\nمثال: <code>https://t.me/mybot?start=123456</code>"
    else: txt = "📢 أرسل معرف القناة\nمثال: <code>@mychannel</code>\n(لازم يكون البوت أدمن فيها ليتحقق من الاشتراك)"
    if mode == "mand": txt = "🔒 <b>مهمة إجبارية</b> (كل مستخدم لازم يكملها ليستعمل البوت)\n\n" + txt
    await c.message.answer(txt + "\n\n/cancel للإلغاء")
    await c.answer()

@admin_r.message(AdmTask.link, F.text, ~F.text.startswith("/"))
async def adm_task_link(m: Message, state: FSMContext):
    d = await state.get_data()
    target = parse_target(m.text, d["kind"])
    if not target: return await m.answer("❌ رابط/معرف غير صالح، أعد المحاولة أو /cancel")
    if d["kind"] == "channel" and not await bot_is_admin(target): return await m.answer("❌ أضف البوت أدمن في القناة أولاً ثم أرسل المعرف مرة أخرى")
    await state.update_data(target=target)
    await state.set_state(AdmTask.descr)
    await m.answer("📝 أرسل اسم الحملة أو وصفها (يظهر للمستخدم)\nمثال: <code>إرسال /start للبوت والبقاء فيه 10 ثواني</code>")

@admin_r.message(AdmTask.descr, F.text, ~F.text.startswith("/"))
async def adm_task_descr(m: Message, state: FSMContext):
    d = await state.get_data()
    descr = m.text.strip()[:300]
    if d["mode"] == "mand":
        await db.execute("INSERT INTO tasks(owner,type,target,reward,total,descr,mandatory) VALUES(NULL,$1,$2,0,1000000000,$3,TRUE)",
                         d["kind"], d["target"], descr)
        await state.clear()
        return await m.answer(f"✅ تمت إضافة مهمة إجبارية: {display(d['target'])}\nكل المستخدمين (القدامى والجدد) لازم يكملونها.",
                              reply_markup=kb([("⬅️ لوحة الأدمن", "admin")]))
    await state.update_data(descr=descr)
    await state.set_state(AdmTask.nums)
    await m.answer("🎁 أرسل: <code>المكافأة العدد</code>\nمثال: <code>1 100</code>\n(المكافأة بالنقاط لكل مستخدم، والعدد = كم مستخدم يقدر ينفذها)")

@admin_r.message(AdmTask.nums, F.text, ~F.text.startswith("/"))
async def adm_task_nums(m: Message, state: FSMContext):
    d = await state.get_data()
    try:
        reward, count = (int(x) for x in m.text.split()[:2])
        assert reward >= 0 and count > 0
    except (ValueError, AssertionError):
        return await m.answer("❌ صيغة خاطئة، مثال: <code>1 100</code>")
    await db.execute("INSERT INTO tasks(owner,type,target,reward,total,descr,mandatory) VALUES(NULL,$1,$2,$3,$4,$5,FALSE)",
                     d["kind"], d["target"], reward, count, d["descr"])
    await state.clear()
    await m.answer(f"✅ تمت إضافة المهمة: {display(d['target'])} | {reward} نقطة | {count} مرة",
                   reply_markup=kb([("⬅️ لوحة الأدمن", "admin")]))

# ── Manage tasks ──
@admin_r.callback_query(F.data.regexp(r"^adm:(tl|td:\d+)$"))
async def adm_tasks(c: CallbackQuery):
    parts = c.data.split(":")
    if parts[1] == "td":
        await db.execute("UPDATE tasks SET active=FALSE WHERE id=$1 AND owner IS NULL", int(parts[2]))
    rows = await db.fetch("SELECT * FROM tasks WHERE active AND owner IS NULL ORDER BY id DESC LIMIT 20")
    if not rows:
        await show(c, "🗂 لا توجد مهام فعالة", kb([("⬅️ رجوع", "admin")]))
        return await c.answer()
    lines = [f"#{r['id']} {'🔒' if r['mandatory'] else '🎯'} {r['type']} {display(r['target'])} — "
             f"{r['done']}/{'∞' if r['mandatory'] else r['total']}" for r in rows]
    btns = [(f"🗑 #{r['id']}", f"adm:td:{r['id']}") for r in rows]
    grid = [btns[i:i + 3] for i in range(0, len(btns), 3)] + [[("⬅️ رجوع", "admin")]]
    await show(c, "🗂 <b>المهام الفعالة</b> (🔒 إجباري / 🎯 عادي)\n\n" + "\n".join(lines), kb(*grid))
    await c.answer()

# ─────────────────────── المنطق المشترك (البوت + الميني اب) ───────────────────────
def utcnow(): return datetime.datetime.now(datetime.timezone.utc)

def xp_of(u) -> int:
    return (u["tasks_done"] or 0) * 2 + (u["referrals"] or 0) * 10 + (u["ads_watched"] or 0) + (u["streak"] or 0) * 2

def level_info(xp: int) -> dict:
    idx = max(i for i, l in enumerate(LEVELS) if xp >= l[0])
    nxt = LEVELS[idx + 1][0] if idx + 1 < len(LEVELS) else None
    return {"idx": idx, "icon": LEVELS[idx][1], "name": LEVELS[idx][2], "bonus": LEVELS[idx][3],
            "xp": xp, "from": LEVELS[idx][0], "next": nxt}

async def complete_task(uid: int, tid: int) -> dict:
    task = await db.fetchrow("SELECT * FROM tasks WHERE id=$1", tid)
    if not task or not task["active"] or task["mandatory"] or task["owner"] == uid:
        return {"code": "gone"}
    if task["type"] == "channel":
        if not await is_member(task["target"], uid): return {"code": "not_joined"}
    else:
        left = BOT_WAIT_SECONDS - (time.time() - VIEWED.get((uid, tid), 0))
        if left > 0: return {"code": "wait", "left": int(left) + 1}
    async with db.acquire() as con:
        async with con.transaction():
            ins = await con.fetchval("INSERT INTO completions VALUES($1,$2) ON CONFLICT DO NOTHING RETURNING 1", tid, uid)
            if not ins: return {"code": "already"}
            upd = await con.fetchval("UPDATE tasks SET done=done+1, active=(done+1<total) WHERE id=$1 AND done<total RETURNING id", tid)
            if not upd:
                await con.execute("DELETE FROM completions WHERE task_id=$1 AND user_id=$2", tid, uid)
                return {"code": "full"}
            pts = await con.fetchval("UPDATE users SET points=points+$2, tasks_done=tasks_done+1 WHERE id=$1 RETURNING points", uid, task["reward"])
    VIEWED.pop((uid, tid), None)
    return {"code": "ok", "reward": task["reward"], "points": pts}

async def claim_daily(uid: int) -> dict:
    async with db.acquire() as con:
        async with con.transaction():
            u = await con.fetchrow("SELECT last_daily, streak, tasks_done, referrals, ads_watched FROM users WHERE id=$1 FOR UPDATE", uid)
            now = utcnow()
            streak = 1
            if u["last_daily"]:
                el = (now - u["last_daily"]).total_seconds()
                if el < 86400: return {"ok": False, "wait": int(86400 - el)}
                if el < 172800: streak = (u["streak"] or 0) + 1
            base = DAILY_BASE + (min(streak, DAILY_MAX_STREAK) - 1) * DAILY_STEP
            lv = level_info(xp_of({**dict(u), "streak": streak}))
            reward = max(1, round(base * (1 + lv["bonus"] / 100)))
            pts = await con.fetchval("UPDATE users SET points=points+$1, last_daily=$2, streak=$3 WHERE id=$4 RETURNING points",
                                     reward, now, streak, uid)
    return {"ok": True, "reward": reward, "streak": streak, "points": pts, "bonus": lv["bonus"]}

def spin_state(u) -> dict:
    """جاهزية العجلة: لفة مجانية كل 24 ساعة + لفات إضافية (من الدعوات)."""
    wait = 0
    if u["last_spin"]:
        wait = max(0, int(86400 - (utcnow() - u["last_spin"]).total_seconds()))
    return {"free": wait == 0, "wait": wait, "extra": u["spins"] or 0}

async def spin_wheel(uid: int) -> dict:
    async with db.acquire() as con:
        async with con.transaction():
            u = await con.fetchrow("SELECT last_spin, spins FROM users WHERE id=$1 FOR UPDATE", uid)
            st = spin_state(u)
            if st["free"]: await con.execute("UPDATE users SET last_spin=now() WHERE id=$1", uid)
            elif st["extra"] > 0: await con.execute("UPDATE users SET spins=spins-1 WHERE id=$1", uid)
            else: return {"ok": False, "wait": st["wait"]}
            idx = random.choices(range(len(WHEEL)), weights=[w for _, w in WHEEL])[0]
            prize = WHEEL[idx][0]
            pts = await con.fetchval("UPDATE users SET points=points+$2 WHERE id=$1 RETURNING points", uid, prize)
            u2 = await con.fetchrow("SELECT last_spin, spins FROM users WHERE id=$1", uid)
    return {"ok": True, "index": idx, "prize": prize, "points": pts, "spin": spin_state(u2)}

async def weekly_top(limit=10):
    return await db.fetch("""SELECT r.id, COALESCE(r.first_name, r.username) AS name, count(*)::int AS n
        FROM users u JOIN users r ON u.referred_by = r.id
        WHERE u.joined >= date_trunc('week', now())
        GROUP BY r.id, r.first_name, r.username ORDER BY n DESC, r.id LIMIT $1""", limit)

async def alltime_top(limit=10):
    return await db.fetch("""SELECT id, COALESCE(first_name, username) AS name, referrals AS n
        FROM users WHERE referrals > 0 AND NOT banned ORDER BY referrals DESC, id LIMIT $1""", limit)

async def create_order(uid: int, kind: str, raw: str, qty: int) -> dict:
    if kind not in ("channel", "bot"): return {"error": "bad_target"}
    if qty < 1 or qty > MAX_ORDER: return {"error": "bad_qty"}
    target = parse_target(raw, kind)
    if not target: return {"error": "bad_target"}
    if kind == "channel" and not await bot_is_admin(target): return {"error": "not_admin"}
    cost = qty * COST_PER_MEMBER
    async with db.acquire() as con:
        async with con.transaction():
            left = await con.fetchval("UPDATE users SET points=points-$1 WHERE id=$2 AND points>=$1 RETURNING points", cost, uid)
            if left is None: return {"error": "no_points", "need": cost}
            await con.execute("INSERT INTO tasks(owner,type,target,reward,total) VALUES($1,$2,$3,$4,$5)", uid, kind, target, TASK_REWARD, qty)
    return {"ok": True, "points": left, "cost": cost, "target": display(target)}

# ── الإعلانات ──
async def ad_next(uid: int) -> dict:
    """يرجع إعلاناً إذا حان وقته، أو مدة الانتظار، أو placeholder إذا لا توجد إعلانات."""
    ads = await db.fetch("SELECT id, title, url, reward FROM ads WHERE active")
    if not ads: return {"ad": None, "placeholder": True}
    last = await db.fetchval("SELECT started FROM ad_views WHERE user_id=$1 ORDER BY started DESC LIMIT 1", uid)
    if last:
        wait = int(AD_INTERVAL - (utcnow() - last).total_seconds())
        if wait > 0: return {"ad": None, "wait": wait}
    today = await db.fetchval("SELECT count(*) FROM ad_views WHERE user_id=$1 AND claimed AND started >= date_trunc('day', now())", uid)
    if today >= AD_DAILY_LIMIT: return {"ad": None, "limit": True}
    seen = {r["ad_id"] for r in await db.fetch("SELECT DISTINCT ad_id FROM ad_views WHERE user_id=$1 AND started >= now()-interval '1 day'", uid)}
    pool = [a for a in ads if a["id"] not in seen] or ads
    a = random.choice(pool)
    return {"ad": {"id": a["id"], "title": a["title"], "reward": a["reward"]}, "watch": AD_WATCH_SECONDS}

async def ad_start(uid: int, ad_id: int) -> dict:
    r = await ad_next(uid)
    if not r.get("ad"): return {"error": "ad_wait", "wait": r.get("wait", 0)}
    ad = await db.fetchrow("SELECT * FROM ads WHERE id=$1 AND active", ad_id)
    if not ad: return {"error": "ad_gone"}
    vid = await db.fetchval("INSERT INTO ad_views(user_id, ad_id) VALUES($1,$2) RETURNING id", uid, ad_id)
    return {"view": vid, "url": ad["url"], "watch": AD_WATCH_SECONDS, "reward": ad["reward"]}

async def ad_claim(uid: int, vid: int) -> dict:
    async with db.acquire() as con:
        async with con.transaction():
            v = await con.fetchrow("SELECT * FROM ad_views WHERE id=$1 AND user_id=$2 FOR UPDATE", vid, uid)
            if not v or v["claimed"]: return {"error": "ad_gone"}
            left = AD_WATCH_SECONDS - (utcnow() - v["started"]).total_seconds()
            if left > 1: return {"error": "ad_early", "left": int(left) + 1}
            ad = await con.fetchrow("SELECT reward FROM ads WHERE id=$1", v["ad_id"])
            reward = ad["reward"] if ad else AD_DEFAULT_REWARD
            await con.execute("UPDATE ad_views SET claimed=TRUE WHERE id=$1", vid)
            await con.execute("UPDATE ads SET views=views+1 WHERE id=$1", v["ad_id"])
            pts = await con.fetchval("UPDATE users SET points=points+$2, ads_watched=ads_watched+1 WHERE id=$1 RETURNING points", uid, reward)
    return {"ok": True, "reward": reward, "points": pts}

# ─────────────────────── إدارة الإعلانات والإذاعة (أدمن) ───────────────────────
class AdmAd(StatesGroup):
    new = State()

class AdmBc(StatesGroup):
    text = State()

@admin_r.callback_query(F.data.regexp(r"^adm:(ads|adt:\d+|add:\d+)$"))
async def adm_ads(c: CallbackQuery):
    parts = c.data.split(":")
    if parts[1] == "adt":
        await db.execute("UPDATE ads SET active = NOT active WHERE id=$1", int(parts[2]))
    elif parts[1] == "add":
        await db.execute("DELETE FROM ads WHERE id=$1", int(parts[2]))
    rows = await db.fetch("SELECT * FROM ads ORDER BY id DESC LIMIT 20")
    lines = [f"#{r['id']} {'🟢' if r['active'] else '⚪️'} {html.escape(r['title'] or '')} — {r['reward']}⭐ — 👁 {r['views']}" for r in rows]
    txt = (f"📺 <b>الإعلانات</b>\n\nيظهر إعلان لكل مستخدم كل {AD_INTERVAL // 60} دقيقة، ويشاهده {AD_WATCH_SECONDS} ثانية ليحصل على نقاطه.\n"
           "إذا لم توجد إعلانات فعالة يظهر مكانها «مساحة إعلانية».\n\n" + ("\n".join(lines) if lines else "لا توجد إعلانات بعد."))
    btns = []
    for r in rows: btns.append([(f"{'⏸' if r['active'] else '▶️'} #{r['id']}", f"adm:adt:{r['id']}"), (f"🗑 #{r['id']}", f"adm:add:{r['id']}")])
    await show(c, txt, kb([("➕ إضافة إعلان", "adm:adn")], *btns, [("⬅️ رجوع", "admin")]))
    await c.answer()

@admin_r.callback_query(F.data == "adm:adn")
async def adm_ad_new(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdmAd.new)
    await c.message.answer("📺 أرسل الإعلان بهذه الصيغة (سطر واحد):\n<code>العنوان | الرابط | النقاط</code>\n"
                           "مثال: <code>قناة أخبار التقنية | https://t.me/mychannel | 2</code>\n\n/cancel للإلغاء")
    await c.answer()

@admin_r.message(AdmAd.new, F.text, ~F.text.startswith("/"))
async def adm_ad_save(m: Message, state: FSMContext):
    parts = [x.strip() for x in m.text.split("|")]
    try:
        title, url = parts[0], parts[1]
        reward = int(parts[2]) if len(parts) > 2 else AD_DEFAULT_REWARD
        assert title and url.startswith("http") and reward >= 0
    except (IndexError, ValueError, AssertionError):
        return await m.answer("❌ صيغة خاطئة. مثال: <code>العنوان | https://رابط | 2</code>")
    await db.execute("INSERT INTO ads(title,url,reward) VALUES($1,$2,$3)", title[:100], url, reward)
    await state.clear()
    await m.answer("✅ تمت إضافة الإعلان.", reply_markup=kb([("📺 الإعلانات", "adm:ads")]))

@admin_r.callback_query(F.data == "adm:bc")
async def adm_bc_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdmBc.text)
    await c.message.answer("📣 أرسل نص الإذاعة (HTML مسموح) وسيصل لكل المستخدمين غير المحظورين.\n/cancel للإلغاء")
    await c.answer()

@admin_r.message(AdmBc.text, F.text, ~F.text.startswith("/"))
async def adm_bc_send(m: Message, state: FSMContext):
    await state.clear()
    ids = [r["id"] for r in await db.fetch("SELECT id FROM users WHERE NOT banned")]
    await m.answer(f"⏳ جاري الإرسال إلى {len(ids)} مستخدم...")
    asyncio.create_task(broadcast(m.from_user.id, m.text, ids))

async def broadcast(admin_id, text, ids):
    ok = fail = 0
    for uid in ids:
        try: await bot.send_message(uid, text); ok += 1
        except Exception: fail += 1
        await asyncio.sleep(0.05)
    try: await bot.send_message(admin_id, f"📣 اكتملت الإذاعة\n✅ {ok}  ❌ {fail}")
    except Exception: pass

# ─────────────────── المهام الخلفية (التحقق والإشعارات) ───────────────────
async def background_worker():
    last_check = 0
    while True:
        try:
            # تنظيف الذاكرة
            now = time.time()
            for k in [k for k, v in VIEWED.items() if now - v > 3600]: VIEWED.pop(k, None)
            await db.execute("DELETE FROM ad_views WHERE started < now() - interval '7 days'")

            if now - last_check > 86400 or last_check == 0:
                last_check = now
                # 1) من غادر القناة بعد إكمال المهمة: خصم النقاط مرة واحدة فقط
                rows = await db.fetch("""
                    SELECT c.task_id, c.user_id, t.target, t.reward FROM completions c
                    JOIN tasks t ON c.task_id = t.id
                    WHERE t.type='channel' AND t.owner IS NOT NULL AND t.reward > 0
                    ORDER BY random() LIMIT 40""")
                for r in rows:
                    st = await member_state(r["target"], r["user_id"])
                    if st is False:  # None = تعذر التحقق → لا عقوبة
                        await db.execute("UPDATE users SET points = GREATEST(points - $1, 0) WHERE id = $2", r["reward"], r["user_id"])
                        await db.execute("DELETE FROM completions WHERE task_id=$1 AND user_id=$2", r["task_id"], r["user_id"])
                        ul = await db.fetchval("SELECT lang FROM users WHERE id=$1", r["user_id"])
                        try: await bot.send_message(r["user_id"], t(ul, "left_penalty", target=display(r["target"]), n=r["reward"]))
                        except Exception: pass
                    await asyncio.sleep(1)
                # 2) تذكير الخاملين (مرة لكل فترة خمول)
                for u in await db.fetch("""SELECT id, lang FROM users WHERE NOT banned
                        AND last_active < now() - interval '2 days' AND last_active > now() - interval '3 days'"""):
                    try: await bot.send_message(u["id"], t(u["lang"], "miss_you"))
                    except Exception: pass
                    await asyncio.sleep(0.1)
        except Exception as e:
            logging.error("Background worker error: %s", e)
        await asyncio.sleep(3600)

# ───────────────────────────────── Run ─────────────────────────────────
async def main():
    global db, BOT_ID, BOT_USERNAME
    logging.basicConfig(level=logging.INFO)
    db = await asyncpg.create_pool(DATABASE_URL)
    await db.execute(SCHEMA)
    me = await bot.get_me()
    BOT_ID, BOT_USERNAME = me.id, me.username
    
    # سيرفر الميني اب (API + الواجهة)
    import webapp
    await webapp.start(sys.modules[__name__])
    if WEBAPP_URL.startswith("https://"):
        try: await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🚀 Mini App", web_app=WebAppInfo(url=WEBAPP_URL)))
        except Exception as e: logging.warning("menu button: %s", e)
    BG.append(asyncio.create_task(background_worker()))
    
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(UserMW())
    dp.callback_query.outer_middleware(UserMW())
    dp.include_routers(admin_r, user_r)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
