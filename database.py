import math
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY,
  username TEXT,
  full_name TEXT,
  balance INTEGER NOT NULL DEFAULT 0,
  banned INTEGER NOT NULL DEFAULT 0,
  referrer_id INTEGER,
  ref_rewarded INTEGER NOT NULL DEFAULT 0,
  tasks_day TEXT,
  tasks_today INTEGER NOT NULL DEFAULT 0,
  last_task_at REAL NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS channels(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL UNIQUE,
  owner_id INTEGER NOT NULL,
  title TEXT,
  username TEXT,
  invite_link TEXT,
  chat_type TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS campaigns(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id INTEGER NOT NULL REFERENCES channels(id),
  owner_id INTEGER NOT NULL,
  target INTEGER NOT NULL,
  delivered INTEGER NOT NULL DEFAULT 0,
  cost_per_member INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',  -- active | paused | done | cancelled
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS joins(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id INTEGER NOT NULL,
  chat_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  reward INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'ok',  -- ok | kept | left
  check_after REAL NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS reports(
  campaign_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  PRIMARY KEY(campaign_id, user_id)
);
CREATE TABLE IF NOT EXISTS ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  amount INTEGER NOT NULL,
  reason TEXT NOT NULL,
  ref TEXT,
  balance_after INTEGER NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  members INTEGER NOT NULL,
  credits INTEGER NOT NULL,
  price INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'awaiting_receipt',  -- awaiting_receipt | pending | approved | rejected
  receipt TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status, id);
CREATE INDEX IF NOT EXISTS idx_joins_user ON joins(user_id);
CREATE INDEX IF NOT EXISTS idx_joins_check ON joins(status, check_after);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id);
"""


def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class DB:
    def __init__(self, path: str):
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    # ---------- helpers ----------
    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def one(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def all(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def _credit(self, c, uid: int, amount: int, reason: str, ref=None) -> int:
        row = c.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
        new = row["balance"] + amount
        c.execute("UPDATE users SET balance=? WHERE user_id=?", (new, uid))
        c.execute(
            "INSERT INTO ledger(user_id, amount, reason, ref, balance_after, created_at) VALUES(?,?,?,?,?,?)",
            (uid, amount, reason, None if ref is None else str(ref), new, time.time()),
        )
        return new

    # ---------- users ----------
    def get_user(self, uid: int):
        return self.one("SELECT * FROM users WHERE user_id=?", (uid,))

    def upsert_user(self, uid, username, full_name, referrer=None):
        """Returns (user_row, is_new)."""
        with self.tx() as c:
            row = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
            if row:
                c.execute("UPDATE users SET username=?, full_name=? WHERE user_id=?", (username, full_name, uid))
                return row, False
            if referrer and referrer != uid:
                if not c.execute("SELECT 1 FROM users WHERE user_id=?", (referrer,)).fetchone():
                    referrer = None
            else:
                referrer = None
            c.execute(
                "INSERT INTO users(user_id, username, full_name, balance, referrer_id, created_at) VALUES(?,?,?,0,?,?)",
                (uid, username, full_name, referrer, time.time()),
            )
            if config.WELCOME_BONUS:
                self._credit(c, uid, config.WELCOME_BONUS, "welcome")
            return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone(), True

    def adjust(self, uid: int, amount: int, reason="admin", ref=None):
        with self.tx() as c:
            if not c.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone():
                return None
            return self._credit(c, uid, amount, reason, ref)

    def set_banned(self, uid: int, banned: bool) -> bool:
        with self.tx() as c:
            cur = c.execute("UPDATE users SET banned=? WHERE user_id=?", (1 if banned else 0, uid))
            return cur.rowcount > 0

    def all_user_ids(self):
        return [r["user_id"] for r in self.all("SELECT user_id FROM users WHERE banned=0")]

    def account_stats(self, uid: int) -> dict:
        earned = self.one("SELECT COALESCE(SUM(amount),0) s FROM ledger WHERE user_id=? AND reason='join'", (uid,))["s"]
        joins = self.one("SELECT COUNT(*) n FROM joins WHERE user_id=?", (uid,))["n"]
        camps = self.one("SELECT COUNT(*) n FROM campaigns WHERE owner_id=?", (uid,))["n"]
        got = self.one(
            "SELECT COALESCE(SUM(delivered),0) s FROM campaigns WHERE owner_id=?", (uid,)
        )["s"]
        refs = self.one("SELECT COUNT(*) n FROM users WHERE referrer_id=?", (uid,))["n"]
        return {"earned": earned, "joins": joins, "campaigns": camps, "members_got": got, "referrals": refs}

    # ---------- anti abuse gate ----------
    def task_gate(self, uid: int):
        u = self.get_user(uid)
        if not u:
            return False, "ابتدا /start را بزنید."
        today = utc_day()
        used = u["tasks_today"] if u["tasks_day"] == today else 0
        if used >= config.DAILY_TASK_LIMIT:
            return False, f"سقف روزانه ({config.DAILY_TASK_LIMIT} عضویت) پر شده است. فردا برگردید."
        wait = config.TASK_COOLDOWN - (time.time() - u["last_task_at"])
        if wait > 0:
            return False, f"لطفاً {math.ceil(wait)} ثانیه صبر کنید و دوباره بزنید."
        return True, ""

    # ---------- channels ----------
    def add_channel(self, chat_id, owner_id, title, username, invite_link, chat_type) -> int:
        with self.tx() as c:
            row = c.execute("SELECT id FROM channels WHERE chat_id=?", (chat_id,)).fetchone()
            if row:
                c.execute(
                    "UPDATE channels SET owner_id=?, title=?, username=?, invite_link=?, chat_type=?, active=1 WHERE id=?",
                    (owner_id, title, username, invite_link, chat_type, row["id"]),
                )
                return row["id"]
            cur = c.execute(
                "INSERT INTO channels(chat_id, owner_id, title, username, invite_link, chat_type, created_at) VALUES(?,?,?,?,?,?,?)",
                (chat_id, owner_id, title, username, invite_link, chat_type, time.time()),
            )
            return cur.lastrowid

    def count_channels(self, owner_id: int) -> int:
        return self.one("SELECT COUNT(*) n FROM channels WHERE owner_id=? AND active=1", (owner_id,))["n"]

    def user_channels(self, owner_id: int):
        return self.all("SELECT * FROM channels WHERE owner_id=? AND active=1 ORDER BY id DESC", (owner_id,))

    def get_channel(self, ch_id: int, owner_id: int | None = None):
        if owner_id is None:
            return self.one("SELECT * FROM channels WHERE id=? AND active=1", (ch_id,))
        return self.one("SELECT * FROM channels WHERE id=? AND owner_id=? AND active=1", (ch_id, owner_id))

    def remove_channel(self, ch_id: int, owner_id: int):
        """Deactivates the channel, cancels its open campaigns and refunds the undelivered part."""
        with self.tx() as c:
            ch = c.execute("SELECT * FROM channels WHERE id=? AND owner_id=? AND active=1", (ch_id, owner_id)).fetchone()
            if not ch:
                return None
            refund = 0
            for camp in c.execute(
                "SELECT * FROM campaigns WHERE channel_id=? AND status IN ('active','paused')", (ch_id,)
            ).fetchall():
                refund += (camp["target"] - camp["delivered"]) * camp["cost_per_member"]
                c.execute(
                    "UPDATE campaigns SET status='cancelled', target=delivered WHERE id=?", (camp["id"],)
                )
            if refund:
                self._credit(c, owner_id, refund, "refund", ref=f"channel:{ch_id}")
            c.execute("UPDATE channels SET active=0 WHERE id=?", (ch_id,))
            return refund

    # ---------- campaigns ----------
    def create_campaign(self, owner_id: int, ch_id: int, n: int):
        """Returns (status, value). status: ok | nochannel | exists | nobalance"""
        with self.tx() as c:
            ch = c.execute("SELECT * FROM channels WHERE id=? AND owner_id=? AND active=1", (ch_id, owner_id)).fetchone()
            if not ch:
                return "nochannel", None
            if c.execute(
                "SELECT 1 FROM campaigns WHERE channel_id=? AND status IN ('active','paused')", (ch_id,)
            ).fetchone():
                return "exists", None
            cost = n * config.COST_PER_MEMBER
            bal = c.execute("SELECT balance FROM users WHERE user_id=?", (owner_id,)).fetchone()["balance"]
            if bal < cost:
                return "nobalance", cost - bal
            cur = c.execute(
                "INSERT INTO campaigns(channel_id, owner_id, target, cost_per_member, created_at) VALUES(?,?,?,?,?)",
                (ch_id, owner_id, n, config.COST_PER_MEMBER, time.time()),
            )
            self._credit(c, owner_id, -cost, "campaign", ref=cur.lastrowid)
            return "ok", cur.lastrowid

    def cancel_campaign(self, camp_id: int, owner_id: int):
        with self.tx() as c:
            camp = c.execute(
                "SELECT * FROM campaigns WHERE id=? AND owner_id=? AND status IN ('active','paused')",
                (camp_id, owner_id),
            ).fetchone()
            if not camp:
                return None
            refund = (camp["target"] - camp["delivered"]) * camp["cost_per_member"]
            c.execute("UPDATE campaigns SET status='cancelled', target=delivered WHERE id=?", (camp_id,))
            if refund:
                self._credit(c, owner_id, refund, "refund", ref=f"campaign:{camp_id}")
            return refund

    def set_campaign_status(self, camp_id: int, status: str) -> bool:
        with self.tx() as c:
            cur = c.execute(
                "UPDATE campaigns SET status=? WHERE id=? AND status IN ('active','paused')", (status, camp_id)
            )
            return cur.rowcount > 0

    def user_campaigns(self, owner_id: int, limit=10):
        return self.all(
            """SELECT c.*, ch.title FROM campaigns c JOIN channels ch ON ch.id=c.channel_id
               WHERE c.owner_id=? ORDER BY c.id DESC LIMIT ?""",
            (owner_id, limit),
        )

    def campaign_full(self, camp_id: int):
        return self.one(
            """SELECT c.*, ch.chat_id, ch.title, ch.username, ch.invite_link, ch.chat_type
               FROM campaigns c JOIN channels ch ON ch.id=c.channel_id WHERE c.id=?""",
            (camp_id,),
        )

    # ---------- tasks ----------
    def next_task(self, uid: int, skip: set):
        rows = self.all(
            """SELECT c.id, c.target, c.delivered, c.owner_id,
                      ch.chat_id, ch.title, ch.username, ch.invite_link, ch.chat_type
               FROM campaigns c JOIN channels ch ON ch.id=c.channel_id
               WHERE c.status='active' AND c.delivered < c.target AND c.owner_id != ?
                 AND ch.chat_id NOT IN (SELECT chat_id FROM joins WHERE user_id=?)
               ORDER BY c.id LIMIT 30""",
            (uid, uid),
        )
        for r in rows:
            if r["id"] not in skip:
                return r
        return None

    def complete_join(self, uid: int, camp_id: int) -> dict:
        now = time.time()
        with self.tx() as c:
            camp = c.execute(
                """SELECT c.*, ch.chat_id, ch.title FROM campaigns c
                   JOIN channels ch ON ch.id=c.channel_id WHERE c.id=?""",
                (camp_id,),
            ).fetchone()
            if not camp or camp["status"] != "active" or camp["delivered"] >= camp["target"]:
                return {"ok": False, "reason": "full"}
            if camp["owner_id"] == uid:
                return {"ok": False, "reason": "own"}
            if c.execute("SELECT 1 FROM joins WHERE chat_id=? AND user_id=?", (camp["chat_id"], uid)).fetchone():
                return {"ok": False, "reason": "dup"}
            reward = config.JOIN_REWARD
            c.execute(
                "INSERT INTO joins(campaign_id, chat_id, user_id, reward, check_after, created_at) VALUES(?,?,?,?,?,?)",
                (camp_id, camp["chat_id"], uid, reward, now + config.RECHECK_DAYS * 86400, now),
            )
            delivered = camp["delivered"] + 1
            status = "done" if delivered >= camp["target"] else "active"
            c.execute("UPDATE campaigns SET delivered=?, status=? WHERE id=?", (delivered, status, camp_id))
            balance = self._credit(c, uid, reward, "join", ref=camp_id)

            u = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
            today = utc_day()
            used = u["tasks_today"] if u["tasks_day"] == today else 0
            c.execute(
                "UPDATE users SET tasks_day=?, tasks_today=?, last_task_at=? WHERE user_id=?",
                (today, used + 1, now, uid),
            )
            ref_to = None
            if u["referrer_id"] and not u["ref_rewarded"] and config.REFERRAL_BONUS:
                if c.execute("SELECT 1 FROM users WHERE user_id=?", (u["referrer_id"],)).fetchone():
                    self._credit(c, u["referrer_id"], config.REFERRAL_BONUS, "referral", ref=uid)
                    ref_to = u["referrer_id"]
                c.execute("UPDATE users SET ref_rewarded=1 WHERE user_id=?", (uid,))
            return {
                "ok": True,
                "reward": reward,
                "balance": balance,
                "completed": status == "done",
                "owner_id": camp["owner_id"],
                "title": camp["title"],
                "target": camp["target"],
                "ref_to": ref_to,
            }

    def report_campaign(self, camp_id: int, uid: int):
        """Returns (count, newly_paused, owner_id) or None if already reported."""
        with self.tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO reports(campaign_id, user_id) VALUES(?,?)", (camp_id, uid))
            if cur.rowcount == 0:
                return None
            n = c.execute("SELECT COUNT(*) n FROM reports WHERE campaign_id=?", (camp_id,)).fetchone()["n"]
            paused = False
            if n >= config.REPORT_LIMIT:
                paused = c.execute(
                    "UPDATE campaigns SET status='paused' WHERE id=? AND status='active'", (camp_id,)
                ).rowcount > 0
            owner = c.execute("SELECT owner_id FROM campaigns WHERE id=?", (camp_id,)).fetchone()
            return n, paused, owner["owner_id"] if owner else None

    # ---------- retention re-check ----------
    def due_joins(self, limit=100):
        return self.all(
            "SELECT * FROM joins WHERE status='ok' AND check_after<=? ORDER BY check_after LIMIT ?",
            (time.time(), limit),
        )

    def mark_kept(self, join_id: int):
        with self.tx() as c:
            c.execute("UPDATE joins SET status='kept' WHERE id=? AND status='ok'", (join_id,))

    def postpone_join(self, join_id: int, seconds: int):
        with self.tx() as c:
            c.execute("UPDATE joins SET check_after=? WHERE id=?", (time.time() + seconds, join_id))

    def mark_left(self, join_id: int):
        """The member left early: take the reward back and reopen the campaign slot."""
        with self.tx() as c:
            j = c.execute("SELECT * FROM joins WHERE id=?", (join_id,)).fetchone()
            if not j or j["status"] != "ok":
                return None
            c.execute("UPDATE joins SET status='left' WHERE id=?", (join_id,))
            c.execute(
                "UPDATE campaigns SET delivered=MAX(delivered-1,0), "
                "status=CASE WHEN status='done' THEN 'active' ELSE status END WHERE id=?",
                (j["campaign_id"],),
            )
            bal = c.execute("SELECT balance FROM users WHERE user_id=?", (j["user_id"],)).fetchone()
            penalty = min(j["reward"], bal["balance"]) if bal else 0
            if penalty > 0:
                self._credit(c, j["user_id"], -penalty, "penalty", ref=j["campaign_id"])
            title = c.execute(
                "SELECT title FROM channels WHERE chat_id=?", (j["chat_id"],)
            ).fetchone()
            return {"user_id": j["user_id"], "penalty": penalty, "title": title["title"] if title else ""}

    # ---------- orders ----------
    def create_order(self, uid: int, pkg_index: int) -> int:
        pkg = config.PACKAGES[pkg_index]
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO orders(user_id, members, credits, price, created_at) VALUES(?,?,?,?,?)",
                (uid, pkg["members"], pkg["members"] * config.COST_PER_MEMBER, pkg["price"], time.time()),
            )
            return cur.lastrowid

    def get_order(self, order_id: int):
        return self.one("SELECT * FROM orders WHERE id=?", (order_id,))

    def set_order_receipt(self, order_id: int, uid: int, receipt: str) -> bool:
        with self.tx() as c:
            cur = c.execute(
                "UPDATE orders SET status='pending', receipt=? WHERE id=? AND user_id=? AND status='awaiting_receipt'",
                (receipt, order_id, uid),
            )
            return cur.rowcount > 0

    def approve_order(self, order_id: int):
        with self.tx() as c:
            o = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not o or o["status"] != "pending":
                return None
            c.execute("UPDATE orders SET status='approved' WHERE id=?", (order_id,))
            bal = self._credit(c, o["user_id"], o["credits"], "purchase", ref=order_id)
            return {**dict(o), "balance": bal}

    def reject_order(self, order_id: int):
        with self.tx() as c:
            o = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not o or o["status"] != "pending":
                return None
            c.execute("UPDATE orders SET status='rejected' WHERE id=?", (order_id,))
            return dict(o)

    # ---------- admin stats ----------
    def stats(self) -> dict:
        g = lambda sql: self.one(sql)[0]
        return {
            "users": g("SELECT COUNT(*) FROM users"),
            "channels": g("SELECT COUNT(*) FROM channels WHERE active=1"),
            "active_campaigns": g("SELECT COUNT(*) FROM campaigns WHERE status='active'"),
            "joins": g("SELECT COUNT(*) FROM joins"),
            "left": g("SELECT COUNT(*) FROM joins WHERE status='left'"),
            "orders": g("SELECT COUNT(*) FROM orders WHERE status='approved'"),
            "revenue": g("SELECT COALESCE(SUM(price),0) FROM orders WHERE status='approved'"),
            "pending_orders": g("SELECT COUNT(*) FROM orders WHERE status='pending'"),
        }
