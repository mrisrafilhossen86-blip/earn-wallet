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


# ---------------------------------------------------------------- static frontend
FRONT_DIR = BASE_DIR / "public"


@app.get("/")
def index():
    return FileResponse(FRONT_DIR / "index.html")


@app.get("/{asset}")
def asset(asset: str):
    if asset in ("app.js", "style.css"):
        return FileResponse(FRONT_DIR / asset)
    raise HTTPException(404, "Not found")
