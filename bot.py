import os
import re
import html
import time
import sqlite3
import logging
import asyncio
from io import BytesIO
from contextlib import contextmanager
from urllib.parse import quote

import gateway  # loads .env first + bank e-mail verification

from telegram import (Update, InlineKeyboardButton as B, InlineKeyboardMarkup as M,
                      ReplyKeyboardMarkup, KeyboardButton)
from telegram.constants import ParseMode, ChatAction
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (Application, ApplicationHandlerStop, CommandHandler, CallbackQueryHandler,
                          MessageHandler, TypeHandler, filters, ContextTypes)

try:  # PTB 21.7+
    from telegram import CopyTextButton
except ImportError:  # pragma: no cover
    CopyTextButton = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("battlex")

# =====================  CONFIG  =====================
# Secrets live in .env (see .env.example) - never inside the code.
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "8920083996").replace(" ", "").split(",") if x.isdigit()}
DB = os.getenv("DB_PATH", "bot.db")
BRAND = os.getenv("BRAND", "BattleX")
MAIL_ON = gateway.MAIL_ON
POLL_SEC = gateway.POLL_SEC
VERIFY_TIMEOUT = int(os.getenv("VERIFY_TIMEOUT", "240"))
LATE_WINDOW = 6 * 3600

# Defaults for settings that the admin can change live from  Admin > Settings
SETTING_DEFAULTS = {
    "upi": os.getenv("UPI_ID", "7050273156@fam"),
    "min_add": os.getenv("MIN_ADD", "10"),
    "min_wd": os.getenv("MIN_WITHDRAW", "50"),
    "ref_bonus": os.getenv("REF_BONUS", "5"),
    "support": os.getenv("SUPPORT", "@admin"),
    "bot_on": "1",
}

FX_FIRE = "5104841245755180586"
FX_PARTY = "5046509860389126442"
FX_LIKE = "5107584321108051014"
LINE = "━━━━━━━━━━━━━━"
CLOSE = "x:close"


def KB(text, style=None):
    return KeyboardButton(text, api_kwargs={"style": style} if style else None)


def btn(text, data=None, style=None, **kw):
    return B(text, callback_data=data, api_kwargs={"style": style} if style else None, **kw)


def back_row(data=CLOSE, label="⬅️ Back"):
    return [btn(label, data, "danger")]


def mk(rows=(), back=CLOSE):
    rows = [list(r) for r in rows]
    if back:
        rows.append(back_row(back))
    return M(rows)


CLOSE_KB = mk([], CLOSE)


def user_menu(uid):
    rows = [[KB("🏆 Tournaments", "success"), KB("💰 Wallet", "primary")],
            [KB("➕ Add Money", "success"), KB("💸 Withdraw", "danger")],
            [KB("🎮 My Matches", "primary"), KB("👤 Profile", "primary")],
            [KB("🏅 Leaderboard", "success"), KB("🎁 Refer & Earn", "danger")],
            [KB("ℹ️ Help", "primary")]]
    if uid in ADMINS:
        rows.append([KB("🛠 Admin Panel", "danger")])
    # one_time_keyboard: menu hides after a tap and re-opens from the keyboard icon next to the input box
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True,
                               is_persistent=False,
                               input_field_placeholder="👇 Choose an option")


def e(x):
    return html.escape(str(x))


# =====================  DB  =====================
def _connect():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


@contextmanager
def tx():
    c = _connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()


def get(sql, args=(), one=False):
    c = _connect()
    try:
        cur = c.execute(sql, args)
        return cur.fetchone() if one else cur.fetchall()
    finally:
        c.close()


def run(sql, args=()):
    with tx() as c:
        cur = c.execute(sql, args)
        return cur.rowcount, cur.lastrowid


def init_db():
    c = _connect()
    c.execute("PRAGMA journal_mode=WAL;")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, balance INTEGER DEFAULT 0, ff_uid TEXT);
    CREATE TABLE IF NOT EXISTS tournaments(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, mode TEXT,
        fee INTEGER, prize INTEGER, slots INTEGER, time TEXT, status TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS entries(tid INTEGER, uid INTEGER, PRIMARY KEY(tid, uid));
    CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, amount INTEGER,
        utr TEXT UNIQUE, status TEXT DEFAULT 'pending');
    CREATE TABLE IF NOT EXISTS withdrawals(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, amount INTEGER,
        upi TEXT, status TEXT DEFAULT 'pending');
    CREATE TABLE IF NOT EXISTS bank_credits(utr TEXT PRIMARY KEY, amount REAL, claimed_by INTEGER,
        created TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS winners(tid INTEGER, uid INTEGER, amount INTEGER, created TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(tid, uid));
    CREATE TABLE IF NOT EXISTS txns(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, amount INTEGER,
        kind TEXT, note TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS seen_mails(uid TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    """)
    for table, col in [("tournaments", "room_id TEXT"), ("tournaments", "room_pw TEXT"),
                       ("tournaments", "p1 INTEGER"), ("tournaments", "p2 INTEGER"),
                       ("tournaments", "p3 INTEGER"), ("tournaments", "per_kill INTEGER"),
                       ("tournaments", "top_kill INTEGER"), ("tournaments", "paid INTEGER DEFAULT 0"),
                       ("users", "ref_by INTEGER"), ("users", "ref_paid INTEGER DEFAULT 0"),
                       ("users", "banned INTEGER DEFAULT 0"),
                       ("payments", "created_ts REAL"), ("payments", "reason TEXT")]:
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    c.execute("UPDATE payments SET created_ts=? WHERE created_ts IS NULL", (time.time(),))
    c.commit()
    c.close()
    gateway.init()


# ---- live settings ----
def cfg(key, c=None):
    sql, args = "SELECT value FROM settings WHERE key=?", (key,)
    r = c.execute(sql, args).fetchone() if c else get(sql, args, one=True)
    return r["value"] if r else SETTING_DEFAULTS[key]


def cfgi(key, c=None):
    try:
        return int(cfg(key, c))
    except (TypeError, ValueError):
        return int(SETTING_DEFAULTS[key])


def set_cfg(key, value):
    run("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))


def bot_is_on():
    return cfg("bot_on") == "1"


# ---- wallet ----
def credit(c, uid, amt, kind, note=""):
    c.execute("INSERT OR IGNORE INTO users(id,name) VALUES(?,?)", (uid, ""))
    c.execute("UPDATE users SET balance=balance+? WHERE id=?", (amt, uid))
    c.execute("INSERT INTO txns(uid,amount,kind,note) VALUES(?,?,?,?)", (uid, amt, kind, note))


def debit(c, uid, amt, kind, note=""):
    n = c.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", (amt, uid, amt)).rowcount
    if n:
        c.execute("INSERT INTO txns(uid,amount,kind,note) VALUES(?,?,?,?)", (uid, -amt, kind, note))
    return bool(n)


def ref_reward(c, uid):
    bonus = cfgi("ref_bonus", c)
    if bonus <= 0:
        return None
    r = c.execute("SELECT ref_by, ref_paid FROM users WHERE id=?", (uid,)).fetchone()
    if r and r["ref_by"] and not r["ref_paid"]:
        c.execute("UPDATE users SET ref_paid=1 WHERE id=?", (uid,))
        credit(c, r["ref_by"], bonus, "bonus", f"Referral {uid}")
        return r["ref_by"]
    return None


def balance(uid):
    r = get("SELECT balance FROM users WHERE id=?", (uid,), one=True)
    return r["balance"] if r else 0


def ensure_user(u):
    run("INSERT OR IGNORE INTO users(id,name) VALUES(?,?)", (u.id, u.first_name))


def filled_slots(tid):
    return get("SELECT COUNT(*) n FROM entries WHERE tid=?", (tid,), one=True)["n"]


# =====================  TOURNAMENT LOGIC  =====================
def join_tournament(uid, tid):
    with tx() as c:
        t = c.execute("SELECT * FROM tournaments WHERE id=?", (tid,)).fetchone()
        if not t or t["status"] != "open":
            return False, "❌ <b>This tournament is closed.</b>"
        if c.execute("SELECT 1 FROM entries WHERE tid=? AND uid=?", (tid, uid)).fetchone():
            return False, "✅ You have already joined this tournament."
        n = c.execute("SELECT COUNT(*) n FROM entries WHERE tid=?", (tid,)).fetchone()["n"]
        if n >= t["slots"]:
            return False, "❌ <b>All slots are full.</b>"
        u = c.execute("SELECT balance, ff_uid FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            return False, "⚠️ Please press /start first."
        if not u["ff_uid"]:
            return False, "⚠️ Set your Free Fire UID in <b>👤 Profile</b> first."
        if not debit(c, uid, t["fee"], "entry", f"Tournament #{tid}"):
            return False, (f"❌ <b>Insufficient balance.</b>\nEntry ₹{t['fee']} • Your balance ₹{u['balance']}\n"
                           f"➕ Use Add Money to top up.")
        c.execute("INSERT INTO entries VALUES(?,?)", (tid, uid))
        return True, (f"✅ <b>Entry Confirmed!</b>\n{LINE}\n🏆 {e(t['title'])}\n💸 ₹{t['fee']} entry fee deducted\n"
                      f"🔑 Room ID & password will be sent before the match.")


# =====================  PAYMENT AUTO-VERIFY (uses gateway.py)  =====================
def evaluate(pid):
    """Returns (status, reason, extra). status: approved / rejected / pending / skip"""
    now = time.time()
    with tx() as c:
        p = c.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
        if not p:
            return "skip", None, None
        late = (p["status"] == "rejected" and p["reason"] == "timeout"
                and now - (p["created_ts"] or 0) < LATE_WINDOW)
        if p["status"] != "pending" and not late:
            return "skip", None, None
        bc = c.execute("SELECT * FROM bank_credits WHERE utr=?", (p["utr"],)).fetchone()
        if bc is None:
            if p["status"] == "pending" and now - (p["created_ts"] or now) > VERIFY_TIMEOUT:
                c.execute("UPDATE payments SET status='rejected', reason='timeout' WHERE id=?", (pid,))
                return "rejected", "timeout", None
            return "pending", None, None
        if bc["claimed_by"] is not None:
            c.execute("UPDATE payments SET status='rejected', reason='used' WHERE id=?", (pid,))
            return "rejected", "used", None
        if abs(float(bc["amount"]) - float(p["amount"])) > 0.01:
            c.execute("UPDATE payments SET status='rejected', reason='amount' WHERE id=?", (pid,))
            return "rejected", "amount", float(bc["amount"])
        c.execute("UPDATE payments SET status='approved', reason=NULL WHERE id=?", (pid,))
        c.execute("UPDATE bank_credits SET claimed_by=? WHERE utr=?", (p["uid"], p["utr"]))
        credit(c, p["uid"], p["amount"], "deposit", f"UTR {p['utr']}")
        return "approved", None, ref_reward(c, p["uid"])


def reject_text(reason, amt, extra):
    if reason == "used":
        return f"❌ <b>UTR Rejected</b>\n{LINE}\nThis UTR has already been used."
    if reason == "amount":
        return (f"❌ <b>Amount Mismatch</b>\n{LINE}\nBank received <b>₹{extra:g}</b> but you requested <b>₹{amt}</b>.\n"
                f"Please contact support: {e(cfg('support'))}")
    return (f"❌ <b>UTR Not Verified</b>\n{LINE}\nNo payment with this UTR was found.\n"
            f"• Wrong UTR? Go to ➕ Add Money and try again.\n"
            f"• Already paid? If the bank confirmation arrives later, your wallet is credited automatically.")


async def settle(ctx, p, msg=None):
    st, reason, extra = evaluate(p["id"])
    uid, amt = p["uid"], p["amount"]
    if st == "approved":
        if msg:
            try:
                await msg.delete()
            except Exception:
                pass
        await safe_send(ctx, uid, f"✅ <b>Payment Verified!</b>\n{LINE}\n💰 ₹{amt} added to your wallet\n"
                                  f"💎 Balance: <b>₹{balance(uid)}</b>", effect=FX_PARTY)
        await notify_ref(ctx, extra)
    elif st == "rejected":
        text = reject_text(reason, amt, extra)
        sent = False
        if msg:
            try:
                await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=CLOSE_KB)
                sent = True
            except Exception:
                pass
        if not sent:
            await safe_send(ctx, uid, text)
        if reason in ("timeout", "amount"):
            mk_ = (M([[btn("✅ Force Approve", f"pay_ok:{p['id']}", "success")], back_row()])
                   if reason == "timeout" else None)
            await notify_admins(ctx, f"⚠️ <b>Deposit auto-rejected ({reason})</b>\n👤 <code>{uid}</code>\n"
                                     f"💵 ₹{amt}\n🔢 UTR: <code>{p['utr']}</code>", mk_)
    elif st == "pending" and msg:
        try:
            await msg.edit_text(
                f"⏳ <b>Waiting for bank confirmation</b>\n{LINE}\nUTR: <code>{p['utr']}</code>\n"
                f"I will keep checking for up to {VERIFY_TIMEOUT // 60} min and notify you automatically.",
                parse_mode=ParseMode.HTML, reply_markup=CLOSE_KB)
        except Exception:
            pass
    return st


async def notify_ref(ctx, ref):
    if ref:
        await safe_send(ctx, ref, f"🎁 <b>Referral Bonus!</b>\n{LINE}\n💰 ₹{cfgi('ref_bonus')} added to your wallet.\n"
                                  f"Your friend made their first deposit 🔥", effect=FX_PARTY)


async def poll_job(ctx: ContextTypes.DEFAULT_TYPE):
    if not MAIL_ON:
        return
    try:
        await asyncio.get_running_loop().run_in_executor(None, gateway.fetch_credits)
    except Exception as ex:
        log.warning("Email poll error: %s", ex)
        return
    rows = get("SELECT * FROM payments WHERE status='pending' OR "
               "(status='rejected' AND reason='timeout' AND created_ts>?)", (time.time() - LATE_WINDOW,))
    for p in rows:
        await settle(ctx, p)


# =====================  SEND / BOX HELPERS  =====================
def is_admin(update):
    return update.effective_user.id in ADMINS


async def safe_send(ctx, chat_id, text, markup=None, effect=None):
    """Sends a message. Without markup it gets a 'Back' button that simply hides the message."""
    if markup is None:
        markup = CLOSE_KB
    kw = dict(parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True)
    try:
        if effect:
            try:
                return await ctx.bot.send_message(chat_id, text, api_kwargs={"message_effect_id": effect}, **kw)
            except BadRequest:
                pass
        return await ctx.bot.send_message(chat_id, text, **kw)
    except RetryAfter as ra:
        await asyncio.sleep(ra.retry_after + 1)
        return await safe_send(ctx, chat_id, text, markup, effect)
    except Exception as ex:
        log.warning("send to %s failed: %s", chat_id, ex)


async def notify_admins(ctx, text, markup=None):
    for a in ADMINS:
        await safe_send(ctx, a, text, markup)


async def say(update, text, markup=None, effect=None):
    """New message in the chat (never a reply, so it works even after we delete the user's message)."""
    ch = update.effective_chat
    kw = dict(parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True)
    if effect:
        try:
            return await ch.send_message(text, api_kwargs={"message_effect_id": effect}, **kw)
        except BadRequest:
            pass
    return await ch.send_message(text, **kw)


async def del_msg(update):
    try:
        await update.effective_message.delete()
    except Exception:
        pass


async def kill_box(ctx):
    mid = ctx.chat_data.pop("box", None)
    chat = ctx.chat_data.get("box_chat")
    if mid and chat:
        try:
            await ctx.bot.delete_message(chat, mid)
        except Exception:
            pass


async def open_box(update, ctx, text, markup=None, effect=None):
    """Opens a fresh box (old box is removed, so only one box is on screen)."""
    await kill_box(ctx)
    m = await say(update, text, markup, effect)
    ctx.chat_data["box"] = m.message_id
    ctx.chat_data["box_chat"] = m.chat_id
    return m


async def show(ctx, chat_id, text, markup=None):
    """Updates the current box (used after the user types something)."""
    mid = ctx.chat_data.get("box")
    if mid:
        try:
            await ctx.bot.edit_message_text(text, chat_id=chat_id, message_id=mid, parse_mode=ParseMode.HTML,
                                            reply_markup=markup, disable_web_page_preview=True)
            return
        except BadRequest as ex:
            if "not modified" in str(ex).lower():
                return
    m = await safe_send(ctx, chat_id, text, markup)
    if m:
        ctx.chat_data["box"] = m.message_id
        ctx.chat_data["box_chat"] = m.chat_id


async def cb(q, ctx, text, markup=None):
    """Edits the box that owns the pressed button."""
    ctx.chat_data["box"] = q.message.message_id
    ctx.chat_data["box_chat"] = q.message.chat_id
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                  disable_web_page_preview=True)
    except BadRequest as ex:
        if "not modified" in str(ex).lower():
            return
        try:  # e.g. the box is a photo (QR) - replace it
            await q.message.delete()
        except Exception:
            pass
        m = await safe_send(ctx, q.message.chat_id, text, markup)
        if m:
            ctx.chat_data["box"] = m.message_id
            ctx.chat_data["box_chat"] = m.chat_id


# ---- tournament card: only the prizes the admin selected are shown ----
def tprizes(t):
    d = {k: (t[k] or 0) for k in ("p1", "p2", "p3", "per_kill", "top_kill")}
    if not any(d.values()) and t["prize"]:  # old tournaments (single prize pool)
        d["p1"] = t["prize"]
    return d


def prize_lines(t):
    pr, out = tprizes(t), []
    for k, ic, lb in (("p1", "🥇", "1st Prize"), ("p2", "🥈", "2nd Prize"), ("p3", "🥉", "3rd Prize")):
        if pr[k]:
            out.append(f"{ic} {lb}: <b>₹{pr[k]}</b>")
    if pr["per_kill"]:
        out.append(f"🎯 Per Kill: <b>₹{pr['per_kill']}</b>")
    if pr["top_kill"]:
        out.append(f"💀 Highest Kill: <b>₹{pr['top_kill']}</b>")
    return "\n".join(out)


def card(t, admin=False):
    filled = filled_slots(t["id"])
    ratio = filled / t["slots"] if t["slots"] else 0
    k = round(10 * ratio)
    bar = "🟩" * k + "⬜" * (10 - k)
    st = t["status"]
    if st == "cancelled":
        badge = "⚫ CANCELLED"
    elif st == "completed":
        badge = "✅ COMPLETED"
    elif st == "closed":
        badge = "🔒 CLOSED"
    elif filled >= t["slots"]:
        badge = "🔴 FULL"
    elif ratio >= 0.9:
        badge = "⚡ ALMOST FULL"
    elif ratio >= 0.6:
        badge = "🔥 FILLING FAST"
    else:
        badge = "🟢 OPEN"
    pl = prize_lines(t)
    s = (f"🏆 <b>{e(t['title'])}</b>  <code>#{t['id']}</code>\n{badge}\n{LINE}\n"
         f"🎮 Mode: <b>{e(t['mode'])}</b>\n💵 Entry: <b>₹{t['fee']}</b>\n"
         + (pl + "\n" if pl else "") +
         f"🕒 Time: <b>{e(t['time'])}</b>\n"
         f"👥 Slots: <b>{filled}/{t['slots']}</b>\n{bar}")
    if admin:
        s += f"\n{LINE}\n🔑 Room: <code>{e(t['room_id'] or '—')}</code> | 🔒 Pass: <code>{e(t['room_pw'] or '—')}</code>"
    return s


async def gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or u.id in ADMINS:
        return
    r = get("SELECT banned FROM users WHERE id=?", (u.id,), one=True)
    if r and r["banned"]:
        if update.callback_query:
            await update.callback_query.answer("🚫 Your account is banned.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text("🚫 Your account is banned. Contact support.")
        raise ApplicationHandlerStop
    if not bot_is_on():
        if update.callback_query:
            await update.callback_query.answer("🔧 Bot is under maintenance. Please try again later.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text("🔧 Bot is under maintenance. Please try again later.")
        raise ApplicationHandlerStop


# =====================  USER SIDE  =====================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    existed = get("SELECT 1 FROM users WHERE id=?", (u.id,), one=True)
    ensure_user(u)
    ctx.user_data.clear()
    await kill_box(ctx)
    if not existed and ctx.args and ctx.args[0].isdigit():
        ref = int(ctx.args[0])
        if ref != u.id and get("SELECT 1 FROM users WHERE id=?", (ref,), one=True):
            run("UPDATE users SET ref_by=? WHERE id=?", (ref, u.id))
    players_n = get("SELECT COUNT(*) n FROM users", one=True)["n"]
    open_n = get("SELECT COUNT(*) n FROM tournaments WHERE status='open'", one=True)["n"]
    await say(update,
              f"👑 <b>{e(BRAND)} Esports</b> 👑\n{LINE}\n"
              f"Welcome, <b>{e(u.first_name)}</b>! 🔥\n\n"
              f"🏆 Daily Free Fire Tournaments\n⚡ Instant Auto-Verified Deposits\n"
              f"💸 Fast Withdrawals\n🎁 Refer &amp; Earn\n{LINE}\n"
              f"👥 <b>{players_n}</b> players  •  🟢 <b>{open_n}</b> live tournaments\n\n"
              f"<i>Open the menu with the keyboard icon ⌨️ next to the message box 👇</i>",
              user_menu(u.id), effect=FX_FIRE)


# ---- Tournaments ----
def tlist_view():
    ts = get("SELECT * FROM tournaments WHERE status='open' ORDER BY id DESC LIMIT 20")
    if not ts:
        return "😴 <b>No tournaments are open right now.</b>\nCome back soon!", mk([])
    rows = [[btn(f"🏆 {t['title'][:22]} • ₹{t['fee']} • {filled_slots(t['id'])}/{t['slots']}",
                 f"tv:{t['id']}", "success")] for t in ts]
    return (f"🏆 <b>Live Tournaments</b>  ({len(ts)})\n{LINE}\nTap a tournament to see details 👇"), mk(rows)


async def show_tournaments(update, ctx):
    await open_box(update, ctx, *tlist_view())


async def h_tl(q, ctx, val):
    await q.answer()
    await cb(q, ctx, *tlist_view())


async def h_tv(q, ctx, val):
    await q.answer()
    t = get("SELECT * FROM tournaments WHERE id=?", (val,), one=True)
    if not t:
        return await cb(q, ctx, "❌ Tournament not found.", mk([], "tl:0"))
    rows = []
    if t["status"] == "open":
        rows.append([btn(f"⚔️ Join Now • ₹{t['fee']}", f"join:{t['id']}", "success")])
    await cb(q, ctx, card(t), mk(rows, "tl:0"))


async def h_join(q, ctx, val):
    await q.answer()
    t = get("SELECT * FROM tournaments WHERE id=?", (val,), one=True)
    if not t or t["status"] != "open":
        return await cb(q, ctx, "❌ This tournament is closed.", mk([], "tl:0"))
    await cb(q, ctx, card(t) + f"\n{LINE}\n💰 Your balance: <b>₹{balance(q.from_user.id)}</b>\n\nConfirm joining?",
             mk([[btn("✅ Confirm", f"yes:{val}", "success")]], f"tv:{val}"))


async def h_yes(q, ctx, val):
    await q.answer()
    ok, msg = join_tournament(q.from_user.id, int(val))
    rows = []
    if not ok and "Insufficient" in msg:
        rows.append([btn("➕ Add Money", "addm:wal", "success")])
    await cb(q, ctx, msg, mk(rows, "tl:0"))
    if ok:
        await safe_send(ctx, q.from_user.id, "🎉 <b>Best of luck, champion!</b> 🔥", effect=FX_PARTY)


# ---- Wallet ----
def wallet_view(uid):
    rows = get("SELECT * FROM txns WHERE uid=? ORDER BY id DESC LIMIT 6", (uid,))
    icons = {"deposit": "➕", "withdraw": "💸", "entry": "🎟", "prize": "🏆", "refund": "↩️",
             "admin": "🛠", "bonus": "🎁"}
    hist = "\n".join(f"{icons.get(r['kind'], '•')} <b>{'+' if r['amount'] > 0 else '-'}₹{abs(r['amount'])}</b> "
                     f"<i>{e(r['kind'])}</i>" for r in rows) or "<i>No transactions yet.</i>"
    return (f"💰 <b>My Wallet</b>\n{LINE}\n💎 Balance: <b>₹{balance(uid)}</b>\n\n🧾 <b>Recent Activity</b>\n{hist}",
            mk([[btn("➕ Add Money", "addm:wal", "success"), btn("💸 Withdraw", "wdm:wal", "primary")]]))


async def show_wallet(update, ctx):
    await open_box(update, ctx, *wallet_view(update.effective_user.id))


async def h_wal(q, ctx, val):
    await q.answer()
    ctx.user_data.clear()
    await cb(q, ctx, *wallet_view(q.from_user.id))


# ---- My matches / Profile / Leaderboard / Refer / Help ----
async def my_matches(update, ctx):
    rows = get("""SELECT t.* FROM tournaments t JOIN entries e ON e.tid=t.id
                  WHERE e.uid=? ORDER BY t.id DESC LIMIT 10""", (update.effective_user.id,))
    if not rows:
        return await open_box(update, ctx, "🎮 You haven't joined any match yet.\n🏆 Open Tournaments to join!", mk([]))
    out = []
    for t in rows:
        s = f"🏆 <b>{e(t['title'])}</b> <code>#{t['id']}</code>\n🕒 {e(t['time'])} • <i>{e(t['status'])}</i>"
        if t["room_id"] and t["status"] in ("open", "closed"):
            s += f"\n🔑 Room: <code>{e(t['room_id'])}</code> | 🔒 Pass: <code>{e(t['room_pw'])}</code>"
        out.append(s)
    await open_box(update, ctx, f"🎮 <b>My Matches</b>\n{LINE}\n" + "\n\n".join(out), mk([]))


def profile_view(uid, note=""):
    u = get("SELECT * FROM users WHERE id=?", (uid,), one=True)
    played = get("SELECT COUNT(*) n FROM entries WHERE uid=?", (uid,), one=True)["n"]
    w = get("SELECT COUNT(*) n, COALESCE(SUM(amount),0) s FROM winners WHERE uid=?", (uid,), one=True)
    rate = f"{round(100 * w['n'] / played)}%" if played else "—"
    return (f"👤 <b>My Profile</b>\n{LINE}\n"
            f"🙍 Name: <b>{e(u['name'])}</b>\n🆔 TG ID: <code>{uid}</code>\n"
            f"🔥 FF UID: <code>{e(u['ff_uid'] or 'not set')}</code>\n{LINE}\n"
            f"🎮 Matches: <b>{played}</b>\n🏆 Wins: <b>{w['n']}</b>  (Win rate {rate})\n"
            f"💵 Winnings: <b>₹{w['s']}</b>\n💰 Balance: <b>₹{u['balance']}</b>{note}",
            mk([[btn("✏️ Change FF UID", "uid:0", "primary")]]))


async def profile(update, ctx):
    await open_box(update, ctx, *profile_view(update.effective_user.id))


async def h_pf(q, ctx, val):
    await q.answer()
    ctx.user_data.clear()
    await cb(q, ctx, *profile_view(q.from_user.id))


async def h_uid(q, ctx, val):
    await q.answer()
    ctx.user_data.update(state="ffuid")
    await cb(q, ctx, f"✏️ <b>Change FF UID</b>\n{LINE}\nSend your <b>Free Fire UID</b> (numbers only):", mk([], "pf:0"))


async def leaderboard(update, ctx):
    rows = get("""SELECT u.name, SUM(w.amount) s, COUNT(*) n FROM winners w JOIN users u ON u.id=w.uid
                  GROUP BY w.uid ORDER BY s DESC LIMIT 10""")
    if not rows:
        return await open_box(update, ctx, "🏅 The leaderboard is empty. Be the first winner! 🔥", mk([]))
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    txt = "\n".join(f"{medals[i]} <b>{e(r['name'])}</b> — ₹{r['s']}  ({r['n']} wins)" for i, r in enumerate(rows))
    rec = get("""SELECT u.name, w.amount, t.title FROM winners w JOIN users u ON u.id=w.uid
                 LEFT JOIN tournaments t ON t.id=w.tid ORDER BY w.created DESC LIMIT 5""")
    recent = "\n".join(f"🎉 <b>{e(r['name'])}</b> won ₹{r['amount']} • {e(r['title'] or '')}" for r in rec)
    await open_box(update, ctx, f"🏅 <b>Top Players</b>\n{LINE}\n{txt}\n\n🆕 <b>Recent Winners</b>\n{recent}", mk([]))


async def refer(update, ctx):
    uid = update.effective_user.id
    link = f"https://t.me/{ctx.bot.username}?start={uid}"
    n = get("SELECT COUNT(*) n FROM users WHERE ref_by=?", (uid,), one=True)["n"]
    paid = get("SELECT COUNT(*) n FROM users WHERE ref_by=? AND ref_paid=1", (uid,), one=True)["n"]
    earned = get("SELECT COALESCE(SUM(amount),0) s FROM txns WHERE uid=? AND kind='bonus'", (uid,), one=True)["s"]
    share = ("https://t.me/share/url?url=" + quote(link) +
             "&text=" + quote(f"🔥 Play Free Fire tournaments on {BRAND} and win real money!"))
    await open_box(update, ctx,
                   f"🎁 <b>Refer &amp; Earn</b>\n{LINE}\n"
                   f"Invite friends. When they make their first deposit you get <b>₹{cfgi('ref_bonus')}</b>!\n\n"
                   f"🔗 Your link:\n<code>{link}</code>\n{LINE}\n"
                   f"👥 Invited: <b>{n}</b>\n✅ Deposited: <b>{paid}</b>\n💰 Earned: <b>₹{earned}</b>",
                   mk([[btn("📤 Share Link", style="success", url=share)]]))


async def help_cmd(update, ctx):
    await open_box(update, ctx,
                   f"ℹ️ <b>Help</b>\n{LINE}\n"
                   f"1️⃣ Set your FF UID in 👤 Profile\n2️⃣ Add balance via ➕ Add Money (scan the QR)\n"
                   f"3️⃣ Join a tournament\n4️⃣ Room ID arrives before the match\n"
                   f"5️⃣ Win and 💸 Withdraw\n{LINE}\n"
                   f"➕ Min add: <b>₹{cfgi('min_add')}</b>\n💸 Min withdraw: <b>₹{cfgi('min_wd')}</b>\n"
                   f"🆘 Support: {e(cfg('support'))}", mk([]))


# ---- Add Money (keypad -> QR scanner) ----
def kp_view(ctx):
    kp = ctx.user_data.get("kp", "")
    amt = int(kp) if kp else 0
    mn = cfgi("min_add")
    txt = (f"➕ <b>Add Money</b>\n{LINE}\n💵 Amount: <b>₹{amt}</b>\n"
           f"<i>Tap the numbers (or type the amount). Min ₹{mn}.\nThen press “Show QR” to get the scanner.</i>")
    quick = [a for a in (10, 20, 50, 100, 200, 500) if a >= mn][:6]
    rows = [[btn(f"₹{a}", f"k:q:{a}", "success") for a in quick[i:i + 3]] for i in range(0, len(quick), 3)]
    rows += [[btn(str(n), f"k:{n}", "primary") for n in (1, 2, 3)],
             [btn(str(n), f"k:{n}", "primary") for n in (4, 5, 6)],
             [btn(str(n), f"k:{n}", "primary") for n in (7, 8, 9)],
             [btn("⌫", "k:del", "danger"), btn("0", "k:0", "primary"), btn("🗑 Clear", "k:clr", "danger")],
             [btn(f"✅ Show QR • ₹{amt}" if amt else "✅ Show QR", "k:go", "success")]]
    return txt, mk(rows, ctx.user_data.get("back", CLOSE))


def add_screen(ctx, back=CLOSE):
    ctx.user_data.clear()
    ctx.user_data.update(state="add_amt", kp="", back=back)
    return kp_view(ctx)


async def begin_add(update, ctx):
    await open_box(update, ctx, *add_screen(ctx, CLOSE))


async def h_addm(q, ctx, val):
    await q.answer()
    await cb(q, ctx, *add_screen(ctx, "wal:0"))


def upi_link(amt):
    return (f"upi://pay?pa={quote(cfg('upi'), safe='@.-_')}&pn={quote(BRAND)}"
            f"&am={amt}&cu=INR&tn={quote('Add money')}")


def make_qr(amt):
    """PNG bytes made locally (pip install qrcode[pil]); falls back to a public QR URL if unavailable."""
    link = upi_link(amt)
    try:
        import qrcode
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
        qr.add_data(link)
        qr.make(fit=True)
        buf = BytesIO()
        qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
        buf.seek(0)
        buf.name = "upi_qr.png"
        return buf
    except Exception as ex:
        log.warning("Local QR failed (%s) - using web QR", ex)
        return "https://api.qrserver.com/v1/create-qr-code/?size=600x600&margin=10&data=" + quote(link)


def pay_instructions(amt):
    return (f"💳 <b>Scan &amp; Pay</b>\n{LINE}\n💵 Amount: <b>₹{amt}</b>\n"
            f"📲 UPI ID: <code>{e(cfg('upi'))}</code>\n\n"
            f"1️⃣ Scan this QR with any UPI app (amount is pre-filled)\n"
            f"2️⃣ Pay exactly <b>₹{amt}</b>\n"
            f"3️⃣ Send the <b>12-digit UTR</b> here 👇\n"
            f"<i>UTR is verified automatically. A wrong UTR is rejected.</i>")


def pay_kb():
    rows = []
    if CopyTextButton:
        rows.append([B("📋 Copy UPI ID", copy_text=CopyTextButton(cfg("upi")), api_kwargs={"style": "primary"})])
    return mk(rows, "addb:0")


async def send_qr(ctx, chat_id, amt):
    await kill_box(ctx)
    ctx.user_data.update(state="add_utr", amt=amt)
    try:
        m = await ctx.bot.send_photo(chat_id, make_qr(amt), caption=pay_instructions(amt),
                                     parse_mode=ParseMode.HTML, reply_markup=pay_kb())
    except Exception as ex:
        log.warning("send_photo failed: %s", ex)
        m = await safe_send(ctx, chat_id, pay_instructions(amt) + f"\n\n🔗 <code>{e(upi_link(amt))}</code>", pay_kb())
    if m:
        ctx.chat_data["box"] = m.message_id
        ctx.chat_data["box_chat"] = m.chat_id


async def h_k(q, ctx, val):
    if "kp" not in ctx.user_data:
        await q.answer()
        return await cb(q, ctx, *add_screen(ctx, "wal:0"))
    kp = ctx.user_data["kp"]
    if val.startswith("q:"):
        kp = val[2:]
    elif val == "del":
        kp = kp[:-1]
    elif val == "clr":
        kp = ""
    elif val == "go":
        amt = int(kp) if kp else 0
        if amt < cfgi("min_add"):
            return await q.answer(f"Minimum ₹{cfgi('min_add')}", show_alert=True)
        await q.answer()
        return await send_qr(ctx, q.message.chat_id, amt)
    elif val.isdigit():
        if len(kp) < 5:
            kp = (kp + val).lstrip("0")
    await q.answer()
    ctx.user_data["kp"] = kp
    await cb(q, ctx, *kp_view(ctx))


async def h_addb(q, ctx, val):
    """Back from the QR screen -> amount keypad."""
    await q.answer()
    amt = ctx.user_data.get("amt")
    try:
        await q.message.delete()
    except Exception:
        pass
    ctx.chat_data.pop("box", None)
    ctx.user_data.update(state="add_amt", kp=str(amt) if amt else "")
    text, kb_ = kp_view(ctx)
    m = await safe_send(ctx, q.message.chat_id, text, kb_)
    if m:
        ctx.chat_data["box"] = m.message_id
        ctx.chat_data["box_chat"] = m.chat_id


# ---- Withdraw ----
def wd_amount_view(ctx, uid, back):
    ctx.user_data.update(state="wd_amt", back=back)
    ctx.user_data.pop("amt", None)
    bal, mn = balance(uid), cfgi("min_wd")
    opts = [a for a in (50, 100, 200, 500) if mn <= a <= bal]
    rows = [[btn(f"₹{a}", f"wda:{a}", "success") for a in opts[i:i + 3]] for i in range(0, len(opts), 3)]
    if bal >= mn:
        rows.append([btn(f"💯 All • ₹{bal}", f"wda:{bal}", "primary")])
    return (f"💸 <b>Withdraw</b>\n{LINE}\n💰 Balance: <b>₹{bal}</b>\n"
            f"Choose an amount or type it (min ₹{mn}):"), mk(rows, back)


async def begin_withdraw(update, ctx):
    ctx.user_data.clear()
    await open_box(update, ctx, *wd_amount_view(ctx, update.effective_user.id, CLOSE))


async def h_wdm(q, ctx, val):
    await q.answer()
    back = "wal:0" if val == "wal" else ctx.user_data.get("back", CLOSE)
    await cb(q, ctx, *wd_amount_view(ctx, q.from_user.id, back))


async def h_wda(q, ctx, val):
    await q.answer()
    amt = int(val)
    if amt > balance(q.from_user.id) or amt < cfgi("min_wd"):
        return await q.answer("Invalid amount", show_alert=True)
    ctx.user_data.update(state="wd_upi", amt=amt)
    await cb(q, ctx, f"📲 <b>Withdraw ₹{amt}</b>\n{LINE}\nSend your <b>UPI ID</b> (where you want the money):",
             mk([], "wdm:re"))


MENU_ACTIONS = {
    "🏆 Tournaments": show_tournaments, "💰 Wallet": show_wallet, "🎮 My Matches": my_matches,
    "🏅 Leaderboard": leaderboard, "ℹ️ Help": help_cmd, "➕ Add Money": begin_add,
    "💸 Withdraw": begin_withdraw, "👤 Profile": profile, "🎁 Refer & Earn": refer,
}


# =====================  ADMIN TERMINAL  =====================
def stats_text():
    u = get("SELECT COUNT(*) n, COALESCE(SUM(balance),0) b FROM users", one=True)
    p = get("SELECT COUNT(*) n FROM payments WHERE status='pending'", one=True)["n"]
    w = get("SELECT COUNT(*) n FROM withdrawals WHERE status='pending'", one=True)["n"]
    t = get("SELECT COUNT(*) n FROM tournaments WHERE status='open'", one=True)["n"]
    dep = get("SELECT COALESCE(SUM(amount),0) s FROM txns WHERE kind='deposit' AND date(created)=date('now')", one=True)["s"]
    wd = get("SELECT COALESCE(-SUM(amount),0) s FROM txns WHERE kind='withdraw' AND date(created)=date('now')", one=True)["s"]
    return (f"📊 <b>Bot Statistics</b>\n{LINE}\n👥 Users: <b>{u['n']}</b>\n💰 Total wallet: <b>₹{u['b']}</b>\n"
            f"🏆 Open tournaments: <b>{t}</b>\n{LINE}\n"
            f"📥 Today's deposits: <b>₹{dep}</b>\n📤 Today's withdraw requests: <b>₹{wd}</b>\n{LINE}\n"
            f"⏳ Pending deposits: <b>{p}</b>\n⏳ Pending withdrawals: <b>{w}</b>\n"
            f"📧 Mail verify: <b>{'ON' if MAIL_ON else 'OFF'}</b>")


def terminal_view():
    p = get("SELECT COUNT(*) n FROM payments WHERE status='pending'", one=True)["n"]
    w = get("SELECT COUNT(*) n FROM withdrawals WHERE status='pending'", one=True)["n"]
    on = bot_is_on()
    rows = [
        [btn("📊 Bot Statistics", "a:stats", "primary")],
        [btn("🏆 Create Tournament", "a:create", "success"), btn("🗂 Manage Tournaments", "a:manage", "primary")],
        [btn("🏁 Declare Winner", "a:winner", "success"), btn("👥 User Control", "a:users", "primary")],
        [btn(f"💳 Deposits ({p})", "a:deps", "success"), btn(f"💸 Withdrawals ({w})", "a:wds", "danger")],
        [btn("💰 Add Balance", "a:addbal", "success"), btn("📢 Broadcast", "a:bc", "primary")],
        [btn("🏦 Bank Credits", "a:credits", "primary"), btn("⚙️ Settings", "a:settings", "primary")],
        [btn(f"Bot Status: {'ON 🟢' if on else 'OFF 🔴'}", "a:botstatus", "success" if on else "danger")],
    ]
    return (f"⚙️ <b>Advanced Admin Terminal</b>\n<i>Authorized Access Granted.</i>\n{LINE}\n"
            f"⏳ Pending deposits: <b>{p}</b>   ⏳ Withdrawals: <b>{w}</b>\n"
            f"📧 Mail auto-verify: <b>{'ON ✅' if MAIL_ON else 'OFF ❌'}</b>"), mk(rows, CLOSE)


async def admin_panel(update, ctx):
    if not is_admin(update):
        return
    ctx.user_data.clear()
    await open_box(update, ctx, *terminal_view())


MENU_ACTIONS["🛠 Admin Panel"] = admin_panel


async def h_a(q, ctx, val):
    await q.answer()
    uid = q.from_user.id
    if val == "home":
        ctx.user_data.clear()
        return await cb(q, ctx, *terminal_view())
    if val == "stats":
        return await cb(q, ctx, stats_text(), mk([[btn("🔄 Refresh", "a:stats", "primary")]], "a:home"))
    if val == "create":
        ctx.user_data.clear()
        ctx.user_data["form"] = {}
        return await cb(q, ctx, *tc_form(ctx))
    if val == "manage":
        return await cb(q, ctx, *manage_list_view(False))
    if val == "winner":
        return await cb(q, ctx, *manage_list_view(True))
    if val == "users":
        ctx.user_data.clear()
        return await cb(q, ctx, *users_menu_view())
    if val == "deps":
        return await cb(q, ctx, *deps_view())
    if val == "wds":
        return await cb(q, ctx, *wds_view())
    if val == "addbal":
        ctx.user_data.clear()
        ctx.user_data["state"] = "ab_uid"
        return await cb(q, ctx, f"💰 <b>Add / Deduct Balance</b>\n{LINE}\nSend the user's <b>Telegram ID</b>:",
                        mk([], "a:home"))
    if val == "bc":
        ctx.user_data.clear()
        ctx.user_data["state"] = "bc_text"
        return await cb(q, ctx, f"📢 <b>Broadcast</b>\n{LINE}\nSend the message to deliver to all users:",
                        mk([], "a:home"))
    if val == "credits":
        return await cb(q, ctx, *credits_view())
    if val == "sync":
        if not MAIL_ON:
            return await cb(q, ctx, *credits_view("❌ Mail auto-verify is OFF (set GMAIL_USER / GMAIL_APP_PASS in .env)."))
        before = get("SELECT COUNT(*) n FROM bank_credits", one=True)["n"]
        try:
            await asyncio.get_running_loop().run_in_executor(None, gateway.fetch_credits)
        except Exception as ex:
            return await cb(q, ctx, *credits_view(f"❌ Mail error: <code>{e(ex)}</code>"))
        after = get("SELECT COUNT(*) n FROM bank_credits", one=True)["n"]
        return await cb(q, ctx, *credits_view(f"🔄 Sync done. New credits found: <b>{after - before}</b>"))
    if val == "settings":
        ctx.user_data.clear()
        return await cb(q, ctx, *settings_view())
    if val == "botstatus":
        set_cfg("bot_on", "0" if bot_is_on() else "1")
        return await cb(q, ctx, *terminal_view())


# ---- Create Tournament: one box, fill the details (prizes are optional), then create ----
TC_REQ = ["title", "mode", "fee", "slots", "time"]
TC_OPT = ["p1", "p2", "p3", "per_kill", "top_kill"]
TC_STEPS = TC_REQ + TC_OPT
TC_LABEL = {"title": "Title", "mode": "Mode", "fee": "Entry Fee", "slots": "Slots", "time": "Match Time",
            "p1": "1st Prize", "p2": "2nd Prize", "p3": "3rd Prize",
            "per_kill": "Per Kill", "top_kill": "Highest Kill"}
TC_ICON = {"title": "📝", "mode": "🎮", "fee": "💵", "slots": "👥", "time": "🕒",
           "p1": "🥇", "p2": "🥈", "p3": "🥉", "per_kill": "🎯", "top_kill": "💀"}
TC_OPTS = {"mode": ["Solo", "Duo", "Squad", "Clash Squad"],
           "fee": [0, 10, 20, 30, 50, 100], "slots": [12, 24, 48, 50, 100],
           "time": ["Today 7:00 PM", "Today 9:00 PM", "Tomorrow 7:00 PM", "Tomorrow 9:00 PM"],
           "p1": [50, 100, 200, 500, 1000], "p2": [30, 50, 100, 250], "p3": [20, 30, 50, 100],
           "per_kill": [1, 2, 5, 10], "top_kill": [10, 20, 50, 100]}
TC_HINT = {"title": "Send the tournament title.\n<i>e.g. BattleX Solo Cup</i>",
           "mode": "Pick a mode or type your own.", "fee": "Pick the entry fee or type a number (0 = Free).",
           "slots": "Pick the slots or type a number.",
           "time": "Pick a time or type it.\n<i>e.g. Today 8:00 PM</i>",
           "p1": "Prize for Rank 1. Pick or type an amount.", "p2": "Prize for Rank 2. Pick or type an amount.",
           "p3": "Prize for Rank 3. Pick or type an amount.",
           "per_kill": "Reward for every kill (₹ per kill).", "top_kill": "Bonus for the highest-kill player."}


def tc_set(f, field, text):
    text = str(text).strip()
    if field == "title":
        if not 3 <= len(text) <= 40:
            return "Title must be 3-40 characters."
    elif field == "mode":
        if not 2 <= len(text) <= 20:
            return "Mode must be 2-20 characters."
    elif field == "time":
        if not 2 <= len(text) <= 40:
            return "Time must be 2-40 characters."
    else:  # numeric fields
        lo = {"fee": 0, "slots": 2}.get(field, 1)
        if not text.isdigit() or not lo <= int(text) <= 100000:
            return "Send a valid number."
        f[field] = int(text)
        return None
    f[field] = text
    return None


def tc_form(ctx):
    f = ctx.user_data.setdefault("form", {})
    ctx.user_data.pop("state", None)

    def val(s):
        v = f[s]
        if s == "fee":
            return "Free" if v == 0 else f"₹{v}"
        return f"₹{v}" if s in TC_OPT else v

    lines = ["<b>Required</b>"]
    for s in TC_REQ:
        lines.append(f"{TC_ICON[s]} {TC_LABEL[s]}: " + (f"<b>{e(val(s))}</b>" if s in f else "<i>— tap to set —</i>"))
    lines.append("\n<b>Prizes (optional)</b>")
    for s in TC_OPT:
        lines.append(f"{TC_ICON[s]} {TC_LABEL[s]}: " + (f"<b>{e(val(s))}</b>" if s in f else "<i>— off —</i>"))
    n = sum(1 for s in TC_REQ if s in f)
    foot = ("✅ <b>Ready to create!</b>\n<i>Only the prizes you set will be shown.</i>" if n == 5
            else f"Fill required details ({n}/5) 👇")
    rows = []
    for grp in (TC_REQ, TC_OPT):
        for i in range(0, len(grp), 2):
            rows.append([btn(("✅ " if s in f else TC_ICON[s] + " ") + TC_LABEL[s], f"tc:f:{s}",
                             "success" if s in f else "primary") for s in grp[i:i + 2]])
    rows.append([btn("🚀 Create Tournament", "tc:publish", "success")] if n == 5
                else [btn(f"🔒 Create ({n}/5 filled)", "tc:need", "danger")])
    return f"🏆 <b>Create Tournament</b>\n{LINE}\n" + "\n".join(lines) + f"\n{LINE}\n{foot}", mk(rows, "a:home")


def tc_field_view(ctx, field, err=""):
    ctx.user_data["state"] = f"tc_{field}"
    f = ctx.user_data.setdefault("form", {})
    rows = []
    if field in TC_OPTS:
        opts = TC_OPTS[field]
        per = 2 if field in ("mode", "time") else 3
        label = lambda a: "Free" if (field == "fee" and a == 0) else (
            f"₹{a}" if field in ("fee",) + tuple(TC_OPT) else str(a))
        rows = [[btn(label(a), f"tc:v:{field}:{a}", "success") for a in opts[i:i + per]]
                for i in range(0, len(opts), per)]
    if field in TC_OPT and field in f:
        rows.append([btn("🗑 Remove this prize", f"tc:x:{field}", "danger")])
    cur = f"\nCurrent: <b>{e(f[field])}</b>" if field in f else ""
    txt = f"{TC_ICON[field]} <b>{TC_LABEL[field]}</b>\n{LINE}\n{TC_HINT[field]}{cur}"
    if err:
        txt += f"\n\n❌ {e(err)}"
    return txt, mk(rows, "tc:form")


async def h_tc(q, ctx, val):
    op, _, rest = val.partition(":")
    if op == "announce":
        await q.answer()
        tid = int(rest)
        t = get("SELECT * FROM tournaments WHERE id=?", (tid,), one=True)
        if not t:
            return
        ctx.application.create_task(broadcast(
            ctx, f"🔥 <b>NEW TOURNAMENT!</b>\n{LINE}\n" + card(t),
            M([[btn(f"⚔️ Join Now • ₹{t['fee']}", f"join:{tid}", "success")], back_row()]), q.from_user.id, raw=True))
        return await cb(q, ctx, q.message.text_html + "\n\n📢 <b>Announcing to all users…</b>", mk([], "a:home"))
    f = ctx.user_data.get("form")
    if f is None:
        await q.answer()
        return await cb(q, ctx, "⚠️ Session expired. Open Create Tournament again.", mk([], "a:home"))
    if op == "need":
        return await q.answer("Fill all 5 required details first", show_alert=True)
    if op == "form":
        await q.answer()
        return await cb(q, ctx, *tc_form(ctx))
    if op == "f" and rest in TC_STEPS:
        await q.answer()
        return await cb(q, ctx, *tc_field_view(ctx, rest))
    if op == "x" and rest in TC_OPT:
        f.pop(rest, None)
        await q.answer("Removed")
        return await cb(q, ctx, *tc_form(ctx))
    if op == "v":
        field, _, v = rest.partition(":")
        if field not in TC_STEPS:
            return await q.answer()
        err = tc_set(f, field, v)
        if err:
            return await q.answer(err, show_alert=True)
        await q.answer()
        return await cb(q, ctx, *tc_form(ctx))
    if op == "publish":
        if not all(k in f for k in TC_REQ):
            return await q.answer("Fill all 5 required details first", show_alert=True)
        await q.answer("🚀 Created!")
        pool = sum(f.get(k, 0) for k in ("p1", "p2", "p3"))
        _, tid = run("INSERT INTO tournaments(title,mode,fee,prize,slots,time,p1,p2,p3,per_kill,top_kill) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (f["title"], f["mode"], f["fee"], pool, f["slots"], f["time"],
                      f.get("p1"), f.get("p2"), f.get("p3"), f.get("per_kill"), f.get("top_kill")))
        ctx.user_data.clear()
        t = get("SELECT * FROM tournaments WHERE id=?", (tid,), one=True)
        return await cb(q, ctx, "🚀 <b>Tournament Created!</b>\n\n" + card(t, admin=True),
                        mk([[btn("📢 Announce to All Users", f"tc:announce:{tid}", "success")],
                            [btn("🗂 Manage", f"tm:view:{tid}", "primary")]], "a:home"))
    await q.answer()


# ---- Manage tournaments / Declare winner ----
def manage_list_view(win):
    sts = ("open", "closed", "completed") if win else ("open", "closed")
    ts = get(f"SELECT * FROM tournaments WHERE status IN ({','.join('?' * len(sts))}) ORDER BY id DESC LIMIT 15", sts)
    title = "🏁 <b>Declare Winner</b>" if win else "🗂 <b>Manage Tournaments</b>"
    if not ts:
        return f"{title}\n{LINE}\n😴 No tournaments found.", mk([], "a:home")
    rows = [[btn(f"#{t['id']} {t['title'][:18]} • {filled_slots(t['id'])}/{t['slots']} • {t['status']}",
                 f"tm:{'win' if win else 'view'}:{t['id']}", "primary")] for t in ts]
    return f"{title}\n{LINE}\nSelect a tournament 👇", mk(rows, "a:home")


def tm_kb(t):
    tid = t["id"]
    rows = []
    if t["status"] in ("open", "closed"):
        rows.append([btn("🔑 Room", f"tm:room:{tid}", "primary"), btn("👥 Players", f"tm:players:{tid}", "primary")])
        rows.append([btn("🏁 Declare Winner", f"tm:win:{tid}", "success"),
                     btn("🔒 Close" if t["status"] == "open" else "🔓 Reopen",
                         f"tm:{'close' if t['status'] == 'open' else 'open'}:{tid}", "primary")])
        rows.append([btn("✅ Complete", f"tm:done:{tid}", "success"), btn("❌ Cancel & Refund", f"tm:cancel:{tid}", "danger")])
    else:
        rows.append([btn("👥 Players", f"tm:players:{tid}", "primary")])
        if t["status"] == "completed" and not t["paid"]:
            rows.append([btn("🏁 Declare Winner", f"tm:win:{tid}", "success")])
    return mk(rows, "a:manage")


def players_of(tid):
    return get("SELECT u.id, u.name, u.ff_uid FROM entries e JOIN users u ON u.id=e.uid WHERE e.tid=?", (tid,))


# ---- Winner selection by Free Fire UID, based on the prizes that were set ----
WIN_SLOTS = {"r1": ("🥇", "Rank 1", "p1"), "r2": ("🥈", "Rank 2", "p2"), "r3": ("🥉", "Rank 3", "p3")}


def pname(uid):
    r = get("SELECT name, ff_uid FROM users WHERE id=?", (uid,), one=True)
    return f"{e(r['name'])} • FF <code>{e(r['ff_uid'])}</code>" if r else f"<code>{uid}</code>"


def find_player(tid, ff):
    return get("SELECT u.id, u.name, u.ff_uid FROM entries en JOIN users u ON u.id=en.uid "
               "WHERE en.tid=? AND u.ff_uid=?", (tid, str(ff).strip()), one=True)


def win_totals(t, wf):
    pr, tot = tprizes(t), {}

    def add(uid, amt, note):
        if uid and amt > 0:
            a = tot.setdefault(uid, [0, []])
            a[0] += amt
            a[1].append(note)
    for k, (ic, lb, pk) in WIN_SLOTS.items():
        add(wf.get(k), pr[pk], f"{lb}: ₹{pr[pk]}")
    add(wf.get("hk"), pr["top_kill"], f"Highest kill: ₹{pr['top_kill']}")
    for uid, n in (wf.get("kills") or {}).items():
        add(uid, n * pr["per_kill"], f"{n} kills: ₹{n * pr['per_kill']}")
    return tot


def win_form_view(ctx, t, note=""):
    ctx.user_data.pop("state", None)
    wf = ctx.user_data.setdefault("wf", {})
    wf["tid"] = t["id"]
    pr, tid = tprizes(t), t["id"]
    lines, rows, ready = [], [], True
    for k, (ic, lb, pk) in WIN_SLOTS.items():
        if not pr[pk]:
            continue
        u = wf.get(k)
        lines.append(f"{ic} {lb} (₹{pr[pk]}): " + (pname(u) if u else "<i>— set —</i>"))
        ready = ready and bool(u)
        rows.append([btn(f"{ic} {lb}" + (" ✅" if u else ""), f"tm:ws:{tid}:{k}", "success" if u else "primary")])
    if pr["top_kill"]:
        u = wf.get("hk")
        lines.append(f"💀 Highest Kill (₹{pr['top_kill']}): " + (pname(u) if u else "<i>— set —</i>"))
        ready = ready and bool(u)
        rows.append([btn("💀 Highest Kill" + (" ✅" if u else ""), f"tm:ws:{tid}:hk", "success" if u else "primary")])
    if pr["per_kill"]:
        kills = wf.get("kills") or {}
        lines.append(f"🎯 Kills (₹{pr['per_kill']}/kill): " + (f"<b>{len(kills)}</b> players" if kills else "<i>— none —</i>"))
        rows.append([btn("🎯 Add Kills" + (" ✅" if kills else ""), f"tm:wk:{tid}", "success" if kills else "primary")])
    if not lines:
        return (f"🏁 <b>Declare Winner</b>\n{LINE}\nNo prizes were set for this tournament.", mk([], f"tm:view:{tid}"))
    tot = win_totals(t, wf)
    summ = "\n".join(f"• {pname(u)} → <b>₹{a[0]}</b>" for u, a in tot.items())
    txt = (f"🏁 <b>Declare Winner</b>\n{LINE}\n🏆 {e(t['title'])}\n"
           f"👥 Players: <b>{filled_slots(tid)}</b>\n{LINE}\n" + "\n".join(lines))
    if summ:
        txt += f"\n{LINE}\n💰 <b>Payout</b>\n{summ}"
    if note:
        txt += f"\n\n{note}"
    if ready and tot:
        rows.append([btn("✅ Confirm & Pay", f"tm:wpay:{tid}", "success")])
    return txt, mk(rows, f"tm:view:{tid}")


def win_slot_view(ctx, t, slot, err=""):
    ctx.user_data["state"] = f"wsel_{slot}"
    if slot == "kills":
        txt = (f"🎯 <b>Add Kills</b>\n{LINE}\nSend one player per line:\n<code>FF_UID kills</code>\n"
               f"e.g.\n<code>123456789 7\n987654321 3</code>")
    else:
        lb = WIN_SLOTS[slot][1] if slot in WIN_SLOTS else "Highest Kill"
        txt = f"🏁 <b>{lb}</b>\n{LINE}\nSend the player's <b>Free Fire UID</b>:"
    if err:
        txt += f"\n\n❌ {e(err)}"
    return txt, mk([], f"tm:wfm:{t['id']}")


async def pay_winners(ctx, t, tot):
    if not tot:
        return "❌ Nothing to pay."
    tid = t["id"]
    with tx() as c:
        if c.execute("SELECT paid FROM tournaments WHERE id=?", (tid,)).fetchone()["paid"]:
            return "❌ Winners already paid for this tournament."
        for uid, (amt, notes) in tot.items():
            c.execute("INSERT OR REPLACE INTO winners(tid,uid,amount) VALUES(?,?,?)", (tid, uid, amt))
            credit(c, uid, amt, "prize", f"Tournament #{tid}: " + ", ".join(notes))
        c.execute("UPDATE tournaments SET paid=1, status='completed' WHERE id=?", (tid,))
    for uid, (amt, notes) in tot.items():
        await safe_send(ctx, uid, f"🏆 <b>CHAMPION!</b> 🏆\n{LINE}\n💰 ₹{amt} prize added to your wallet.\n"
                                  + "\n".join(f"• {e(n)}" for n in notes) + "\n🔥 Keep winning!", effect=FX_PARTY)
        await asyncio.sleep(0.05)
    return f"✅ Prizes paid to <b>{len(tot)}</b> players • Total ₹{sum(a[0] for a in tot.values())}."


async def do_cancel(ctx, tid):
    with tx() as c:
        t = c.execute("SELECT * FROM tournaments WHERE id=? AND status NOT IN ('cancelled')", (tid,)).fetchone()
        if not t:
            return None
        c.execute("UPDATE tournaments SET status='cancelled' WHERE id=?", (tid,))
        uids = [r["uid"] for r in c.execute("SELECT uid FROM entries WHERE tid=?", (tid,))]
        for u in uids:
            credit(c, u, t["fee"], "refund", f"Tournament #{tid} cancelled")
    for u in uids:
        await safe_send(ctx, u, f"⚠️ <b>{e(t['title'])}</b> was cancelled.\n↩️ ₹{t['fee']} refunded to your wallet.")
        await asyncio.sleep(0.05)
    return len(uids)


async def send_room(ctx, tid, rid, pw):
    t = get("SELECT * FROM tournaments WHERE id=?", (tid,), one=True)
    run("UPDATE tournaments SET room_id=?, room_pw=? WHERE id=?", (rid, pw, tid))
    sent = 0
    for r in get("SELECT uid FROM entries WHERE tid=?", (tid,)):
        if await safe_send(ctx, r["uid"],
                           f"🎮 <b>{e(t['title'])}</b> — Room Details\n{LINE}\n🔑 Room ID: <code>{e(rid)}</code>\n"
                           f"🔒 Password: <code>{e(pw)}</code>\n🕒 Time: {e(t['time'])}\n\n🔥 <b>All the best!</b>",
                           effect=FX_FIRE):
            sent += 1
        await asyncio.sleep(0.05)
    return sent


async def h_tm(q, ctx, val):
    parts = val.split(":")
    op, tid = parts[0], int(parts[1])
    t = get("SELECT * FROM tournaments WHERE id=?", (tid,), one=True)
    if not t:
        return await q.answer("Tournament not found", show_alert=True)
    await q.answer()
    view_back = f"tm:view:{tid}"

    if op == "view":
        ctx.user_data.clear()
        return await cb(q, ctx, card(t, admin=True), tm_kb(t))
    if op == "room":
        ctx.user_data.clear()
        ctx.user_data.update(state="room_id", form={"tid": tid})
        return await cb(q, ctx, f"🔑 <b>Set Room — {e(t['title'])}</b>\n{LINE}\nSend the <b>Room ID</b>:", mk([], view_back))
    if op == "players":
        rows = players_of(tid)
        txt = "\n".join(f"{i}. {e(r['name'])} | TG <code>{r['id']}</code> | FF <code>{e(r['ff_uid'])}</code>"
                        for i, r in enumerate(rows[:40], 1)) or "No players yet."
        if len(rows) > 40:
            txt += f"\n… and {len(rows) - 40} more"
        return await cb(q, ctx, f"👥 <b>Players — {e(t['title'])}</b> ({len(rows)})\n{LINE}\n{txt}", mk([], view_back))
    if op == "win":
        ctx.user_data.clear()
        if t["paid"]:
            return await cb(q, ctx, "✅ Winners are already declared and paid.", mk([], view_back))
        return await cb(q, ctx, *win_form_view(ctx, t))
    if op == "wfm":
        return await cb(q, ctx, *win_form_view(ctx, t))
    if op == "ws" and len(parts) > 2 and parts[2] in ("r1", "r2", "r3", "hk"):
        return await cb(q, ctx, *win_slot_view(ctx, t, parts[2]))
    if op == "wk":
        return await cb(q, ctx, *win_slot_view(ctx, t, "kills"))
    if op == "wpay":
        wf = ctx.user_data.get("wf") or {}
        if wf.get("tid") != tid:
            return await cb(q, ctx, "⚠️ Session expired.", mk([], view_back))
        res = await pay_winners(ctx, t, win_totals(t, wf))
        ctx.user_data.clear()
        return await cb(q, ctx, res, mk([], view_back))
    if op == "close":
        run("UPDATE tournaments SET status='closed' WHERE id=? AND status='open'", (tid,))
    elif op == "open":
        run("UPDATE tournaments SET status='open' WHERE id=? AND status='closed'", (tid,))
    elif op == "done":
        run("UPDATE tournaments SET status='completed' WHERE id=? AND status IN ('open','closed')", (tid,))
    elif op == "cancel":
        return await cb(q, ctx, f"⚠️ <b>Cancel “{e(t['title'])}” and refund everyone?</b>",
                        mk([[btn("✅ Yes, cancel", f"tm:cancelyes:{tid}", "danger")]], view_back))
    elif op == "cancelyes":
        n = await do_cancel(ctx, tid)
        return await cb(q, ctx, f"❌ Cancelled. {n} players refunded." if n is not None else "Already cancelled.",
                        mk([], "a:manage"))
    t2 = get("SELECT * FROM tournaments WHERE id=?", (tid,), one=True)
    await cb(q, ctx, card(t2, admin=True), tm_kb(t2))


# ---- Deposits / withdrawals ----
def deps_view():
    rows = get("SELECT * FROM payments WHERE status='pending' ORDER BY id")
    if not rows:
        return "✅ <b>No pending deposits.</b>", mk([], "a:home")
    kb_ = [[btn(f"💳 #{p['id']} • ₹{p['amount']} • {p['uid']}", f"pv:{p['id']}", "success")] for p in rows[:30]]
    return f"💳 <b>Pending Deposits</b> ({len(rows)})\n{LINE}\nTap one to review 👇", mk(kb_, "a:home")


def wds_view():
    rows = get("SELECT * FROM withdrawals WHERE status='pending' ORDER BY id")
    if not rows:
        return "✅ <b>No pending withdrawals.</b>", mk([], "a:home")
    kb_ = [[btn(f"💸 #{w['id']} • ₹{w['amount']} • {w['uid']}", f"wv:{w['id']}", "danger")] for w in rows[:30]]
    return f"💸 <b>Pending Withdrawals</b> ({len(rows)})\n{LINE}\nTap one to review 👇", mk(kb_, "a:home")


async def h_pv(q, ctx, val):
    await q.answer()
    p = get("SELECT * FROM payments WHERE id=?", (val,), one=True)
    if not p:
        return await cb(q, ctx, "Not found.", mk([], "a:deps"))
    txt = (f"💳 <b>Deposit #{p['id']}</b>\n{LINE}\n👤 <code>{p['uid']}</code>\n💵 ₹{p['amount']}\n"
           f"🔢 UTR: <code>{p['utr']}</code>\n📌 Status: <b>{e(p['status'])}</b>")
    rows = [[btn("✅ Approve", f"pay_ok:{p['id']}:d", "success"), btn("❌ Reject", f"pay_no:{p['id']}:d", "danger")]] \
        if p["status"] == "pending" else []
    await cb(q, ctx, txt, mk(rows, "a:deps"))


async def h_wv(q, ctx, val):
    await q.answer()
    w = get("SELECT * FROM withdrawals WHERE id=?", (val,), one=True)
    if not w:
        return await cb(q, ctx, "Not found.", mk([], "a:wds"))
    txt = (f"💸 <b>Withdraw #{w['id']}</b>\n{LINE}\n👤 <code>{w['uid']}</code>\n💵 ₹{w['amount']}\n"
           f"📲 <code>{e(w['upi'])}</code>\n📌 Status: <b>{e(w['status'])}</b>")
    rows = [[btn("✅ Paid", f"wd_ok:{w['id']}:d", "success"), btn("❌ Reject", f"wd_no:{w['id']}:d", "danger")]] \
        if w["status"] == "pending" else []
    await cb(q, ctx, txt, mk(rows, "a:wds"))


async def _pay(q, ctx, val, new):
    parts = val.split(":")
    pid, from_list = parts[0], len(parts) > 1
    ref = p = None
    with tx() as c:
        if new == "approved":
            n = c.execute("UPDATE payments SET status='approved', reason=NULL WHERE id=? "
                          "AND status IN ('pending','rejected')", (pid,)).rowcount
        else:
            n = c.execute("UPDATE payments SET status='rejected', reason='admin' WHERE id=? "
                          "AND status='pending'", (pid,)).rowcount
        if n:
            p = c.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
            if new == "approved":
                c.execute("UPDATE bank_credits SET claimed_by=? WHERE utr=? AND claimed_by IS NULL", (p["uid"], p["utr"]))
                credit(c, p["uid"], p["amount"], "deposit", f"Manual UTR {p['utr']}")
                ref = ref_reward(c, p["uid"])
    if not n:
        return await q.answer("Already processed", show_alert=True)
    await q.answer()
    if new == "approved":
        await safe_send(ctx, p["uid"], f"✅ <b>₹{p['amount']} added to your wallet!</b>", effect=FX_PARTY)
        await notify_ref(ctx, ref)
    else:
        await safe_send(ctx, p["uid"], "❌ Your add-money request was rejected.")
    await cb(q, ctx, q.message.text_html + f"\n\n➡️ <b>{new.upper()}</b>", mk([], "a:deps" if from_list else CLOSE))


async def h_pay_ok(q, ctx, val):
    await _pay(q, ctx, val, "approved")


async def h_pay_no(q, ctx, val):
    await _pay(q, ctx, val, "rejected")


async def _wd(q, ctx, val, new):
    parts = val.split(":")
    wid, from_list = parts[0], len(parts) > 1
    w = None
    with tx() as c:
        n = c.execute("UPDATE withdrawals SET status=? WHERE id=? AND status='pending'", (new, wid)).rowcount
        if n:
            w = c.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
            if new == "rejected":
                credit(c, w["uid"], w["amount"], "refund", f"Withdraw #{wid} rejected")
    if not n:
        return await q.answer("Already processed", show_alert=True)
    await q.answer()
    if new == "paid":
        await safe_send(ctx, w["uid"], f"✅ <b>₹{w['amount']} sent to your UPI!</b> 🎉", effect=FX_PARTY)
    else:
        await safe_send(ctx, w["uid"], f"❌ Withdrawal rejected. ₹{w['amount']} refunded to your wallet.")
    await cb(q, ctx, q.message.text_html + f"\n\n➡️ <b>{new.upper()}</b>", mk([], "a:wds" if from_list else CLOSE))


async def h_wd_ok(q, ctx, val):
    await _wd(q, ctx, val, "paid")


async def h_wd_no(q, ctx, val):
    await _wd(q, ctx, val, "rejected")


def credits_view(note=""):
    rows = get("SELECT * FROM bank_credits ORDER BY created DESC LIMIT 8")
    txt = "\n".join(f"• <code>{r['utr']}</code> | ₹{r['amount']:g} | "
                    f"{'✅ used by ' + str(r['claimed_by']) if r['claimed_by'] else '🟡 unclaimed'}"
                    for r in rows) or "No bank emails detected yet."
    if note:
        txt += f"\n{LINE}\n{note}"
    return (f"🏦 <b>Bank Credits (from email)</b>\n{LINE}\n{txt}",
            mk([[btn("🔄 Sync Mail Now", "a:sync", "success")]], "a:home"))


# ---- Users ----
def users_menu_view():
    n = get("SELECT COUNT(*) n FROM users", one=True)["n"]
    return (f"👥 <b>User Control Panel</b>\n{LINE}\nTotal users: <b>{n}</b>",
            mk([[btn("🔎 Find by ID", "u:find", "primary")],
                [btn("📋 Recent Users", "u:list", "success"), btn("🚫 Banned", "u:banned", "danger")]], "a:home"))


def user_card(uid, back="a:users"):
    u = get("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if not u:
        return "❌ User not found.", mk([], back)
    dep = get("SELECT COALESCE(SUM(amount),0) s FROM txns WHERE uid=? AND kind='deposit'", (uid,), one=True)["s"]
    wd = get("SELECT COALESCE(-SUM(amount),0) s FROM txns WHERE uid=? AND kind='withdraw'", (uid,), one=True)["s"]
    played = get("SELECT COUNT(*) n FROM entries WHERE uid=?", (uid,), one=True)["n"]
    banned = bool(u["banned"])
    txt = (f"👤 <b>{e(u['name'])}</b> <code>{uid}</code>\n{LINE}\n🔥 FF UID: <code>{e(u['ff_uid'] or '—')}</code>\n"
           f"💰 Balance: <b>₹{u['balance']}</b>\n📥 Deposits: ₹{dep}\n📤 Withdraws: ₹{wd}\n🎮 Matches: {played}\n"
           f"🚫 Banned: <b>{'Yes' if banned else 'No'}</b>")
    return txt, mk([[btn("✅ Unban" if banned else "🚫 Ban", f"um:{'unban' if banned else 'ban'}:{uid}",
                         "success" if banned else "danger"),
                     btn("💰 Adjust Balance", f"um:bal:{uid}", "primary")]], back)


async def h_u(q, ctx, val):
    await q.answer()
    if val == "find":
        ctx.user_data.clear()
        ctx.user_data["state"] = "um_find"
        return await cb(q, ctx, f"🔎 <b>Find User</b>\n{LINE}\nSend the user's <b>Telegram ID</b>:", mk([], "a:users"))
    banned = val == "banned"
    rows = get("SELECT * FROM users WHERE COALESCE(banned,0)=? ORDER BY rowid DESC LIMIT 20", (1 if banned else 0,))
    if not rows:
        return await cb(q, ctx, "😴 No users here.", mk([], "a:users"))
    kb_ = [[btn(f"{(r['name'] or '—')[:16]} • {r['id']} • ₹{r['balance']}", f"um:view:{r['id']}", "primary")] for r in rows]
    await cb(q, ctx, f"{'🚫 <b>Banned Users</b>' if banned else '📋 <b>Recent Users</b>'}\n{LINE}", mk(kb_, "a:users"))


def ab_prompt(uid):
    return (f"💰 User <code>{uid}</code> — balance <b>₹{balance(uid)}</b>\n{LINE}\n"
            f"Send an amount (negative to deduct) or tap a quick option:")


def ab_kb(back):
    return mk([[btn("+₹10", "ab:10", "success"), btn("+₹50", "ab:50", "success"), btn("+₹100", "ab:100", "success")],
               [btn("+₹500", "ab:500", "success"), btn("−₹50", "ab:-50", "danger")]], back)


async def h_um(q, ctx, val):
    op, _, tid = val.partition(":")
    target = int(tid)
    await q.answer()
    if op in ("ban", "unban"):
        run("UPDATE users SET banned=? WHERE id=?", (1 if op == "ban" else 0, target))
        return await cb(q, ctx, *user_card(target))
    if op == "view":
        return await cb(q, ctx, *user_card(target))
    if op == "bal":
        back = f"um:view:{target}"
        ctx.user_data.clear()
        ctx.user_data.update(state="ab_amt", form={"uid": target, "back": back})
        return await cb(q, ctx, ab_prompt(target), ab_kb(back))


async def apply_balance(ctx, uid, amt):
    if not get("SELECT 1 FROM users WHERE id=?", (uid,), one=True):
        return "❌ User not found."
    with tx() as c:
        if amt > 0:
            credit(c, uid, amt, "admin", "Admin adjustment")
            ok = True
        else:
            ok = debit(c, uid, -amt, "admin", "Admin adjustment")
    if not ok:
        return "❌ User balance is too low."
    if amt > 0:
        await safe_send(ctx, uid, f"💰 <b>₹{amt}</b> added to your wallet by admin.", effect=FX_LIKE)
    return f"✅ Done. New balance of <code>{uid}</code>: <b>₹{balance(uid)}</b>"


async def h_ab(q, ctx, val):
    await q.answer()
    f = ctx.user_data.get("form") or {}
    if not f.get("uid"):
        return await cb(q, ctx, "⚠️ Session expired.", mk([], "a:home"))
    puid, back = f["uid"], f.get("back", "a:home")
    ctx.user_data.clear()
    await cb(q, ctx, await apply_balance(ctx, puid, int(val)), mk([], back))


async def broadcast(ctx, text, markup, admin_id, raw=False):
    body = text if raw else f"📢 <b>Announcement</b>\n{LINE}\n{text}"
    n = 0
    for u in get("SELECT id FROM users WHERE COALESCE(banned,0)=0"):
        if await safe_send(ctx, u["id"], body, markup):
            n += 1
        await asyncio.sleep(0.05)
    await safe_send(ctx, admin_id, f"📢 Delivered to <b>{n}</b> users.")


async def h_bc(q, ctx, val):
    await q.answer()
    f = ctx.user_data.get("form") or {}
    if not f.get("text"):
        return await cb(q, ctx, "⚠️ Session expired.", mk([], "a:home"))
    text = f["text"]
    ctx.user_data.clear()
    ctx.application.create_task(broadcast(ctx, text, None, q.from_user.id))
    await cb(q, ctx, "📢 <b>Broadcasting…</b> You'll get a report when it's done.", mk([], "a:home"))


# ---- Settings ----
SET_META = {"upi": ("📲 UPI ID", "Send the new UPI ID (like name@bank)"),
            "min_add": ("➕ Min Add Money", "Send the minimum deposit (number)"),
            "min_wd": ("💸 Min Withdraw", "Send the minimum withdrawal (number)"),
            "ref_bonus": ("🎁 Referral Bonus", "Send the referral bonus in ₹ (0 = off)"),
            "support": ("🆘 Support Handle", "Send the support @username")}


def settings_view(note=""):
    lines = [f"{SET_META[k][0]}: <b>{e(cfg(k))}</b>" for k in SET_META]
    on = bot_is_on()
    keys = list(SET_META)
    rows = [[btn(SET_META[k][0].split(" ", 1)[1], f"s:e:{k}", "primary") for k in keys[i:i + 2]]
            for i in range(0, len(keys), 2)]
    rows.append([btn(f"Bot Status: {'ON 🟢' if on else 'OFF 🔴'}", "s:bot", "success" if on else "danger")])
    txt = f"⚙️ <b>Settings</b>\n{LINE}\n" + "\n".join(lines) + f"\n{LINE}\nTap an item to change it 👇"
    if note:
        txt += f"\n\n{note}"
    return txt, mk(rows, "a:home")


def set_validate(key, text):
    text = text.strip()
    if key == "upi":
        if not re.fullmatch(r"[\w.\-]{2,}@[A-Za-z]{2,}", text):
            return None, "Send a valid UPI ID (like name@bank)."
    elif key in ("min_add", "min_wd"):
        if not text.isdigit() or not 1 <= int(text) <= 100000:
            return None, "Send a number from 1 to 100000."
    elif key == "ref_bonus":
        if not text.isdigit() or int(text) > 100000:
            return None, "Send a number (0 turns it off)."
    elif key == "support":
        if not 2 <= len(text) <= 40:
            return None, "Send 2-40 characters."
    return text, None


async def h_s(q, ctx, val):
    await q.answer()
    op, _, key = val.partition(":")
    if op == "bot":
        set_cfg("bot_on", "0" if bot_is_on() else "1")
        return await cb(q, ctx, *settings_view())
    if op == "e" and key in SET_META:
        ctx.user_data.clear()
        ctx.user_data["state"] = f"set_{key}"
        return await cb(q, ctx, f"{SET_META[key][0]}\n{LINE}\nCurrent: <b>{e(cfg(key))}</b>\n{SET_META[key][1]}",
                        mk([], "a:settings"))


# ---- admin typed input ----
ADMIN_STATE_PREFIXES = ("tc_", "room_", "wsel_", "um_", "ab_", "bc_", "set_")


async def admin_state(update, ctx, state, text):
    chat = update.effective_chat.id
    f = ctx.user_data.setdefault("form", {})

    if state.startswith("tc_"):
        field = state[3:]
        if field not in TC_STEPS:
            return
        err = tc_set(f, field, text)
        if err:
            return await show(ctx, chat, *tc_field_view(ctx, field, err))
        return await show(ctx, chat, *tc_form(ctx))
    if state == "room_id":
        f["rid"] = text
        ctx.user_data["state"] = "room_pw"
        return await show(ctx, chat, f"🔒 Now send the <b>Room Password</b>:", mk([], f"tm:room:{f.get('tid')}"))
    if state == "room_pw":
        tid, rid = f.get("tid"), f.get("rid")
        ctx.user_data.clear()
        if not tid:
            return await show(ctx, chat, "⚠️ Session expired.", mk([], "a:home"))
        sent = await send_room(ctx, tid, rid, text)
        return await show(ctx, chat, f"📨 Room details sent to <b>{sent}</b> players. ✅", mk([], f"tm:view:{tid}"))
    if state.startswith("wsel_"):
        slot = state[5:]
        wf = ctx.user_data.get("wf") or {}
        t = get("SELECT * FROM tournaments WHERE id=?", (wf.get("tid"),), one=True)
        if not t:
            return await show(ctx, chat, "⚠️ Session expired.", mk([], "a:home"))
        if slot == "kills":
            kills, bad = wf.setdefault("kills", {}), []
            for line in text.splitlines():
                if not line.strip():
                    continue
                pp = [x for x in re.split(r"[\s,:=\-]+", line.strip()) if x]
                p = find_player(t["id"], pp[0]) if len(pp) == 2 and pp[1].isdigit() else None
                if not p:
                    bad.append(line.strip())
                elif int(pp[1]) == 0:
                    kills.pop(p["id"], None)
                else:
                    kills[p["id"]] = int(pp[1])
            note = ("❌ Not found / invalid:\n" + "\n".join(f"<code>{e(b)}</code>" for b in bad)) if bad else ""
            return await show(ctx, chat, *win_form_view(ctx, t, note))
        p = find_player(t["id"], text)
        if not p:
            return await show(ctx, chat, *win_slot_view(ctx, t, slot, "No player with this Free Fire UID in this tournament."))
        if slot in WIN_SLOTS and any(wf.get(k) == p["id"] for k in WIN_SLOTS if k != slot):
            return await show(ctx, chat, *win_slot_view(ctx, t, slot, "This player already has another rank."))
        wf[slot] = p["id"]
        return await show(ctx, chat, *win_form_view(ctx, t))
    if state == "um_find":
        if not text.isdigit():
            return await show(ctx, chat, f"🔎 <b>Find User</b>\n{LINE}\n❌ Send a numeric Telegram ID.", mk([], "a:users"))
        ctx.user_data.clear()
        return await show(ctx, chat, *user_card(int(text)))
    if state == "ab_uid":
        if not text.isdigit() or not get("SELECT 1 FROM users WHERE id=?", (int(text),), one=True):
            return await show(ctx, chat, f"💰 <b>Add / Deduct Balance</b>\n{LINE}\n❌ User not found. Send a valid Telegram ID.",
                              mk([], "a:home"))
        ctx.user_data.update(state="ab_amt", form={"uid": int(text), "back": "a:addbal"})
        return await show(ctx, chat, ab_prompt(int(text)), ab_kb("a:addbal"))
    if state == "ab_amt":
        puid, back = f.get("uid"), f.get("back", "a:home")
        try:
            amt = int(text)
        except ValueError:
            amt = 0
        if amt == 0 or not puid:
            return await show(ctx, chat, ab_prompt(puid or 0) + "\n\n❌ Send a number (not 0; negative to deduct).",
                              ab_kb(back))
        ctx.user_data.clear()
        return await show(ctx, chat, await apply_balance(ctx, puid, amt), mk([], back))
    if state == "bc_text":
        f["text"] = e(text)
        ctx.user_data["state"] = "bc_preview"
        return await show(ctx, chat, f"👀 <b>Preview</b>\n{LINE}\n📢 <b>Announcement</b>\n{f['text']}\n{LINE}\nSend to all users?",
                          mk([[btn("🚀 Send", "bc:send", "success")]], "a:bc"))
    if state.startswith("set_"):
        key = state[4:]
        if key not in SET_META:
            return
        value, err = set_validate(key, text)
        if err:
            return await show(ctx, chat, f"{SET_META[key][0]}\n{LINE}\n{SET_META[key][1]}\n\n❌ {err}", mk([], "a:settings"))
        set_cfg(key, value)
        ctx.user_data.clear()
        return await show(ctx, chat, *settings_view(f"✅ <b>{SET_META[key][0]}</b> updated."))


# =====================  TEXT + BUTTON ROUTERS  =====================
def verify_frames():
    return ["▰▱▱▱▱", "▰▰▱▱▱", "▰▰▰▱▱", "▰▰▰▰▱", "▰▰▰▰▰"]


async def animate(msg, task):
    for fr in verify_frames():
        if task.done():
            break
        try:
            await msg.edit_text(f"🔍 <b>Verifying payment…</b>\n{fr}", parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await asyncio.sleep(1.2)


def admin_review_kb(prefix_ok, prefix_no, ident):
    return M([[btn("✅ " + ("Approve" if prefix_ok == "pay_ok" else "Paid"), f"{prefix_ok}:{ident}", "success"),
               btn("❌ Reject", f"{prefix_no}:{ident}", "danger")], back_row()])


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid = update.effective_user.id
    chat = update.effective_chat.id
    ensure_user(update.effective_user)

    if text in MENU_ACTIONS:
        ctx.user_data.clear()
        await del_msg(update)
        await update.effective_chat.send_action(ChatAction.TYPING)
        return await MENU_ACTIONS[text](update, ctx)

    state = ctx.user_data.get("state")
    if not state:
        return await say(update, "👇 Open the menu with the keyboard icon ⌨️", user_menu(uid))
    await del_msg(update)  # keep the chat clean: the box is updated instead

    if uid in ADMINS and state.startswith(ADMIN_STATE_PREFIXES):
        return await admin_state(update, ctx, state, text)

    if state == "ffuid":
        if not text.isdigit() or not (6 <= len(text) <= 15):
            return await show(ctx, chat, f"✏️ <b>Change FF UID</b>\n{LINE}\n❌ Send a valid numeric Free Fire UID:",
                              mk([], "pf:0"))
        run("UPDATE users SET ff_uid=? WHERE id=?", (text, uid))
        ctx.user_data.clear()
        return await show(ctx, chat, *profile_view(uid, f"\n\n✅ <b>UID saved.</b> 🔥"))

    if state == "add_amt":
        mn = cfgi("min_add")
        if not text.isdigit() or int(text) < mn or int(text) > 50000:
            t_, k_ = kp_view(ctx)
            return await show(ctx, chat, t_ + f"\n\n❌ Send an amount from ₹{mn} to ₹50000.", k_)
        return await send_qr(ctx, chat, int(text))

    if state == "add_utr":
        if not text.isdigit() or len(text) != 12:
            return await safe_send(ctx, chat, "❌ UTR must be exactly 12 digits. Send it again.")
        amt = ctx.user_data["amt"]
        ex = get("SELECT * FROM payments WHERE utr=?", (text,), one=True)
        if ex:
            if ex["uid"] == uid and ex["status"] == "rejected":
                run("UPDATE payments SET status='pending', reason=NULL, amount=?, created_ts=? WHERE id=?",
                    (amt, time.time(), ex["id"]))
                pid = ex["id"]
            elif ex["uid"] == uid and ex["status"] == "pending":
                return await safe_send(ctx, chat, "⏳ This UTR is already being verified.")
            else:
                return await safe_send(ctx, chat, "❌ This UTR has already been used.")
        else:
            _, pid = run("INSERT INTO payments(uid,amount,utr,created_ts) VALUES(?,?,?,?)", (uid, amt, text, time.time()))
        ctx.user_data.clear()
        await kill_box(ctx)  # removes the QR
        msg = await safe_send(ctx, chat, f"🔍 <b>Verifying payment…</b>\n{verify_frames()[0]}", None)
        if msg is None:
            return
        ctx.chat_data["box"], ctx.chat_data["box_chat"] = msg.message_id, msg.chat_id
        if MAIL_ON:
            task = asyncio.get_running_loop().run_in_executor(None, gateway.fetch_credits)
            await animate(msg, task)
            try:
                await task
            except Exception as ex2:
                log.warning("fetch error: %s", ex2)
        p = get("SELECT * FROM payments WHERE id=?", (pid,), one=True)
        if not MAIL_ON:
            await msg.edit_text("⏳ <b>Payment submitted.</b> Waiting for admin approval.",
                                parse_mode=ParseMode.HTML, reply_markup=CLOSE_KB)
            return await notify_admins(
                ctx, f"💳 <b>Deposit #{pid}</b>\n👤 <code>{uid}</code>\n💵 ₹{amt}\n🔢 UTR: <code>{text}</code>",
                admin_review_kb("pay_ok", "pay_no", pid))
        return await settle(ctx, p, msg)

    if state == "wd_amt":
        mn = cfgi("min_wd")
        back = ctx.user_data.get("back", CLOSE)
        if not text.isdigit() or int(text) < mn:
            t_, k_ = wd_amount_view(ctx, uid, back)
            return await show(ctx, chat, t_ + f"\n\n❌ Minimum ₹{mn}.", k_)
        if int(text) > balance(uid):
            t_, k_ = wd_amount_view(ctx, uid, back)
            return await show(ctx, chat, t_ + "\n\n❌ Insufficient balance.", k_)
        ctx.user_data.update(state="wd_upi", amt=int(text))
        return await show(ctx, chat, f"📲 <b>Withdraw ₹{int(text)}</b>\n{LINE}\nSend your <b>UPI ID</b> (where you want the money):",
                          mk([], "wdm:re"))

    if state == "wd_upi":
        if not re.fullmatch(r"[\w.\-]{2,}@[A-Za-z]{2,}", text):
            return await show(ctx, chat, f"📲 <b>Withdraw ₹{ctx.user_data.get('amt', 0)}</b>\n{LINE}\n"
                                         f"❌ Send a valid UPI ID (like name@bank).", mk([], "wdm:re"))
        amt = ctx.user_data["amt"]
        with tx() as c:
            if not debit(c, uid, amt, "withdraw", f"UPI {text}"):
                ctx.user_data.clear()
                return await show(ctx, chat, "❌ Insufficient balance.", mk([]))
            wid = c.execute("INSERT INTO withdrawals(uid,amount,upi) VALUES(?,?,?)", (uid, amt, text)).lastrowid
        ctx.user_data.clear()
        await show(ctx, chat, f"⏳ <b>Withdrawal Request Sent</b>\n{LINE}\n💵 ₹{amt} → <code>{e(text)}</code>\n"
                              f"Admin will process it soon.", mk([]))
        return await notify_admins(
            ctx, f"💸 <b>Withdraw #{wid}</b>\n👤 <code>{uid}</code>\n💵 ₹{amt}\n📲 UPI: <code>{e(text)}</code>",
            admin_review_kb("wd_ok", "wd_no", wid))

    await say(update, "👇 Open the menu with the keyboard icon ⌨️", user_menu(uid))


async def h_close(q, ctx, val):
    """The Back button of a root box: removes (hides) the box."""
    await q.answer()
    ctx.user_data.clear()
    try:
        await q.message.delete()
    except Exception:
        try:
            await q.edit_message_reply_markup(None)
        except Exception:
            pass
    if ctx.chat_data.get("box") == q.message.message_id:
        ctx.chat_data.pop("box", None)


USER_ROUTES = {"x": h_close, "no": h_close, "tl": h_tl, "tv": h_tv, "join": h_join, "yes": h_yes,
               "wal": h_wal, "pf": h_pf, "uid": h_uid, "addm": h_addm, "k": h_k, "addb": h_addb,
               "wdm": h_wdm, "wda": h_wda}
ADMIN_ROUTES = {"a": h_a, "tc": h_tc, "tm": h_tm, "um": h_um, "u": h_u, "ab": h_ab, "bc": h_bc, "s": h_s,
                "pv": h_pv, "wv": h_wv, "pay_ok": h_pay_ok, "pay_no": h_pay_no,
                "wd_ok": h_wd_ok, "wd_no": h_wd_no}


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    action, _, val = q.data.partition(":")
    ensure_user(q.from_user)
    if action in ADMIN_ROUTES:
        if q.from_user.id not in ADMINS:
            return await q.answer("Not allowed", show_alert=True)
        return await ADMIN_ROUTES[action](q, ctx, val)
    if action in USER_ROUTES:
        return await USER_ROUTES[action](q, ctx, val)
    await q.answer()


async def cmd_admin(update, ctx):
    await del_msg(update)
    await admin_panel(update, ctx)


async def on_error(update, ctx):
    log.error("Unhandled error", exc_info=ctx.error)


def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN missing. Copy .env.example to .env and fill it in.")
    init_db()
    if not MAIL_ON:
        log.warning("GMAIL_APP_PASS not set: auto-verify OFF, deposits will go to manual admin approval.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(TypeHandler(Update, gate), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(poll_job, interval=POLL_SEC, first=10)
    app.run_polling()


if __name__ == "__main__":
    main()
