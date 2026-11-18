import asyncio
import html
import io
import logging
import os
import shutil
import sqlite3
import threading
import time
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M, InputFile, Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          ContextTypes, MessageHandler, filters)

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
# کانال و گروه اجباری، با کاما جدا کن: @mychannel,@mygroup (ربات باید ادمین هر دو باشد)
CHATS = [c.strip() for c in (os.environ.get("REQUIRED_CHATS") or os.environ.get("REQUIRED_CHANNEL", "")).split(",") if c.strip()]
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")  # آدرس https سرویس Render
BOT_USERNAME = ""
DB_PATH = os.environ.get("DB_PATH", "data.db")
PER = 5

# ───────────────────────── دیتابیس ─────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT, name TEXT, joined INTEGER, banned INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS scripts(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, game TEXT, description TEXT,
  code TEXT, norm TEXT, created INTEGER, hidden INTEGER DEFAULT 0, views INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS ratings(user_id INTEGER, script_id INTEGER, stars INTEGER, PRIMARY KEY(user_id, script_id));
CREATE TABLE IF NOT EXISTS seen(user_id INTEGER, script_id INTEGER, ts INTEGER, PRIMARY KEY(user_id, script_id));
CREATE TABLE IF NOT EXISTS favs(user_id INTEGER, script_id INTEGER, ts INTEGER, PRIMARY KEY(user_id, script_id));
"""


def connect():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    c.commit()
    return c


db = connect()


def q(sql, p=()):
    return db.execute(sql, p).fetchall()


def run(sql, p=()):
    db.execute(sql, p)
    db.commit()


def norm(t):
    return (t or "").replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ").lower().strip()


esc = html.escape

# ───────────────────────── ابزارهای کمکی ─────────────────────────
HOME = "🎮 <b>به ربات اسکریپت روبلاکس خوش آمدی!</b>\n\nاسکریپت بازی مورد نظرت رو جستجو کن، امتیاز بده و به علاقه‌مندی‌ها اضافه کن."


def menu(uid):
    rows = [
        [B("🔍 جستجوی اسکریپت", callback_data="search")],
        [B("🔥 برترین‌ها", callback_data="ls:top:0"), B("🆕 جدیدترین‌ها", callback_data="ls:new:0")],
        [B("🎮 لیست بازی‌ها", callback_data="games")],
        [B("👁 دیده‌شده‌ها", callback_data="ls:seen:0"), B("❤️ علاقه‌مندی‌ها", callback_data="ls:fav:0")],
    ]
    if WEBAPP_URL:
        rows.insert(0, [B("🌐 اکسپلورر (نسخه وب)", web_app=WebAppInfo(url=WEBAPP_URL))])
    if uid in ADMINS:
        rows.append([B("⚙️ پنل مدیریت", callback_data="ad:home")])
    return M(rows)


async def reply(update, text, markup=None):
    cq = update.callback_query
    if cq:
        try:
            await cq.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
    await update.effective_message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def gate(update, ctx):
    u = update.effective_user
    run("INSERT INTO users(id,username,name,joined) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET username=excluded.username, name=excluded.name",
        (u.id, u.username or "", u.full_name, int(time.time())))
    if u.id in ADMINS:
        return True
    if q("SELECT banned FROM users WHERE id=?", (u.id,))[0]["banned"]:
        await reply(update, "⛔ دسترسی شما به ربات مسدود شده است.")
        return False
    for c in CHATS:
        try:
            m = await ctx.bot.get_chat_member(c, u.id)
            ok = not (m.status in ("left", "kicked") or (m.status == "restricted" and not m.is_member))
        except Exception as e:
            logging.error("membership check failed for %s: %s", c, e)
            await reply(update, "⚠️ ربات در کانال/گروه ادمین نیست یا آدرس اشتباه است. به ادمین خبر بده.")
            return False
        if not ok:
            kb = [[B(f"📢 عضویت در {x}", url=f"https://t.me/{x.lstrip('@')}")] for x in CHATS]
            kb.append([B("✅ عضو شدم", callback_data="chk")])
            await reply(update, "برای دریافت اسکریپت اول در کانال و گروه زیر عضو شو، بعد «عضو شدم» را بزن:", M(kb))
            return False
    return True


# ───────────────────────── لیست‌ها ─────────────────────────
SEL = ("SELECT s.id, s.title, s.game, s.hidden, s.views, "
       "COALESCE((SELECT AVG(stars) FROM ratings r WHERE r.script_id=s.id),0) AS avg, "
       "(SELECT COUNT(*) FROM ratings r WHERE r.script_id=s.id) AS cnt FROM scripts s")
TITLES = {"search": "🔍 نتایج جستجو", "top": "🔥 برترین اسکریپت‌ها", "new": "🆕 جدیدترین‌ها",
          "seen": "👁 دیده‌شده‌ها", "fav": "❤️ علاقه‌مندی‌ها", "g": "🎮 بازی", "al": "📋 مدیریت اسکریپت‌ها"}


def fetch(kind, uid, page, ctx):
    join, jp, where, wp, order = "", [], "s.hidden=0", [], "s.id DESC"
    if kind == "search":
        for w in norm(ctx.user_data.get("q", "")).split():
            where += " AND s.norm LIKE ?"
            wp.append(f"%{w}%")
        order = "s.views DESC"
    elif kind == "top":
        where += " AND EXISTS(SELECT 1 FROM ratings r2 WHERE r2.script_id=s.id)"
        order = "avg DESC, cnt DESC"
    elif kind in ("seen", "fav"):
        t = "seen" if kind == "seen" else "favs"
        join, jp, order = f" JOIN {t} x ON x.script_id=s.id AND x.user_id=?", [uid], "x.ts DESC"
    elif kind == "g":
        where += " AND s.game=?"
        wp.append(ctx.user_data.get("game", ""))
    elif kind == "al":
        where = "1=1"
    total = q(f"SELECT COUNT(*) c FROM scripts s{join} WHERE {where}", jp + wp)[0]["c"]
    rows = q(f"{SEL}{join} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?", jp + wp + [PER, page * PER])
    return rows, total


async def show_list(update, ctx, kind, page):
    uid = update.effective_user.id
    if kind == "al" and uid not in ADMINS:
        return
    page = max(0, page)
    rows, total = fetch(kind, uid, page, ctx)
    pages = max(1, -(-total // PER))
    if page >= pages:
        page = pages - 1
        rows, total = fetch(kind, uid, page, ctx)
    title = TITLES[kind]
    if kind == "g":
        title += f": {esc(ctx.user_data.get('game', ''))}"
    if kind == "search":
        title += f" «{esc(ctx.user_data.get('q', ''))}»"
    text = f"<b>{title}</b>\nصفحه {page + 1} از {pages} • {total} مورد" if rows else f"<b>{title}</b>\n\nچیزی پیدا نشد."
    kb = [[B(f"{'🙈 ' if r['hidden'] else ''}{r['title'][:28]} | {r['game'][:14]} | ⭐{r['avg']:.1f}",
             callback_data=f"sc:{r['id']}:{kind}:{page}")] for r in rows]
    nav = []
    if page > 0:
        nav.append(B("◀️ قبلی", callback_data=f"ls:{kind}:{page - 1}"))
    if page + 1 < pages:
        nav.append(B("بعدی ▶️", callback_data=f"ls:{kind}:{page + 1}"))
    if nav:
        kb.append(nav)
    if kind == "search":
        kb.append([B("🔍 جستجوی دوباره", callback_data="search")])
    kb.append([B("🏠 منو", callback_data="home")])
    await reply(update, text, M(kb))


# ───────────────────────── نمایش اسکریپت ─────────────────────────
async def send_code(ctx, chat_id, s):
    body = f"<pre>{esc(s['code'])}</pre>"
    if len(body) <= 4000:
        await ctx.bot.send_message(chat_id, body, parse_mode=ParseMode.HTML)
    else:
        await send_file(ctx, chat_id, s)


async def send_file(ctx, chat_id, s):
    name = "".join(c for c in s["title"] if c.isalnum() or c in "-_") or "script"
    await ctx.bot.send_document(chat_id, InputFile(io.BytesIO(s["code"].encode()), filename=f"{name}.lua"))


async def show_script(update, ctx, sid, kind, page, first_open):
    uid = update.effective_user.id
    rows = q("SELECT * FROM scripts WHERE id=?", (sid,))
    if not rows or (rows[0]["hidden"] and uid not in ADMINS):
        await reply(update, "این اسکریپت پیدا نشد.", M([[B("🏠 منو", callback_data="home")]]))
        return
    s = rows[0]
    if first_open:
        if not q("SELECT 1 FROM seen WHERE user_id=? AND script_id=?", (uid, sid)):
            run("UPDATE scripts SET views=views+1 WHERE id=?", (sid,))
        run("INSERT OR REPLACE INTO seen VALUES(?,?,?)", (uid, sid, int(time.time())))
        s = q("SELECT * FROM scripts WHERE id=?", (sid,))[0]
    st = q("SELECT COALESCE(AVG(stars),0) a, COUNT(*) c FROM ratings WHERE script_id=?", (sid,))[0]
    mine = q("SELECT stars FROM ratings WHERE user_id=? AND script_id=?", (uid, sid))
    mine = mine[0]["stars"] if mine else 0
    is_fav = bool(q("SELECT 1 FROM favs WHERE user_id=? AND script_id=?", (uid, sid)))
    text = (f"🎮 <b>{esc(s['title'])}</b>\n🕹 بازی: {esc(s['game'])}\n\n"
            f"{esc(s['description'])}\n\n"
            f"⭐ امتیاز: <b>{st['a']:.1f}</b> از ۵ ({st['c']} رأی)\n👁 بازدید: {s['views']}\n\n"
            + (f"امتیاز شما: {'⭐' * mine}" if mine else "به این اسکریپت امتیاز بده 👇"))
    if uid in ADMINS and BOT_USERNAME:
        text += f"\n\n🔗 لینک اختصاصی:\n<code>https://t.me/{BOT_USERNAME}?start=s_{sid}</code>"
    cx = f"{kind}:{page}"
    kb = [
        [B(("✅" if i == mine else "") + f"{i}⭐", callback_data=f"rt:{sid}:{i}:{cx}") for i in range(1, 6)],
        [B("💔 حذف از علاقه‌مندی" if is_fav else "❤️ علاقه‌مندی", callback_data=f"fv:{sid}:{cx}"),
         B("📄 دریافت فایل", callback_data=f"fl:{sid}")],
    ]
    if uid in ADMINS:
        kb.append([B("🗑 حذف", callback_data=f"ad:del:{sid}:{cx}"),
                   B("👁 نمایش" if s["hidden"] else "🙈 مخفی", callback_data=f"ad:hide:{sid}:{cx}")])
    kb.append([B("🔙 بازگشت", callback_data=f"ls:{kind}:{page}")])
    await reply(update, text, M(kb))
    if first_open:
        await send_code(ctx, update.effective_chat.id, s)


# ───────────────────────── پنل ادمین ─────────────────────────
def panel():
    return M([
        [B("➕ افزودن اسکریپت", callback_data="ad:add"), B("📋 مدیریت اسکریپت‌ها", callback_data="ls:al:0")],
        [B("👥 اعضا", callback_data="ad:users:0"), B("📊 آمار", callback_data="ad:stats")],
        [B("📢 پیام همگانی", callback_data="ad:bc"), B("🚫 بن / آنبن", callback_data="ad:ban")],
        [B("💾 بکاپ دیتابیس", callback_data="ad:bk")],
        [B("🏠 منو", callback_data="home")],
    ])


async def send_backup(ctx, chat_id):
    tmp = "backup.db"
    dst = sqlite3.connect(tmp)
    db.backup(dst)
    dst.close()
    with open(tmp, "rb") as f:
        await ctx.bot.send_document(chat_id, f, filename="backup.db",
                                    caption="💾 بکاپ. برای بازگردانی همین فایل را با کپشن /restore بفرست.")


async def broadcast(ctx, admin_id, text):
    ids = [r["id"] for r in q("SELECT id FROM users WHERE banned=0")]
    ok = 0
    for i in ids:
        try:
            await ctx.bot.send_message(i, text)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await ctx.bot.send_message(admin_id, f"📢 ارسال شد به {ok} از {len(ids)} نفر.")


async def admin_cb(update, ctx, d):
    a = d[1]
    cq = update.callback_query
    back = M([[B("⚙️ پنل", callback_data="ad:home")]])
    if a == "home":
        ctx.user_data.clear()
        await reply(update, "⚙️ <b>پنل مدیریت</b>", panel())
    elif a == "stats":
        day = int(time.time()) // 86400 * 86400
        n = lambda sql: q(sql)[0][0]
        t = (f"📊 <b>آمار</b>\n\n👥 اعضا: {n('SELECT COUNT(*) FROM users')} (امروز: "
             f"{q('SELECT COUNT(*) FROM users WHERE joined>=?', (day,))[0][0]})\n"
             f"🚫 بن‌شده: {n('SELECT COUNT(*) FROM users WHERE banned=1')}\n"
             f"📜 اسکریپت: {n('SELECT COUNT(*) FROM scripts')}\n"
             f"⭐ رأی‌ها: {n('SELECT COUNT(*) FROM ratings')}\n"
             f"👁 بازدید کل: {n('SELECT COALESCE(SUM(views),0) FROM scripts')}\n"
             f"❤️ علاقه‌مندی‌ها: {n('SELECT COUNT(*) FROM favs')}")
        await reply(update, t, back)
    elif a == "add":
        ctx.user_data.update(st="a_title", new={})
        await reply(update, "➕ <b>عنوان اسکریپت</b> را بفرست:\n(لغو: /cancel)")
    elif a == "bc":
        ctx.user_data["st"] = "bc"
        await reply(update, "📢 متن پیام همگانی را بفرست:\n(لغو: /cancel)")
    elif a == "ban":
        ctx.user_data["st"] = "ban"
        await reply(update, "🚫 آیدی عددی کاربر را بفرست (برای بن یا آنبن):")
    elif a == "bk":
        await send_backup(ctx, cq.message.chat_id)
    elif a == "users":
        page = int(d[2])
        total = q("SELECT COUNT(*) FROM users")[0][0]
        rows = q("SELECT * FROM users ORDER BY joined DESC LIMIT 10 OFFSET ?", (page * 10,))
        lines = [f"{'🚫' if r['banned'] else '👤'} <code>{r['id']}</code> {esc(r['name'] or '')} "
                 f"{'@' + esc(r['username']) if r['username'] else ''}" for r in rows]
        nav = []
        if page > 0:
            nav.append(B("◀️", callback_data=f"ad:users:{page - 1}"))
        if (page + 1) * 10 < total:
            nav.append(B("▶️", callback_data=f"ad:users:{page + 1}"))
        kb = ([nav] if nav else []) + [[B("⚙️ پنل", callback_data="ad:home")]]
        await reply(update, f"👥 <b>اعضا ({total})</b>\n\n" + "\n".join(lines), M(kb))
    elif a == "del":
        sid, cx = int(d[2]), f"{d[3]}:{d[4]}"
        await reply(update, "⚠️ این اسکریپت برای همیشه حذف شود؟",
                    M([[B("✅ بله، حذف", callback_data=f"ad:delyes:{sid}:{cx}"),
                        B("❌ نه", callback_data=f"sc:{sid}:{cx}")]]))
    elif a == "delyes":
        sid = int(d[2])
        for t in ("scripts WHERE id", "ratings WHERE script_id", "seen WHERE script_id", "favs WHERE script_id"):
            db.execute(f"DELETE FROM {t}=?", (sid,))
        db.commit()
        await show_list(update, ctx, d[3], int(d[4]))
    elif a == "hide":
        run("UPDATE scripts SET hidden=1-hidden WHERE id=?", (int(d[2]),))
        await show_script(update, ctx, int(d[2]), d[3], int(d[4]), False)


# ───────────────────────── هندلرها ─────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    arg = ctx.args[0] if ctx.args else ""
    if arg.startswith("s_") and arg[2:].isdigit():
        ctx.user_data["pending"] = int(arg[2:])
    if await gate(update, ctx):
        sid = ctx.user_data.pop("pending", None)
        if sid:
            await show_script(update, ctx, sid, "new", 0, True)
        else:
            await reply(update, HOME, menu(update.effective_user.id))


async def admin_cmd(update, ctx):
    if update.effective_user.id in ADMINS:
        await reply(update, "⚙️ <b>پنل مدیریت</b>", panel())


async def cancel(update, ctx):
    ctx.user_data.clear()
    await reply(update, "لغو شد.", menu(update.effective_user.id))


async def backup_cmd(update, ctx):
    if update.effective_user.id in ADMINS:
        await send_backup(ctx, update.effective_chat.id)


async def on_cb(update, ctx):
    cq = update.callback_query
    await cq.answer()
    if not await gate(update, ctx):
        return
    d = cq.data.split(":")
    a, uid = d[0], cq.from_user.id
    if a == "chk":
        sid = ctx.user_data.pop("pending", None)
        if sid:
            await show_script(update, ctx, sid, "new", 0, True)
        else:
            await reply(update, HOME, menu(uid))
    elif a == "home":
        ctx.user_data.clear()
        await reply(update, HOME, menu(uid))
    elif a == "search":
        ctx.user_data["st"] = "search"
        await reply(update, "🔍 اسم بازی یا اسکریپت را بفرست:", M([[B("🏠 منو", callback_data="home")]]))
    elif a == "games":
        games = [r["game"] for r in q("SELECT game FROM scripts WHERE hidden=0 GROUP BY game "
                                      "ORDER BY COUNT(*) DESC LIMIT 40")]
        ctx.user_data["games"] = games
        kb = [[B(g[:40], callback_data=f"gm:{i}")] for i, g in enumerate(games)]
        kb.append([B("🏠 منو", callback_data="home")])
        await reply(update, "🎮 <b>بازی‌ها</b>" if games else "هنوز بازی‌ای ثبت نشده.", M(kb))
    elif a == "gm":
        games = ctx.user_data.get("games", [])
        if int(d[1]) < len(games):
            ctx.user_data["game"] = games[int(d[1])]
            await show_list(update, ctx, "g", 0)
        else:
            await reply(update, HOME, menu(uid))
    elif a == "ls":
        await show_list(update, ctx, d[1], int(d[2]))
    elif a == "sc":
        await show_script(update, ctx, int(d[1]), d[2], int(d[3]), True)
    elif a == "rt":
        run("INSERT OR REPLACE INTO ratings VALUES(?,?,?)", (uid, int(d[1]), int(d[2])))
        await show_script(update, ctx, int(d[1]), d[3], int(d[4]), False)
    elif a == "fv":
        sid = int(d[1])
        if q("SELECT 1 FROM favs WHERE user_id=? AND script_id=?", (uid, sid)):
            run("DELETE FROM favs WHERE user_id=? AND script_id=?", (uid, sid))
        else:
            run("INSERT INTO favs VALUES(?,?,?)", (uid, sid, int(time.time())))
        await show_script(update, ctx, sid, d[2], int(d[3]), False)
    elif a == "fl":
        r = q("SELECT * FROM scripts WHERE id=? AND (hidden=0 OR ?)", (int(d[1]), uid in ADMINS))
        if r:
            await send_file(ctx, cq.message.chat_id, r[0])
    elif a == "ad" and uid in ADMINS:
        await admin_cb(update, ctx, d)


async def on_text(update, ctx):
    if not await gate(update, ctx):
        return
    uid, st, t = update.effective_user.id, ctx.user_data.get("st"), update.message.text.strip()
    adm = uid in ADMINS
    if st == "search":
        ctx.user_data.update(q=t, st=None)
        await show_list(update, ctx, "search", 0)
    elif adm and st == "a_title":
        ctx.user_data["new"]["title"] = t
        ctx.user_data["st"] = "a_game"
        await reply(update, "🕹 <b>اسم بازی</b> را بفرست:")
    elif adm and st == "a_game":
        ctx.user_data["new"]["game"] = t
        ctx.user_data["st"] = "a_desc"
        await reply(update, "📝 <b>توضیحات</b> را بفرست (برای رد کردن: -):")
    elif adm and st == "a_desc":
        ctx.user_data["new"]["desc"] = "" if t == "-" else t
        ctx.user_data["st"] = "a_code"
        await reply(update, "📜 <b>کد اسکریپت</b> را به‌صورت متن یا فایل (.txt / .lua) بفرست:")
    elif adm and st == "a_code":
        await save_script(update, ctx, t)
    elif adm and st == "bc":
        ctx.user_data.clear()
        ctx.application.create_task(broadcast(ctx, uid, t))
        await reply(update, "📢 ارسال شروع شد…", panel())
    elif adm and st == "ban" and t.lstrip("-").isdigit():
        ctx.user_data.clear()
        r = q("SELECT banned FROM users WHERE id=?", (int(t),))
        if r:
            run("UPDATE users SET banned=1-banned WHERE id=?", (int(t),))
            await reply(update, "🚫 بن شد." if not r[0]["banned"] else "✅ آنبن شد.", panel())
        else:
            await reply(update, "کاربری با این آیدی پیدا نشد.", panel())
    else:
        await reply(update, "از منو یکی را انتخاب کن 👇", menu(uid))


async def save_script(update, ctx, code):
    n = ctx.user_data.pop("new", {})
    ctx.user_data.clear()
    cur = db.execute("INSERT INTO scripts(title,game,description,code,norm,created) VALUES(?,?,?,?,?,?)",
                     (n["title"], n["game"], n.get("desc", ""), code,
                      norm(f"{n['title']} {n['game']} {n.get('desc', '')}"), int(time.time())))
    db.commit()
    link = f"https://t.me/{BOT_USERNAME}?start=s_{cur.lastrowid}"
    await reply(update, f"✅ اسکریپت ثبت شد.\n\n🔗 لینک اختصاصی (بفرست توی کانال):\n<code>{link}</code>",
                M([[B("➕ افزودن دیگر", callback_data="ad:add"), B("⚙️ پنل", callback_data="ad:home")]]))


async def on_doc(update, ctx):
    global db
    if update.effective_user.id not in ADMINS:
        return
    doc = update.message.document
    cap = (update.message.caption or "").strip()
    if cap == "/restore":
        p = "restore.tmp"
        await (await doc.get_file()).download_to_drive(p)
        try:
            t = sqlite3.connect(p)
            t.execute("SELECT COUNT(*) FROM scripts")
            t.close()
        except Exception:
            await update.message.reply_text("❌ فایل بکاپ معتبر نیست.")
            return
        db.close()
        shutil.copy(p, DB_PATH)
        db = connect()
        await update.message.reply_text("✅ بازگردانی شد.")
    elif ctx.user_data.get("st") == "a_code":
        if doc.file_size and doc.file_size > 1_000_000:
            await update.message.reply_text("❌ فایل بزرگ‌تر از ۱ مگابایت است.")
            return
        data = await (await doc.get_file()).download_as_bytearray()
        await save_script(update, ctx, bytes(data).decode("utf-8", errors="replace"))


# ───────────────────────── وب‌سرور سلامت (برای Render) ─────────────────────────
def api_scripts(qs):
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    where, p = "s.hidden=0", []
    for w in norm(qs.get("q", [""])[0]).split():
        where += " AND s.norm LIKE ?"
        p.append(f"%{w}%")
    g = qs.get("game", [""])[0]
    if g:
        where += " AND s.game=?"
        p.append(g)
    rows = c.execute(f"{SEL} WHERE {where} ORDER BY s.views DESC LIMIT 100", p).fetchall()
    games = [r[0] for r in c.execute("SELECT game FROM scripts WHERE hidden=0 GROUP BY game ORDER BY COUNT(*) DESC LIMIT 30")]
    c.close()
    return {"bot": BOT_USERNAME, "games": games,
            "items": [{"id": r["id"], "title": r["title"], "game": r["game"], "views": r["views"],
                       "avg": round(r["avg"], 1), "cnt": r["cnt"]} for r in rows]}


class Web(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path == "/api/scripts":
                body, ctype = json.dumps(api_scripts(parse_qs(u.query)), ensure_ascii=False).encode(), "application/json"
            elif u.path == "/":
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"), "rb") as f:
                    body, ctype = f.read(), "text/html"
            else:
                body, ctype = b"ok", "text/plain"
        except Exception:
            body, ctype = b"error", "text/plain"
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def health():
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), Web).serve_forever()


async def post_init(app):
    global BOT_USERNAME
    BOT_USERNAME = (await app.bot.get_me()).username


def main():
    threading.Thread(target=health, daemon=True).start()
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = (Application.builder().token(TOKEN).post_init(post_init)
           .connect_timeout(30).read_timeout(30).write_timeout(30).pool_timeout(30)
           .get_updates_connect_timeout(30).get_updates_read_timeout(30)
           .get_updates_write_timeout(30).get_updates_pool_timeout(30).build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.Document.ALL, on_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(drop_pending_updates=True, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
