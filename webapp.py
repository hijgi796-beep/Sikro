"""Mini App: خادم الويب + API. يعمل بجانب البوت في نفس العملية."""
import hashlib
import hmac
import json
import logging
import os
import time
import base64
from urllib.parse import parse_qsl

from aiohttp import web

from webapp_assets import INDEX_HTML, LOGO_B64

LOGO = base64.b64decode(LOGO_B64)


def check_init(init_data: str, token: str):
    """التحقق من توقيع Telegram initData. يرجع بيانات المستخدم أو None."""
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
        h = data.pop("hash", None)
        if not h: return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), h): return None
        if time.time() - int(data.get("auth_date", "0")) > 3 * 86400: return None
        return json.loads(data["user"])
    except Exception:
        return None


def build_app(core) -> web.Application:
    app = web.Application()

    def api(path, gated=True):
        def deco(fn):
            async def handler(request: web.Request):
                user = check_init(request.headers.get("X-Init-Data", ""), core.TOKEN)
                if not user: return web.json_response({"error": "auth"}, status=401)
                uid = user["id"]
                row = await core.db.fetchrow("SELECT banned, lang FROM users WHERE id=$1", uid)
                if not row: return web.json_response({"error": "nouser"}, status=403)
                if row["banned"] and uid not in core.ADMINS: return web.json_response({"error": "banned"}, status=403)
                try: body = await request.json()
                except Exception: body = {}
                try:
                    await core.db.execute("UPDATE users SET last_active=now() WHERE id=$1", uid)
                    if gated and uid not in core.ADMINS:
                        missing = await core.gate_missing(uid)
                        if missing: return web.json_response({"error": "gate", "tasks": gate_json(missing)})
                    return web.json_response(await fn(uid, row["lang"], body if isinstance(body, dict) else {}))
                except Exception:
                    logging.exception("api %s failed", path)
                    return web.json_response({"error": "server"}, status=500)
            app.router.add_post("/api/" + path, handler)
            return fn
        return deco

    def gate_json(rows):
        return [{"id": r["id"], "type": r["type"], "name": core.display(r["target"]),
                 "link": core.link(r["target"]), "descr": r["descr"]} for r in rows]

    async def my_state(uid):
        u = await core.db.fetchrow("SELECT * FROM users WHERE id=$1", uid)
        now = core.utcnow()
        dwait = 0
        if u["last_daily"]: dwait = max(0, int(86400 - (now - u["last_daily"]).total_seconds()))
        streak = u["streak"] or 0
        if u["last_daily"] and (now - u["last_daily"]).total_seconds() >= 172800: streak = 0
        lv = core.level_info(core.xp_of(u))
        return {
            "id": uid, "name": u["first_name"] or u["username"] or "User", "lang": u["lang"],
            "points": u["points"], "referrals": u["referrals"], "tasks_done": u["tasks_done"],
            "streak": streak, "daily_wait": dwait, "level": lv,
            "spin": core.spin_state(u), "ads_watched": u["ads_watched"],
            "ref_link": f"https://t.me/{core.BOT_USERNAME}?start=ref_{uid}",
            "ref_bonus": core.REF_BONUS, "cost": core.COST_PER_MEMBER, "daily_base": core.DAILY_BASE,
            "wheel": [p for p, _ in core.WHEEL], "levels": [[l[0], l[1], l[2], l[3]] for l in core.LEVELS],
            "ad_interval": core.AD_INTERVAL, "bot": core.BOT_USERNAME,
        }

    @api("me", gated=False)
    async def me(uid, lang, body):
        st = await my_state(uid)
        st["gate"] = gate_json(await core.gate_missing(uid)) if uid not in core.ADMINS else []
        return st

    @api("gate/open", gated=False)
    async def gate_open(uid, lang, body):
        core.VIEWED.setdefault((uid, int(body.get("id", 0))), time.time())
        return {"ok": True}

    @api("gate/verify", gated=False)
    async def gate_verify(uid, lang, body):
        return {"gate": gate_json(await core.gate_verify(uid))}

    @api("daily")
    async def daily(uid, lang, body):
        r = await core.claim_daily(uid)
        return {**r, "me": await my_state(uid)} if r["ok"] else {"error": "daily_wait", "wait": r["wait"]}

    @api("spin")
    async def spin(uid, lang, body):
        r = await core.spin_wheel(uid)
        return r if r["ok"] else {"error": "spin_wait", "wait": r["wait"]}

    @api("tasks")
    async def tasks(uid, lang, body):
        rows = await core.db.fetch("""SELECT * FROM tasks t WHERE active AND NOT mandatory AND done<total
            AND (owner IS NULL OR owner<>$1)
            AND NOT EXISTS(SELECT 1 FROM completions c WHERE c.task_id=t.id AND c.user_id=$1)
            ORDER BY id LIMIT 15""", uid)
        return {"tasks": [{"id": r["id"], "type": r["type"], "name": core.display(r["target"]), "link": core.link(r["target"]),
                           "reward": r["reward"], "descr": r["descr"]} for r in rows], "wait": core.BOT_WAIT_SECONDS}

    @api("task/open")
    async def task_open(uid, lang, body):
        core.VIEWED[(uid, int(body.get("id", 0)))] = time.time()
        return {"ok": True}

    @api("task/done")
    async def task_done(uid, lang, body):
        r = await core.complete_task(uid, int(body.get("id", 0)))
        if r["code"] == "ok": return {"ok": True, "reward": r["reward"], "points": r["points"]}
        return {"error": "task_" + r["code"], "left": r.get("left", 0)}

    @api("ad/next")
    async def ad_next(uid, lang, body):
        return await core.ad_next(uid)

    @api("ad/start")
    async def ad_start(uid, lang, body):
        return await core.ad_start(uid, int(body.get("id", 0)))

    @api("ad/claim")
    async def ad_claim(uid, lang, body):
        return await core.ad_claim(uid, int(body.get("view", 0)))

    @api("leaderboard")
    async def leaderboard(uid, lang, body):
        wk = await core.weekly_top(10)
        al = await core.alltime_top(10)
        mine = await core.db.fetchval("""SELECT count(*)::int FROM users WHERE joined >= date_trunc('week', now())
                                         AND referred_by=$1""", uid)
        f = lambda rows: [{"name": r["name"] or "User", "n": r["n"], "me": r["id"] == uid} for r in rows]
        return {"weekly": f(wk), "all": f(al), "mine_week": mine}

    @api("order")
    async def order(uid, lang, body):
        try: qty = int(body.get("qty", 0))
        except (TypeError, ValueError): qty = 0
        return await core.create_order(uid, str(body.get("kind", "")), str(body.get("target", "")), qty)

    @api("orders")
    async def orders(uid, lang, body):
        rows = await core.db.fetch("SELECT type, target, done, total, active FROM tasks WHERE owner=$1 ORDER BY id DESC LIMIT 10", uid)
        return {"orders": [{"type": r["type"], "name": core.display(r["target"]), "done": r["done"], "total": r["total"]} for r in rows]}

    async def index(request):
        return web.Response(text=INDEX_HTML, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def logo(request):
        return web.Response(body=LOGO, content_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

    app.router.add_get("/", index)
    app.router.add_get("/logo.jpg", logo)
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    return app


async def start(core):
    runner = web.AppRunner(build_app(core))
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logging.info("Mini App server on port %s", port)
