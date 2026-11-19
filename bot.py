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
import csv
from urllib.parse import quote

import httpx
from telegram import (InlineKeyboardButton as B, InlineKeyboardMarkup as M, InputFile, MenuButtonWebApp, Update, WebAppInfo,
                      InlineQueryResultArticle, InputTextMessageContent)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          ContextTypes, MessageHandler, InlineQueryHandler, filters)

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # جلوگیری از چاپ توکن در لاگ
logging.getLogger("httpcore").setLevel(logging.WARNING)
import webapp  # noqa: E402  (بک‌اند مینی‌اپ)

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_LIST = [int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x]
ADMINS = set(ADMIN_LIST)
# کانال و گروه اجباری، با کاما جدا کن: @mychannel,@mygroup (ربات باید ادمین هر دو باشد)
CHATS = [c.strip() for c in (os.environ.get("REQUIRED_CHATS") or os.environ.get("REQUIRED_CHANNEL", "")).split(",") if c.strip()]
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")  # آدرس https سرویس Render
BOT_USERNAME = ""
DB_PATH = os.environ.get("DB_PATH", "data.db")
# چتی که ویدیوها و بکاپ خودکار در آن ذخیره می‌شود (آیدی عددی ادمین یا کانال خصوصی که ربات ادمینش است)
STORAGE_CHAT = os.environ.get("STORAGE_CHAT") or (str(ADMIN_LIST[0]) if ADMIN_LIST else "")
APP_SHORTNAME = os.environ.get("APP_SHORTNAME", "")  # اختیاری: shortname مینی‌اپ در BotFather برای لینک اشتراک
webapp.init(TOKEN, ADMINS, CHATS, DB_PATH, lambda: BOT_USERNAME, STORAGE_CHAT, APP_SHORTNAME)
PER = 5
CPER = 5  # تعداد نظر در هر صفحه
REPORT_LIMIT = int(os.environ.get("REPORT_LIMIT", "5"))  # بعد از این تعداد گزارش، اسکریپت خودکار مخفی می‌شود

# ───────────────────────── دیتابیس ─────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT, name TEXT, joined INTEGER, banned INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS scripts(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, game TEXT, description TEXT,
  code TEXT, norm TEXT, created INTEGER, hidden INTEGER DEFAULT 0, views INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS ratings(user_id INTEGER, script_id INTEGER, stars INTEGER, PRIMARY KEY(user_id, script_id));
CREATE TABLE IF NOT EXISTS seen(user_id INTEGER, script_id INTEGER, ts INTEGER, PRIMARY KEY(user_id, script_id));
CREATE TABLE IF NOT EXISTS favs(user_id INTEGER, script_id INTEGER, ts INTEGER, PRIMARY KEY(user_id, script_id));
CREATE TABLE IF NOT EXISTS likes(user_id INTEGER, script_id INTEGER, ts INTEGER, PRIMARY KEY(user_id, script_id));
CREATE TABLE IF NOT EXISTS comments(id INTEGER PRIMARY KEY AUTOINCREMENT, script_id INTEGER, user_id INTEGER, text TEXT, created INTEGER);
CREATE TABLE IF NOT EXISTS follows(follower INTEGER, target INTEGER, ts INTEGER, PRIMARY KEY(follower, target));
CREATE TABLE IF NOT EXISTS reports(user_id INTEGER, script_id INTEGER, ts INTEGER, PRIMARY KEY(user_id, script_id));
CREATE TABLE IF NOT EXISTS game_follows(user_id INTEGER, game TEXT, ts INTEGER, PRIMARY KEY(user_id, game));
CREATE TABLE IF NOT EXISTS search_log(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, q TEXT, ts INTEGER);
"""


def migrate(c):
    """ستون‌های جدید را به دیتابیس‌های قدیمی (و بکاپ‌های قدیمی) اضافه می‌کند."""
    for t, col, ddl in (("scripts", "user_id", "INTEGER DEFAULT 0"), ("scripts", "kind", "TEXT DEFAULT 'script'"),
                        ("scripts", "file_id", "TEXT DEFAULT ''"), ("scripts", "thumb", "TEXT DEFAULT ''"),
                        ("scripts", "checked", "INTEGER DEFAULT 0"), ("users", "photo", "TEXT DEFAULT ''")):
        if col not in {r[1] for r in c.execute(f"PRAGMA table_info({t})")}:
            c.execute(f"ALTER TABLE {t} ADD COLUMN {col} {ddl}")
    c.executescript("CREATE INDEX IF NOT EXISTS ix_scripts_user ON scripts(user_id);"
                    "CREATE INDEX IF NOT EXISTS ix_comments_script ON comments(script_id);"
                    "CREATE INDEX IF NOT EXISTS ix_likes_script ON likes(script_id);"
                    "CREATE INDEX IF NOT EXISTS ix_reports_script ON reports(script_id);"
                    "CREATE INDEX IF NOT EXISTS ix_seen_ts ON seen(ts);")
    c.commit()


def connect():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    migrate(c)
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
_rl = {}


def limited(uid, key, limit, win):
    """محدودیت نرخ ساده در حافظه؛ ادمین‌ها معاف‌اند."""
    if uid in ADMINS:
        return False
    now, k = time.time(), (uid, key)
    hits = [t for t in _rl.get(k, []) if now - t < win]
    if len(hits) >= limit:
        _rl[k] = hits
        return True
    hits.append(now)
    _rl[k] = hits
    return False


async def tell_admins(ctx, text):
    for a in ADMINS:
        try:
            await ctx.bot.send_message(a, text, disable_web_page_preview=True)
        except Exception:
            pass


def slink(sid):
    return f"https://t.me/{BOT_USERNAME}?start=s_{sid}"


HELP = ("ℹ️ <b>راهنما</b>\n\n"
        "🔍 جستجو: اسم بازی یا اسکریپت\n"
        "👍 لایک، ⭐ امتیاز، ❤️ علاقه‌مندی و 💬 نظر زیر هر اسکریپت\n"
        "📩 ارسال به پیوی: اسکریپت دوباره برایت فرستاده می‌شود\n"
        "🔔 دنبال کردن بازی: اسکریپت جدید بازی را به تو خبر می‌دهیم\n"
        "🚩 کار نمی‌کنه: گزارش اسکریپت خراب\n"
        "🔗 اشتراک: لینک اسکریپت را برای دوستانت بفرست\n"
        "⌨️ توی هر چتی بنویس <code>@ربات اسم‌بازی</code> تا جستجوی سریع بیاید")


HOME = "🎮 <b>به ربات اسکریپت روبلاکس خوش آمدی!</b>\n\nاسکریپت بازی مورد نظرت رو جستجو کن، امتیاز بده و به علاقه‌مندی‌ها اضافه کن."


def menu(uid):
    rows = [
        [B("🔍 جستجوی اسکریپت", callback_data="search")],
        [B("🔥 برترین‌ها", callback_data="ls:top:0"), B("🆕 جدیدترین‌ها", callback_data="ls:new:0")],
        [B("🔥 داغ این هفته", callback_data="ls:hot:0"), B("🔔 بازی‌های دنبال‌شده", callback_data="ls:gf:0")],
        [B("🎮 لیست بازی‌ها", callback_data="games")],
        [B("👁 دیده‌شده‌ها", callback_data="ls:seen:0"), B("❤️ علاقه‌مندی‌ها", callback_data="ls:fav:0")],
        [B("ℹ️ راهنما", callback_data="help")],
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
          "seen": "👁 دیده‌شده‌ها", "fav": "❤️ علاقه‌مندی‌ها", "g": "🎮 بازی", "al": "📋 مدیریت اسکریپت‌ها",
          "hot": "🔥 داغ این هفته", "gf": "🔔 بازی‌های دنبال‌شده", "rep": "🚩 گزارش‌شده‌ها"}


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
    elif kind == "hot":
        join = (" JOIN (SELECT script_id, COUNT(*) hc FROM seen WHERE ts>=? GROUP BY script_id) h ON h.script_id=s.id")
        jp, order = [int(time.time()) - 7 * 86400], "h.hc DESC, s.id DESC"
    elif kind == "gf":
        where += " AND s.game IN (SELECT game FROM game_follows WHERE user_id=?)"
        wp.append(uid)
    elif kind == "rep":
        where = "EXISTS(SELECT 1 FROM reports r3 WHERE r3.script_id=s.id)"
        order = "(SELECT COUNT(*) FROM reports r4 WHERE r4.script_id=s.id) DESC"
    total = q(f"SELECT COUNT(*) c FROM scripts s{join} WHERE {where}", jp + wp)[0]["c"]
    rows = q(f"{SEL}{join} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?", jp + wp + [PER, page * PER])
    return rows, total


async def show_list(update, ctx, kind, page):
    uid = update.effective_user.id
    if kind in ("al", "rep") and uid not in ADMINS:
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
    if s["kind"] == "video":
        return await send_file(ctx, chat_id, s)
    body = f"<pre>{esc(s['code'])}</pre>"
    if len(body) <= 4000:
        await ctx.bot.send_message(chat_id, body, parse_mode=ParseMode.HTML)
    else:
        await send_file(ctx, chat_id, s)


async def send_file(ctx, chat_id, s):
    if s["kind"] == "video" and s["file_id"]:
        try:
            await ctx.bot.send_video(chat_id, s["file_id"], caption=s["title"][:200])
        except BadRequest:
            await ctx.bot.send_document(chat_id, s["file_id"], caption=s["title"][:200])
        return
    name = "".join(c for c in s["title"] if c.isalnum() or c in "-_") or "script"
    await ctx.bot.send_document(chat_id, InputFile(io.BytesIO(s["code"].encode()), filename=f"{name}.lua"))


async def show_script(update, ctx, sid, kind, page, first_open):
    uid = update.effective_user.id
    rows = q("SELECT * FROM scripts WHERE id=?", (sid,))
    if not rows or (rows[0]["hidden"] and uid not in ADMINS):
        await reply(update, "این اسکریپت پیدا نشد.", M([[B("🏠 منو", callback_data="home")]]))
        return
    s = rows[0]
    ctx.user_data["ret"] = f"{kind}:{page}"
    if first_open:
        if not q("SELECT 1 FROM seen WHERE user_id=? AND script_id=?", (uid, sid)):
            run("UPDATE scripts SET views=views+1 WHERE id=?", (sid,))
        run("INSERT OR REPLACE INTO seen VALUES(?,?,?)", (uid, sid, int(time.time())))
        s = q("SELECT * FROM scripts WHERE id=?", (sid,))[0]
    st = q("SELECT COALESCE(AVG(stars),0) a, COUNT(*) c FROM ratings WHERE script_id=?", (sid,))[0]
    mine = q("SELECT stars FROM ratings WHERE user_id=? AND script_id=?", (uid, sid))
    mine = mine[0]["stars"] if mine else 0
    is_fav = bool(q("SELECT 1 FROM favs WHERE user_id=? AND script_id=?", (uid, sid)))
    n_like = q("SELECT COUNT(*) FROM likes WHERE script_id=?", (sid,))[0][0]
    liked = bool(q("SELECT 1 FROM likes WHERE user_id=? AND script_id=?", (uid, sid)))
    n_cm = q("SELECT COUNT(*) FROM comments WHERE script_id=?", (sid,))[0][0]
    following = bool(q("SELECT 1 FROM game_follows WHERE user_id=? AND game=?", (uid, s["game"])))
    text = (f"🎮 <b>{esc(s['title'])}</b>\n🕹 بازی: {esc(s['game'])}\n\n"
            f"{esc(s['description'])}\n\n"
            f"⭐ امتیاز: <b>{st['a']:.1f}</b> از ۵ ({st['c']} رأی)\n"
            f"👍 {n_like} لایک • 💬 {n_cm} نظر • 👁 {s['views']} بازدید\n")
    if s["checked"]:
        text += f"✅ آخرین بررسی: {time.strftime('%Y/%m/%d', time.gmtime(s['checked']))}\n"
    text += "\n" + (f"امتیاز شما: {'⭐' * mine}" if mine else "به این اسکریپت امتیاز بده 👇")
    n_rep = q("SELECT COUNT(*) FROM reports WHERE script_id=?", (sid,))[0][0] if uid in ADMINS else 0
    if uid in ADMINS:
        if n_rep:
            text += f"\n\n🚩 تعداد گزارش «کار نمی‌کنه»: {n_rep}"
        if BOT_USERNAME:
            text += f"\n\n🔗 لینک اختصاصی:\n<code>{slink(sid)}</code>"
    cx = f"{kind}:{page}"
    kb = [
        [B(f"{'👍✅' if liked else '👍'} لایک ({n_like})", callback_data=f"lk:{sid}:{cx}"),
         B(f"💬 نظرات ({n_cm})", callback_data=f"cm:{sid}:0")],
        [B((("✅" if i == mine else "") + f"{i}⭐"), callback_data=f"rt:{sid}:{i}:{cx}") for i in range(1, 6)],
        [B("💔 حذف از علاقه‌مندی" if is_fav else "❤️ علاقه‌مندی", callback_data=f"fv:{sid}:{cx}"),
         B("📄 دریافت فایل", callback_data=f"fl:{sid}")],
    ]
    row = [B("📩 ارسال به پیوی", callback_data=f"pm:{sid}")]
    if BOT_USERNAME:
        share = f"https://t.me/share/url?url={quote(slink(sid))}&text={quote('🎮 ' + s['title'] + ' — ' + s['game'])}"
        row.append(B("🔗 اشتراک", url=share))
    kb.append(row)
    kb.append([B("🔕 لغو دنبال کردن بازی" if following else "🔔 دنبال کردن بازی", callback_data=f"gf:{sid}:{cx}"),
               B("🚩 کار نمی‌کنه", callback_data=f"rp:{sid}:{cx}")])
    if uid in ADMINS:
        kb.append([B("🗑 حذف", callback_data=f"ad:del:{sid}:{cx}"),
                   B("👁 نمایش" if s["hidden"] else "🙈 مخفی", callback_data=f"ad:hide:{sid}:{cx}")])
        if n_rep or s["hidden"]:
            kb.append([B("✅ بررسی شد (پاک کردن گزارش‌ها)", callback_data=f"ad:chk:{sid}:{cx}")])
    kb.append([B("🔙 بازگشت", callback_data=f"ls:{kind}:{page}")])
    await reply(update, text, M(kb))
    if first_open:
        await send_code(ctx, update.effective_chat.id, s)


# ───────────────────────── نظرات ─────────────────────────
async def show_comments(update, ctx, sid, page):
    uid = update.effective_user.id
    sc = q("SELECT title, hidden FROM scripts WHERE id=?", (sid,))
    if not sc or (sc[0]["hidden"] and uid not in ADMINS):
        await reply(update, "این اسکریپت پیدا نشد.", M([[B("🏠 منو", callback_data="home")]]))
        return
    total = q("SELECT COUNT(*) FROM comments WHERE script_id=?", (sid,))[0][0]
    pages = max(1, -(-total // CPER))
    page = min(max(0, page), pages - 1)
    rows = q("SELECT c.id, c.text, c.user_id, u.name FROM comments c LEFT JOIN users u ON u.id=c.user_id "
             "WHERE c.script_id=? ORDER BY c.id DESC LIMIT ? OFFSET ?", (sid, CPER, page * CPER))
    lines = [f"{i}. <b>{esc(r['name'] or 'کاربر')}</b>: {esc(r['text'])}" for i, r in enumerate(rows, 1)]
    text = (f"💬 <b>نظرات «{esc(sc[0]['title'])}»</b> ({total})"
            + (f"\nصفحه {page + 1} از {pages}\n\n" + "\n\n".join(lines) if lines
               else "\n\nهنوز نظری نیست. اولین نفر باش!"))
    kb = [[B("✍️ نوشتن نظر", callback_data=f"cw:{sid}")]]
    for i, r in enumerate(rows, 1):
        if uid in ADMINS or r["user_id"] == uid:
            kb.append([B(f"🗑 حذف نظر {i}", callback_data=f"cd:{r['id']}:{sid}:{page}")])
    nav = []
    if page > 0:
        nav.append(B("◀️ قبلی", callback_data=f"cm:{sid}:{page - 1}"))
    if page + 1 < pages:
        nav.append(B("بعدی ▶️", callback_data=f"cm:{sid}:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([B("🔙 بازگشت به اسکریپت", callback_data=f"sc:{sid}:{ctx.user_data.get('ret', 'new:0')}")])
    await reply(update, text, M(kb))


async def notify_followers(ctx, sid, title, game, skip=0):
    ids = [r[0] for r in q("SELECT g.user_id FROM game_follows g JOIN users u ON u.id=g.user_id "
                           "WHERE g.game=? AND u.banned=0 AND g.user_id!=?", (game, skip))]
    for i in ids:
        try:
            await ctx.bot.send_message(i, f"🔔 اسکریپت جدید برای «{game}»:\n«{title}»\n\n{slink(sid)}",
                                       disable_web_page_preview=True)
        except Exception:
            pass
        await asyncio.sleep(0.05)


# ───────────────────────── پنل ادمین ─────────────────────────
def panel():
    return M([
        [B("➕ افزودن اسکریپت", callback_data="ad:add"), B("📋 مدیریت اسکریپت‌ها", callback_data="ls:al:0")],
        [B("👥 اعضا", callback_data="ad:users:0"), B("📊 آمار", callback_data="ad:stats")],
        [B("📢 پیام همگانی", callback_data="ad:bc"), B("🚫 بن / آنبن", callback_data="ad:ban")],
        [B("🚩 گزارش‌شده‌ها", callback_data="ls:rep:0"), B("🔎 جستجوهای پرتکرار", callback_data="ad:ts")],
        [B("📥 ایمپورت دسته‌ای", callback_data="ad:imp"), B("💾 بکاپ دیتابیس", callback_data="ad:bk")],
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
             f"❤️ علاقه‌مندی‌ها: {n('SELECT COUNT(*) FROM favs')}\n"
             f"👍 لایک‌ها: {n('SELECT COUNT(*) FROM likes')}\n"
             f"💬 نظرها: {n('SELECT COUNT(*) FROM comments')}\n"
             f"🔔 دنبال‌کنندگان بازی: {n('SELECT COUNT(*) FROM game_follows')}\n"
             f"🚩 گزارش‌های باز: {n('SELECT COUNT(*) FROM reports')}")
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
    elif a == "ts":
        rows = q("SELECT lower(q) t, COUNT(*) c FROM search_log WHERE ts>=? GROUP BY t ORDER BY c DESC LIMIT 15",
                 (int(time.time()) - 30 * 86400,))
        lines = [f"{i}. {esc(r['t'])} — {r['c']}" for i, r in enumerate(rows, 1)]
        await reply(update, "🔎 <b>جستجوهای پرتکرار (۳۰ روز اخیر)</b>\n\n" + ("\n".join(lines) or "هنوز جستجویی ثبت نشده."), back)
    elif a == "imp":
        ctx.user_data["st"] = "imp"
        await reply(update, "📥 <b>ایمپورت دسته‌ای</b>\n\nفایل JSON یا CSV بفرست (حداکثر ۳ مگابایت، ۵۰۰ مورد).\n"
                            "JSON: لیستی از <code>{\"title\",\"game\",\"description\",\"code\"}</code>\n"
                            "CSV: ستون‌های <code>title,game,description,code</code>\n(لغو: /cancel)")
    elif a == "chk":
        sid = int(d[2])
        run("DELETE FROM reports WHERE script_id=?", (sid,))
        run("UPDATE scripts SET checked=?, hidden=0 WHERE id=?", (int(time.time()), sid))
        await show_script(update, ctx, sid, d[3], int(d[4]), False)
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
        for t in ("scripts WHERE id", "ratings WHERE script_id", "seen WHERE script_id", "favs WHERE script_id",
                  "likes WHERE script_id", "comments WHERE script_id", "reports WHERE script_id"):
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


async def help_cmd(update, ctx):
    await reply(update, HELP, M([[B("🏠 منو", callback_data="home")]]))


async def on_inline(update, ctx):
    iq = update.inline_query
    where, wp = "s.hidden=0", []
    for w in norm(iq.query).split():
        where += " AND s.norm LIKE ?"
        wp.append(f"%{w}%")
    rows = q(f"{SEL} WHERE {where} ORDER BY s.views DESC LIMIT 20", wp)
    out = []
    for r in rows:
        link = slink(r["id"])
        out.append(InlineQueryResultArticle(
            id=str(r["id"]), title=r["title"], description=f"🕹 {r['game']} • ⭐{r['avg']:.1f} • 👁{r['views']}",
            input_message_content=InputTextMessageContent(f"🎮 {r['title']}\n🕹 {r['game']}\n\n{link}"),
            reply_markup=M([[B("📥 دریافت اسکریپت", url=link)]])))
    await iq.answer(out, cache_time=30, is_personal=False)


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
    d = cq.data.split(":")
    a, uid = d[0], cq.from_user.id
    late = a in ("pm", "rp", "gf", "lk")  # این‌ها خودشان پاسخ (پیام کوتاه) به callback می‌دهند
    if not late:
        await cq.answer()
    if not await gate(update, ctx):
        if late:
            await cq.answer()
        return
    if a == "help":
        await reply(update, HELP, M([[B("🏠 منو", callback_data="home")]]))
    elif a == "lk":
        sid = int(d[1])
        if q("SELECT 1 FROM likes WHERE user_id=? AND script_id=?", (uid, sid)):
            run("DELETE FROM likes WHERE user_id=? AND script_id=?", (uid, sid))
            await cq.answer("لایک برداشته شد")
        else:
            run("INSERT OR IGNORE INTO likes VALUES(?,?,?)", (uid, sid, int(time.time())))
            await cq.answer("👍 لایک شد")
        await show_script(update, ctx, sid, d[2], int(d[3]), False)
    elif a == "cm":
        await show_comments(update, ctx, int(d[1]), int(d[2]))
    elif a == "cw":
        ctx.user_data.update(st="comment", cs=int(d[1]))
        await reply(update, "✍️ نظرت رو بفرست (حداکثر ۵۰۰ کاراکتر):\n(لغو: /cancel)",
                    M([[B("🔙 بازگشت به نظرات", callback_data=f"cm:{d[1]}:0")]]))
    elif a == "cd":
        cid, sid = int(d[1]), int(d[2])
        r = q("SELECT user_id FROM comments WHERE id=?", (cid,))
        if r and (uid in ADMINS or r[0]["user_id"] == uid):
            run("DELETE FROM comments WHERE id=?", (cid,))
        await show_comments(update, ctx, sid, int(d[3]))
    elif a == "pm":
        if limited(uid, "pm", 10, 300):
            await cq.answer("کمی صبر کن ⏳", show_alert=True)
            return
        r = q("SELECT * FROM scripts WHERE id=? AND (hidden=0 OR ?)", (int(d[1]), uid in ADMINS))
        if not r:
            await cq.answer("این اسکریپت پیدا نشد.", show_alert=True)
            return
        sc = r[0]
        try:
            await ctx.bot.send_message(uid, f"🎮 <b>{esc(sc['title'])}</b>\n🕹 {esc(sc['game'])}\n\n{esc(sc['description'])}",
                                       parse_mode=ParseMode.HTML)
            await send_code(ctx, uid, sc)
            await cq.answer("✅ به پیوی‌ات فرستاده شد")
        except Exception:
            await cq.answer("اول ربات را استارت کن (/start) و دوباره بزن.", show_alert=True)
    elif a == "gf":
        sid = int(d[1])
        r = q("SELECT game FROM scripts WHERE id=?", (sid,))
        if r:
            g = r[0]["game"]
            if q("SELECT 1 FROM game_follows WHERE user_id=? AND game=?", (uid, g)):
                run("DELETE FROM game_follows WHERE user_id=? AND game=?", (uid, g))
                await cq.answer("🔕 دیگه خبر این بازی رو نمی‌گیری")
            else:
                run("INSERT OR IGNORE INTO game_follows VALUES(?,?,?)", (uid, g, int(time.time())))
                await cq.answer("🔔 از این به بعد اسکریپت جدید این بازی رو بهت خبر می‌دیم")
        await show_script(update, ctx, sid, d[2], int(d[3]), False)
    elif a == "rp":
        sid = int(d[1])
        r = q("SELECT title, hidden FROM scripts WHERE id=?", (sid,))
        if not r:
            await cq.answer()
        elif q("SELECT 1 FROM reports WHERE user_id=? AND script_id=?", (uid, sid)):
            await cq.answer("قبلاً گزارش دادی 🙏", show_alert=True)
        elif limited(uid, "rp", 10, 3600):
            await cq.answer("کمی صبر کن ⏳", show_alert=True)
        else:
            run("INSERT OR IGNORE INTO reports VALUES(?,?,?)", (uid, sid, int(time.time())))
            n = q("SELECT COUNT(*) FROM reports WHERE script_id=?", (sid,))[0][0]
            if n >= REPORT_LIMIT and not r[0]["hidden"]:
                run("UPDATE scripts SET hidden=1 WHERE id=?", (sid,))
                await tell_admins(ctx, f"🙈 اسکریپت #{sid} «{r[0]['title']}» بعد از {n} گزارش مخفی شد.\n{slink(sid)}")
            elif n == 1:
                await tell_admins(ctx, f"🚩 اولین گزارش «کار نمی‌کنه» برای #{sid} «{r[0]['title']}»\n{slink(sid)}")
            await cq.answer("🚩 گزارش ثبت شد؛ ممنون")
    elif a == "chk":
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
    if st == "comment":
        sid = ctx.user_data.get("cs")
        sc = q("SELECT title, user_id FROM scripts WHERE id=? AND (hidden=0 OR ?)", (sid, adm)) if sid else []
        if not sc:
            ctx.user_data.clear()
            await reply(update, "این اسکریپت پیدا نشد.", menu(uid))
        elif len(t) > 500:
            await reply(update, "❌ نظر بیشتر از ۵۰۰ کاراکتر است؛ کوتاه‌ترش کن و دوباره بفرست.")
        elif limited(uid, "cm", 10, 600):
            await reply(update, "⏳ کمی صبر کن؛ تعداد نظرهات زیاد شده.")
        else:
            run("INSERT INTO comments(script_id,user_id,text,created) VALUES(?,?,?,?)", (sid, uid, t, int(time.time())))
            ctx.user_data.pop("st", None)
            ctx.user_data.pop("cs", None)
            owner = sc[0]["user_id"]
            if owner and owner != uid:
                try:
                    await ctx.bot.send_message(owner, f"💬 {update.effective_user.full_name} روی «{sc[0]['title']}» نظر داد:\n\n"
                                                      f"{t[:300]}\n\n{slink(sid)}", disable_web_page_preview=True)
                except Exception:
                    pass
            await show_comments(update, ctx, sid, 0)
    elif st == "search":
        if limited(uid, "search", 20, 60):
            await reply(update, "⏳ کمی صبر کن؛ جستجوهات زیاد شده.")
            return
        run("INSERT INTO search_log(user_id,q,ts) VALUES(?,?,?)", (uid, t[:100], int(time.time())))
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
    ctx.application.create_task(notify_followers(ctx, cur.lastrowid, n["title"], n["game"]))
    await reply(update, f"✅ اسکریپت ثبت شد.\n\n🔗 لینک اختصاصی (بفرست توی کانال):\n<code>{link}</code>",
                M([[B("➕ افزودن دیگر", callback_data="ad:add"), B("⚙️ پنل", callback_data="ad:home")]]))


def parse_import(raw, name):
    if name.lower().endswith(".json") or raw.lstrip().startswith(("[", "{")):
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("scripts", [])
        return [x for x in data if isinstance(x, dict)]
    return list(csv.DictReader(io.StringIO(raw)))


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
    elif ctx.user_data.get("st") == "imp":
        if doc.file_size and doc.file_size > 3_000_000:
            await update.message.reply_text("❌ فایل بزرگ‌تر از ۳ مگابایت است.")
            return
        raw = bytes(await (await doc.get_file()).download_as_bytearray()).decode("utf-8-sig", errors="replace")
        try:
            items = parse_import(raw, doc.file_name or "")
        except Exception as e:
            await update.message.reply_text(f"❌ فایل خوانده نشد: {e}")
            return
        ok = skip = 0
        for it in items[:500]:
            t_, g_, c_ = (str(it.get("title") or "").strip()[:80], str(it.get("game") or "").strip()[:40],
                          str(it.get("code") or "").strip())
            d_ = str(it.get("description") or "").strip()[:1000]
            if len(t_) < 2 or not g_ or len(c_) < 3 or q("SELECT 1 FROM scripts WHERE title=? AND game=?", (t_, g_)):
                skip += 1
                continue
            db.execute("INSERT INTO scripts(title,game,description,code,norm,created) VALUES(?,?,?,?,?,?)",
                       (t_, g_, d_, c_, norm(f"{t_} {g_} {d_}"), int(time.time())))
            ok += 1
        db.commit()
        ctx.user_data.clear()
        await update.message.reply_text(f"📥 ایمپورت تمام شد: {ok} مورد اضافه شد، {skip} مورد رد شد (تکراری یا ناقص).",
                                        reply_markup=panel())
    elif ctx.user_data.get("st") == "a_code":
        if doc.file_size and doc.file_size > 1_000_000:
            await update.message.reply_text("❌ فایل بزرگ‌تر از ۱ مگابایت است.")
            return
        data = await (await doc.get_file()).download_as_bytearray()
        await save_script(update, ctx, bytes(data).decode("utf-8", errors="replace"))


# ───────────────────────── بکاپ خودکار روی تلگرام ─────────────────────────
# دیسک Render رایگان موقتی است. هر چند دقیقه اگر دیتابیس تغییر کرده باشد، فایلش در STORAGE_CHAT
# فرستاده و پین می‌شود؛ هنگام بالا آمدن اگر دیتابیس خالی بود، از همان پین بازگردانی می‌شود.
BACKUP_OK = True


def db_count():
    c = sqlite3.connect(DB_PATH)
    try:
        return c.execute("SELECT (SELECT COUNT(*) FROM scripts)+(SELECT COUNT(*) FROM users)").fetchone()[0]
    finally:
        c.close()


def auto_restore():
    global db, BACKUP_OK
    if not STORAGE_CHAT or db_count():
        return
    try:
        pm = webapp.tg("getChat", {"chat_id": STORAGE_CHAT}).get("pinned_message") or {}
        doc = pm.get("document") or {}
        if doc.get("file_name") != "backup.db":
            return
        path = webapp.tg("getFile", {"file_id": doc["file_id"]})["file_path"]
        data = httpx.get(f"https://api.telegram.org/file/bot{TOKEN}/{path}", timeout=120).content
        with open("restore.tmp", "wb") as f:
            f.write(data)
        t = sqlite3.connect("restore.tmp")
        t.execute("SELECT COUNT(*) FROM scripts")
        t.close()
        db.close()
        shutil.copy("restore.tmp", DB_PATH)
        db = connect()
        logging.info("database restored from pinned backup")
    except Exception as e:
        BACKUP_OK = False  # برای اینکه بکاپ قدیمی با دیتابیس خالی بازنویسی نشود
        logging.error("auto restore failed, auto backup disabled: %s", e)


def backup_loop():
    last, prev = 0, None
    time.sleep(60)
    while BACKUP_OK and STORAGE_CHAT:
        try:
            m = os.path.getmtime(DB_PATH)
            if m != last and db_count():
                src, dst = sqlite3.connect(DB_PATH), sqlite3.connect("auto_backup.db")
                src.backup(dst)
                dst.close()
                src.close()
                with open("auto_backup.db", "rb") as f:
                    data = f.read()
                r = webapp.tg("sendDocument", {"chat_id": STORAGE_CHAT, "caption": "💾 auto backup",
                                               "disable_notification": "true"},
                              {"document": ("backup.db", data, "application/octet-stream")}, timeout=180)
                webapp.tg("pinChatMessage", {"chat_id": STORAGE_CHAT, "message_id": r["message_id"],
                                             "disable_notification": "true"})
                if prev:
                    try:
                        webapp.tg("deleteMessage", {"chat_id": STORAGE_CHAT, "message_id": prev})
                    except Exception:
                        pass
                prev, last = r["message_id"], m
        except Exception as e:
            logging.warning("auto backup failed: %s", e)
        time.sleep(300)


async def post_init(app):
    global BOT_USERNAME
    BOT_USERNAME = (await app.bot.get_me()).username
    if WEBAPP_URL:
        try:
            await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="اکسپلورر", web_app=WebAppInfo(url=WEBAPP_URL)))
        except Exception as e:
            logging.warning("menu button: %s", e)


def build_app():
    app = (Application.builder().token(TOKEN).post_init(post_init)
           .connect_timeout(30).read_timeout(30).write_timeout(30).pool_timeout(30)
           .get_updates_connect_timeout(30).get_updates_read_timeout(30)
           .get_updates_write_timeout(30).get_updates_pool_timeout(30).build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(InlineQueryHandler(on_inline))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.Document.ALL, on_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    async def on_error(update, ctx):
        logging.error("handler error: %s", ctx.error)
    app.add_error_handler(on_error)
    return app


def main():
    webapp.start(int(os.environ.get("PORT", 10000)))
    auto_restore()
    threading.Thread(target=backup_loop, daemon=True).start()
    delay = 5
    while True:  # اگر تلگرام موقتاً 502 داد، پروسه نمی‌میرد و دوباره تلاش می‌کند
        try:
            asyncio.set_event_loop(asyncio.new_event_loop())
            build_app().run_polling(drop_pending_updates=True, bootstrap_retries=-1)
            break
        except Exception as e:
            logging.error("polling crashed: %s; retry in %ss", e, delay)
            time.sleep(delay)
            delay = min(delay * 2, 120)


if __name__ == "__main__":
    main()
