"""Earn Wallet Mini App API (FastAPI + SQLite).

Shares the SAME SQLite database as the Telegram bot (m3bot_payment/bot_data.db),
so balances, transfers, withdrawals, bonuses and promo codes are identical
between the bot, the mini app and the rest of the system.

Every request is authenticated with Telegram's signed initData, sent in the
X-Init-Data header. The HMAC is checked on the server with the bot token, so
a user can only ever act on their own account.

Run:  uvicorn miniapp_api:app --host 0.0.0.0 --port 8000
"""
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
ADMIN_ID = int(os.getenv("ADMIN_ID", "").strip() or 6423965105)
# Path to the bot's database. Default assumes this folder sits next to m3bot_payment/.
DB_PATH = os.getenv("BOT_DB_PATH", "").strip() or str(BASE_DIR.parent / "m3bot_payment" / "bot_data.db")
# Folder that contains receipt_image.py (the same file the bot uses to draw receipts).
BOT_DIR = os.getenv("BOT_DIR", "").strip() or str(BASE_DIR.parent / "m3bot_payment")
RECEIPT_DESIGN = os.getenv("RECEIPT_DESIGN", "matched").strip() or "matched"
INITDATA_MAX_AGE = int(os.getenv("INITDATA_MAX_AGE", "86400") or 86400)
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]

# Same business rules as main.py (money is stored in poisha: 100 poisha = 1 BDT)
REFERRAL_BONUS = 500
MIN_TRANSFER = 100
MIN_WITHDRAW = 20000
DAILY_BONUS_COOLDOWN = 24 * 60 * 60
DAILY_BONUS_STREAK_WINDOW = 48 * 60 * 60
DAILY_BONUS_TIERS = [(6, 1000), (3, 500), (1, 300)]
HISTORY_LIMIT = 15
UID_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
DHAKA = ZoneInfo("Asia/Dhaka")
CASHBACK_OFFERS = [
    {"id": 1, "title": "Send BDT 50+ in transfers", "kind": "transfer", "target": 5000, "reward": 500,
     "need": "Send ৳50 in total through transfers"},
    {"id": 2, "title": "Refer 3 friends", "kind": "referral", "target": 3, "reward": 1000,
     "need": "3 friends join with your referral link"},
    {"id": 3, "title": "7-day Daily Bonus streak", "kind": "streak", "target": 7, "reward": 1500,
     "need": "Claim the Daily Bonus 7 days in a row"},
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("miniapp")

app = FastAPI(title="Earn Wallet Mini App API", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS or ["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["X-Init-Data", "Content-Type"],
)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": "Please check the values you entered."})


# ---------------------------------------------------------------- database
@contextmanager
def tx():
    """One short write-capable transaction (BEGIN IMMEDIATE), same as the bot."""
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_db():
    """Create tables if missing (same schema as main.py). Never drops data."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id        INTEGER PRIMARY KEY,
            username       TEXT,
            first_name     TEXT,
            balance        INTEGER NOT NULL DEFAULT 0,
            referred_by    INTEGER,
            referral_count INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS withdrawals (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            bkash        TEXT NOT NULL,
            amount       INTEGER NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
            processed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS transfers (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            txn_id       TEXT UNIQUE NOT NULL,
            sender_id    INTEGER NOT NULL,
            recipient_id INTEGER NOT NULL,
            amount       INTEGER NOT NULL,
            created_at   TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS bonuses (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            amount     INTEGER NOT NULL,
            streak     INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS promos (
            code          TEXT PRIMARY KEY,
            amount        INTEGER NOT NULL,
            max_claims    INTEGER NOT NULL,
            claimed_count INTEGER NOT NULL DEFAULT 0,
            created_by    INTEGER,
            active        INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS promo_claims (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            code       TEXT NOT NULL,
            user_id    INTEGER NOT NULL,
            amount     INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(code, user_id)
        );
        CREATE TABLE IF NOT EXISTS cashback_claims (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            offer_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            code       TEXT NOT NULL UNIQUE,
            amount     INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(offer_id, user_id)
        );
        """)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "bonus_last_ts" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN bonus_last_ts INTEGER NOT NULL DEFAULT 0")
        if "bonus_streak" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN bonus_streak INTEGER NOT NULL DEFAULT 0")
        if "public_uid" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN public_uid TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_public_uid ON users(public_uid)")
        for table in ("withdrawals",):
            tcols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if "txn_id" not in tcols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN txn_id TEXT")
        conn.commit()
    finally:
        conn.close()


@app.on_event("startup")
def _startup():
    init_db()
    log.info("miniapp api ready, db=%s", DB_PATH)


# ---------------------------------------------------------------- helpers
def new_public_uid(c) -> str:
    while True:
        cand = "EW-" + "".join(secrets.choice(UID_ALPHABET) for _ in range(8))
        if not c.execute("SELECT 1 FROM users WHERE public_uid=?", (cand,)).fetchone():
            return cand


def ensure_public_uid(c, uid):
    row = c.execute("SELECT public_uid FROM users WHERE user_id=?", (uid,)).fetchone()
    if row is not None and not row["public_uid"]:
        c.execute("UPDATE users SET public_uid=? WHERE user_id=?", (new_public_uid(c), uid))


def new_txn() -> str:
    return "TXN" + secrets.token_hex(8).upper()


def new_code(c, prefix: str, table: str) -> str:
    while True:
        a = "".join(secrets.choice(UID_ALPHABET) for _ in range(4))
        b = "".join(secrets.choice(UID_ALPHABET) for _ in range(4))
        cand = f"{prefix}-{a}-{b}"
        if not c.execute(f"SELECT 1 FROM {table} WHERE code=?", (cand,)).fetchone():
            return cand


def bdt(p: int) -> str:
    return f"BDT {p / 100:,.2f}"


def esc(s) -> str:
    return html.escape(str(s or ""))


def parse_money(text) -> int | None:
    """'12', '12.5', '৳12.50' -> poisha; None if invalid, zero or more than 2 decimals."""
    s = str(text or "").translate(BN_DIGITS).replace("৳", "").replace(",", "").replace("BDT", "").strip()
    if not re.fullmatch(r"\d+(\.\d{1,2})?", s):
        return None
    try:
        v = Decimal(s)
    except InvalidOperation:
        return None
    return int(v * 100) if v > 0 else None


def parse_int(text) -> int | None:
    s = str(text or "").translate(BN_DIGITS).strip()
    return int(s) if re.fullmatch(r"\d{1,15}", s) else None


def display_name(row, prefix="sn", uname="su") -> str:
    return row[prefix] or (f"@{row[uname]}" if row[uname] else "User")


def db_ts_to_dhaka(s) -> datetime:
    try:
        return datetime.strptime(str(s), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(DHAKA)
    except (TypeError, ValueError):
        return datetime.now(DHAKA)


def fmt_dhaka(s) -> str:
    return db_ts_to_dhaka(s).strftime("%d %b %Y, %I:%M %p")


def fmt_wait(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    return f"{h}h {rem // 60:02d}m"


def tg_api(method: str, payload: dict):
    """Best-effort Telegram Bot API call (notifications). Never raises."""
    if not BOT_TOKEN or BOT_TOKEN.startswith("test:"):
        return None
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        log.info("telegram %s failed: %s", method, e)
        return None


# ---------------------------------------------------------------- Telegram initData
def validate_init_data(init_data: str) -> dict:
    """Checks Telegram's HMAC signature (WebAppData algorithm) and returns the user dict."""
    if not BOT_TOKEN:
        raise HTTPException(500, "Server is missing BOT_TOKEN.")
    if not init_data:
        raise HTTPException(401, "Open this app from the bot's menu button in Telegram.")
    data = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    received_hash = data.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Invalid Telegram session.")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received_hash):
        raise HTTPException(401, "Telegram signature check failed. Reopen the app from the bot.")
    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError:
        auth_date = 0
    if time.time() - auth_date > INITDATA_MAX_AGE:
        raise HTTPException(401, "Session expired. Close and reopen the app from the bot.")
    try:
        user = json.loads(data.get("user", ""))
        uid = int(user["id"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(401, "Invalid Telegram user.")
    return {"id": uid, "username": user.get("username") or "", "first_name": user.get("first_name") or ""}


def current_tg(x_init_data: str | None = Header(default=None)) -> dict:
    return validate_init_data(x_init_data or "")


def upsert_user(c, tg: dict):
    """Create the wallet row if this Telegram user never pressed /start in the bot."""
    c.execute("INSERT OR IGNORE INTO users(user_id, username, first_name, balance, public_uid) "
              "VALUES(?,?,?,0,?)", (tg["id"], tg["username"] or None, tg["first_name"] or None,
                                    new_public_uid(c)))
    c.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?",
              (tg["username"] or None, tg["first_name"] or None, tg["id"]))
    ensure_public_uid(c, tg["id"])
    return c.execute("SELECT * FROM users WHERE user_id=?", (tg["id"],)).fetchone()


def find_user_by_ref(c, text: str):
    t = str(text or "").translate(BN_DIGITS).strip().upper().replace(" ", "")
    if re.fullmatch(r"EW-[A-Z0-9]{8}", t):
        return c.execute("SELECT * FROM users WHERE public_uid=?", (t,)).fetchone()
    n = parse_int(t)
    if n is None:
        return None
    return c.execute("SELECT * FROM users WHERE user_id=?", (n,)).fetchone()


def daily_bonus_for_day(streak_day: int) -> int:
    for min_day, amount in DAILY_BONUS_TIERS:
        if streak_day >= min_day:
            return amount
    return DAILY_BONUS_TIERS[-1][1]


def cashback_progress(c, uid: int, offer: dict):
    kind = offer["kind"]
    if kind == "transfer":
        cur = c.execute("SELECT COALESCE(SUM(amount),0) AS s FROM transfers WHERE sender_id=?",
                        (uid,)).fetchone()["s"]
        return cur, offer["target"], f"{bdt(cur)} / {bdt(offer['target'])} sent"
    if kind == "referral":
        cur = c.execute("SELECT COUNT(*) AS n FROM users WHERE referred_by=?", (uid,)).fetchone()["n"]
        return cur, offer["target"], f"{cur}/{offer['target']} referrals"
    u = c.execute("SELECT bonus_streak, bonus_last_ts FROM users WHERE user_id=?", (uid,)).fetchone()
    last = (u["bonus_last_ts"] or 0) if u else 0
    live = bool(last) and (int(time.time()) - last < DAILY_BONUS_STREAK_WINDOW)
    cur = ((u["bonus_streak"] or 0) if u else 0) if live else 0
    return cur, offer["target"], f"{cur}/{offer['target']} days in a row"


def history_items(c, uid: int, limit: int = HISTORY_LIMIT) -> list[dict]:
    """Unified transaction list (transfers, withdrawals, bonuses, promos, cashback)."""
    items = []
    trs = c.execute(
        "SELECT t.*, s.first_name AS sn, s.username AS su, s.public_uid AS spu, "
        "r.first_name AS rn, r.username AS ru, r.public_uid AS rpu "
        "FROM transfers t LEFT JOIN users s ON s.user_id=t.sender_id "
        "LEFT JOIN users r ON r.user_id=t.recipient_id "
        "WHERE t.sender_id=? OR t.recipient_id=? ORDER BY t.id DESC LIMIT ?", (uid, uid, limit)).fetchall()
    for t in trs:
        if t["sender_id"] == uid:
            other = display_name(t, "rn", "ru")
            items.append({"ref": t["txn_id"], "kind": "sent", "title": "Sent", "detail": f"to {other}",
                          "amount": -t["amount"], "receipt": True, "created_raw": t["created_at"],
                          "created": fmt_dhaka(t["created_at"]), "other_id": t["rpu"] or ""})
        else:
            other = display_name(t, "sn", "su")
            items.append({"ref": t["txn_id"], "kind": "received", "title": "Received", "detail": f"from {other}",
                          "amount": t["amount"], "receipt": True, "created_raw": t["created_at"],
                          "created": fmt_dhaka(t["created_at"]), "other_id": t["spu"] or ""})
    wds = c.execute("SELECT * FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, limit)).fetchall()
    for w in wds:
        ref = w["txn_id"] or f"WD#{w['id']}"
        items.append({"ref": ref, "kind": "withdrawal", "title": "Withdrawal",
                      "detail": f"to bKash {w['bkash']} · {w['status']}",
                      "amount": -w["amount"], "receipt": w["status"] == "paid",
                      "created_raw": w["created_at"], "created": fmt_dhaka(w["created_at"]), "other_id": ""})
    for b in c.execute("SELECT * FROM bonuses WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, limit)).fetchall():
        items.append({"ref": f"BON{b['id']}", "kind": "bonus", "title": "Daily Bonus",
                      "detail": f"streak day {b['streak']}", "amount": b["amount"], "receipt": False,
                      "created_raw": b["created_at"], "created": fmt_dhaka(b["created_at"]), "other_id": ""})
    for p in c.execute("SELECT * FROM promo_claims WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, limit)).fetchall():
        items.append({"ref": f"PROMO-{p['id']}", "kind": "promo", "title": "Promo Code",
                      "detail": f"code {p['code']}", "amount": p["amount"], "receipt": False,
                      "created_raw": p["created_at"], "created": fmt_dhaka(p["created_at"]), "other_id": ""})
    for cb in c.execute("SELECT * FROM cashback_claims WHERE user_id=? ORDER BY id DESC LIMIT ?",
                        (uid, limit)).fetchall():
        items.append({"ref": cb["code"], "kind": "cashback", "title": "Cashback",
                      "detail": f"code {cb['code']}", "amount": cb["amount"], "receipt": False,
                      "created_raw": cb["created_at"], "created": fmt_dhaka(cb["created_at"]), "other_id": ""})
    items.sort(key=lambda x: (str(x["created_raw"]), x["ref"]), reverse=True)
    return items[:limit]


def public_state(c, uid: int, bot_username: str = BOT_USERNAME) -> dict:
    u = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    refs = c.execute("SELECT COUNT(*) n FROM users WHERE referred_by=?", (uid,)).fetchone()["n"]
    last = u["bonus_last_ts"] or 0
    now = int(time.time())
    wait = max(0, last + DAILY_BONUS_COOLDOWN - now) if last else 0
    return {
        "user": {"id": uid, "first_name": u["first_name"] or "", "username": u["username"] or ""},
        "public_uid": u["public_uid"] or "-",
        "balance": u["balance"],
        "balance_text": bdt(u["balance"]),
        "referral_link": f"https://t.me/{bot_username}?start={uid}" if bot_username else "",
        "referral_count": refs,
        "referral_earned": refs * REFERRAL_BONUS,
        "referral_bonus": REFERRAL_BONUS,
        "bonus": {"claimable": wait == 0, "wait_seconds": wait, "wait_text": fmt_wait(wait) if wait else "",
                  "streak": u["bonus_streak"] or 0},
        "min_withdraw": MIN_WITHDRAW,
        "min_transfer": MIN_TRANSFER,
        "bot_username": bot_username,
    }


# ---------------------------------------------------------------- request models
class SendIn(BaseModel):
    recipient: str = Field(..., min_length=1, max_length=40)
    amount: str = Field(..., min_length=1, max_length=20)


class WithdrawIn(BaseModel):
    bkash: str = Field(..., min_length=11, max_length=20)
    amount: str = Field(..., min_length=1, max_length=20)


class PromoIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=40)


class CashbackIn(BaseModel):
    offer_id: int


# ---------------------------------------------------------------- endpoints
@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/auth")
def auth(tg: dict = Depends(current_tg)):
    """Validates initData, creates the wallet row if needed, returns the profile + state."""
    with tx() as c:
        upsert_user(c, tg)
        return public_state(c, tg["id"])


@app.get("/api/state")
def state(tg: dict = Depends(current_tg)):
    """Balance, public ID, bonus status, referral stats and the last transactions."""
    with tx() as c:
        upsert_user(c, tg)
        data = public_state(c, tg["id"])
        data["transactions"] = history_items(c, tg["id"])
    return data


@app.get("/api/transactions")
def transactions(tg: dict = Depends(current_tg)):
    with tx() as c:
        upsert_user(c, tg)
        return {"transactions": history_items(c, tg["id"], limit=100)}


@app.get("/api/cashback")
def cashback(tg: dict = Depends(current_tg)):
    out = []
    with tx() as c:
        upsert_user(c, tg)
        uid = tg["id"]
        for o in CASHBACK_OFFERS:
            cur, target, prog = cashback_progress(c, uid, o)
            done = c.execute("SELECT code FROM cashback_claims WHERE offer_id=? AND user_id=?",
                             (o["id"], uid)).fetchone()
            out.append({"id": o["id"], "title": o["title"], "need": o["need"], "reward": o["reward"],
                        "progress": prog, "complete": cur >= target,
                        "claimed_code": done["code"] if done else None})
    return {"offers": out}


@app.post("/api/cashback/claim")
def cashback_claim(body: CashbackIn, tg: dict = Depends(current_tg)):
    offer = next((o for o in CASHBACK_OFFERS if o["id"] == body.offer_id), None)
    if offer is None:
        raise HTTPException(404, "Offer not found.")
    with tx() as c:
        upsert_user(c, tg)
        uid = tg["id"]
        prev = c.execute("SELECT code FROM cashback_claims WHERE offer_id=? AND user_id=?",
                         (offer["id"], uid)).fetchone()
        if prev:
            return {"ok": False, "message": "You already claimed this offer.", "code": prev["code"]}
        cur, target, prog = cashback_progress(c, uid, offer)
        if cur < target:
            return {"ok": False, "message": f"Not complete yet: {prog}."}
        code = new_code(c, "CB", "cashback_claims")
        c.execute("INSERT INTO cashback_claims(offer_id, user_id, code, amount) VALUES(?,?,?,?)",
                  (offer["id"], uid, code, offer["reward"]))
        c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (offer["reward"], uid))
        newbal = c.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()["balance"]
    return {"ok": True, "message": f"Cashback {bdt(offer['reward'])} added.", "code": code,
            "balance": newbal, "balance_text": bdt(newbal)}


@app.get("/api/receipts")
def receipts(request: Request, tg: dict = Depends(current_tg)):
    """Receipts that can be shown as an image. Each url is for GET /api/receipt/{ref} (needs the same initData header)."""
    with tx() as c:
        upsert_user(c, tg)
        items = history_items(c, tg["id"], limit=HISTORY_LIMIT)
    return {"receipts": [
        {"ref": i["ref"], "kind": i["kind"], "amount": i["amount"], "created": i["created"],
         "url": f"/api/receipt/{urllib.parse.quote(i['ref'])}"}
        for i in items if i["receipt"]]}


@app.get("/api/receipt/{ref}")
def receipt_png(ref: str, tg: dict = Depends(current_tg)):
    """Renders the same receipt picture the bot sends (receipt_image.py). Only the sender or recipient can see it."""
    ref = ref.strip()
    if not re.fullmatch(r"(TXN[0-9A-F]{16}|WD#\d+|[A-Z0-9-]{4,40})", ref):
        raise HTTPException(400, "Bad receipt id.")
    uid = tg["id"]
    with tx() as c:
        t = c.execute("SELECT t.*, s.first_name AS sn, s.username AS su, r.first_name AS rn, "
                      "r.username AS ru, r.public_uid AS rpu, s.public_uid AS spu "
                      "FROM transfers t LEFT JOIN users s ON s.user_id=t.sender_id "
                      "LEFT JOIN users r ON r.user_id=t.recipient_id WHERE t.txn_id=?", (ref,)).fetchone()
        w = None
        if t is None and ref.startswith("WD#") and ref[3:].isdigit():
            w = c.execute("SELECT * FROM withdrawals WHERE id=? AND user_id=?", (int(ref[3:]), uid)).fetchone()
            if w is None or w["status"] != "paid":
                raise HTTPException(404, "Receipt not available.")
        elif t is None:
            w = c.execute("SELECT * FROM withdrawals WHERE txn_id=? AND user_id=? AND status='paid'",
                          (ref, uid)).fetchone()
            if w is None:
                raise HTTPException(404, "Receipt not found.")
    if t is not None and uid not in (t["sender_id"], t["recipient_id"]):
        raise HTTPException(404, "Receipt not found.")
    try:
        if str(BOT_DIR) not in sys.path:
            sys.path.insert(0, str(BOT_DIR))
        import receipt_image  # the bot's own renderer (Pillow)
        when = db_ts_to_dhaka(t["created_at"] if t is not None else w["created_at"])
        if t is not None:
            if uid == t["sender_id"]:
                png = receipt_image.render_receipt("sent", t["amount"], t["rn"] or (f"@{t['ru']}" if t["ru"] else "User"),
                                                   t["rpu"] or "-", t["txn_id"], when, design=RECEIPT_DESIGN)
            else:
                png = receipt_image.render_receipt("received", t["amount"], t["sn"] or (f"@{t['su']}" if t["su"] else "User"),
                                                   t["spu"] or "-", t["txn_id"], when, design=RECEIPT_DESIGN)
        else:
            png = receipt_image.render_withdrawal(w["amount"], w["txn_id"] or f"WD#{w['id']}", w["bkash"], when,
                                                  design=RECEIPT_DESIGN)
    except Exception as e:
        log.warning("receipt render failed: %s", e)
        raise HTTPException(500, "Could not draw the receipt right now.")
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})


@app.post("/api/send")
def send(body: SendIn, tg: dict = Depends(current_tg)):
    amount = parse_money(body.amount)
    if amount is None:
        return JSONResponse(status_code=400, content={"ok": False, "message": "Enter a valid amount, for example 25 or 25.50."})
    if amount < MIN_TRANSFER:
        return JSONResponse(status_code=400, content={"ok": False, "message": f"The minimum transfer is {bdt(MIN_TRANSFER)}."})
    sender = tg["id"]
    txn = new_txn()
    with tx() as c:
        upsert_user(c, tg)
        r = find_user_by_ref(c, body.recipient)
        if r is None:
            return JSONResponse(status_code=404, content={"ok": False, "message": "No user was found with this ID."})
        rid = r["user_id"]
        if rid == sender:
            return JSONResponse(status_code=400, content={"ok": False, "message": "You cannot transfer to yourself."})
        s = c.execute("SELECT * FROM users WHERE user_id=?", (sender,)).fetchone()
        if s["balance"] < amount:
            return JSONResponse(status_code=400, content={"ok": False, "message": f"Not enough balance. Your balance: {bdt(s['balance'])}"})
        cur = c.execute("UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?", (amount, sender, amount))
        if cur.rowcount != 1:
            return JSONResponse(status_code=409, content={"ok": False, "message": "Your balance changed. Please try again."})
        c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, rid))
        c.execute("INSERT INTO transfers(txn_id, sender_id, recipient_id, amount) VALUES(?,?,?,?)",
                  (txn, sender, rid, amount))
        ensure_public_uid(c, rid)
        newbal = c.execute("SELECT balance FROM users WHERE user_id=?", (sender,)).fetchone()["balance"]
        rpu = c.execute("SELECT public_uid FROM users WHERE user_id=?", (rid,)).fetchone()["public_uid"]
        rname = display_name(r, "first_name", "username")
    # Tell the recipient in Telegram (best effort; the transfer is already final)
    tg_api("sendMessage", {"chat_id": rid, "parse_mode": "HTML", "text": (
        "🟢 <b>Payment Received Successfully!</b>\n\n"
        f"💵 Amount: <b>{bdt(amount)}</b>\n"
        f"👤 From: <b>{esc(tg['first_name'] or 'User')}</b> <code>{esc(s['public_uid'])}</code>\n"
        f"🆔 TXN: <code>{txn}</code>\n\n✅ Status: <b>Verified</b>")})
    return {"ok": True, "message": f"Sent {bdt(amount)} to {rname} ({rpu}).", "txn_id": txn,
            "balance": newbal, "balance_text": bdt(newbal), "receipt_url": f"/api/receipt/{txn}"}


@app.post("/api/withdraw")
def withdraw(body: WithdrawIn, tg: dict = Depends(current_tg)):
    num = str(body.bkash or "").translate(BN_DIGITS).replace(" ", "").replace("-", "")
    if not re.fullmatch(r"01\d{9}", num):
        return JSONResponse(status_code=400, content={"ok": False, "message": "The bKash number must be 11 digits and start with 01."})
    amount = parse_money(body.amount)
    if amount is None:
        return JSONResponse(status_code=400, content={"ok": False, "message": "Enter a valid amount, for example 250."})
    if amount < MIN_WITHDRAW:
        return JSONResponse(status_code=400, content={"ok": False, "message": f"The minimum withdrawal is {bdt(MIN_WITHDRAW)}."})
    uid = tg["id"]
    wtxn = new_txn()
    with tx() as c:
        upsert_user(c, tg)
        if c.execute("SELECT 1 FROM withdrawals WHERE user_id=? AND status='pending'", (uid,)).fetchone():
            return JSONResponse(status_code=400, content={"ok": False, "message": "You already have a pending withdrawal request."})
        bal = c.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()["balance"]
        if bal < amount:
            return JSONResponse(status_code=400, content={"ok": False, "message": f"Not enough balance. Your balance: {bdt(bal)}"})
        c.execute("UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?", (amount, uid, amount))
        wid = c.execute("INSERT INTO withdrawals(user_id, bkash, amount, txn_id) VALUES(?,?,?,?)",
                        (uid, num, amount, wtxn)).lastrowid
        newbal = c.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()["balance"]
    tg_api("sendMessage", {"chat_id": ADMIN_ID, "parse_mode": "HTML", "text": (
        "📤 <b>New withdrawal request (Mini App)</b>\n"
        f"🧾 #{wid} · <code>{wtxn}</code>\n"
        f"👤 {esc(tg['first_name'])} (<code>{uid}</code>)\n"
        f"📱 bKash: <code>{num}</code>\n💰 Amount: <b>{bdt(amount)}</b>\n\n"
        "Approve or reject it from the bot's admin panel (Pending withdrawals).")})
    return {"ok": True, "message": f"Withdrawal #{wid} submitted. You will be notified once it is paid.",
            "withdrawal_id": wid, "txn_id": wtxn, "balance": newbal, "balance_text": bdt(newbal)}


@app.post("/api/bonus")
def bonus(tg: dict = Depends(current_tg)):
    uid = tg["id"]
    now = int(time.time())
    with tx() as c:
        upsert_user(c, tg)
        u = c.execute("SELECT bonus_last_ts, bonus_streak FROM users WHERE user_id=?", (uid,)).fetchone()
        last = u["bonus_last_ts"] or 0
        if last and now - last < DAILY_BONUS_COOLDOWN:
            return JSONResponse(status_code=429, content={
                "ok": False, "message": f"Already claimed today. Next claim in {fmt_wait(last + DAILY_BONUS_COOLDOWN - now)}.",
                "wait_seconds": last + DAILY_BONUS_COOLDOWN - now})
        streak = (u["bonus_streak"] or 0) + 1 if (last and now - last < DAILY_BONUS_STREAK_WINDOW) else 1
        amount = daily_bonus_for_day(streak)
        c.execute("UPDATE users SET balance=balance+?, bonus_last_ts=?, bonus_streak=? WHERE user_id=?",
                  (amount, now, streak, uid))
        c.execute("INSERT INTO bonuses(user_id, amount, streak) VALUES(?,?,?)", (uid, amount, streak))
        newbal = c.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()["balance"]
    return {"ok": True, "message": f"Daily bonus {bdt(amount)} claimed. Streak: {streak} day(s).",
            "amount": amount, "streak": streak, "balance": newbal, "balance_text": bdt(newbal)}


@app.post("/api/promo")
def promo(body: PromoIn, tg: dict = Depends(current_tg)):
    code = re.sub(r"\s+", "", body.code or "").translate(BN_DIGITS).upper()
    uid = tg["id"]
    with tx() as c:
        upsert_user(c, tg)
        p = c.execute("SELECT * FROM promos WHERE code=?", (code,)).fetchone()
        if not p:
            return JSONResponse(status_code=404, content={"ok": False, "message": "This promo code is not valid. Check the spelling and try again."})
        if not p["active"]:
            return JSONResponse(status_code=400, content={"ok": False, "message": "This promo code is no longer active."})
        if p["claimed_count"] >= p["max_claims"]:
            return JSONResponse(status_code=400, content={"ok": False, "message": "This promo code has already been claimed by everyone it was made for."})
        if c.execute("SELECT 1 FROM promo_claims WHERE code=? AND user_id=?", (code, uid)).fetchone():
            return JSONResponse(status_code=400, content={"ok": False, "message": "You have already claimed this promo code."})
        cur = c.execute("UPDATE promos SET claimed_count=claimed_count+1, "
                        "active=CASE WHEN claimed_count+1>=max_claims THEN 0 ELSE 1 END "
                        "WHERE code=? AND claimed_count<max_claims AND active=1", (code,))
        if cur.rowcount != 1:
            return JSONResponse(status_code=400, content={"ok": False, "message": "This promo code is no longer available."})
        c.execute("INSERT INTO promo_claims(code, user_id, amount) VALUES(?,?,?)", (code, uid, p["amount"]))
        c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (p["amount"], uid))
        newbal = c.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()["balance"]
    return {"ok": True, "message": f"Promo applied: {bdt(p['amount'])} added.", "amount": p["amount"],
            "balance": newbal, "balance_text": bdt(newbal)}


# ---------------------------------------------------------------- static frontend (embedded, no public folder needed)
from fastapi.responses import HTMLResponse

# Prefer a real public/ folder if present; otherwise use the embedded copies below.
FRONT_DIR = BASE_DIR / "public"

try:
    EMBED_INDEX = (FRONT_DIR / "index.html").read_text(encoding="utf-8")
    EMBED_CSS = (FRONT_DIR / "style.css").read_text(encoding="utf-8")
    EMBED_JS = (FRONT_DIR / "app.js").read_text(encoding="utf-8")
except FileNotFoundError:
    EMBED_INDEX = EMBED_INDEX_FALLBACK
    EMBED_CSS = EMBED_CSS_FALLBACK
    EMBED_JS = EMBED_JS_FALLBACK


@app.get("/")
def index():
    return HTMLResponse(EMBED_INDEX)


@app.get("/{asset}")
def asset(asset: str):
    if asset == "app.js":
        return Response(EMBED_JS, media_type="application/javascript")
    if asset == "style.css":
        return Response(EMBED_CSS, media_type="text/css")
    raise HTTPException(404, "Not found")


# ==== EMBEDDED FRONTEND (auto-generated) ====
EMBED_INDEX_FALLBACK = '<!doctype html>\n<html lang="en">\n<head>\n  <meta charset="utf-8" />\n  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover" />\n  <meta name="theme-color" content="#0b0a1f" />\n  <!-- Set this to your API address if the frontend is hosted separately (e.g. Netlify). Leave empty when served by miniapp_api.py. -->\n  <meta name="api-base" content="" />\n  <title>Earn Wallet</title>\n  <link rel="preconnect" href="https://fonts.googleapis.com" />\n  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />\n  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&display=swap" rel="stylesheet" />\n  <link rel="stylesheet" href="style.css" />\n  <script src="https://telegram.org/js/telegram-web-app.js"></script>\n</head>\n<body>\n  <div class="bg-orbs" aria-hidden="true"><span></span><span></span><span></span></div>\n\n  <!-- Top bar -->\n  <header class="topbar">\n    <button class="icon-btn" id="menuBtn" aria-label="Open menu"><span></span><span></span><span></span></button>\n    <div class="brand"><span class="logo">EW</span><span id="topTitle">Earn Wallet</span></div>\n    <div class="avatar" id="avatar">?</div>\n  </header>\n\n  <!-- Side drawer -->\n  <div class="scrim" id="scrim"></div>\n  <nav class="drawer" id="drawer" aria-label="Main menu">\n    <div class="drawer-head">\n      <div class="avatar big" id="drawerAvatar">?</div>\n      <div>\n        <div class="drawer-name" id="drawerName">Loading...</div>\n        <div class="drawer-uid" id="drawerUid">-</div>\n      </div>\n    </div>\n    <ul class="menu">\n      <li><button data-go="home"><i>🏠</i>Home</button></li>\n      <li><button data-go="send"><i>💸</i>Send</button></li>\n      <li><button data-go="withdraw"><i>🏧</i>Withdraw</button></li>\n      <li><button data-go="transactions"><i>📜</i>Transactions</button></li>\n      <li><button data-go="refer"><i>🎁</i>Refer &amp; Earn</button></li>\n      <li><button data-go="bonus"><i>⭐</i>Daily Bonus</button></li>\n      <li><button data-go="promo"><i>🎟️</i>Promo Code</button></li>\n      <li><button data-go="cashback"><i>💎</i>Cashback Offers</button></li>\n      <li><button data-go="support"><i>🛠️</i>Support</button></li>\n    </ul>\n  </nav>\n\n  <main class="app">\n    <!-- HOME -->\n    <section class="screen active" data-screen="home">\n      <div class="card balance-card glass">\n        <div class="shine"></div>\n        <div class="label">Total balance</div>\n        <div class="balance"><span id="balance">BDT 0.00</span></div>\n        <div class="uid-row">\n          <span>ID <b id="publicUid">-</b></span>\n          <button class="chip" id="copyUidBtn">Copy</button>\n        </div>\n      </div>\n\n      <div class="quick">\n        <button class="quick-btn" data-go="send"><span class="q-ico">↗</span>Send</button>\n        <button class="quick-btn" data-go="withdraw"><span class="q-ico">⇩</span>Withdraw</button>\n        <button class="quick-btn" data-go="refer"><span class="q-ico">+</span>Refer</button>\n        <button class="quick-btn" data-go="bonus"><span class="q-ico">★</span>Bonus</button>\n      </div>\n\n      <div class="section-head">\n        <h3>Recent activity</h3>\n        <button class="link" data-go="transactions">See all</button>\n      </div>\n      <div class="list glass" id="recentList"><div class="empty">Loading...</div></div>\n    </section>\n\n    <!-- SEND -->\n    <section class="screen" data-screen="send">\n      <div class="card glass form">\n        <h2>Send money</h2>\n        <p class="muted">Enter the recipient\'s EW- ID or their Telegram ID.</p>\n        <label>Recipient</label>\n        <input id="sendTo" placeholder="EW-XXXXXXXX or 123456789" autocomplete="off" />\n        <label>Amount (BDT)</label>\n        <input id="sendAmt" inputmode="decimal" placeholder="0.00" autocomplete="off" />\n        <div class="hint">Minimum transfer BDT 1.00</div>\n        <div class="err" id="sendErr"></div>\n      </div>\n    </section>\n\n    <section class="screen" data-screen="send_confirm">\n      <div class="card glass confirm">\n        <h2>Confirm transfer</h2>\n        <div class="row"><span>Amount</span><b id="cSendAmt">-</b></div>\n        <div class="row"><span>To</span><b id="cSendTo">-</b></div>\n        <p class="muted">Tap Confirm below to send. This cannot be undone.</p>\n      </div>\n    </section>\n\n    <!-- WITHDRAW -->\n    <section class="screen" data-screen="withdraw">\n      <div class="card glass form">\n        <h2>Withdraw to bKash</h2>\n        <p class="muted">Minimum withdrawal BDT 200.00. Only one pending withdrawal at a time.</p>\n        <label>bKash number</label>\n        <input id="wdNum" inputmode="numeric" placeholder="01XXXXXXXXX" maxlength="14" autocomplete="off" />\n        <label>Amount (BDT)</label>\n        <input id="wdAmt" inputmode="decimal" placeholder="200.00" autocomplete="off" />\n        <div class="err" id="wdErr"></div>\n      </div>\n    </section>\n\n    <section class="screen" data-screen="withdraw_confirm">\n      <div class="card glass confirm">\n        <h2>Confirm withdrawal</h2>\n        <div class="row"><span>Amount</span><b id="cWdAmt">-</b></div>\n        <div class="row"><span>bKash</span><b id="cWdNum">-</b></div>\n        <p class="muted">The admin pays this to your bKash and you get a notification when it is paid.</p>\n      </div>\n    </section>\n\n    <!-- TRANSACTIONS -->\n    <section class="screen" data-screen="transactions">\n      <div class="section-head"><h3>All transactions</h3></div>\n      <div class="list glass" id="txList"><div class="empty">Loading...</div></div>\n    </section>\n\n    <section class="screen" data-screen="receipt">\n      <div class="card glass receipt-wrap">\n        <h2 id="rcTitle">Receipt</h2>\n        <div class="rc-loading" id="rcLoading">Drawing receipt...</div>\n        <img id="rcImg" alt="Receipt" />\n        <div class="err" id="rcErr"></div>\n      </div>\n    </section>\n\n    <!-- REFER -->\n    <section class="screen" data-screen="refer">\n      <div class="card glass refer-hero">\n        <div class="big-ico">🎁</div>\n        <h2>Refer &amp; Earn</h2>\n        <p class="muted">Get <b id="referBonus">BDT 5.00</b> for every friend who joins with your link.</p>\n        <div class="linkbox" id="referLink">-</div>\n        <button class="btn primary" id="copyRefBtn">Copy referral link</button>\n      </div>\n      <div class="stats">\n        <div class="stat glass"><span>Referrals</span><b id="refCount">0</b></div>\n        <div class="stat glass"><span>Earned</span><b id="refEarned">BDT 0.00</b></div>\n      </div>\n    </section>\n\n    <!-- BONUS -->\n    <section class="screen" data-screen="bonus">\n      <div class="card glass bonus-card">\n        <div class="orb-pulse" id="bonusOrb">★</div>\n        <h2>Daily Bonus</h2>\n        <p class="muted" id="bonusText">Checking...</p>\n        <div class="streak">Streak: <b id="bonusStreak">0</b> day(s)</div>\n        <button class="btn primary" id="bonusBtn" disabled>Claim bonus</button>\n      </div>\n    </section>\n\n    <!-- PROMO -->\n    <section class="screen" data-screen="promo">\n      <div class="card glass form">\n        <h2>Promo code</h2>\n        <p class="muted">Enter a code to add its bonus to your balance.</p>\n        <label>Code</label>\n        <input id="promoCode" placeholder="PROMO CODE" autocapitalize="characters" autocomplete="off" />\n        <div class="err" id="promoErr"></div>\n        <div class="ok" id="promoOk"></div>\n      </div>\n    </section>\n\n    <!-- CASHBACK -->\n    <section class="screen" data-screen="cashback">\n      <div class="section-head"><h3>Cashback offers</h3></div>\n      <div id="cbList"><div class="empty">Loading...</div></div>\n    </section>\n\n    <!-- SUPPORT -->\n    <section class="screen" data-screen="support">\n      <div class="card glass form">\n        <h2>Support</h2>\n        <p class="muted">Need help with a payment or withdrawal? Message the admin on Telegram and include your transaction ID.</p>\n        <a class="btn primary" id="supportBtn" href="#" target="_blank" rel="noopener">Open support chat</a>\n      </div>\n    </section>\n  </main>\n\n  <div class="toast" id="toast"></div>\n  <script src="app.js"></script>\n</body>\n</html>\n'
EMBED_CSS_FALLBACK = ':root {\n  --bg: #0b0a1f;\n  --bg2: #15113a;\n  --violet: #8b5cf6;\n  --violet2: #6d28d9;\n  --cyan: #22d3ee;\n  --pink: #f472b6;\n  --text: #f4f4ff;\n  --muted: #9b97c7;\n  --glass: rgba(255, 255, 255, 0.06);\n  --glass-border: rgba(255, 255, 255, 0.12);\n  --ok: #34d399;\n  --bad: #fb7185;\n  --radius: 22px;\n  --tg-bottom: 0px;\n}\n\n* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }\n\nhtml, body {\n  margin: 0;\n  min-height: 100%;\n  background: radial-gradient(1200px 700px at 10% -10%, #2a1670 0%, transparent 60%),\n              radial-gradient(900px 600px at 110% 10%, #0e3a5c 0%, transparent 55%),\n              var(--bg);\n  color: var(--text);\n  font-family: "Outfit", system-ui, -apple-system, "Segoe UI", sans-serif;\n  overflow-x: hidden;\n}\n\nbody { padding-bottom: calc(var(--tg-bottom) + 24px); }\n\n/* animated background orbs */\n.bg-orbs { position: fixed; inset: 0; pointer-events: none; z-index: 0; overflow: hidden; }\n.bg-orbs span {\n  position: absolute; width: 260px; height: 260px; border-radius: 50%;\n  filter: blur(70px); opacity: .35; animation: drift 18s ease-in-out infinite alternate;\n}\n.bg-orbs span:nth-child(1) { background: var(--violet); top: -80px; left: -60px; }\n.bg-orbs span:nth-child(2) { background: var(--cyan); top: 40%; right: -100px; animation-duration: 22s; }\n.bg-orbs span:nth-child(3) { background: var(--pink); bottom: -100px; left: 30%; animation-duration: 26s; }\n@keyframes drift {\n  from { transform: translate(0, 0) scale(1); }\n  to { transform: translate(40px, 60px) scale(1.15); }\n}\n\n/* top bar */\n.topbar {\n  position: sticky; top: 0; z-index: 20;\n  display: flex; align-items: center; justify-content: space-between;\n  padding: 14px 16px; gap: 12px;\n  background: rgba(11, 10, 31, .6);\n  backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);\n  border-bottom: 1px solid var(--glass-border);\n}\n.brand { display: flex; align-items: center; gap: 10px; font-weight: 700; letter-spacing: .3px; }\n.logo {\n  display: grid; place-items: center; width: 30px; height: 30px; border-radius: 10px;\n  background: linear-gradient(135deg, var(--violet), var(--cyan)); color: #fff; font-size: 12px; font-weight: 800;\n  box-shadow: 0 6px 18px rgba(139, 92, 246, .45);\n}\n.avatar {\n  width: 34px; height: 34px; border-radius: 50%; display: grid; place-items: center;\n  background: linear-gradient(135deg, var(--pink), var(--violet)); font-weight: 700; font-size: 14px;\n}\n.avatar.big { width: 52px; height: 52px; font-size: 20px; }\n\n.icon-btn {\n  width: 40px; height: 40px; border-radius: 12px; border: 1px solid var(--glass-border);\n  background: var(--glass); display: flex; flex-direction: column; justify-content: center; align-items: center; gap: 4px;\n  cursor: pointer; transition: transform .2s ease;\n}\n.icon-btn span { width: 18px; height: 2px; border-radius: 2px; background: var(--text); transition: all .3s ease; }\n.icon-btn:active { transform: scale(.94); }\n\n/* drawer */\n.scrim {\n  position: fixed; inset: 0; background: rgba(3, 2, 12, .6); opacity: 0; pointer-events: none;\n  transition: opacity .3s ease; z-index: 40;\n}\n.scrim.show { opacity: 1; pointer-events: auto; }\n.drawer {\n  position: fixed; top: 0; bottom: 0; left: 0; width: min(84vw, 320px); z-index: 50;\n  padding: 26px 18px; transform: translateX(-105%); transition: transform .38s cubic-bezier(.2, .8, .2, 1);\n  background: linear-gradient(180deg, rgba(31, 22, 74, .96), rgba(11, 10, 31, .98));\n  border-right: 1px solid var(--glass-border); box-shadow: 20px 0 50px rgba(0, 0, 0, .5);\n  overflow-y: auto;\n}\n.drawer.open { transform: translateX(0); }\n.drawer-head { display: flex; gap: 14px; align-items: center; padding: 6px 4px 22px; border-bottom: 1px solid var(--glass-border); margin-bottom: 14px; }\n.drawer-name { font-weight: 700; font-size: 17px; }\n.drawer-uid { color: var(--muted); font-size: 13px; margin-top: 2px; }\n.menu { list-style: none; padding: 0; margin: 0; display: grid; gap: 6px; }\n.menu button {\n  width: 100%; display: flex; align-items: center; gap: 14px; padding: 13px 14px; border-radius: 14px;\n  background: transparent; border: 1px solid transparent; color: var(--text); font: inherit; font-size: 16px; cursor: pointer;\n  transition: background .2s ease, border-color .2s ease, transform .2s ease; text-align: left;\n}\n.menu button:hover, .menu button.active { background: var(--glass); border-color: var(--glass-border); }\n.menu button:active { transform: scale(.98); }\n.menu i { font-style: normal; width: 26px; text-align: center; }\n\n/* layout */\n.app { position: relative; z-index: 1; padding: 16px 16px 40px; max-width: 560px; margin: 0 auto; }\n.screen { display: none; animation: fadeUp .4s cubic-bezier(.2, .8, .2, 1); }\n.screen.active { display: block; }\n@keyframes fadeUp { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: none; } }\n\n.glass {\n  background: var(--glass); border: 1px solid var(--glass-border); border-radius: var(--radius);\n  backdrop-filter: blur(18px) saturate(140%); -webkit-backdrop-filter: blur(18px) saturate(140%);\n  box-shadow: 0 10px 40px rgba(0, 0, 0, .3), inset 0 1px 0 rgba(255, 255, 255, .06);\n}\n.card { padding: 22px; margin-bottom: 14px; position: relative; overflow: hidden; }\nh2 { font-size: 22px; margin: 0 0 6px; font-weight: 700; }\nh3 { font-size: 16px; margin: 0; font-weight: 600; }\n.muted { color: var(--muted); font-size: 14px; line-height: 1.5; margin: 0 0 14px; }\n.hint { color: var(--muted); font-size: 12px; margin: 6px 2px 0; }\n.err { color: var(--bad); font-size: 14px; min-height: 18px; margin-top: 10px; }\n.ok { color: var(--ok); font-size: 14px; min-height: 18px; margin-top: 10px; }\n.empty { padding: 26px; text-align: center; color: var(--muted); font-size: 14px; }\n\n/* balance card */\n.balance-card { padding: 26px 24px 22px; background: linear-gradient(135deg, rgba(139, 92, 246, .35), rgba(34, 211, 238, .14) 60%, rgba(244, 114, 182, .18)), var(--glass); }\n.balance-card .shine {\n  position: absolute; top: -60%; left: -40%; width: 60%; height: 220%; transform: rotate(20deg);\n  background: linear-gradient(90deg, transparent, rgba(255, 255, 255, .18), transparent);\n  animation: sweep 5s ease-in-out infinite;\n}\n@keyframes sweep { 0%, 100% { left: -60%; } 50% { left: 130%; } }\n.label { color: var(--muted); font-size: 14px; letter-spacing: .4px; text-transform: uppercase; }\n.balance {\n  font-size: 40px; font-weight: 800; margin: 8px 0 14px; letter-spacing: -.5px; line-height: 1.1;\n  background: linear-gradient(90deg, #fff, #c4b5fd, #67e8f9, #fff); background-size: 220% auto;\n  -webkit-background-clip: text; background-clip: text; color: transparent;\n  animation: gradientMove 6s linear infinite;\n}\n@keyframes gradientMove { to { background-position: 220% center; } }\n.uid-row { display: flex; align-items: center; justify-content: space-between; color: var(--muted); font-size: 14px; }\n.uid-row b { color: var(--text); letter-spacing: .5px; }\n.chip {\n  border: 1px solid var(--glass-border); background: rgba(255, 255, 255, .08); color: var(--text);\n  border-radius: 999px; padding: 6px 14px; font: inherit; font-size: 13px; cursor: pointer;\n}\n.chip:active { transform: scale(.96); }\n\n/* quick actions */\n.quick { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin: 6px 0 22px; }\n.quick-btn {\n  display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 14px 4px; font: inherit; font-size: 13px;\n  color: var(--text); cursor: pointer; border-radius: 18px; border: 1px solid var(--glass-border);\n  background: var(--glass); backdrop-filter: blur(14px); transition: transform .2s ease, background .2s ease;\n}\n.quick-btn:hover { background: rgba(255, 255, 255, .1); }\n.quick-btn:active { transform: scale(.94); }\n.q-ico {\n  width: 44px; height: 44px; border-radius: 15px; display: grid; place-items: center; font-size: 18px; font-weight: 700;\n  background: linear-gradient(135deg, var(--violet), var(--cyan)); box-shadow: 0 8px 20px rgba(139, 92, 246, .35);\n}\n\n.section-head { display: flex; align-items: center; justify-content: space-between; margin: 4px 4px 10px; }\n.link { background: none; border: 0; color: var(--cyan); font: inherit; font-size: 14px; cursor: pointer; padding: 0; }\n\n/* lists */\n.list { padding: 6px; }\n.tx {\n  display: flex; align-items: center; gap: 12px; padding: 12px; border-radius: 16px; cursor: pointer;\n  transition: background .2s ease, transform .2s ease; animation: fadeUp .35s ease both;\n}\n.tx:hover { background: rgba(255, 255, 255, .05); }\n.tx:active { transform: scale(.985); }\n.tx .ico { width: 40px; height: 40px; border-radius: 14px; display: grid; place-items: center; font-size: 18px; flex: none; background: rgba(255, 255, 255, .08); }\n.tx .main { flex: 1; min-width: 0; }\n.tx .t { font-weight: 600; font-size: 15px; }\n.tx .d { color: var(--muted); font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }\n.tx .amt { font-weight: 700; font-size: 15px; white-space: nowrap; }\n.tx .amt.pos { color: var(--ok); }\n.tx .amt.neg { color: var(--bad); }\n.tx .rc { font-size: 11px; color: var(--cyan); }\n\n/* forms */\n.form label { display: block; font-size: 13px; color: var(--muted); margin: 14px 2px 6px; }\n.form label:first-of-type { margin-top: 0; }\ninput {\n  width: 100%; padding: 15px 16px; border-radius: 16px; border: 1px solid var(--glass-border);\n  background: rgba(8, 6, 28, .6); color: var(--text); font: inherit; font-size: 17px; outline: none;\n  transition: border-color .2s ease, box-shadow .2s ease;\n}\ninput:focus { border-color: var(--violet); box-shadow: 0 0 0 4px rgba(139, 92, 246, .25); }\ninput::placeholder { color: #6b679a; }\n\n.confirm .row { display: flex; justify-content: space-between; align-items: center; padding: 14px 0; border-bottom: 1px solid var(--glass-border); }\n.confirm .row span { color: var(--muted); }\n.confirm .row b { font-size: 17px; }\n\n.btn {\n  display: inline-block; width: 100%; padding: 15px; border-radius: 16px; border: 0; font: inherit; font-size: 16px;\n  font-weight: 700; color: #fff; text-align: center; text-decoration: none; cursor: pointer; margin-top: 8px;\n  transition: transform .15s ease, opacity .2s ease;\n}\n.btn.primary { background: linear-gradient(120deg, var(--violet), #3b82f6 55%, var(--cyan)); background-size: 200% auto; animation: gradientMove 5s linear infinite; box-shadow: 0 10px 28px rgba(99, 102, 241, .4); }\n.btn:active { transform: scale(.98); }\n.btn:disabled { opacity: .45; cursor: not-allowed; animation: none; }\n\n/* refer */\n.refer-hero { text-align: center; }\n.big-ico { font-size: 44px; margin-bottom: 4px; animation: floaty 3s ease-in-out infinite; }\n@keyframes floaty { 50% { transform: translateY(-6px); } }\n.linkbox {\n  word-break: break-all; font-size: 13px; background: rgba(8, 6, 28, .6); border: 1px dashed var(--glass-border);\n  padding: 12px; border-radius: 14px; margin: 12px 0 4px; color: #c4b5fd;\n}\n.stats { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }\n.stat { padding: 18px; display: flex; flex-direction: column; gap: 6px; }\n.stat span { color: var(--muted); font-size: 13px; }\n.stat b { font-size: 22px; }\n\n/* bonus */\n.bonus-card { text-align: center; padding: 30px 22px; }\n.orb-pulse {\n  width: 92px; height: 92px; margin: 0 auto 14px; border-radius: 50%; display: grid; place-items: center;\n  font-size: 40px; background: radial-gradient(circle, #fde68a, #f59e0b 60%, #b45309);\n  box-shadow: 0 0 0 0 rgba(251, 191, 36, .6); animation: pulse 2.2s infinite;\n}\n.orb-pulse.idle { filter: grayscale(.7) opacity(.6); animation: none; }\n@keyframes pulse {\n  0% { box-shadow: 0 0 0 0 rgba(251, 191, 36, .55); }\n  70% { box-shadow: 0 0 0 26px rgba(251, 191, 36, 0); }\n  100% { box-shadow: 0 0 0 0 rgba(251, 191, 36, 0); }\n}\n.streak { color: var(--muted); margin: 6px 0 4px; }\n\n/* receipt */\n.receipt-wrap { text-align: center; }\n.receipt-wrap img { width: 100%; border-radius: 18px; margin-top: 10px; display: none; box-shadow: 0 16px 40px rgba(0, 0, 0, .45); animation: fadeUp .45s ease; }\n.rc-loading { color: var(--muted); padding: 40px 0; }\n\n/* toast */\n.toast {\n  position: fixed; left: 50%; bottom: 28px; transform: translate(-50%, 140%); z-index: 90; max-width: 90vw;\n  padding: 13px 18px; border-radius: 16px; font-size: 14.5px; text-align: center;\n  background: rgba(21, 17, 58, .95); border: 1px solid var(--glass-border); box-shadow: 0 14px 40px rgba(0, 0, 0, .5);\n  transition: transform .4s cubic-bezier(.2, .8, .2, 1);\n}\n.toast.show { transform: translate(-50%, 0); }\n.toast.bad { border-color: rgba(251, 113, 133, .6); }\n.toast.good { border-color: rgba(52, 211, 153, .6); }\n\n/* cashback */\n.cb { padding: 18px; margin-bottom: 12px; }\n.cb .top { display: flex; justify-content: space-between; align-items: center; gap: 10px; }\n.cb .reward { font-weight: 800; color: var(--ok); }\n.bar { height: 10px; border-radius: 999px; background: rgba(255, 255, 255, .08); margin: 12px 0 8px; overflow: hidden; }\n.bar > i { display: block; height: 100%; width: 0; border-radius: 999px; background: linear-gradient(90deg, var(--violet), var(--cyan)); transition: width .8s cubic-bezier(.2, .8, .2, 1); }\n.cb .small { color: var(--muted); font-size: 13px; }\n.stat-line { font-size: 13px; color: var(--muted); }\n\n@media (prefers-reduced-motion: reduce) {\n  *, *::before, *::after { animation: none !important; transition: none !important; }\n}\n'
EMBED_JS_FALLBACK = '(function () {\n  "use strict";\n\n  const TG = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;\n  const API_BASE = (document.querySelector(\'meta[name="api-base"]\') || {}).content || "";\n  const MIN_WITHDRAW = 20000; // poisha (BDT 200.00)\n  const MIN_TRANSFER = 100;   // poisha (BDT 1.00)\n\n  const state = { data: null, initData: "", stack: ["home"], pending: null, lastBalance: 0 };\n  const $ = (id) => document.getElementById(id);\n  const screens = () => document.querySelectorAll(".screen");\n\n  /* ---------- helpers ---------- */\n  function fmt(p) {\n    const n = Number(p || 0) / 100;\n    return "BDT " + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });\n  }\n  function toPoisha(text) {\n    const s = String(text || "").replace(/BDT|,|\\s/gi, "");\n    if (!/^\\d+(\\.\\d{1,2})?$/.test(s)) return null;\n    const v = Math.round(parseFloat(s) * 100);\n    return v > 0 ? v : null;\n  }\n  function haptic(kind) {\n    try {\n      if (!TG || !TG.HapticFeedback) return;\n      if (kind === "success" || kind === "error" || kind === "warning") TG.HapticFeedback.notificationOccurred(kind);\n      else TG.HapticFeedback.impactOccurred(kind || "light");\n    } catch (_) {}\n  }\n  function toast(msg, cls) {\n    const t = $("toast");\n    t.textContent = msg;\n    t.className = "toast show " + (cls || "");\n    clearTimeout(toast._t);\n    toast._t = setTimeout(() => (t.className = "toast " + (cls || "")), 2600);\n  }\n  function esc(s) {\n    return String(s == null ? "" : s).replace(/[&<>"\']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", \'"\': "&quot;", "\'": "&#39;" }[c]));\n  }\n  async function api(path, opts) {\n    const o = Object.assign({ method: "GET", headers: {} }, opts || {});\n    o.headers["X-Init-Data"] = state.initData;\n    if (o.body && typeof o.body !== "string") {\n      o.headers["Content-Type"] = "application/json";\n      o.body = JSON.stringify(o.body);\n    }\n    const res = await fetch(API_BASE + path, o);\n    let json = null;\n    try { json = await res.json(); } catch (_) {}\n    if (!res.ok) {\n      const msg = (json && (json.message || json.detail)) || "Something went wrong. Try again.";\n      const err = new Error(msg);\n      err.status = res.status;\n      err.payload = json;\n      throw err;\n    }\n    return json;\n  }\n\n  /* ---------- count-up animation ---------- */\n  function animateBalance(to) {\n    const el = $("balance");\n    const from = state.lastBalance || 0;\n    state.lastBalance = to;\n    const start = performance.now();\n    const dur = 900;\n    function step(now) {\n      const t = Math.min(1, (now - start) / dur);\n      const eased = 1 - Math.pow(1 - t, 3);\n      const v = Math.round(from + (to - from) * eased);\n      el.textContent = fmt(v);\n      if (t < 1) requestAnimationFrame(step);\n    }\n    requestAnimationFrame(step);\n  }\n\n  /* ---------- rendering ---------- */\n  function txRow(t, i) {\n    const pos = t.amount >= 0;\n    const icon = { sent: "↗", received: "↙", withdrawal: "🏧", bonus: "★", promo: "🎟", cashback: "💎" }[t.kind] || "•";\n    const sign = pos ? "+" : "−";\n    const amt = fmt(Math.abs(t.amount));\n    const clickable = t.receipt;\n    return `<div class="tx" data-ref="${esc(t.ref)}" data-receipt="${clickable ? 1 : 0}" style="animation-delay:${i * 40}ms">\n      <div class="ico">${icon}</div>\n      <div class="main">\n        <div class="t">${esc(t.title)}</div>\n        <div class="d">${esc(t.detail)} · ${esc(t.created)}</div>\n        ${clickable ? \'<div class="rc">View receipt →</div>\' : ""}\n      </div>\n      <div class="amt ${pos ? "pos" : "neg"}">${sign}${amt}</div>\n    </div>`;\n  }\n  function renderLists(txs) {\n    const list = txs || [];\n    const recent = list.slice(0, 5);\n    $("recentList").innerHTML = recent.length ? recent.map(txRow).join("") : \'<div class="empty">No transactions yet. Your first earnings will show here.</div>\';\n    $("txList").innerHTML = list.length ? list.map(txRow).join("") : \'<div class="empty">No transactions yet.</div>\';\n  }\n  function renderState(d) {\n    state.data = d;\n    const name = (d.user && d.user.first_name) || "Friend";\n    const initial = (name[0] || "?").toUpperCase();\n    $("avatar").textContent = initial;\n    $("drawerAvatar").textContent = initial;\n    $("drawerName").textContent = name;\n    $("drawerUid").textContent = d.public_uid;\n    $("publicUid").textContent = d.public_uid;\n    $("referLink").textContent = d.referral_link || "Link unavailable right now";\n    $("referBonus").textContent = fmt(d.referral_bonus);\n    $("refCount").textContent = d.referral_count;\n    $("refEarned").textContent = fmt(d.referral_earned);\n    $("supportBtn").href = d.bot_username ? "https://t.me/" + d.bot_username : "#";\n    animateBalance(d.balance);\n    const b = d.bonus || {};\n    $("bonusStreak").textContent = b.streak || 0;\n    $("bonusBtn").disabled = !b.claimable;\n    $("bonusOrb").classList.toggle("idle", !b.claimable);\n    $("bonusText").textContent = b.claimable\n      ? "Your bonus is ready. Tap claim to add it to your balance."\n      : "Come back in " + (b.wait_text || "a while") + " to claim again.";\n    renderLists(d.transactions);\n  }\n  async function loadState() {\n    try {\n      const d = await api("/api/state");\n      renderState(d);\n    } catch (e) {\n      toast(e.message, "bad");\n      haptic("error");\n    }\n  }\n\n  /* ---------- navigation ---------- */\n  const TITLES = {\n    home: "Earn Wallet", send: "Send", send_confirm: "Confirm transfer", withdraw: "Withdraw",\n    withdraw_confirm: "Confirm withdrawal", transactions: "Transactions", receipt: "Receipt",\n    refer: "Refer & Earn", bonus: "Daily Bonus", promo: "Promo Code", cashback: "Cashback Offers", support: "Support"\n  };\n  const MENU_SCREEN = { home: "home", send: "send", withdraw: "withdraw", transactions: "transactions", refer: "refer", bonus: "bonus", promo: "promo", cashback: "cashback", support: "support" };\n\n  function show(name, push) {\n    const current = state.stack[state.stack.length - 1];\n    if (name === current) return;\n    if (push !== false && name !== "home") {\n      state.stack.push(name);\n    } else if (push === false) {\n      state.stack = [name];\n    }\n    if (name === "home") state.stack = ["home"];\n    screens().forEach((s) => s.classList.toggle("active", s.dataset.screen === name));\n    $("topTitle").textContent = TITLES[name] || "Earn Wallet";\n    document.querySelectorAll(".menu button").forEach((b) => b.classList.toggle("active", b.dataset.go === MENU_SCREEN[name]));\n    window.scrollTo({ top: 0, behavior: "smooth" });\n    updateChrome();\n    if (name === "cashback") loadCashback();\n    if (name === "bonus") renderBonusScreen();\n    if (name === "refer") loadState();\n  }\n  function goBack() {\n    if (state.stack.length > 1) {\n      state.stack.pop();\n      const prev = state.stack.pop();\n      show(prev, true);\n    }\n  }\n  function updateChrome() {\n    const atHome = state.stack.length <= 1;\n    if (TG && TG.BackButton) {\n      if (atHome) TG.BackButton.hide(); else TG.BackButton.show();\n    }\n    // hamburger stays visible; back is handled by Telegram BackButton\n  }\n  function openDrawer(open) {\n    $("drawer").classList.toggle("open", open);\n    $("scrim").classList.toggle("show", open);\n    haptic("light");\n  }\n\n  /* ---------- MainButton helper ---------- */\n  let mainHandler = null;\n  function setMain(text, handler, enabled) {\n    if (!TG || !TG.MainButton) return;\n    if (mainHandler) TG.MainButton.offClick(mainHandler);\n    mainHandler = handler;\n    TG.MainButton.setText(text);\n    if (enabled === false) TG.MainButton.disable(); else TG.MainButton.enable();\n    TG.MainButton.show();\n    if (handler) TG.MainButton.onClick(handler);\n  }\n  function clearMain() {\n    if (!TG || !TG.MainButton) return;\n    if (mainHandler) TG.MainButton.offClick(mainHandler);\n    mainHandler = null;\n    TG.MainButton.hide();\n  }\n\n  /* ---------- send flow ---------- */\n  function sendFlow() {\n    const to = $("sendTo").value.trim();\n    const p = toPoisha($("sendAmt").value);\n    $("sendErr").textContent = "";\n    if (!to) return ($("sendErr").textContent = "Enter a recipient ID."), haptic("error");\n    if (!p) return ($("sendErr").textContent = "Enter a valid amount, for example 25 or 25.50."), haptic("error");\n    if (p < MIN_TRANSFER) return ($("sendErr").textContent = "The minimum transfer is BDT 1.00."), haptic("error");\n    if (state.data && p > state.data.balance) return ($("sendErr").textContent = "Not enough balance."), haptic("error");\n    state.pending = { type: "send", recipient: to, amount: p };\n    $("cSendAmt").textContent = fmt(p);\n    $("cSendTo").textContent = to;\n    show("send_confirm");\n    setMain("CONFIRM SEND " + fmt(p), confirmSend, true);\n    haptic("medium");\n  }\n  async function confirmSend() {\n    if (!state.pending || state.pending.type !== "send") return;\n    const amount = (state.pending.amount / 100).toFixed(2);\n    if (TG && TG.MainButton) TG.MainButton.showProgress(false);\n    try {\n      const r = await api("/api/send", { method: "POST", body: { recipient: state.pending.recipient, amount } });\n      haptic("success");\n      toast(r.message, "good");\n      state.pending = null;\n      $("sendTo").value = ""; $("sendAmt").value = "";\n      clearMain();\n      await loadState();\n      show("transactions");\n    } catch (e) {\n      haptic("error");\n      toast(e.message, "bad");\n    }\n  }\n\n  /* ---------- withdraw flow ---------- */\n  function withdrawFlow() {\n    const num = $("wdNum").value.replace(/[\\s-]/g, "");\n    const p = toPoisha($("wdAmt").value);\n    $("wdErr").textContent = "";\n    if (!/^01\\d{9}$/.test(num)) return ($("wdErr").textContent = "bKash number must be 11 digits and start with 01."), haptic("error");\n    if (!p) return ($("wdErr").textContent = "Enter a valid amount."), haptic("error");\n    if (p < MIN_WITHDRAW) return ($("wdErr").textContent = "The minimum withdrawal is BDT 200.00."), haptic("error");\n    if (state.data && p > state.data.balance) return ($("wdErr").textContent = "Not enough balance."), haptic("error");\n    state.pending = { type: "withdraw", bkash: num, amount: p };\n    $("cWdAmt").textContent = fmt(p);\n    $("cWdNum").textContent = num;\n    show("withdraw_confirm");\n    setMain("CONFIRM WITHDRAWAL " + fmt(p), confirmWithdraw, true);\n    haptic("medium");\n  }\n  async function confirmWithdraw() {\n    if (!state.pending || state.pending.type !== "withdraw") return;\n    try {\n      const r = await api("/api/withdraw", { method: "POST", body: { bkash: state.pending.bkash, amount: (state.pending.amount / 100).toFixed(2) } });\n      haptic("success");\n      toast(r.message, "good");\n      state.pending = null;\n      $("wdNum").value = ""; $("wdAmt").value = "";\n      clearMain();\n      await loadState();\n      show("transactions");\n    } catch (e) {\n      haptic("error");\n      toast(e.message, "bad");\n    }\n  }\n\n  /* ---------- receipts (image needs the auth header, so fetch as blob) ---------- */\n  let lastBlobUrl = null;\n  async function openReceipt(ref) {\n    show("receipt");\n    $("rcTitle").textContent = "Receipt " + ref;\n    $("rcImg").style.display = "none";\n    $("rcErr").textContent = "";\n    $("rcLoading").style.display = "block";\n    try {\n      const res = await fetch(API_BASE + "/api/receipt/" + encodeURIComponent(ref), { headers: { "X-Init-Data": state.initData } });\n      if (!res.ok) {\n        let msg = "Receipt not available.";\n        try { msg = (await res.json()).detail || msg; } catch (_) {}\n        throw new Error(msg);\n      }\n      const blob = await res.blob();\n      if (lastBlobUrl) URL.revokeObjectURL(lastBlobUrl);\n      lastBlobUrl = URL.createObjectURL(blob);\n      $("rcImg").src = lastBlobUrl;\n      $("rcImg").style.display = "block";\n      $("rcLoading").style.display = "none";\n    } catch (e) {\n      $("rcLoading").style.display = "none";\n      $("rcErr").textContent = e.message;\n    }\n  }\n\n  /* ---------- bonus ---------- */\n  function renderBonusScreen() {\n    if (state.data) renderState(state.data);\n  }\n  async function claimBonus() {\n    $("bonusBtn").disabled = true;\n    try {\n      const r = await api("/api/bonus", { method: "POST" });\n      haptic("success");\n      toast(r.message, "good");\n      await loadState();\n    } catch (e) {\n      haptic("error");\n      toast(e.message, "bad");\n      await loadState();\n    }\n  }\n\n  /* ---------- promo ---------- */\n  async function claimPromo() {\n    const code = $("promoCode").value.trim();\n    $("promoErr").textContent = ""; $("promoOk").textContent = "";\n    if (!code) return ($("promoErr").textContent = "Enter a promo code."), haptic("error");\n    try {\n      const r = await api("/api/promo", { method: "POST", body: { code } });\n      haptic("success");\n      $("promoOk").textContent = r.message;\n      $("promoCode").value = "";\n      toast(r.message, "good");\n      await loadState();\n    } catch (e) {\n      haptic("error");\n      $("promoErr").textContent = e.message;\n    }\n  }\n\n  /* ---------- cashback ---------- */\n  async function loadCashback() {\n    try {\n      const r = await api("/api/cashback");\n      $("cbList").innerHTML = r.offers.map((o) => {\n        const pct = o.complete ? 100 : Math.min(100, Math.round(((parseProgress(o.progress)) || 0) * 100));\n        const btn = o.claimed_code\n          ? `<div class="stat-line" style="margin-top:10px">Claimed · code <b>${esc(o.claimed_code)}</b></div>`\n          : `<button class="btn primary" data-claim="${o.id}" ${o.complete ? "" : "disabled"}>${o.complete ? "Claim " + fmt(o.reward) : "Not yet"}</button>`;\n        return `<div class="glass cb">\n          <div class="top"><b>${esc(o.title)}</b><span class="reward">+${fmt(o.reward)}</span></div>\n          <div class="small">${esc(o.need)}</div>\n          <div class="bar"><i style="width:${pct}%"></i></div>\n          <div class="small">${esc(o.progress)}</div>\n          ${btn}\n        </div>`;\n      }).join("");\n    } catch (e) {\n      $("cbList").innerHTML = `<div class="empty">${esc(e.message)}</div>`;\n    }\n  }\n  function parseProgress(s) {\n    const m = String(s || "").match(/(\\d+(?:\\.\\d+)?)\\s*\\/\\s*(\\d+(?:\\.\\d+)?)/);\n    if (!m) return 0;\n    return Math.min(1, parseFloat(m[1]) / parseFloat(m[2]));\n  }\n  async function claimCashback(id) {\n    try {\n      const r = await api("/api/cashback/claim", { method: "POST", body: { offer_id: Number(id) } });\n      if (r.ok) { haptic("success"); toast(r.message, "good"); } else { haptic("warning"); toast(r.message, "bad"); }\n      await loadCashback();\n      await loadState();\n    } catch (e) {\n      haptic("error");\n      toast(e.message, "bad");\n    }\n  }\n\n  /* ---------- wiring ---------- */\n  function wire() {\n    $("menuBtn").addEventListener("click", () => openDrawer(true));\n    $("scrim").addEventListener("click", () => openDrawer(false));\n    document.querySelectorAll("[data-go]").forEach((el) => {\n      el.addEventListener("click", () => {\n        const target = el.dataset.go;\n        openDrawer(false);\n        haptic("light");\n        if (target === "home") { state.stack = ["home"]; show("home", false); }\n        else { state.stack = ["home"]; show(target, true); }\n        if (target === "send") clearMain();\n        if (target === "withdraw") clearMain();\n      });\n    });\n    $("copyUidBtn").addEventListener("click", () => copy(state.data && state.data.public_uid, "ID copied"));\n    $("copyRefBtn").addEventListener("click", () => copy(state.data && state.data.referral_link, "Referral link copied"));\n    $("bonusBtn").addEventListener("click", claimBonus);\n    $("promoCode").addEventListener("keydown", (e) => { if (e.key === "Enter") claimPromo(); });\n    $("txList").addEventListener("click", onTxClick);\n    $("recentList").addEventListener("click", onTxClick);\n    $("cbList").addEventListener("click", (e) => {\n      const b = e.target.closest("[data-claim]");\n      if (b) claimCashback(b.dataset.claim);\n    });\n    // primary actions on the form screens: MainButton shows on the form, sends to confirm screen\n    ["sendTo", "sendAmt"].forEach((id) => $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") sendFlow(); }));\n    ["wdNum", "wdAmt"].forEach((id) => $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") withdrawFlow(); }));\n    if (TG && TG.onEvent) TG.onEvent("backButtonClicked", goBack);\n  }\n  function onTxClick(e) {\n    const row = e.target.closest(".tx");\n    if (!row) return;\n    if (row.dataset.receipt !== "1") { haptic("light"); toast("No receipt for this item.", "bad"); return; }\n    haptic("light");\n    openReceipt(row.dataset.ref);\n  }\n  function copy(text, okMsg) {\n    if (!text) return toast("Nothing to copy yet", "bad");\n    const done = () => { haptic("success"); toast(okMsg, "good"); };\n    if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));\n    else fallbackCopy(text, done);\n  }\n  function fallbackCopy(text, done) {\n    const ta = document.createElement("textarea");\n    ta.value = text; document.body.appendChild(ta); ta.select();\n    try { document.execCommand("copy"); done(); } catch (_) { toast("Copy failed", "bad"); }\n    ta.remove();\n  }\n\n  /* MainButton is active on the form screens (Send/Withdraw) as "Review"; confirm screens use the handlers set in the flows. */\n  function formMainButton() {\n    const cur = state.stack[state.stack.length - 1];\n    if (cur === "send") setMain("REVIEW TRANSFER", sendFlow, true);\n    else if (cur === "withdraw") setMain("REVIEW WITHDRAWAL", withdrawFlow, true);\n    else clearMain();\n  }\n\n  function applyTelegramTheme() {\n    if (!TG) return;\n    try {\n      const tp = TG.themeParams || {};\n      if (tp.secondary_bg_color) document.documentElement.style.setProperty("--glass", "rgba(255,255,255,.06)");\n      if (TG.setHeaderColor) TG.setHeaderColor("#0b0a1f");\n      if (TG.setBackgroundColor) TG.setBackgroundColor("#0b0a1f");\n      if (TG.setBottomBarColor) TG.setBottomBarColor("#0b0a1f");\n    } catch (_) {}\n  }\n\n  async function boot() {\n    wire();\n    if (TG) {\n      TG.ready();\n      TG.expand();\n      applyTelegramTheme();\n      if (TG.BackButton) TG.BackButton.onClick(goBack);\n      state.initData = TG.initData || "";\n    }\n    if (!state.initData) {\n      $("drawerName").textContent = "Open from Telegram";\n      toast("Open this app from the bot\'s menu button in Telegram.", "bad");\n      return;\n    }\n    try {\n      const auth = await api("/api/auth", { method: "POST" });\n      renderState(auth);\n    } catch (e) {\n      toast(e.message, "bad");\n      haptic("error");\n      return;\n    }\n    // Refresh the form MainButton whenever the visible screen changes.\n    window.__formMain = formMainButton;\n    formMainButton();\n    setInterval(() => { if (!document.hidden) loadState(); }, 20000);\n  }\n\n  // Keep MainButton in sync with the visible form screen.\n  const _show = show;\n  show = function (name, push) { _show(name, push); if (window.__formMain) window.__formMain(); };\n\n  document.addEventListener("DOMContentLoaded", boot);\n})();\n'
