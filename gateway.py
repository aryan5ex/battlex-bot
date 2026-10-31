"""
gateway.py — Bank e-mail payment verification (Gmail IMAP -> bank_credits table).

bot.py imports this file, so keep both in the same folder.
Secrets are NOT stored here. Put them in a `.env` file next to this script
(see .env.example) or in real environment variables.

Standalone test:  python gateway.py
"""
import os
import re
import time
import html
import email
import sqlite3
import logging
import imaplib
import threading
from datetime import datetime, timedelta
from email import policy
from email.utils import parseaddr

log = logging.getLogger("gateway")


# ---------- tiny .env loader (no extra dependency) ----------
def _load_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

# ---------- CONFIG ----------
GMAIL_USER = os.getenv("GMAIL_USER", "").strip()
GMAIL_APP_PASS = os.getenv("GMAIL_APP_PASS", "").replace(" ", "").strip()
BANK_DOMAINS = [d.strip().lower() for d in os.getenv("BANK_DOMAINS", "fampay.in,famapp.in,fam.app").split(",") if d.strip()]
REQUIRE_DKIM = os.getenv("REQUIRE_DKIM", "1") == "1"
POLL_SEC = int(os.getenv("POLL_SEC", "20"))
DB = os.getenv("DB_PATH", "bot.db")
MAIL_ON = bool(GMAIL_USER and GMAIL_APP_PASS)

# ---------- PARSING ----------
UTR_CTX_RE = re.compile(r"(?:utr|rrn|ref(?:erence)?(?:\s*(?:no|number|id))?)\D{0,20}(\d{12})(?!\d)", re.I)
UTR_RE = re.compile(r"(?<!\d)(\d{12})(?!\d)")
AMT_CTX_RE = re.compile(r"(?:credited|received)[^\d₹]{0,60}?(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d{1,2})?)", re.I)
AMT_RE = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d{1,2})?)", re.I)
DKIM_RE = re.compile(r"dkim=pass[^;]*?header\.(?:d|i)=@?([\w.\-]+)", re.I)
DMARC_RE = re.compile(r"dmarc=pass[^;]*?header\.from=@?([\w.\-]+)", re.I)
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_mail_lock = threading.Lock()


def _db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    c = _db()
    try:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("""CREATE TABLE IF NOT EXISTS bank_credits(
            utr TEXT PRIMARY KEY, amount REAL, claimed_by INTEGER,
            created TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("CREATE TABLE IF NOT EXISTS seen_mails(uid TEXT PRIMARY KEY)")
        c.commit()
    finally:
        c.close()


def domain_ok(dom):
    dom = dom.lower()
    return any(dom == d or dom.endswith("." + d) for d in BANK_DOMAINS)


def parse_credit(text):
    """Returns (utr, amount) for a 'money credited' mail, else None."""
    low = text.lower()
    if not ("credited" in low or "received" in low) or "debited" in low:
        return None
    u = UTR_CTX_RE.search(text) or UTR_RE.search(text)
    a = AMT_CTX_RE.search(text) or AMT_RE.search(text)
    if not (u and a):
        return None
    return u.group(1), float(a.group(1).replace(",", ""))


def mail_trusted(msg):
    """Only accept mails whose DKIM / DMARC passed for a bank domain (anti-spoofing)."""
    if not REQUIRE_DKIM:
        return True
    auth = " ".join(str(x) for x in (msg.get_all("Authentication-Results") or []))
    return (any(domain_ok(x) for x in DKIM_RE.findall(auth))
            or any(domain_ok(x) for x in DMARC_RE.findall(auth)))


def process_mail(raw):
    msg = email.message_from_bytes(raw, policy=policy.default)
    addr = parseaddr(str(msg.get("From", "")))[1].lower()
    if "@" not in addr or not domain_ok(addr.rsplit("@", 1)[1]):
        return
    if not mail_trusted(msg):
        log.warning("Mail skipped (auth failed): %s", msg.get("Subject"))
        return
    part = msg.get_body(preferencelist=("plain", "html"))
    body = part.get_content() if part else ""
    body = re.sub(r"(?is)<(style|script).*?</\1>", " ", body)
    body = html.unescape(re.sub(r"<[^>]+>", " ", body))
    body = re.sub(r"\s+", " ", body)
    res = parse_credit(f"{msg.get('Subject', '')} {body}")
    if not res:
        log.info("Mail not parsed | subject=%s", msg.get("Subject"))
        return
    c = _db()
    try:
        c.execute("INSERT OR IGNORE INTO bank_credits(utr,amount) VALUES(?,?)", res)
        c.commit()
    finally:
        c.close()
    log.info("Bank credit saved: UTR %s Rs %s", *res)


def _seen(key):
    c = _db()
    try:
        return c.execute("SELECT 1 FROM seen_mails WHERE uid=?", (key,)).fetchone() is not None
    finally:
        c.close()


def _mark_seen(key):
    c = _db()
    try:
        c.execute("INSERT OR IGNORE INTO seen_mails(uid) VALUES(?)", (key,))
        c.commit()
    finally:
        c.close()


def fetch_credits():
    """Blocking. Reads the last 3 days of bank mails and stores new credits."""
    if not MAIL_ON:
        return
    with _mail_lock:
        m = imaplib.IMAP4_SSL("imap.gmail.com")
        try:
            m.login(GMAIL_USER, GMAIL_APP_PASS)
            m.select("INBOX", readonly=True)
            d0 = datetime.now() - timedelta(days=3)
            since = f"{d0.day:02d}-{MONTHS[d0.month - 1]}-{d0.year}"
            for dom in BANK_DOMAINS:
                typ, data = m.uid("SEARCH", None, "SINCE", since, "FROM", f'"{dom}"')
                if typ != "OK" or not data or not data[0]:
                    continue
                for uid_b in data[0].split():
                    key = uid_b.decode()
                    if _seen(key):
                        continue
                    typ, d = m.uid("FETCH", uid_b, "(BODY.PEEK[])")
                    if typ != "OK" or not d or not d[0]:
                        continue
                    try:
                        process_mail(d[0][1])
                    except Exception as ex:
                        log.warning("Mail process error: %s", ex)
                    _mark_seen(key)
        finally:
            try:
                m.logout()
            except Exception:
                pass


def claim(utr, amount, uid):
    """Standalone helper: 'ok' / 'already_used' / 'not_found'."""
    try:
        target = float(amount)
    except ValueError:
        return "not_found"
    c = _db()
    try:
        cur = c.execute("UPDATE bank_credits SET claimed_by=? WHERE utr=? AND claimed_by IS NULL "
                        "AND amount BETWEEN ? AND ?", (uid, utr, target - 0.01, target + 0.01))
        c.commit()
        if cur.rowcount:
            return "ok"
        row = c.execute("SELECT claimed_by FROM bank_credits WHERE utr=?", (utr,)).fetchone()
        return "already_used" if row and row["claimed_by"] is not None else "not_found"
    finally:
        c.close()


def poll_forever(interval=None):
    if not MAIL_ON:
        raise RuntimeError("GMAIL_USER / GMAIL_APP_PASS not set (.env).")
    init()
    while True:
        try:
            fetch_credits()
        except Exception as ex:
            log.warning("Email poll error: %s", ex)
        time.sleep(interval or POLL_SEC)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    print("Gateway running... Ctrl+C to stop")
    poll_forever()
