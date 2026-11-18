"""بک‌اند مینی‌اپ (شبیه اینستاگرام): API فید، پست، ویدیو، لایک، کامنت، فالو + پروکسی مدیا.
فقط از stdlib + httpx (که همراه python-telegram-bot نصب می‌شود) استفاده می‌کند."""
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, parse_qsl, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = {}
MAX_VIDEO = 19 * 1024 * 1024      # getFile ربات‌ها حداکثر ۲۰MB را اجازه می‌دهد
MAX_CODE = 500_000


def init(token, admins, chats, db_path, bot_username, storage_chat, app_short):
    CFG.update(token=token, admins=set(admins), chats=list(chats), db_path=db_path,
               bot=bot_username, storage=storage_chat, app_short=app_short)


# ───────────────────────── تلگرام ─────────────────────────
def tg(method, data=None, files=None, timeout=60):
    import httpx
    r = httpx.post(f"https://api.telegram.org/bot{CFG['token']}/{method}", data=data, files=files, timeout=timeout)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(j.get("description", "telegram error"))
    return j["result"]


def bg(fn, *a):
    def run():
        try:
            fn(*a)
        except Exception as e:
            logging.warning("bg task failed: %s", e)
    threading.Thread(target=run, daemon=True).start()


def tell(uid, text):
    tg("sendMessage", {"chat_id": uid, "text": text, "disable_web_page_preview": "true"}, timeout=20)


def tell_admins(text):
    for a in CFG["admins"]:
        try:
            tell(a, text)
        except Exception:
            pass


def link(sid):
    return f"https://t.me/{CFG['bot']()}?start=s_{sid}"


_fp = {}


def tg_path(fid):
    c = _fp.get(fid)
    if c and time.time() - c[0] < 3000:
        return c[1]
    path = tg("getFile", {"file_id": fid}, timeout=20)["file_path"]
    _fp[fid] = (time.time(), path)
    return path


# ───────────────────────── احراز هویت ─────────────────────────
def verify_init(init_data):
    """اعتبارسنجی initData مینی‌اپ تلگرام (HMAC)."""
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        h = pairs.pop("hash", None)
        if not h:
            return None
        dcs = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", CFG["token"].encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, h):
            return None
        if time.time() - int(pairs.get("auth_date", 0)) > 3 * 86400:
            return None
        return json.loads(pairs["user"])
    except Exception:
        return None


_mem = {}


def is_member(uid):
    if uid in CFG["admins"] or not CFG["chats"]:
        return True
    now, c = time.time(), _mem.get(uid)
    if c and now - c[0] < (300 if c[1] else 10):
        return c[1]
    ok = True
    for ch in CFG["chats"]:
        try:
            m = tg("getChatMember", {"chat_id": ch, "user_id": uid}, timeout=15)
            if m["status"] in ("left", "kicked") or (m["status"] == "restricted" and not m.get("is_member", True)):
                ok = False
                break
        except Exception as e:
            logging.error("membership check failed for %s: %s", ch, e)
            ok = False
            break
    _mem[uid] = (now, ok)
    return ok


_rl = {}


def rate(uid, key, limit, win):
    if uid in CFG["admins"]:
        return
    now, k = time.time(), (uid, key)
    hits = [t for t in _rl.get(k, []) if now - t < win]
    if len(hits) >= limit:
        raise ApiError(429, "کمی صبر کن؛ تعداد درخواست‌هات زیاد شده.")
    hits.append(now)
    _rl[k] = hits


class ApiError(Exception):
    def __init__(self, code, msg, **extra):
        self.code, self.msg, self.extra = code, msg, extra


def dbc():
    c = sqlite3.connect(CFG["db_path"], timeout=15)
    c.row_factory = sqlite3.Row
    return c


def norm(t):
    return (t or "").replace("ي", "ی").replace("ك", "ک").replace("‌", " ").lower().strip()


def clean(s, n):
    return str(s or "").replace("\r\n", "\n").strip()[:n]


class Ctx:
    def __init__(self, db, uid, user, qs, handler):
        self.db, self.uid, self.user, self.qs, self.h = db, uid, user, qs, handler
        self._member = None

    def arg(self, k, d=""):
        return (self.qs.get(k) or [d])[0]

    def num(self, k, d=0):
        try:
            return int(self.arg(k, str(d)))
        except ValueError:
            return d

    @property
    def admin(self):
        return self.uid in CFG["admins"]

    @property
    def member(self):
        if self._member is None:
            self._member = bool(self.uid) and is_member(self.uid)
        return self._member

    def body(self):
        n = int(self.h.headers.get("Content-Length") or 0)
        if n > 1_600_000:
            raise ApiError(413, "حجم درخواست زیاد است.")
        try:
            return json.loads(self.h.rfile.read(n) or b"{}")
        except ValueError:
            raise ApiError(400, "درخواست نامعتبر")

    def need_write(self):
        if not self.uid:
            raise ApiError(401, "این کار فقط داخل تلگرام ممکن است.")
        r = self.db.execute("SELECT banned FROM users WHERE id=?", (self.uid,)).fetchone()
        if r and r["banned"]:
            raise ApiError(403, "⛔ دسترسی شما به ربات مسدود شده است.")
        if not self.member:
            raise ApiError(403, "برای این کار اول در کانال/گروه عضو شو.", join=True)


ROUTES = {}


def route(method, name):
    def deco(fn):
        ROUTES[(method, name)] = fn
        return fn
    return deco


# ───────────────────────── پست‌ها ─────────────────────────
BASE = """SELECT s.id,s.title,s.game,s.description,s.kind,s.thumb,s.views,s.created,s.user_id,s.hidden,
 substr(s.code,1,1200) AS preview, length(s.code) AS clen,
 (SELECT COUNT(*) FROM likes l WHERE l.script_id=s.id) AS likes,
 (SELECT COUNT(*) FROM comments c WHERE c.script_id=s.id) AS comments,
 EXISTS(SELECT 1 FROM likes l WHERE l.script_id=s.id AND l.user_id=:me) AS liked,
 EXISTS(SELECT 1 FROM favs f WHERE f.script_id=s.id AND f.user_id=:me) AS saved,
 u.name AS uname,u.username AS uuser,u.photo AS uphoto
 FROM scripts s LEFT JOIN users u ON u.id=s.user_id"""


def author(uid, name, username, photo):
    if uid == 0:
        return {"id": 0, "name": "تیم اسکریپت", "username": "", "photo": "", "official": True}
    return {"id": uid, "name": name or "کاربر", "username": username or "", "photo": photo or "", "official": False}


def pj(r, c):
    kind = r["kind"] or "script"
    d = {"id": r["id"], "kind": kind, "title": r["title"], "game": r["game"], "caption": r["description"] or "",
         "created": r["created"] or 0, "views": r["views"], "likes": r["likes"], "comments": r["comments"],
         "liked": bool(r["liked"]), "saved": bool(r["saved"]), "hidden": bool(r["hidden"]),
         "thumb": bool(r["thumb"]), "mine": bool(c.uid) and r["user_id"] == c.uid,
         "can_del": c.admin or (bool(c.uid) and r["user_id"] == c.uid),
         "user": author(r["user_id"], r["uname"], r["uuser"], r["uphoto"])}
    if kind == "script":
        if c.member:
            txt = "\n".join((r["preview"] or "").split("\n")[:10])
            d["preview"], d["more"] = txt, (r["clen"] or 0) > len(txt)
        else:
            d["locked"] = True
    return d


def purge(db, sid):
    for t, col in (("scripts", "id"), ("ratings", "script_id"), ("seen", "script_id"),
                   ("favs", "script_id"), ("likes", "script_id"), ("comments", "script_id")):
        db.execute(f"DELETE FROM {t} WHERE {col}=?", (sid,))


@route("GET", "me")
def r_me(c):
    if c.arg("refresh"):
        _mem.pop(c.uid, None)
    u = c.user or {}
    return {"uid": c.uid, "name": u.get("name", ""), "username": u.get("username", ""), "photo": u.get("photo", ""),
            "admin": c.admin, "member": c.member, "bot": CFG["bot"](), "app_short": CFG["app_short"],
            "chats": [{"title": ch, "url": "https://t.me/" + ch.lstrip("@")} for ch in CFG["chats"]]}


@route("GET", "feed")
def r_feed(c):
    where, p = ["s.hidden=0"], {"me": c.uid}
    kind = c.arg("kind")
    if kind in ("video", "script"):
        where.append("s.kind=:kind")
        p["kind"] = kind
    if c.arg("game"):
        where.append("s.game=:game")
        p["game"] = c.arg("game")
    for i, w in enumerate(norm(c.arg("q")).split()[:6]):
        where.append(f"s.norm LIKE :w{i}")
        p[f"w{i}"] = f"%{w}%"
    if c.arg("following"):
        where.append("(s.user_id IN (SELECT target FROM follows WHERE follower=:me) OR s.user_id=:me)")
    if c.arg("saved"):
        where.append("s.id IN (SELECT script_id FROM favs WHERE user_id=:me)")
    if c.arg("user") != "":
        where.append("s.user_id=:u")
        p["u"] = c.num("user")
    order = "(likes*3+comments*2+s.views) DESC, s.id DESC" if c.arg("sort") == "top" else "s.id DESC"
    limit, off = max(1, min(c.num("limit", 8), 30)), max(0, c.num("offset"))
    rows = c.db.execute(f"{BASE} WHERE {' AND '.join(where)} ORDER BY {order} LIMIT {limit + 1} OFFSET {off}", p).fetchall()
    return {"items": [pj(r, c) for r in rows[:limit]], "more": len(rows) > limit}


def get_post(c, sid):
    r = c.db.execute(f"{BASE} WHERE s.id=:id", {"me": c.uid, "id": sid}).fetchone()
    if not r or (r["hidden"] and not c.admin and r["user_id"] != c.uid):
        raise ApiError(404, "این پست پیدا نشد.")
    return r


@route("GET", "post")
def r_post(c):
    return pj(get_post(c, c.num("id")), c)


@route("GET", "code")
def r_code(c):
    sid = c.num("id")
    get_post(c, sid)
    if not c.member:
        raise ApiError(403, "برای دیدن کد اول در کانال/گروه عضو شو.", join=True)
    r = c.db.execute("SELECT code FROM scripts WHERE id=?", (sid,)).fetchone()
    return {"code": r["code"] or ""}


@route("GET", "games")
def r_games(c):
    rows = c.db.execute("SELECT game, COUNT(*) n FROM scripts WHERE hidden=0 AND game!='' "
                        "GROUP BY game ORDER BY n DESC LIMIT 20").fetchall()
    return {"games": [{"game": r["game"], "n": r["n"]} for r in rows]}


@route("GET", "profile")
def r_profile(c):
    uid = c.num("uid")
    row = c.db.execute("SELECT name,username,photo FROM users WHERE id=?", (uid,)).fetchone()
    if uid != 0 and not row:
        raise ApiError(404, "کاربر پیدا نشد.")
    one = lambda sql, *a: c.db.execute(sql, a).fetchone()[0]
    return {"user": author(uid, row and row["name"], row and row["username"], row and row["photo"]),
            "posts": one("SELECT COUNT(*) FROM scripts WHERE user_id=? AND hidden=0", uid),
            "likes": one("SELECT COUNT(*) FROM likes WHERE script_id IN (SELECT id FROM scripts WHERE user_id=? AND hidden=0)", uid),
            "followers": one("SELECT COUNT(*) FROM follows WHERE target=?", uid),
            "following": one("SELECT COUNT(*) FROM follows WHERE follower=?", uid),
            "is_following": bool(c.uid) and bool(one("SELECT COUNT(*) FROM follows WHERE follower=? AND target=?", c.uid, uid)),
            "is_me": bool(c.uid) and c.uid == uid}


def insert_post(c, kind, title, game, cap, code="", file_id="", thumb=""):
    cur = c.db.execute(
        "INSERT INTO scripts(title,game,description,code,norm,created,user_id,kind,file_id,thumb) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (title, game, cap, code, norm(f"{title} {game} {cap}"), int(time.time()), c.uid, kind, file_id, thumb))
    c.db.commit()
    sid = cur.lastrowid
    who = (c.user or {}).get("name", "")
    label = "ویدیو" if kind == "video" else "اسکریپت"
    bg(tell_admins, f"🆕 {label} جدید از {who} ({c.uid})\n«{title}» — {game}\n{link(sid)}")
    return sid


def post_fields(title, game, cap):
    title, game, cap = clean(title, 80), clean(game, 40), clean(cap, 1000)
    if len(title) < 2:
        raise ApiError(400, "عنوان را وارد کن.")
    if not game:
        raise ApiError(400, "اسم بازی را وارد کن.")
    return title, game, cap


@route("POST", "post")
def r_newpost(c):
    c.need_write()
    rate(c.uid, "post", 10, 3600)
    b = c.body()
    title, game, cap = post_fields(b.get("title"), b.get("game"), b.get("caption"))
    code = str(b.get("code") or "").replace("\r\n", "\n").strip()
    if len(code) < 3:
        raise ApiError(400, "کد اسکریپت را وارد کن.")
    if len(code) > MAX_CODE:
        raise ApiError(413, "کد خیلی طولانی است.")
    return {"id": insert_post(c, "script", title, game, cap, code=code)}


@route("POST", "upload")
def r_upload(c):
    c.need_write()
    rate(c.uid, "post", 10, 3600)
    title, game, cap = post_fields(c.arg("title"), c.arg("game"), c.arg("caption"))
    mime = (c.h.headers.get("Content-Type") or "video/mp4").split(";")[0].strip()
    if not mime.startswith("video/"):
        raise ApiError(400, "فقط فایل ویدیو مجاز است.")
    n = int(c.h.headers.get("Content-Length") or 0)
    if n <= 0 or n > MAX_VIDEO:
        raise ApiError(413, "حجم ویدیو باید کمتر از ۱۹ مگابایت باشد.")
    if not CFG["storage"]:
        raise ApiError(500, "ذخیره‌ساز ویدیو تنظیم نشده (STORAGE_CHAT).")
    buf = bytearray()
    while len(buf) < n:
        chunk = c.h.rfile.read(min(65536, n - len(buf)))
        if not chunk:
            break
        buf += chunk
    try:
        res = tg("sendVideo", {"chat_id": CFG["storage"], "supports_streaming": "true",
                               "caption": f"👤 {(c.user or {}).get('name', '')} ({c.uid})\n{title}"[:900]},
                 {"video": ("video" + (".webm" if "webm" in mime else ".mp4"), bytes(buf), mime)}, timeout=240)
    except Exception as e:
        logging.error("video upload failed: %s", e)
        raise ApiError(502, "آپلود ویدیو به تلگرام ناموفق بود. دوباره امتحان کن.")
    v = res.get("video") or res.get("animation") or res.get("document")
    if not v:
        raise ApiError(502, "تلگرام فایل را نپذیرفت.")
    th = (v.get("thumbnail") or v.get("thumb") or {}).get("file_id", "")
    return {"id": insert_post(c, "video", title, game, cap, file_id=v["file_id"], thumb=th)}


@route("POST", "like")
def r_like(c):
    c.need_write()
    sid = int(c.body().get("id", 0))
    get_post(c, sid)
    if c.db.execute("SELECT 1 FROM likes WHERE user_id=? AND script_id=?", (c.uid, sid)).fetchone():
        c.db.execute("DELETE FROM likes WHERE user_id=? AND script_id=?", (c.uid, sid))
        liked = False
    else:
        c.db.execute("INSERT INTO likes VALUES(?,?,?)", (c.uid, sid, int(time.time())))
        liked = True
    return {"liked": liked, "likes": c.db.execute("SELECT COUNT(*) FROM likes WHERE script_id=?", (sid,)).fetchone()[0]}


@route("POST", "save")
def r_save(c):
    c.need_write()
    sid = int(c.body().get("id", 0))
    get_post(c, sid)
    if c.db.execute("SELECT 1 FROM favs WHERE user_id=? AND script_id=?", (c.uid, sid)).fetchone():
        c.db.execute("DELETE FROM favs WHERE user_id=? AND script_id=?", (c.uid, sid))
        return {"saved": False}
    c.db.execute("INSERT INTO favs VALUES(?,?,?)", (c.uid, sid, int(time.time())))
    return {"saved": True}


@route("POST", "follow")
def r_follow(c):
    c.need_write()
    t = int(c.body().get("uid", 0))
    if t == c.uid or t == 0:
        raise ApiError(400, "امکان‌پذیر نیست.")
    if c.db.execute("SELECT 1 FROM follows WHERE follower=? AND target=?", (c.uid, t)).fetchone():
        c.db.execute("DELETE FROM follows WHERE follower=? AND target=?", (c.uid, t))
        on = False
    else:
        c.db.execute("INSERT INTO follows VALUES(?,?,?)", (c.uid, t, int(time.time())))
        on = True
    return {"following": on, "followers": c.db.execute("SELECT COUNT(*) FROM follows WHERE target=?", (t,)).fetchone()[0]}


@route("POST", "view")
def r_view(c):
    if not c.uid:
        return {}
    sid = int(c.body().get("id", 0))
    if not c.db.execute("SELECT 1 FROM seen WHERE user_id=? AND script_id=?", (c.uid, sid)).fetchone():
        c.db.execute("UPDATE scripts SET views=views+1 WHERE id=?", (sid,))
    c.db.execute("INSERT OR REPLACE INTO seen VALUES(?,?,?)", (c.uid, sid, int(time.time())))
    return {}


def cj(r, c):
    return {"id": r["id"], "text": r["text"], "created": r["created"],
            "user": author(r["user_id"], r["name"], r["username"], r["photo"]),
            "can_del": c.admin or (bool(c.uid) and r["user_id"] == c.uid)}


CSEL = ("SELECT c.id,c.text,c.created,c.user_id,u.name,u.username,u.photo "
        "FROM comments c LEFT JOIN users u ON u.id=c.user_id")


@route("GET", "comments")
def r_comments(c):
    sid, off = c.num("id"), max(0, c.num("offset"))
    rows = c.db.execute(f"{CSEL} WHERE c.script_id=? ORDER BY c.id DESC LIMIT 31 OFFSET ?", (sid, off)).fetchall()
    return {"items": [cj(r, c) for r in rows[:30]], "more": len(rows) > 30}


@route("POST", "comment")
def r_comment(c):
    c.need_write()
    rate(c.uid, "cm", 20, 600)
    b = c.body()
    sid, text = int(b.get("id", 0)), clean(b.get("text"), 500)
    if not text:
        raise ApiError(400, "متن نظر خالی است.")
    post = get_post(c, sid)
    cur = c.db.execute("INSERT INTO comments(script_id,user_id,text,created) VALUES(?,?,?,?)",
                       (sid, c.uid, text, int(time.time())))
    c.db.commit()
    owner = post["user_id"]
    if owner and owner != c.uid:
        bg(tell, owner, f"💬 {(c.user or {}).get('name', '')} روی پست «{post['title']}» نظر داد:\n\n{text[:300]}\n\n{link(sid)}")
    return cj(c.db.execute(f"{CSEL} WHERE c.id=?", (cur.lastrowid,)).fetchone(), c)


@route("POST", "comment_delete")
def r_comment_del(c):
    c.need_write()
    cid = int(c.body().get("id", 0))
    r = c.db.execute("SELECT user_id FROM comments WHERE id=?", (cid,)).fetchone()
    if r and (c.admin or r["user_id"] == c.uid):
        c.db.execute("DELETE FROM comments WHERE id=?", (cid,))
    return {}


@route("POST", "post_delete")
def r_post_del(c):
    c.need_write()
    sid = int(c.body().get("id", 0))
    r = c.db.execute("SELECT user_id FROM scripts WHERE id=?", (sid,)).fetchone()
    if not r or not (c.admin or r["user_id"] == c.uid):
        raise ApiError(403, "اجازه حذف نداری.")
    purge(c.db, sid)
    return {}


@route("POST", "report")
def r_report(c):
    c.need_write()
    rate(c.uid, "rp", 5, 3600)
    sid = int(c.body().get("id", 0))
    post = get_post(c, sid)
    bg(tell_admins, f"🚩 گزارش پست #{sid} «{post['title']}» از طرف {(c.user or {}).get('name', '')} ({c.uid})\n{link(sid)}")
    return {}


# ───────────────────────── سرور HTTP ─────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, obj, code=200):
        self.send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json", {"Cache-Control": "no-store"})

    def do_HEAD(self):
        self.handle_req("GET")

    def do_GET(self):
        self.handle_req("GET")

    def do_POST(self):
        self.handle_req("POST")

    def handle_req(self, method):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        try:
            if method == "GET" and path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html", {"Cache-Control": "no-cache"})
            if method == "GET" and re.match(r"^/api/(media|thumb)/\d+$", path):
                return self.media(path)
            if not path.startswith("/api/"):
                return self.send(200, b"ok", "text/plain")
            db = dbc()
            try:
                user = verify_init(self.headers.get("X-Init", ""))
                uid = int(user["id"]) if user else 0
                if uid:
                    name = (user.get("first_name", "") + " " + user.get("last_name", "")).strip() or user.get("username") or str(uid)
                    user = {"id": uid, "name": name, "username": user.get("username", ""), "photo": user.get("photo_url", "")}
                    if path == "/api/me" or not db.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
                        db.execute("INSERT INTO users(id,username,name,joined,photo) VALUES(?,?,?,?,?) "
                                   "ON CONFLICT(id) DO UPDATE SET username=excluded.username, name=excluded.name, "
                                   "photo=CASE WHEN excluded.photo!='' THEN excluded.photo ELSE photo END",
                                   (uid, user["username"], name, int(time.time()), user["photo"]))
                        db.commit()
                fn = ROUTES.get((method, path[5:]))
                if not fn:
                    raise ApiError(404, "پیدا نشد.")
                out = fn(Ctx(db, uid, user if uid else None, qs, self))
                db.commit()
            finally:
                db.close()
            self.json(out)
        except ApiError as e:
            self.json({"error": e.msg, **e.extra}, e.code)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logging.exception("request failed: %s", self.path)
            try:
                self.json({"error": "خطای سرور"}, 500)
            except Exception:
                pass

    def media(self, path):
        import httpx
        _, _, kind, sid = path.split("/")
        db = dbc()
        r = db.execute("SELECT file_id,thumb,hidden FROM scripts WHERE id=?", (int(sid),)).fetchone()
        db.close()
        fid = r and (r["file_id"] if kind == "media" else r["thumb"])
        if not r or r["hidden"] or not fid:
            return self.send(404, b"not found", "text/plain")
        try:
            url = f"https://api.telegram.org/file/bot{CFG['token']}/{tg_path(fid)}"
        except Exception:
            return self.send(404, b"not found", "text/plain")
        rng = self.headers.get("Range", "")
        m = re.match(r"bytes=(\d+)-(\d*)", rng)
        hd = {"Accept-Encoding": "identity"}
        if rng:
            hd["Range"] = rng
        try:
            with httpx.stream("GET", url, headers=hd, timeout=60) as up:
                if up.status_code not in (200, 206):
                    return self.send(502, b"upstream error", "text/plain")
                ct = up.headers.get("content-type", "")
                if not ct or ct.startswith("application/octet"):
                    ct = "video/mp4" if kind == "media" else "image/jpeg"
                total = int(up.headers.get("content-length") or 0)
                a, z, code = 0, None, up.status_code
                if m and up.status_code == 200 and total:      # آپستریم Range را نادیده گرفته؛ خودمان شبیه‌سازی می‌کنیم
                    a = int(m.group(1))
                    z = min(int(m.group(2)) if m.group(2) else total - 1, total - 1)
                    code = 206
                self.send_response(code)
                self.send_header("Content-Type", ct)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "public, max-age=86400")
                if code == 206 and up.status_code == 200:
                    self.send_header("Content-Range", f"bytes {a}-{z}/{total}")
                    self.send_header("Content-Length", str(z - a + 1))
                else:
                    for h in ("content-length", "content-range"):
                        if h in up.headers:
                            self.send_header(h, up.headers[h])
                self.end_headers()
                if self.command == "HEAD":
                    return
                pos = 0
                for chunk in up.iter_raw(65536):
                    if z is None:
                        self.wfile.write(chunk)
                        continue
                    lo, hi = max(a - pos, 0), min(z - pos + 1, len(chunk))
                    if hi > lo:
                        self.wfile.write(chunk[lo:hi])
                    pos += len(chunk)
                    if pos > z:
                        break
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            logging.warning("media proxy failed: %s", e)


def start(port):
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
