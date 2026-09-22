"""
Phone Assistant — Step 1: Gmail over API, no voice yet.

Endpoints:
  GET  /                      health check
  POST /accounts              create an account  {"name": "...", "phone": "+1..."}
  GET  /accounts              list accounts
  GET  /link/new?account_id=1       -> a signed link for the customer
  GET  /link/start?t=...            -> the page they open to connect Gmail
  GET  /link/callback               (Google redirects here, don't call it yourself)
  GET  /link/code?account_id=1      -> a six-digit code for the /connect page
  GET  /test/unread?account_id=1    -> what the voice agent will read out
  GET  /test/read?account_id=1&msg_id=...
  POST /test/send             {"account_id":1,"to":"...","subject":"...","body":"..."}
"""

import os
import base64
import re as _re_scrub
import json
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

import time
import threading
import urllib.request
import urllib.parse
import urllib.error
import base64 as _b64
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import (RedirectResponse, HTMLResponse,
                               JSONResponse)
from pydantic import BaseModel
from cryptography.fernet import Fernet
from sqlalchemy import (create_engine, Column, Integer, String, DateTime,
                        Float,
                        Text, ForeignKey)
from sqlalchemy.orm import declarative_base, sessionmaker
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ----------------------------------------------------------------- config

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./local.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")   # e.g. https://xxx.up.railway.app
ENCRYPTION_KEY = os.environ["ENCRYPTION_KEY"]       # Fernet key, see setup notes
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

# Envelope encryption. Set KMS_KEY_ID (and AWS creds) for production;
# without it, falls back to the local Fernet key.
KMS_KEY_ID = os.environ.get("KMS_KEY_ID", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
# Azure alternative: full key id, e.g.
# https://myvault.vault.azure.net/keys/phone-assistant/<version>
AZURE_KEY_ID = os.environ.get("AZURE_KEY_ID", "")
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")

# Stripe. When this is set, a card is handed to Stripe and we keep only the
# token it gives back - the digits never reach the database. Without it,
# nothing changes and cards are stored encrypted as before.
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")

# SMS — set SMS_PROVIDER to "twilio" or "bulkvs"
SMS_PROVIDER = os.environ.get("SMS_PROVIDER", "").lower()
SMS_FROM = os.environ.get("SMS_FROM", "")           # your sending number
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
BULKVS_USER = os.environ.get("BULKVS_API_USER", "")
BULKVS_PASS = os.environ.get("BULKVS_API_PASSWORD", "")
TELNYX_API_KEY = os.environ.get("TELNYX_API_KEY", "")
TELNYX_PROFILE_ID = os.environ.get("TELNYX_MESSAGING_PROFILE_ID", "")

BROWSERBASE_API_KEY = os.environ.get("BROWSERBASE_API_KEY", "")
BROWSERBASE_PROJECT_ID = os.environ.get("BROWSERBASE_PROJECT_ID", "")
# Browsers must look like they're in the customer's own country/state, or
# Google and the banks flag every sign-in as suspicious.
PROXY_COUNTRY = os.environ.get("PROXY_COUNTRY", "US")
PROXY_STATE = os.environ.get("PROXY_STATE", "NY")
PROXY_CITY = os.environ.get("PROXY_CITY", "")

# The clock your customers are on. Every spoken date and time is converted
# into this. Change it in Railway if you move markets.
LOCAL_TZ = os.environ.get("LOCAL_TZ", "America/New_York")


def _tz():
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(LOCAL_TZ)
    except Exception:
        return timezone.utc


SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

# STORED_TOKEN_SCOPES: a saved token is never refreshed with SCOPES
# attached. A refresh that names scopes asks Google for all of them, and
# Google refuses outright if the customer never granted one - adding
# Contacts, Drive and Tasks to SCOPES broke every existing mailbox, email
# included. Refreshing without scopes returns whatever they did grant.
# Google lets people untick individual boxes on the consent screen. When
# they do, the token comes back with fewer scopes than we asked for, and
# oauthlib treats that as an error and throws - the customer would get a
# crash page instead of a connection. Accept what was granted; the tools
# report a missing permission on their own.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

fernet = Fernet(ENCRYPTION_KEY.encode())

# ----------------------------------------------------------------- database

Base = declarative_base()
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)


class Account(Base):
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
    name = Column(String(120))
    pin = Column(String(10), default="1234")
    created_at = Column(DateTime, default=datetime.utcnow)
    stripe_customer = Column(String(40), default="")


class PhoneNumber(Base):
    __tablename__ = "phone_numbers"
    id = Column(Integer, primary_key=True)
    number = Column(String(20), unique=True)          # E.164, e.g. +18455551234
    account_id = Column(Integer, ForeignKey("accounts.id"))


class Connection(Base):
    """One mailbox. A customer can have several."""
    __tablename__ = "connections"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"))
    provider = Column(String(30), default="google")
    email = Column(String(200))
    label = Column(String(60), default="")        # "work", "shul", "personal"
    is_default = Column(Integer, default=0)
    use_count = Column(Integer, default=0)
    secret_blob = Column(Text)                        # encrypted token json
    linked_at = Column(DateTime, default=datetime.utcnow)


class Call(Base):
    __tablename__ = "calls"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)
    from_number = Column(String(20))
    started_at = Column(DateTime, default=datetime.utcnow)
    ended_at = Column(DateTime, nullable=True)
    duration_sec = Column(Integer, default=0)
    verified = Column(Integer, default=0)
    room = Column(String(120))


class CallTurn(Base):
    __tablename__ = "call_turns"
    id = Column(Integer, primary_key=True)
    call_id = Column(Integer, ForeignKey("calls.id"))
    at = Column(DateTime, default=datetime.utcnow)
    who = Column(String(20))          # caller / agent / tool
    text = Column(Text)
    tool = Column(String(60), default="")
    latency_ms = Column(Integer, default=0)


class Memory(Base):
    """Shared conversation history across voice and SMS, per account."""
    __tablename__ = "memory"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"))
    at = Column(DateTime, default=datetime.utcnow)
    channel = Column(String(10))      # voice / sms
    who = Column(String(10))          # user / assistant
    text = Column(Text)


class Dlr(Base):
    """Carrier delivery receipts — why a text did or didn't arrive."""
    __tablename__ = "dlr"
    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=datetime.utcnow)
    ref_id = Column(String(60), default="")
    to_number = Column(String(20), default="")
    status = Column(String(40), default="")
    raw = Column(Text, default="")


class Onboard(Base):
    """One assisted Gmail sign-in. The password is never stored here."""
    __tablename__ = "onboard"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"))
    email = Column(String(200), default="")
    state = Column(String(30), default="starting")
    message = Column(Text, default="")
    # same rule as Job.reason: code branches on this, never on the prose
    reason = Column(String(40), default="")
    history = Column(Text, default="")
    at = Column(DateTime, default=datetime.utcnow)


class Followup(Base):
    """Something a caller asked to be looked at, or that the agent couldn't do."""
    __tablename__ = "followups"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)
    call_id = Column(Integer, nullable=True)
    at = Column(DateTime, default=datetime.utcnow)
    reason = Column(String(60), default="")
    note = Column(Text, default="")
    channel = Column(String(10), default="voice")
    done = Column(Integer, default=0)


class SiteLogin(Base):
    """A customer's login for a site that has no API. Encrypted at rest;
    the password is never returned through the API."""
    __tablename__ = "site_logins"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"))
    site = Column(String(80))                 # "amazon", "walmart"
    username = Column(String(200), default="")
    secret_blob = Column(Text)
    at = Column(DateTime, default=datetime.utcnow)
    last_used = Column(DateTime, nullable=True)
    use_count = Column(Integer, default=0)


class SiteSession(Base):
    """A saved browser session per customer per site, so we stay logged in."""
    __tablename__ = "site_sessions"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"))
    site = Column(String(80))
    context_id = Column(String(120), default="")
    at = Column(DateTime, default=datetime.utcnow)
    last_ok = Column(DateTime, nullable=True)


class Job(Base):
    """Background browser work: log in, check, order."""
    __tablename__ = "jobs"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)
    call_id = Column(Integer, nullable=True)
    kind = Column(String(40), default="")        # site_login, order
    site = Column(String(80), default="")
    payload = Column(Text, default="{}")
    state = Column(String(30), default="queued")
    message = Column(Text, default="")
    # why it ended, as a fixed word rather than English prose: bad_password,
    # signed_out, needs_code, ... Anything that has to DECIDE something
    # reads this, never the message.
    reason = Column(String(40), default="")
    history = Column(Text, default="")
    at = Column(DateTime, default=datetime.utcnow)
    done_at = Column(DateTime, nullable=True)


class Recipe(Base):
    """Steps that worked for a task on a site, recorded automatically."""
    __tablename__ = "recipes"
    id = Column(Integer, primary_key=True)
    site = Column(String(80))
    task = Column(String(80))               # short label, e.g. order_status
    example_goal = Column(Text, default="")
    steps = Column(Text, default="[]")      # json list of actions
    times_ok = Column(Integer, default=0)
    times_failed = Column(Integer, default=0)
    at = Column(DateTime, default=datetime.utcnow)
    last_ok = Column(DateTime, nullable=True)
    retired = Column(Integer, default=0)


class SiteRequest(Base):
    """Every request against a site, so you can see what's popular."""
    __tablename__ = "site_requests"
    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=datetime.utcnow)
    site = Column(String(80))
    task = Column(String(80), default="")
    goal = Column(Text, default="")
    path = Column(String(20), default="")   # recipe / agent / fallback
    outcome = Column(String(20), default="")  # ok / failed
    seconds = Column(Integer, default=0)
    job_id = Column(Integer, nullable=True)


class Address(Base):
    __tablename__ = "addresses"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"))
    label = Column(String(40), default="home")
    line1 = Column(String(200), default="")
    line2 = Column(String(200), default="")
    city = Column(String(100), default="")
    state = Column(String(40), default="")
    zip = Column(String(20), default="")
    is_default = Column(Integer, default=0)


class PaymentCard(Base):
    """Card number encrypted in the vault; only last four in the clear."""
    __tablename__ = "payment_cards"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"))
    label = Column(String(40), default="")
    last4 = Column(String(4), default="")
    brand = Column(String(20), default="")
    exp = Column(String(7), default="")             # MM/YY
    name_on_card = Column(String(120), default="")
    secret_blob = Column(Text)
    is_default = Column(Integer, default=0)
    at = Column(DateTime, default=datetime.utcnow)


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"))
    call_id = Column(Integer, nullable=True)
    site = Column(String(80), default="")
    item = Column(Text, default="")
    quantity = Column(Integer, default=1)
    expected_price = Column(String(20), default="")
    address_id = Column(Integer, nullable=True)
    card_id = Column(Integer, nullable=True)
    state = Column(String(30), default="draft")   # draft/confirmed/placing/placed/failed/cancelled
    confirmation = Column(String(120), default="")
    final_total = Column(String(20), default="")
    message = Column(Text, default="")
    history = Column(Text, default="")
    job_id = Column(Integer, nullable=True)
    at = Column(DateTime, default=datetime.utcnow)
    placed_at = Column(DateTime, nullable=True)


class Usage(Base):
    """What one call or text actually consumed, and what it cost."""
    __tablename__ = "usage"
    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=datetime.utcnow)
    call_id = Column(Integer, nullable=True)
    account_id = Column(Integer, nullable=True)
    kind = Column(String(20), default="voice")   # voice/sms
    audio_in = Column(Integer, default=0)
    audio_out = Column(Integer, default=0)
    text_in = Column(Integer, default=0)
    text_out = Column(Integer, default=0)
    cached_in = Column(Integer, default=0)
    mini_in = Column(Integer, default=0)
    mini_out = Column(Integer, default=0)
    brain_in = Column(Integer, default=0)      # the model driving browsers
    brain_out = Column(Integer, default=0)
    call_seconds = Column(Integer, default=0)
    browser_seconds = Column(Integer, default=0)
    searches = Column(Integer, default=0)
    texts = Column(Integer, default=0)
    cost_cents = Column(Float, default=0.0)
    breakdown = Column(Text, default="")


class Profile(Base):
    """The standing facts about one customer - not what was said, what is
    TRUE: how they like things done, who their family are, what they order,
    what they can and can't manage. Built up after each call and read at the
    start of the next, so nobody has to explain themselves twice."""
    __tablename__ = "profiles"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), unique=True)
    notes = Column(Text, default="")          # one fact per line
    updated = Column(DateTime, default=datetime.utcnow)
    by_hand = Column(Text, default="")        # what staff added; never
    #                                           overwritten by the system


class Block(Base):
    """Every time a site refused us, and which kind of refusal it was."""
    __tablename__ = "blocks"
    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=datetime.utcnow)
    account_id = Column(Integer, nullable=True)
    job_id = Column(Integer, nullable=True)
    site = Column(String(80), default="")
    kind = Column(String(20), default="")      # puzzle/fingerprint/ip_block
    vendor = Column(String(30), default="")    # perimeterx/cloudflare/...
    url = Column(String(300), default="")
    saw = Column(Text, default="")             # what the page said


class Change(Base):
    """Everything done on a customer's behalf: an email sent, a file
    changed, a card charged. The live log carries every step of every job
    and scrolls away in minutes; this is the short list a person can read,
    kept so the office can answer "what did it do for my mother today?"."""
    __tablename__ = "changes"
    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=datetime.utcnow)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)
    call_id = Column(Integer, nullable=True)
    area = Column(String(20), default="")     # email/drive/contacts/...
    what = Column(String(40), default="")     # sent/replied/created/charged
    detail = Column(Text, default="")         # in plain words
    undo = Column(String(200), default="")    # how to put it back, if it can


class Event(Base):
    """Live log: every step of every job, sign-in, order and failure."""
    __tablename__ = "events"
    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=datetime.utcnow)
    kind = Column(String(20), default="")      # signin/job/order/call/error
    ref = Column(String(40), default="")       # e.g. signin 12, order 3
    account_id = Column(Integer, nullable=True)
    level = Column(String(10), default="info")  # info/warn/error
    text = Column(Text, default="")


class SecretAccess(Base):
    """Every time a stored password is decrypted, and why."""
    __tablename__ = "secret_access"
    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=datetime.utcnow)
    account_id = Column(Integer, nullable=True)
    site = Column(String(80), default="")
    purpose = Column(String(120), default="")


Base.metadata.create_all(engine)


def _ensure_columns():
    """Add columns that newer versions introduced, on existing databases."""
    from sqlalchemy import text as _sql
    wanted = {
        "connections": [
            ("label", "VARCHAR(60) DEFAULT ''"),
            ("is_default", "INTEGER DEFAULT 0"),
            ("use_count", "INTEGER DEFAULT 0"),
        ],
        "onboard": [
            ("history", "TEXT DEFAULT ''"),
            ("reason", "VARCHAR(40) DEFAULT ''"),
        ],
        "usage": [
            ("brain_in", "INTEGER DEFAULT 0"),
            ("brain_out", "INTEGER DEFAULT 0"),
        ],
        "jobs": [
            ("reason", "VARCHAR(40) DEFAULT ''"),
        ],
        "accounts": [
            ("stripe_customer", "VARCHAR(40) DEFAULT ''"),
        ],
    }
    # Each ALTER gets its own transaction: in Postgres one failure aborts
    # the whole transaction, so batching them means later ones never run.
    for table, cols in wanted.items():
        for name, decl in cols:
            try:
                with engine.begin() as c:
                    c.execute(_sql(
                        f"ALTER TABLE {table} "
                        f"ADD COLUMN IF NOT EXISTS {name} {decl}"))
            except Exception:
                try:
                    with engine.begin() as c:      # SQLite has no IF NOT EXISTS
                        c.execute(_sql(
                            f"ALTER TABLE {table} ADD COLUMN {name} {decl}"))
                except Exception:
                    pass          # already there


_ensure_columns()




# ------------------------------------------------------------------ costs
# Every rate can be overridden from Railway. VERIFY THESE against your own
# invoices before pricing customers - vendors change them.
def _rate(name, default):
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return float(default)


RATES = {
    # OpenAI Realtime, per 1M tokens
    "realtime_audio_in": _rate("RATE_RT_AUDIO_IN", 32.00),
    "realtime_audio_out": _rate("RATE_RT_AUDIO_OUT", 64.00),
    "realtime_text_in": _rate("RATE_RT_TEXT_IN", 4.00),
    "realtime_text_out": _rate("RATE_RT_TEXT_OUT", 16.00),
    "realtime_cached_in": _rate("RATE_RT_CACHED_IN", 0.40),
    # gpt-4o-mini text brain, per 1M tokens
    "mini_in": _rate("RATE_MINI_IN", 0.15),
    "mini_out": _rate("RATE_MINI_OUT", 0.60),
    # the model driving the browser - change these to match MODEL_BROWSER
    "brain_in": _rate("RATE_BRAIN_IN", 2.50),
    "brain_out": _rate("RATE_BRAIN_OUT", 10.00),
    # per minute
    "livekit_agent_min": _rate("RATE_LIVEKIT_AGENT_MIN", 0.005),
    "livekit_sip_min": _rate("RATE_LIVEKIT_SIP_MIN", 0.004),
    "telephony_min": _rate("RATE_TELEPHONY_MIN", 0.0085),
    "browser_min": _rate("RATE_BROWSER_MIN", 0.10),
    # per unit
    "search": _rate("RATE_SEARCH", 0.001),
    "sms": _rate("RATE_SMS", 0.0079),
}



def price_usage(u) -> tuple:
    """Return (dollars, breakdown dict) for one usage row."""
    m = 1_000_000.0
    parts = {
        "voice model in": u.audio_in / m * RATES["realtime_audio_in"],
        "voice model out": u.audio_out / m * RATES["realtime_audio_out"],
        "text in": u.text_in / m * RATES["realtime_text_in"],
        "text out": u.text_out / m * RATES["realtime_text_out"],
        "cached in": u.cached_in / m * RATES["realtime_cached_in"],
        "helper model": (u.mini_in / m * RATES["mini_in"]
                         + u.mini_out / m * RATES["mini_out"]),
        "browser brain": ((u.brain_in or 0) / m * RATES["brain_in"]
                          + (u.brain_out or 0) / m * RATES["brain_out"]),
        "livekit": (u.call_seconds / 60.0
                    * (RATES["livekit_agent_min"]
                       + RATES["livekit_sip_min"])),
        "phone line": u.call_seconds / 60.0 * RATES["telephony_min"],
        "browser": u.browser_seconds / 60.0 * RATES["browser_min"],
        "web search": u.searches * RATES["search"],
        "texts": u.texts * RATES["sms"],
    }
    parts = {k: round(v, 6) for k, v in parts.items() if v > 0}
    return round(sum(parts.values()), 6), parts


# The fields that mean "how much" and can be added up across reports.
# Anything not in here (call_id, account_id, kind) identifies the row and
# must never be arithmetic.
USAGE_COUNTERS = {
    "audio_in", "audio_out", "text_in", "text_out", "cached_in",
    "mini_in", "mini_out", "brain_in", "brain_out",
    "call_seconds", "browser_seconds", "searches", "texts",
}


def record_usage(**kw):
    """Add or update the usage row for a call. Never raises."""
    try:
        db = Session()
        row = None
        if kw.get("call_id"):
            row = db.query(Usage).filter_by(call_id=kw["call_id"]).first()
        if not row:
            row = Usage(**{k: v for k, v in kw.items()
                           if hasattr(Usage, k)})
            db.add(row)
        else:
            # Add up the counters only. This used to add EVERY number,
            # including call_id - so a second report for the same call
            # turned call_id 41 into 82 and the whole cost of that call
            # vanished. And kind must not flip: the browser reporting its
            # tokens against a call must not stop it being a voice call.
            for k, v in kw.items():
                if not hasattr(Usage, k) or v is None:
                    continue
                if k in USAGE_COUNTERS and isinstance(v, (int, float)):
                    setattr(row, k, (getattr(row, k) or 0) + v)
                elif k in ("call_id", "account_id", "kind"):
                    if not getattr(row, k, None):
                        setattr(row, k, v)
        db.flush()
        dollars, parts = price_usage(row)
        row.cost_cents = round(dollars * 100, 4)
        row.breakdown = json.dumps(parts)
        db.commit()
        db.close()
        return dollars
    except Exception as e:
        emit("cost", "usage", f"could not record usage: {str(e)[:150]}",
             "warn")
        return 0.0


# --------------------------------------------------------------- scrubbing
# The model sometimes writes a password into a note or a problem report.
# Nothing that reaches storage or the live log is trusted to be clean.

_SECRET_PATTERNS = [
    # password: X   password is X   password = "X"
    _re_scrub.compile(
        r"(?i)\b(pass(?:word|wd|code)|pwd|passphrase|cvv)\b"
        r"\s*(?:is|was|=|:)\s*['\"]?([^\s'\",.;]{3,64})['\"]?"),
    # the password 'X'
    _re_scrub.compile(
        r"(?i)\b(pass(?:word|wd|code)|pwd)\b\s+['\"]([^'\"]{3,64})['\"]"),
    # pin: 1234   pin is 1234   code = 483920
    _re_scrub.compile(
        r"(?i)\b(pin|otp|passcode|security code|verification code|"
        r"one[- ]time code)\b\s*(?:is|was|=|:)\s*['\"]?(\d{3,10})"
        r"['\"]?"),
    # card numbers
    _re_scrub.compile(r"\b(?:\d[ -]?){13,19}\b"),
]

# "capital D, e, s, k, t, o, p, two, zero" — a password being spelled out
_SPELLED = _re_scrub.compile(
    r"(?i)\b(?:capital|uppercase|lowercase)\s+\w\b"
    r"(?:\s*,\s*(?:capital\s+|lowercase\s+)?"
    r"(?:[a-z0-9]|zero|one|two|three|four|five|six|seven|eight|nine|"
    r"exclamation(?:\s+mark)?|dot|period|dash|underscore|at|hash|star)\b)"
    r"{3,}")


def scrub(text: str) -> str:
    """Remove credentials. Deliberately narrow: it must never eat ordinary
    words like 'before' in 'verify your PIN before we continue'."""
    if not text:
        return text
    out = _SPELLED.sub("[password removed]", text)
    for pat in _SECRET_PATTERNS:
        if pat.groups >= 2:
            out = pat.sub(lambda m: f"{m.group(1)} [removed]", out)
        else:
            out = pat.sub("[number removed]", out)
    return out


def record_change(account_id, area: str, what: str, detail: str,
                  call_id=None, undo: str = ""):
    """One line the office can read later. Never raises, never holds a
    password or card number - scrub() runs over it like everything else."""
    try:
        db = Session()
        db.add(Change(account_id=account_id, call_id=call_id,
                      area=area[:20], what=what[:40],
                      detail=scrub(detail or "")[:1000], undo=undo[:200]))
        db.commit()
        db.close()
    except Exception:
        pass


def emit(kind: str, ref: str, text: str, level: str = "info",
         account_id=None):
    """Write to the live log. Never raises."""
    try:
        db = Session()
        db.add(Event(kind=kind, ref=ref[:40], account_id=account_id,
                     level=level, text=scrub(text)[:2000]))
        db.commit()
        db.close()
    except Exception:
        pass


# ----------------------------------------------------------------- vault
# Swap the two functions below for AWS KMS before real customers.
# Everything else in the app stays the same.

_kms = None
_akv = None


def _kms_client():
    global _kms
    if _kms is None and KMS_KEY_ID:
        import boto3
        _kms = boto3.client("kms", region_name=AWS_REGION)
    return _kms


def _azure_client():
    """Azure Key Vault, using the app's client id/secret from the env."""
    global _akv
    if _akv is None and AZURE_KEY_ID:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.keys.crypto import CryptographyClient
        _akv = CryptographyClient(AZURE_KEY_ID, DefaultAzureCredential())
    return _akv


def vault_put(data: dict) -> str:
    """Encrypt. With a cloud key: a fresh data key per secret, itself wrapped
    by a master key that never leaves the cloud. Without: the local key."""
    raw = json.dumps(data).encode()

    az = _azure_client()
    if az:
        from azure.keyvault.keys.crypto import KeyWrapAlgorithm
        from cryptography.fernet import Fernet as _F
        import os as _os
        dk = _os.urandom(32)
        key = _b64.urlsafe_b64encode(dk)
        sealed = _F(key).encrypt(raw)
        wrapped = az.wrap_key(KeyWrapAlgorithm.rsa_oaep_256, dk).encrypted_key
        del key, dk
        return ("akv:" + _b64.b64encode(wrapped).decode() + ":"
                + sealed.decode())

    c = _kms_client()
    if c:
        from cryptography.fernet import Fernet as _F
        dk = c.generate_data_key(KeyId=KMS_KEY_ID, KeySpec="AES_256")
        key = _b64.urlsafe_b64encode(dk["Plaintext"])
        sealed = _F(key).encrypt(raw)
        del key
        return "kms:" + _b64.b64encode(dk["CiphertextBlob"]).decode() \
               + ":" + sealed.decode()
    return fernet.encrypt(raw).decode()


def vault_get(blob: str) -> dict:
    if blob.startswith("akv:"):
        az = _azure_client()
        if not az:
            raise HTTPException(500,
                                "This secret needs Azure Key Vault, which "
                                "isn't configured.")
        from azure.keyvault.keys.crypto import KeyWrapAlgorithm
        from cryptography.fernet import Fernet as _F
        _, wrapped, sealed = blob.split(":", 2)
        dk = az.unwrap_key(KeyWrapAlgorithm.rsa_oaep_256,
                           _b64.b64decode(wrapped)).key
        key = _b64.urlsafe_b64encode(dk)
        out = _F(key).decrypt(sealed.encode())
        del key, dk
        return json.loads(out.decode())

    if blob.startswith("kms:"):
        c = _kms_client()
        if not c:
            raise HTTPException(500, "This secret needs KMS, which isn't set.")
        from cryptography.fernet import Fernet as _F
        _, wrapped, sealed = blob.split(":", 2)
        dk = c.decrypt(CiphertextBlob=_b64.b64decode(wrapped))
        key = _b64.urlsafe_b64encode(dk["Plaintext"])
        out = _F(key).decrypt(sealed.encode())
        del key
        return json.loads(out.decode())
    return json.loads(fernet.decrypt(blob.encode()).decode())

# ----------------------------------------------------------------- google

LINK_LIFE_MIN = int(os.environ.get("LINK_LIFE_MIN", "30"))


def _make_link_token(account_id: int, minutes: int = 0) -> str:
    """A signed, expiring ticket for one customer to connect their email."""
    import hmac
    import hashlib
    until = int(time.time()) + (minutes or LINK_LIFE_MIN) * 60
    body = f"{account_id}.{until}"
    sig = hmac.new(ENCRYPTION_KEY.encode(), body.encode(),
                   hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def _check_link_token(t: str):
    """The account this ticket is for, or None if it's bad or stale."""
    import hmac
    import hashlib
    try:
        acc, until, sig = (t or "").split(".")
        body = f"{acc}.{until}"
        want = hmac.new(ENCRYPTION_KEY.encode(), body.encode(),
                        hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, want):
            return None
        if int(until) < int(time.time()):
            return None
        return int(acc)
    except Exception:
        return None


CONNECT_CODE_HOURS = 1
CONNECT_MAX_FAILS = 5        # per phone number, per hour
CONNECT_MAX_FAILS_IP = 20    # per address, per hour
_CONNECT_FAILS: dict = {}


def _connect_code(account_id: int, hour: int | None = None,
                  purpose: str = "connect") -> str:
    """Six digits the assistant reads out, for a helper to type in on the
    connect page. Nothing is stored: it is worked out from the account and
    the hour, signed with our key, so it can't be guessed from the account
    number and it dies on its own."""
    import hmac
    import hashlib
    if hour is None:
        hour = int(time.time() // (CONNECT_CODE_HOURS * 3600))
    mac = hmac.new(ENCRYPTION_KEY.encode(),
                   f"{purpose}.{account_id}.{hour}".encode(),
                   hashlib.sha256).digest()
    return f"{int.from_bytes(mac[:8], 'big') % 1000000:06d}"


def _connect_code_ok(account_id: int, code: str,
                     purpose: str = "connect") -> bool:
    """This hour's code or last hour's, so one given at 2:59 still works."""
    import hmac
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6:
        return False
    now = int(time.time() // (CONNECT_CODE_HOURS * 3600))
    return any(hmac.compare_digest(_connect_code(account_id, h, purpose),
                                   code)
               for h in (now, now - 1))


def _connect_too_many(key: str, limit: int, add: bool = False) -> bool:
    """Six digits are only safe if nobody can try them all. Count failures
    per phone number and per address over the last hour."""
    cutoff = time.time() - 3600
    hits = [t for t in _CONNECT_FAILS.get(key, []) if t > cutoff]
    if add:
        hits.append(time.time())
    _CONNECT_FAILS[key] = hits
    return len(hits) >= limit


def _flow(state=None):
    cfg = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [f"{PUBLIC_URL}/link/callback"],
        }
    }
    f = Flow.from_client_config(cfg, scopes=SCOPES, state=state)
    f.redirect_uri = f"{PUBLIC_URL}/link/callback"
    return f


def list_mailboxes(account_id: int) -> list:
    db = Session()
    rows = (db.query(Connection)
              .filter_by(account_id=account_id, provider="google").all())
    out = [{"id": r.id, "email": r.email, "label": r.label,
            "default": bool(r.is_default), "used": r.use_count or 0}
           for r in rows]
    db.close()
    out.sort(key=lambda x: (not x["default"], -x["used"]))
    return out


def pick_connection(account_id: int, which: str = ""):
    """Find the right mailbox: by name, else the default, else most used."""
    db = Session()
    rows = (db.query(Connection)
              .filter_by(account_id=account_id, provider="google").all())
    if not rows:
        db.close()
        return None, []
    chosen = None
    if which:
        w = which.strip().lower()
        for r in rows:
            if w and (w == (r.label or "").lower()
                      or w == (r.email or "").lower()):
                chosen = r
                break
        if not chosen:
            for r in rows:
                if w in (r.label or "").lower() or w in (r.email or "").lower():
                    chosen = r
                    break
    if not chosen:
        rows.sort(key=lambda r: (not bool(r.is_default), -(r.use_count or 0)))
        chosen = rows[0]
    chosen.use_count = (chosen.use_count or 0) + 1
    db.commit()
    cid, email = chosen.id, chosen.email
    blob = chosen.secret_blob
    others = [{"email": r.email, "label": r.label} for r in rows]
    db.close()
    return {"id": cid, "email": email, "blob": blob}, others


def gmail_client(account_id: int, which: str = ""):
    """Returns an authorised Gmail client for one of this account's mailboxes."""
    conn, _ = pick_connection(account_id, which)
    if not conn:
        raise HTTPException(400, "This account has no Gmail linked yet.")

    tok = vault_get(conn["blob"])
    creds = Credentials(
        token=tok.get("token"),
        refresh_token=tok.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=None,    # see STORED_TOKEN_SCOPES
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)

# ----------------------------------------------------------------- tools
# These are the functions the voice agent will call later.


def _clock(t) -> str:
    """4:07 PM. Written out rather than %-I, which is Linux-only."""
    return t.strftime("%I:%M %p").lstrip("0")


def _when(internal_ms) -> str:
    """Turn Gmail's timestamp into something worth saying out loud, in the
    CALLER'S clock. This used to format UTC as if it were local, so every
    time the assistant said was four or five hours ahead of the customer."""
    try:
        t = datetime.fromtimestamp(int(internal_ms) / 1000,
                                   tz=timezone.utc).astimezone(_tz())
    except Exception:
        return ""
    now = datetime.now(_tz())
    delta = now - t
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        # the clock time too: "51 minutes ago" said just after midnight
        # left the model believing it was still the same day
        if t.date() != now.date():
            return f"{mins} minutes ago, at {_clock(t)} last night"
        return f"{mins} minutes ago, at {_clock(t)}"
    if t.date() == now.date():
        return f"today at {_clock(t)}"
    if (now.date() - t.date()).days == 1:
        return f"yesterday at {_clock(t)}"
    if delta.days < 7:
        return f"{t:%A} at {_clock(t)}"
    return f"{t:%b} {t.day} at {_clock(t)}"


def local_str(dt, style: str = "stamp") -> str:
    """A stored UTC timestamp written in the caller's clock.

    Everything in the database is UTC. Every one of these used to be
    strftime'd straight out, so the admin panel, the live log and the
    history handed to the model were all hours off - the same mistake that
    made the assistant read out an email as 7pm when it arrived at 3pm.
    Nothing formats a stored time by hand any more; it comes through here.
    Also avoids %-d and %-I, which don't exist on Windows."""
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    t = dt.astimezone(_tz())
    if style == "seconds":
        return t.strftime("%I:%M:%S %p").lstrip("0")
    if style == "day":
        return f"{t:%b} {t.day}"
    if style == "full":
        return f"{t:%b} {t.day} " + t.strftime("%I:%M:%S %p").lstrip("0")
    return f"{t:%b} {t.day} {_clock(t)}"


def tool_unread_summary(account_id: int, limit: int = 5, which: str = "",
                        primary_only: bool = False) -> dict:
    """Unread mail. primary_only skips Promotions, Social and Updates -
    the tabs people don't think of as their real inbox."""
    svc = gmail_client(account_id, which)
    query = "is:unread in:inbox"
    if primary_only:
        query += " category:primary"
    res = svc.users().messages().list(
        userId="me", q=query, maxResults=limit).execute()
    ids = [m["id"] for m in res.get("messages", [])]

    items = []
    for mid in ids:
        m = svc.users().messages().get(
            userId="me", id=mid, format="metadata",
            metadataHeaders=["From", "Subject", "Date"]).execute()
        h = {x["name"]: x["value"] for x in m["payload"].get("headers", [])}
        items.append({
            "id": mid,
            "from": h.get("From", ""),
            "subject": h.get("Subject", "(no subject)"),
            "when": _when(m.get("internalDate")),
            "category": _category(m.get("labelIds") or []),
            "snippet": m.get("snippet", "")[:200],
        })

    total = res.get("resultSizeEstimate", len(items))
    return {"unread_count": total, "messages": items}


SIGNED_OUT_MARKS = _re_scrub.compile(
    r"(?i)(sign in or create account|sign in to do more|"
    r"create your account|please sign in|log in to your account|"
    r"you.re signed out|track your order status)")


def _browser_error(e) -> str:
    """Say what a failure during a browser job actually means. Not every
    HTTP error comes from Browserbase - the thinking is OpenAI's."""
    t = str(e)
    if "openai" in t.lower() or getattr(e, "_from_openai", False):
        if "401" in t:
            return ("OpenAI rejected the API key (401). OPENAI_API_KEY is "
                    "missing or wrong on the BACKEND service in Railway - "
                    "the voice agent having one is not enough.")
        if "429" in t:
            return "OpenAI is rate limiting or the account is out of credit."
        return f"The thinking step failed: {t[:160]}"
    if "401" in t:
        return ("Browserbase rejected the API key (401). Check "
                "BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID in Railway.")
    if "402" in t:
        return ("Browserbase says payment required (402) - the account is "
                "out of sessions or minutes. Check the Browserbase "
                "dashboard; no code change will fix it.")
    if "500" in t and "connect.browserbase" in t:
        return ("Browserbase could not start a browser (500). This normally "
                "follows the account running out of sessions or minutes - "
                "check the Browserbase dashboard.")
    if "429" in t:
        return "Too many browser sessions at once. Try again in a minute."
    if "timeout" in t.lower():
        return "The site took too long to respond."
    return f"Browser error: {t[:200]}"


SIGNED_IN_MARKS = _re_scrub.compile(
    r"(?i)(deliver to|hello,\s*\w|your orders|account & lists|sign out|"
    r"my account|order history)")


def looks_signed_in(text: str) -> bool:
    """Fast path only: obvious English wording that needs no thinking.
    Never the last word - see signed_in()."""
    if not text:
        return False
    return bool(SIGNED_IN_MARKS.search(text)) and not looks_signed_out(text)


def signed_in(page, why_for: str = "") -> tuple:
    """Is this page showing the customer their own account? Returns
    (yes_or_no, reason).

    Deliberately not a list of phrases. Word lists only ever describe the
    shops someone has already added, in the language they added them in.
    This works in three stages, cheapest first:

      1. A password box on the page means we are still at the door.
      2. Wording that obviously settles it either way - no model call.
      3. Anything else: the model looks at the page, in any language.

    When it genuinely cannot tell, it answers YES. Wrongly claiming a
    session expired sends the customer through a sign-in they didn't need
    and files a job for the office; wrongly proceeding just reads a page
    that turns out to have nothing on it. The first mistake is worse."""
    text = page_text(page, 1500)
    if q(page, 'input[type="password"]'):
        return False, "there is still a password box on the page"
    if looks_signed_out(text):
        return False, "the page is asking them to sign in"
    if looks_signed_in(text):
        return True, "the page is showing their account"
    if not OPENAI_API_KEY or not text:
        return True, "no clear sign either way - carrying on"

    msg = (f"URL: {page_url(page)}\n\nPAGE TEXT:\n{text}\n\n"
           f"Is this person signed in to their own account on this site?\n"
           f"Seeing anything personal - their name, address, orders, "
           f"balance, saved details - means yes. A sign-in, registration "
           f"or password form means no. The page may be in any language, "
           f"and may be a site you have never seen.\n"
           f'Reply with JSON only: {{"signed_in": true, "why": "..."}}')
    try:
        d = _openai_chat(model=MODEL_BROWSER, cheap=False, messages=[
            _user_turn(msg, page_shot(page) if BROWSER_VISION else "")])
        got = _first_json(d["choices"][0]["message"].get("content") or "")
        if "signed_in" in got:
            return bool(got["signed_in"]), str(got.get("why", ""))[:160]
    except Exception as e:
        emit("browser", "signed_in", f"could not judge the page "
                                     f"({why_for}): {str(e)[:120]}", "warn")
    return True, "could not tell - carrying on rather than blocking them"


BOT_CHECK_MARKS = _re_scrub.compile(
    r"(?i)(press(ing)?\s*(and|&)\s*hold|activat\w*\s+and\s+hold|"
    r"hold\w*\s+(the\s+)?button|prove you.re (not a robot|human)|"
    r"(confirm|verify) (that )?you.?re (a )?human|"
    r"verify you are (a )?human|i.m not a robot|captcha|recaptcha|"
    r"unusual traffic from your|are you a robot|human verification)")


def looks_like_bot_check(text: str) -> bool:
    """The site is asking for a human. We do not try to get past these -
    we stop and say so. Recognising it early also saves burning every
    remaining step on a wall that will not move."""
    return bool(text and BOT_CHECK_MARKS.search(text))


CODE_DEST = _re_scrub.compile(
    r"(?i)(sent (?:the |a |your )?(?:code|otp)[^.<]{0,60}|"
    r"code (?:was )?sent to[^.<]{0,40}|"
    r"(?:emailed|texted|messaged|called) (?:it )?to[^.<]{0,40}|"
    r"(?:to|on) your (?:phone|email|mobile|number)[^.<]{0,40}|"
    r"(?:phone|email|number|address) (?:ending|ending in)[^.<]{0,20}|"
    r"\*{2,}[-\s]*\d{2,4})")

CODE_BAD = _re_scrub.compile(
    r"(?i)(code (?:you entered )?is not valid|invalid code|"
    r"incorrect code|wrong code|code (?:is )?expired|"
    r"couldn.t verify the code|enter a valid code)")


def code_destination(text: str) -> str:
    """Where a site says it sent its one-time code. A caller who is told
    only "they sent a code" has nowhere to look; the page nearly always
    says "to your phone ***-**96" and we were throwing that away."""
    m = CODE_DEST.search(text or "")
    return " ".join(m.group(0).split())[:120] if m else ""


def looks_signed_out(text: str) -> bool:
    """A page that shows sign-in prompts and no account name."""
    if not text:
        return False
    if SIGNED_OUT_MARKS.search(text):
        return True
    return False


# ---------------------------------------------------------------- blocks
# Which wall is it? "Walmart blocked us" is not actionable. A different
# address, a slower pace, a saved login and "this door does not open"
# are four different answers, and only the measurement tells them apart.

BLOCK_VENDORS = (
    ("perimeterx", r"(?i)(perimeterx|px-captcha|human security|"
                   r"press (and|&) hold)"),
    ("cloudflare", r"(?i)(cloudflare|cf-ray|checking your browser|"
                   r"attention required|error 10\d\d|"
                   r"enable javascript and cookies to continue)"),
    ("akamai", r"(?i)(akamai|reference #\d|access denied.{0,40}"
               r"reference)"),
    ("datadome", r"(?i)(datadome|geo\.captcha-delivery\.com)"),
    ("imperva", r"(?i)(incapsula|imperva|request unsuccessful|"
                r"pardon our interruption)"),
    ("recaptcha", r"(?i)(recaptcha|g-recaptcha|i.m not a robot)"),
    ("hcaptcha", r"(?i)hcaptcha"),
    ("arkose", r"(?i)(arkose|funcaptcha)"),
    ("aws_waf", r"(?i)(aws waf|awswaf)"),
    ("queue_it", r"(?i)queue-?it"),
)

# kind -> (what it is, can anything legitimate change it, what to do)
BLOCK_KINDS = {
    "puzzle": ("a puzzle for a human: press and hold, tick a box, pick "
               "pictures", False,
               "Nobody can do this for a caller with no screen. Use a "
               "sanctioned route (partner API, ACP) or have staff place "
               "the order."),
    "fingerprint": ("the site decided we are a robot from the browser "
                    "itself, with no puzzle offered", False,
                    "A different address will not help. This needs a "
                    "sanctioned route, or a person."),
    "ip_block": ("the address we came from is refused", True,
                 "Worth retrying from the caller's own region, or a "
                 "residential address. Check /browser/proxy_status."),
    "rate_limit": ("too many requests too quickly", True,
                   "Wait and try again more slowly. Nothing is wrong "
                   "with the account."),
    "geo_block": ("the site does not serve this country", True,
                  "Try the caller's own country."),
    "login_wall": ("it will not go further without an account", True,
                   "Save the customer's login for this site, then try "
                   "again."),
    "site_error": ("the site's own error page, not a block", True,
                   "Worth trying again shortly."),
    "unknown": ("refused, and it does not say why", False,
                "Look at the stored page text and name it properly."),
}

BLOCK_MARKS = (
    # order matters: a page can say several of these at once, and the
    # strongest signal must win. Amazon's silent refusal says "something
    # went wrong" AND "to discuss automated access" - it is not an outage.
    ("fingerprint", r"(?i)(to discuss automated access|automated queries|"
                    r"unusual activity from your|suspicious activity|"
                    r"bot detected|request looks automated|"
                    r"enable javascript and cookies|"
                    r"checking your browser)"),
    ("rate_limit", r"(?i)(too many requests|rate limit|"
                   r"slow down|try again in a (few|moment)|"
                   r"you have exceeded)"),
    ("geo_block", r"(?i)(not available in your (country|region)|"
                  r"geo.?restricted|unavailable in your country)"),
    ("login_wall", r"(?i)(sign in to (your account|continue|see)|"
                   r"please sign in|create an account to|"
                   r"log in to continue|members only)"),
    ("ip_block", r"(?i)(access denied|you don.t have permission to access|"
                 r"your ip address|blocked your ip|403 forbidden)"),
    ("site_error", r"(?i)(something went wrong|oops|please refresh|"
                   r"temporarily unavailable|internal server error|"
                   r"service unavailable)"),
)


def classify_block(text: str, url: str = "") -> dict:
    """What kind of wall this is, in a word, plus what would change it."""
    body = " ".join((text or "").split())[:4000]
    vendor = ""
    for name, pattern in BLOCK_VENDORS:
        if _re_scrub.search(pattern, body):
            vendor = name
            break
    kind = ""
    if looks_like_bot_check(body):
        kind = "puzzle"
    if not kind:
        for name, pattern in BLOCK_MARKS:
            if _re_scrub.search(pattern, body):
                kind = name
                break
    if not kind and looks_signed_out(body):
        kind = "login_wall"
    if not kind:
        kind = "fingerprint" if vendor else "unknown"
    what, retry, advice = BLOCK_KINDS[kind]
    return {"kind": kind, "vendor": vendor, "what": what,
            "worth_retrying": retry, "advice": advice,
            "url": (url or "")[:300], "saw": body[:400]}


def record_block(account_id, site: str, text: str, url: str = "",
                 job_id=None) -> dict:
    """Name it, write it down, and say it once in the live log. Knowing
    Walmart is a fingerprint wall and Lowe's an address refusal is what
    decides where the ordering work goes."""
    got = classify_block(text, url)
    try:
        db = Session()
        db.add(Block(account_id=account_id or None, site=(site or "?")[:80],
                     job_id=job_id, kind=got["kind"],
                     vendor=got["vendor"], url=got["url"],
                     saw=scrub(got["saw"])))
        db.commit()
        db.close()
    except Exception:
        pass
    emit("block", site or "?",
         f"{got['kind']}"
         + (f" ({got['vendor']})" if got["vendor"] else "")
         + f": {got['what']}", "warn", account_id)
    return got


def _category(labels) -> str:
    """Which Gmail tab a message landed in."""
    for lab, name in (("CATEGORY_PROMOTIONS", "Promotions"),
                      ("CATEGORY_SOCIAL", "Social"),
                      ("CATEGORY_UPDATES", "Updates"),
                      ("CATEGORY_FORUMS", "Forums"),
                      ("SPAM", "Spam")):
        if lab in labels:
            return name
    return "Inbox"


def _extract_body(payload) -> str:
    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(
            payload["body"]["data"]).decode("utf-8", "ignore")
    for part in payload.get("parts", []) or []:
        if part.get("mimeType") == "text/plain":
            got = _extract_body(part)
            if got:
                return got
    for part in payload.get("parts", []) or []:
        got = _extract_body(part)
        if got:
            return got
    return ""


def tool_read_email(account_id: int, msg_id: str, which: str = "") -> dict:
    svc = gmail_client(account_id, which)
    m = svc.users().messages().get(
        userId="me", id=msg_id, format="full").execute()
    h = {x["name"]: x["value"] for x in m["payload"].get("headers", [])}
    body = _extract_body(m["payload"]).strip()
    return {
        "from": h.get("From", ""),
        "subject": h.get("Subject", "(no subject)"),
        "date": h.get("Date", ""),
        "when": _when(m.get("internalDate")),
        "body": body[:4000],
    }


def tool_mark_read(account_id: int, msg_ids, read: bool = True,
                   which: str = "") -> dict:
    """Mark specific messages read or unread."""
    svc = gmail_client(account_id, which)
    ids = [msg_ids] if isinstance(msg_ids, str) else list(msg_ids)
    if not ids:
        return {"changed": 0}
    body = ({"removeLabelIds": ["UNREAD"]} if read
            else {"addLabelIds": ["UNREAD"]})
    body["ids"] = ids
    svc.users().messages().batchModify(userId="me", body=body).execute()
    return {"changed": len(ids), "read": read}


def tool_mark_all_read(account_id: int, which: str = "",
                       primary_only: bool = False, limit: int = 500) -> dict:
    """Clear the unread flag across the inbox."""
    svc = gmail_client(account_id, which)
    query = "is:unread in:inbox"
    if primary_only:
        query += " category:primary"
    done = 0
    while done < limit:
        res = svc.users().messages().list(
            userId="me", q=query, maxResults=min(500, limit - done)).execute()
        ids = [m["id"] for m in res.get("messages", [])]
        if not ids:
            break
        svc.users().messages().batchModify(
            userId="me",
            body={"ids": ids, "removeLabelIds": ["UNREAD"]}).execute()
        done += len(ids)
        if len(ids) < 500:
            break
    return {"marked_read": done,
            "scope": "primary inbox" if primary_only else "whole inbox"}


def tool_send_email(account_id: int, to: str, subject: str,
                    body: str, which: str = "") -> dict:
    svc = gmail_client(account_id, which)
    msg = MIMEText(body)
    msg["to"] = to
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    sent = svc.users().messages().send(
        userId="me", body={"raw": raw}).execute()
    return {"sent": True, "id": sent.get("id")}


def _headers_of(msg) -> dict:
    return {x["name"]: x["value"]
            for x in msg.get("payload", {}).get("headers", [])}


def tool_reply_email(account_id: int, msg_id: str, body: str,
                     which: str = "", all_recipients: bool = False) -> dict:
    """Reply to a message, in its own thread so it reads as a reply."""
    svc = gmail_client(account_id, which)
    orig = svc.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["From", "To", "Cc", "Subject", "Message-ID",
                         "References", "Reply-To"]).execute()
    h = _headers_of(orig)
    to = h.get("Reply-To") or h.get("From", "")
    subject = h.get("Subject", "")
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    msg = MIMEText(body)
    msg["to"] = to
    if all_recipients and h.get("Cc"):
        msg["cc"] = h["Cc"]
    msg["subject"] = subject
    mid = h.get("Message-ID", "")
    if mid:
        msg["In-Reply-To"] = mid
        msg["References"] = (h.get("References", "") + " " + mid).strip()
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    sent = svc.users().messages().send(
        userId="me",
        body={"raw": raw, "threadId": orig.get("threadId")}).execute()
    return {"sent": True, "id": sent.get("id"), "to": to,
            "subject": subject}


def tool_forward_email(account_id: int, msg_id: str, to: str,
                       note: str = "", which: str = "") -> dict:
    """Pass a message on to somebody else, original text included."""
    svc = gmail_client(account_id, which)
    orig = svc.users().messages().get(
        userId="me", id=msg_id, format="full").execute()
    h = _headers_of(orig)
    subject = h.get("Subject", "")
    if not subject.lower().startswith("fwd:"):
        subject = "Fwd: " + subject
    original = _extract_body(orig["payload"]).strip()[:20000]
    parts = []
    if note.strip():
        parts.append(note.strip())
        parts.append("")
    parts += ["---------- Forwarded message ----------",
              f"From: {h.get('From', '')}",
              f"Date: {h.get('Date', '')}",
              f"Subject: {h.get('Subject', '')}",
              "", original]
    msg = MIMEText("\n".join(parts))
    msg["to"] = to
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    sent = svc.users().messages().send(userId="me",
                                       body={"raw": raw}).execute()
    return {"sent": True, "id": sent.get("id"), "to": to,
            "subject": subject}


def tool_draft_email(account_id: int, to: str, subject: str, body: str,
                     which: str = "") -> dict:
    """Write it now, send it later - or let the office finish it."""
    svc = gmail_client(account_id, which)
    msg = MIMEText(body)
    msg["to"] = to
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    d = svc.users().drafts().create(
        userId="me", body={"message": {"raw": raw}}).execute()
    return {"saved": True, "id": d.get("id"), "to": to, "subject": subject}


# What each spoken action does to a message. Trash is its own API call;
# the rest are label changes.
MESSAGE_ACTIONS = {
    "archive": {"removeLabelIds": ["INBOX"]},
    "unarchive": {"addLabelIds": ["INBOX"]},
    "star": {"addLabelIds": ["STARRED"]},
    "unstar": {"removeLabelIds": ["STARRED"]},
    "important": {"addLabelIds": ["IMPORTANT"]},
    "spam": {"addLabelIds": ["SPAM"], "removeLabelIds": ["INBOX"]},
    "not_spam": {"addLabelIds": ["INBOX"], "removeLabelIds": ["SPAM"]},
}


def tool_message_action(account_id: int, msg_ids, action: str,
                        which: str = "") -> dict:
    """Archive, star, mark spam or move to the bin.

    Nothing here destroys anything. Trash is recoverable for 30 days and
    everything else is a label that can be put back - deliberately, because
    a caller cannot see what just happened."""
    ids = [msg_ids] if isinstance(msg_ids, str) else list(msg_ids)
    if not ids:
        return {"changed": 0}
    svc = gmail_client(account_id, which)
    if action == "trash":
        for i in ids:
            svc.users().messages().trash(userId="me", id=i).execute()
        return {"changed": len(ids), "action": "trash",
                "undo": "in the bin - recoverable for 30 days"}
    if action == "untrash":
        for i in ids:
            svc.users().messages().untrash(userId="me", id=i).execute()
        return {"changed": len(ids), "action": "untrash"}
    body = MESSAGE_ACTIONS.get(action)
    if not body:
        raise HTTPException(400, f"Don't know how to '{action}'.")
    svc.users().messages().batchModify(
        userId="me", body=dict(body, ids=ids)).execute()
    return {"changed": len(ids), "action": action}


def _walk_parts(payload, out):
    for p in payload.get("parts", []) or []:
        fn = p.get("filename") or ""
        bid = (p.get("body") or {}).get("attachmentId")
        if fn and bid:
            out.append({"filename": fn,
                        "mime": p.get("mimeType", ""),
                        "bytes": (p.get("body") or {}).get("size", 0),
                        "id": bid})
        _walk_parts(p, out)
    return out


def tool_attachments(account_id: int, msg_id: str, which: str = "") -> dict:
    """What is actually attached to a message."""
    svc = gmail_client(account_id, which)
    m = svc.users().messages().get(
        userId="me", id=msg_id, format="full").execute()
    return {"attachments": _walk_parts(m.get("payload", {}), [])}


def document_text(raw: bytes, limit: int = 12000) -> dict:
    """Words out of a file, whatever it came from: a PDF, a Word document
    or plain text. Anything else is said to be unreadable rather than read
    out as gibberish."""
    import io as _io
    import re
    import html
    if raw[:5].startswith(b"%PDF"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(_io.BytesIO(raw))
            text = " ".join((p.extract_text() or "") for p in reader.pages)
            return {"text": " ".join(text.split())[:limit], "kind": "pdf"}
        except Exception as e:
            return {"text": "", "error": f"could not read the PDF: "
                                         f"{str(e)[:120]}"}
    if raw[:2] == b"PK":
        # .docx is a zip with the words in word/document.xml
        try:
            import zipfile
            with zipfile.ZipFile(_io.BytesIO(raw)) as z:
                xml = z.read("word/document.xml").decode("utf-8", "ignore")
            xml = re.sub(r"</w:p>", "\n", xml)
            text = html.unescape(re.sub(r"<[^>]+>", "", xml))
            return {"text": " ".join(text.split())[:limit], "kind": "word"}
        except Exception:
            return {"text": "", "error": "that kind of file can't be read "
                                         "aloud"}
    sample = raw[:2000]
    if sample and sum(b < 9 or 13 < b < 32 for b in sample) > len(sample) // 20:
        return {"text": "", "error": "that kind of file can't be read aloud"}
    return {"text": " ".join(raw.decode("utf-8", "ignore").split())[:limit],
            "kind": "text"}


def tool_attachment_text(account_id: int, msg_id: str, attachment_id: str,
                         which: str = "", limit: int = 12000) -> dict:
    """Read an attached document."""
    svc = gmail_client(account_id, which)
    a = svc.users().messages().attachments().get(
        userId="me", messageId=msg_id, id=attachment_id).execute()
    return document_text(base64.urlsafe_b64decode(a.get("data", "")), limit)


def tool_search_email(account_id: int, query: str, limit: int = 5,
                      which: str = "", newest_first: bool = False) -> dict:
    """Search the whole mailbox, not just unread.

    Gmail orders search results by its own relevance, which is not the same
    as by date - asking for "the five most recent" and reading them back in
    Gmail's order gave people a list that wasn't newest first."""
    svc = gmail_client(account_id, which)
    res = svc.users().messages().list(
        userId="me", q=query, maxResults=limit).execute()
    items = []
    for m in res.get("messages", []):
        d = svc.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"]).execute()
        h = {x["name"]: x["value"] for x in d["payload"].get("headers", [])}
        items.append({
            "id": m["id"],
            "from": h.get("From", ""),
            "to": h.get("To", ""),
            "subject": h.get("Subject", "(no subject)"),
            "when": _when(d.get("internalDate")),
            "at_ms": int(d.get("internalDate") or 0),
            "category": _category(d.get("labelIds") or []),
            "unread": "UNREAD" in (d.get("labelIds") or []),
            "snippet": d.get("snippet", "")[:200],
        })
    if newest_first:
        items.sort(key=lambda x: -x["at_ms"])
    return {"found": len(items), "messages": items}


CODE_MAIL = _re_scrub.compile(
    r"(?i)(verification|security|one.?time|sign.?in|log.?in|confirm)")
CODE_DIGITS = _re_scrub.compile(r"(?<![\d-])(\d{4,8})(?![\d-])")


def code_from_email(account_id: int, site: str, since_ms: int,
                    which: str = "") -> str:
    """The code a site just emailed, read from the customer's own inbox.

    Callers were being asked to find a message they cannot see: no screen,
    often no text messages. If the code came by email and we already have
    permission to read that mailbox, there is nothing to ask them for.
    Only messages that arrived AFTER the sign-in started are considered,
    so an old code is never reused. The code itself is never logged."""
    site_word = (site or "").split(".")[0].lower()
    try:
        found = tool_search_email(
            account_id, "newer_than:1d in:anywhere", limit=8, which=which,
            newest_first=True)
    except Exception:
        return ""
    for m in found.get("messages", []):
        if int(m.get("at_ms") or 0) < since_ms - 60000:
            continue
        who = (m.get("from") or "").lower()
        subject = m.get("subject", "") or ""
        snippet = m.get("snippet", "") or ""
        if site_word and site_word not in who and site_word not in \
                subject.lower():
            continue
        if not CODE_MAIL.search(subject + " " + snippet):
            continue
        hit = CODE_DIGITS.search(subject) or CODE_DIGITS.search(snippet)
        if hit:
            return hit.group(1)
    return ""


def tool_find_contact(account_id: int, name: str, which: str = "") -> dict:
    """Find someone's email address: their Google Contacts first, then
    anyone they've emailed with."""
    seen = {}
    try:
        for c in tool_contacts_search(account_id, name, which)["contacts"]:
            for e in c["emails"]:
                seen[e.lower()] = c["name"] or e
    except Exception:
        pass    # no Contacts permission yet - past emails still work
    if seen:
        return {"matches": [{"name": v, "email": k}
                            for k, v in seen.items()][:5],
                "source": "contacts"}
    svc = gmail_client(account_id, which)
    for q in (f"from:{name}", f"to:{name}", name):
        try:
            res = svc.users().messages().list(
                userId="me", q=q, maxResults=10).execute()
        except Exception:
            continue
        for m in res.get("messages", []):
            d = svc.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["From", "To"]).execute()
            h = {x["name"]: x["value"] for x in d["payload"].get("headers", [])}
            for field in ("From", "To"):
                for chunk in (h.get(field, "") or "").split(","):
                    chunk = chunk.strip()
                    if "@" not in chunk:
                        continue
                    if "<" in chunk:
                        label = chunk.split("<")[0].strip().strip('"')
                        addr = chunk.split("<")[1].rstrip(">").strip()
                    else:
                        label, addr = "", chunk
                    if name.lower() in (label + " " + addr).lower():
                        seen[addr.lower()] = label or addr
        if seen:
            break
    return {"matches": [{"name": v, "email": k} for k, v in seen.items()][:5]}



def google_client(account_id: int, api: str, version: str, which: str = ""):
    """Same credentials, different Google API."""
    conn, _ = pick_connection(account_id, which)
    if not conn:
        raise HTTPException(400, "This account has no Google account linked.")
    tok = vault_get(conn["blob"])
    creds = Credentials(
        token=tok.get("token"),
        refresh_token=tok.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=None,    # see STORED_TOKEN_SCOPES
    )
    return build(api, version, credentials=creds, cache_discovery=False)


# ---------------------------------------------- contacts, drive and tasks
# Each of these needs its own permission. Anyone connected before they
# were added hasn't granted it, and Google refuses the call. That comes
# back to the assistant as reason "needs_reconnect" (see google_refused),
# never as a crash.

PERSON_FIELDS = "names,emailAddresses,phoneNumbers,addresses,birthdays"


def _person(p: dict) -> dict:
    names = p.get("names") or [{}]
    bday = ((p.get("birthdays") or [{}])[0].get("date") or {})
    out = {
        "id": p.get("resourceName", ""),
        "name": names[0].get("displayName", ""),
        "emails": [e.get("value") for e in p.get("emailAddresses") or []
                   if e.get("value")],
        "phones": [{"number": n.get("value"), "type": n.get("type", "")}
                   for n in p.get("phoneNumbers") or [] if n.get("value")],
        "address": ((p.get("addresses") or [{}])[0]
                    .get("formattedValue", "")).replace("\n", ", "),
    }
    if bday.get("month") and bday.get("day"):
        import calendar as _calendar
        out["birthday"] = f"{_calendar.month_name[bday['month']]} {bday['day']}"
    return out


def tool_contacts_search(account_id: int, name: str, which: str = "") -> dict:
    """Someone in their Google Contacts: numbers, email, address, birthday."""
    svc = google_client(account_id, "people", "v1", which)
    # Google's documented quirk: the search index is only brought up to
    # date by a request with an empty query, so send one first.
    svc.people().searchContacts(query="", readMask="names").execute()
    res = svc.people().searchContacts(query=name, readMask=PERSON_FIELDS,
                                      pageSize=10).execute()
    found = [_person(r.get("person", {})) for r in res.get("results", [])]
    if not found:
        # the index can lag behind a contact added minutes ago
        want = name.lower().split()
        page_token = None
        for _ in range(5):
            res = svc.people().connections().list(
                resourceName="people/me", personFields=PERSON_FIELDS,
                pageSize=1000, pageToken=page_token).execute()
            for p in res.get("connections", []):
                person = _person(p)
                if want and all(w in person["name"].lower() for w in want):
                    found.append(person)
            page_token = res.get("nextPageToken")
            if not page_token:
                break
    return {"found": len(found), "contacts": found[:5]}


def tool_contact_add(account_id: int, name: str, phone: str = "",
                     email: str = "", which: str = "") -> dict:
    svc = google_client(account_id, "people", "v1", which)
    body = {"names": [{"unstructuredName": name.strip()}]}
    if phone.strip():
        body["phoneNumbers"] = [{"value": phone.strip()}]
    if email.strip():
        body["emailAddresses"] = [{"value": email.strip()}]
    p = svc.people().createContact(
        body=body, personFields="names,emailAddresses,phoneNumbers").execute()
    return {"saved": True, "contact": _person(p)}


# what each kind of Drive file is called out loud, and how to get words out
DRIVE_KINDS = {
    "application/vnd.google-apps.document": ("Google Doc", "text/plain"),
    "application/vnd.google-apps.spreadsheet": ("spreadsheet", "text/csv"),
    "application/vnd.google-apps.presentation": ("slide show", "text/plain"),
    "application/pdf": ("PDF", ""),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        ("Word document", ""),
    "text/plain": ("text file", ""),
    "text/csv": ("spreadsheet", ""),
}
DRIVE_MAX_BYTES = 15 * 1024 * 1024


def _drive_kind(mime: str) -> str:
    if mime in DRIVE_KINDS:
        return DRIVE_KINDS[mime][0]
    for start, word in (("image/", "picture"), ("video/", "video"),
                        ("audio/", "recording")):
        if mime.startswith(start):
            return word
    return "file"


def _google_time(stamp: str):
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except Exception:
        return None


def tool_drive_search(account_id: int, words: str = "", limit: int = 5,
                      which: str = "") -> dict:
    """Files in their Google Drive, including ones shared with them. By
    name first, newest first; if nothing is called that, by what's inside."""
    svc = google_client(account_id, "drive", "v3", which)
    fields = ("files(id,name,mimeType,modifiedTime,size,"
              "owners(displayName),sharingUser(displayName))")
    base = ("trashed = false and "
            "mimeType != 'application/vnd.google-apps.folder'")
    w = (words or "").replace("\\", " ").replace("'", "\\'").strip()
    limit = max(1, min(int(limit or 5), 10))
    if w:
        files = svc.files().list(
            q=f"{base} and name contains '{w}'", orderBy="modifiedTime desc",
            pageSize=limit, fields=fields).execute().get("files", [])
        if not files:
            # Drive can't sort a search of the contents
            files = svc.files().list(
                q=f"{base} and fullText contains '{w}'", pageSize=limit,
                fields=fields).execute().get("files", [])
    else:
        files = svc.files().list(q=base, orderBy="modifiedTime desc",
                                 pageSize=limit,
                                 fields=fields).execute().get("files", [])
    out = []
    for f in files:
        mime = f.get("mimeType", "")
        who = ((f.get("sharingUser") or {}).get("displayName")
               or ((f.get("owners") or [{}])[0].get("displayName", "")))
        out.append({"id": f.get("id"), "name": f.get("name", ""),
                    "kind": _drive_kind(mime),
                    "changed": local_str(_google_time(
                        f.get("modifiedTime", "")), "day"),
                    "from": who, "readable": mime in DRIVE_KINDS})
    return {"found": len(out), "files": out}


def tool_drive_read(account_id: int, file_id: str, which: str = "",
                    limit: int = 12000) -> dict:
    svc = google_client(account_id, "drive", "v3", which)
    meta = svc.files().get(fileId=file_id,
                           fields="id,name,mimeType,size").execute()
    mime, name = meta.get("mimeType", ""), meta.get("name", "")
    if mime not in DRIVE_KINDS:
        return {"name": name, "text": "",
                "error": f"a {_drive_kind(mime)} can't be read aloud"}
    if int(meta.get("size") or 0) > DRIVE_MAX_BYTES:
        return {"name": name, "text": "", "error": "that file is too big to "
                                                   "read aloud"}
    export = DRIVE_KINDS[mime][1]
    if mime.startswith("application/vnd.google-apps."):
        raw = svc.files().export(fileId=file_id, mimeType=export).execute()
    else:
        raw = svc.files().get_media(fileId=file_id).execute()
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    out = document_text(raw, limit)
    out["name"] = name
    return out


# ---- changing Drive files
# The caller can't see the screen, so: nothing here deletes, nothing here
# shares, and the assistant reads every change back before calling these.
# Google Docs and Sheets keep their own version history, so a wrong edit
# can be put back from the file's history.

DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
SLIDES = "application/vnd.google-apps.presentation"
# files Google can turn into its own editable kind when it copies them
CONVERTIBLE = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        DOC,
    "application/msword": DOC,
    "application/rtf": DOC,
    "text/plain": DOC,
    "application/pdf": DOC,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": SHEET,
    "application/vnd.ms-excel": SHEET,
    "text/csv": SHEET,
}
SEND_MAX_BYTES = 18 * 1024 * 1024


def _drive_meta(svc, file_id: str) -> dict:
    return svc.files().get(fileId=file_id,
                           fields="id,name,mimeType,size").execute()


def _must_be(meta: dict, want: str):
    """Only Google's own Docs and Sheets can be edited in place. Anything
    else gets a reason code: not_editable means a copy can be made."""
    mime = meta.get("mimeType", "")
    if mime == want:
        return
    if CONVERTIBLE.get(mime) == want:
        raise HTTPException(409, "not_editable")
    raise HTTPException(409, "wrong_kind")


def _made(f: dict) -> dict:
    mime = f.get("mimeType", "")
    return {"id": f.get("id"), "name": f.get("name", ""),
            "kind": _drive_kind(mime), "from": "", "changed": "just now",
            "readable": mime in DRIVE_KINDS}


def _upload(svc, name: str, data: bytes, source_mime: str, as_mime: str):
    from googleapiclient.http import MediaIoBaseUpload
    import io as _io
    media = MediaIoBaseUpload(_io.BytesIO(data), mimetype=source_mime,
                              resumable=False)
    return svc.files().create(
        body={"name": (name or "Untitled").strip()[:200],
              "mimeType": as_mime},
        media_body=media, fields="id,name,mimeType").execute()


def tool_doc_create(account_id: int, title: str, text: str,
                    which: str = "") -> dict:
    svc = google_client(account_id, "drive", "v3", which)
    f = _upload(svc, title, (text or "").encode("utf-8"), "text/plain", DOC)
    return {"created": True, "file": _made(f)}


def tool_sheet_create(account_id: int, title: str, columns: list,
                      rows: list, which: str = "") -> dict:
    import csv
    import io as _io
    buf = _io.StringIO()
    w = csv.writer(buf)
    if columns:
        w.writerow(columns)
    for r in rows or []:
        w.writerow(r)
    svc = google_client(account_id, "drive", "v3", which)
    f = _upload(svc, title, buf.getvalue().encode("utf-8"), "text/csv", SHEET)
    return {"created": True, "file": _made(f)}


def tool_doc_add(account_id: int, file_id: str, text: str,
                 which: str = "") -> dict:
    drive = google_client(account_id, "drive", "v3", which)
    meta = _drive_meta(drive, file_id)
    _must_be(meta, DOC)
    docs = google_client(account_id, "docs", "v1", which)
    docs.documents().batchUpdate(documentId=file_id, body={"requests": [
        {"insertText": {"endOfSegmentLocation": {},
                        "text": "\n" + (text or "").strip()}}]}).execute()
    return {"changed": True, "name": meta.get("name")}


def tool_doc_replace(account_id: int, file_id: str, find: str,
                     replace_with: str, all_of_them: bool = False,
                     which: str = "") -> dict:
    """Change some words in a Google Doc. If the words appear more than
    once, say how many times and change nothing unless all_of_them."""
    find = (find or "").strip()
    if not find:
        raise HTTPException(400, "What words should be changed?")
    drive = google_client(account_id, "drive", "v3", which)
    meta = _drive_meta(drive, file_id)
    _must_be(meta, DOC)
    raw = drive.files().export(fileId=file_id, mimeType="text/plain").execute()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "ignore")
    found = " ".join(raw.split()).lower().count(" ".join(find.split()).lower())
    if found == 0:
        return {"changed": False, "found": 0, "name": meta.get("name")}
    if found > 1 and not all_of_them:
        return {"changed": False, "found": found, "name": meta.get("name")}
    docs = google_client(account_id, "docs", "v1", which)
    res = docs.documents().batchUpdate(documentId=file_id, body={"requests": [
        {"replaceAllText": {"containsText": {"text": find, "matchCase": False},
                            "replaceText": replace_with or ""}}]}).execute()
    n = sum((r.get("replaceAllText") or {}).get("occurrencesChanged", 0)
            for r in res.get("replies", []))
    return {"changed": n > 0, "found": found, "times": n,
            "name": meta.get("name")}


def _col_letters(n: int) -> str:
    """1 -> A, 27 -> AA."""
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _tab_range(tab: str) -> str:
    return "'" + tab.replace("'", "''") + "'"


def _sheet_values(account_id: int, file_id: str, which: str = ""):
    drive = google_client(account_id, "drive", "v3", which)
    meta = _drive_meta(drive, file_id)
    _must_be(meta, SHEET)
    sheets = google_client(account_id, "sheets", "v4", which)
    info = sheets.spreadsheets().get(
        spreadsheetId=file_id, fields="sheets.properties.title").execute()
    tab = info["sheets"][0]["properties"]["title"]
    vals = (sheets.spreadsheets().values()
            .get(spreadsheetId=file_id, range=_tab_range(tab)).execute()
            .get("values", []))
    return meta, sheets, tab, vals


def tool_sheet_read(account_id: int, file_id: str, which: str = "",
                    limit: int = 60) -> dict:
    meta, _, tab, vals = _sheet_values(account_id, file_id, which)
    header = vals[0] if vals else []
    rows = [{"row": i + 2, "cells": r} for i, r in enumerate(vals[1:])
            if any(str(c).strip() for c in r)]
    return {"name": meta.get("name"), "tab": tab, "columns": header,
            "rows": rows[:limit], "total_rows": len(rows)}


def _column_number(header: list, column: str) -> int:
    """The caller says "phone", not "column C". Match the heading; a bare
    letter like "C" is accepted too. 0 when nothing matches or it's
    ambiguous."""
    said = (column or "").strip()
    want = said.lower()
    if not want:
        return 0
    names = [str(h).strip().lower() for h in header]
    if want in names:
        return names.index(want) + 1
    if said.isalpha() and said.isupper() and len(said) <= 2:
        n = 0
        for ch in said:
            n = n * 26 + (ord(ch) - 64)
        return n
    close = [i for i, h in enumerate(names)
             if h and (want in h or h in want)]
    return close[0] + 1 if len(close) == 1 else 0


def tool_sheet_add_row(account_id: int, file_id: str, values: list,
                       which: str = "") -> dict:
    meta, sheets, tab, _ = _sheet_values(account_id, file_id, which)
    sheets.spreadsheets().values().append(
        spreadsheetId=file_id, range=_tab_range(tab) + "!A1",
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body={"values": [values]}).execute()
    return {"changed": True, "name": meta.get("name")}


def tool_sheet_update(account_id: int, file_id: str, row: int, column: str,
                      value: str, which: str = "") -> dict:
    meta, sheets, tab, vals = _sheet_values(account_id, file_id, which)
    header = vals[0] if vals else []
    col = _column_number(header, column)
    if not col:
        raise HTTPException(400, f"no_such_column: the columns are "
                                 f"{', '.join(map(str, header)) or 'unnamed'}")
    if row < 1:
        raise HTTPException(400, "no_such_row")
    old = ""
    if row - 1 < len(vals) and col - 1 < len(vals[row - 1]):
        old = vals[row - 1][col - 1]
    cell = f"{_tab_range(tab)}!{_col_letters(col)}{row}"
    sheets.spreadsheets().values().update(
        spreadsheetId=file_id, range=cell, valueInputOption="USER_ENTERED",
        body={"values": [[value]]}).execute()
    return {"changed": True, "name": meta.get("name"), "was": old,
            "column": header[col - 1] if col - 1 < len(header) else "",
            "row": row}


def tool_drive_editable_copy(account_id: int, file_id: str,
                             which: str = "") -> dict:
    """A Word, Excel or PDF file can't be edited in place. Google can copy
    it into its own Doc or Sheet; the original stays exactly as it was."""
    drive = google_client(account_id, "drive", "v3", which)
    meta = _drive_meta(drive, file_id)
    target = CONVERTIBLE.get(meta.get("mimeType", ""))
    if not target:
        raise HTTPException(409, "wrong_kind")
    name = meta.get("name", "Copy")
    base = name.rsplit(".", 1)[0] if "." in name[-6:] else name
    f = drive.files().copy(fileId=file_id,
                           body={"name": base + " (editable)",
                                 "mimeType": target},
                           fields="id,name,mimeType").execute()
    return {"created": True, "file": _made(f), "original": name}


def _as_pdf(drive, meta: dict):
    mime = meta.get("mimeType", "")
    if mime in (DOC, SHEET, SLIDES):
        data = drive.files().export(fileId=meta["id"],
                                    mimeType="application/pdf").execute()
        return data, meta.get("name", "document") + ".pdf", "application/pdf"
    if mime.startswith("application/vnd.google-apps."):
        raise HTTPException(409, "wrong_kind")
    if int(meta.get("size") or 0) > SEND_MAX_BYTES:
        raise HTTPException(409, "too_big")
    return (drive.files().get_media(fileId=meta["id"]).execute(),
            meta.get("name", "file"), mime or "application/octet-stream")


def tool_drive_save_pdf(account_id: int, file_id: str,
                        which: str = "") -> dict:
    drive = google_client(account_id, "drive", "v3", which)
    meta = _drive_meta(drive, file_id)
    if meta.get("mimeType") not in (DOC, SHEET, SLIDES):
        raise HTTPException(409, "wrong_kind")
    data, fname, _ = _as_pdf(drive, meta)
    from googleapiclient.http import MediaIoBaseUpload
    import io as _io
    f = drive.files().create(
        body={"name": fname},
        media_body=MediaIoBaseUpload(_io.BytesIO(data),
                                     mimetype="application/pdf"),
        fields="id,name,mimeType").execute()
    return {"created": True, "file": _made(f)}


def tool_email_drive_file(account_id: int, file_id: str, to: str,
                          note: str = "", which: str = "") -> dict:
    """Send a Drive file as an attachment. Google Docs and Sheets go as a
    PDF, which anyone can open."""
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email import encoders
    drive = google_client(account_id, "drive", "v3", which)
    meta = _drive_meta(drive, file_id)
    data, fname, ctype = _as_pdf(drive, meta)
    if len(data) > SEND_MAX_BYTES:
        raise HTTPException(409, "too_big")
    msg = MIMEMultipart()
    msg["to"] = to
    msg["subject"] = meta.get("name", fname)
    msg.attach(MIMEText((note or "").strip() or f"Attached: {fname}"))
    main_type, _, sub_type = ctype.partition("/")
    part = MIMEBase(main_type or "application", sub_type or "octet-stream")
    part.set_payload(data)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=fname)
    msg.attach(part)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    sent = gmail_client(account_id, which).users().messages().send(
        userId="me", body={"raw": raw}).execute()
    return {"sent": True, "id": sent.get("id"), "to": to, "file": fname}


def _spoken_date(day: str) -> str:
    try:
        d = datetime.fromisoformat(day[:10])
        return f"{d:%A}, {d:%B} {d.day}"
    except Exception:
        return ""


def tool_tasks_list(account_id: int, which: str = "") -> dict:
    """Their to-do list: what's not done yet, the ones with a date first."""
    svc = google_client(account_id, "tasks", "v1", which)
    res = svc.tasks().list(tasklist="@default", showCompleted=False,
                           showHidden=False, maxResults=50).execute()
    items = []
    for t in res.get("items", []):
        if not (t.get("title") or "").strip():
            continue
        due = (t.get("due") or "")[:10]
        items.append({"id": t.get("id"), "title": t.get("title", ""),
                      "notes": (t.get("notes") or "")[:300],
                      "due": due, "due_spoken": _spoken_date(due)})
    items.sort(key=lambda t: (not t["due"], t["due"]))
    return {"count": len(items), "tasks": items}


def tool_task_add(account_id: int, title: str, due_date: str = "",
                  notes: str = "", which: str = "") -> dict:
    svc = google_client(account_id, "tasks", "v1", which)
    body = {"title": title.strip()[:500]}
    if notes.strip():
        body["notes"] = notes.strip()[:2000]
    if due_date.strip():
        # Google Tasks keeps only the date; the time part is ignored
        body["due"] = f"{due_date.strip()[:10]}T00:00:00.000Z"
    t = svc.tasks().insert(tasklist="@default", body=body).execute()
    return {"added": True, "id": t.get("id"), "title": t.get("title"),
            "due_spoken": _spoken_date(due_date)}


def tool_task_done(account_id: int, task_id: str, done: bool = True,
                   which: str = "") -> dict:
    svc = google_client(account_id, "tasks", "v1", which)
    t = svc.tasks().patch(
        tasklist="@default", task=task_id,
        body={"status": "completed" if done else "needsAction"}).execute()
    return {"done": t.get("status") == "completed", "title": t.get("title")}


def _cal(account_id: int, which: str = ""):
    return google_client(account_id, "calendar", "v3", which)


def tool_list_events(account_id: int, days: int = 1, which: str = "") -> dict:
    """Upcoming events over the next N days."""
    svc = _cal(account_id, which)
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days)
    res = svc.events().list(
        calendarId="primary",
        timeMin=now.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True, orderBy="startTime", maxResults=20).execute()

    items = []
    for e in res.get("items", []):
        start = e.get("start", {})
        items.append({
            "id": e.get("id"),
            "title": e.get("summary", "(no title)"),
            "start": start.get("dateTime") or start.get("date"),
            "all_day": "date" in start,
            "location": e.get("location", ""),
        })
    return {"count": len(items), "events": items}


def tool_create_event(account_id: int, title: str, start_iso: str,
                      minutes: int = 60, location: str = "",
                      notes: str = "") -> dict:
    """Create an event. start_iso like 2026-09-10T14:00:00."""
    svc = _cal(account_id)
    tz = (svc.settings().get(setting="timezone").execute()
          .get("value", "America/New_York"))
    start = datetime.fromisoformat(start_iso.replace("Z", ""))
    end = start + timedelta(minutes=minutes)
    body = {
        "summary": title,
        "location": location,
        "description": notes,
        "start": {"dateTime": start.isoformat(), "timeZone": tz},
        "end": {"dateTime": end.isoformat(), "timeZone": tz},
    }
    ev = svc.events().insert(calendarId="primary", body=body).execute()
    return {"created": True, "id": ev.get("id"),
            "title": title, "start": start.isoformat()}


def tool_cancel_event(account_id: int, event_id: str) -> dict:
    svc = _cal(account_id)
    svc.events().delete(calendarId="primary", eventId=event_id).execute()
    return {"cancelled": True}


def tool_find_free(account_id: int, date_iso: str,
                   minutes: int = 60) -> dict:
    """Open slots on a given date, 9am-6pm."""
    svc = _cal(account_id)
    tz = (svc.settings().get(setting="timezone").execute()
          .get("value", "America/New_York"))
    day = datetime.fromisoformat(date_iso[:10])
    start = day.replace(hour=9, minute=0)
    end = day.replace(hour=18, minute=0)

    res = svc.freebusy().query(body={
        "timeMin": start.isoformat() + "Z",
        "timeMax": end.isoformat() + "Z",
        "timeZone": tz,
        "items": [{"id": "primary"}],
    }).execute()

    busy = []
    for b in res["calendars"]["primary"].get("busy", []):
        busy.append((
            datetime.fromisoformat(b["start"].replace("Z", "")),
            datetime.fromisoformat(b["end"].replace("Z", "")),
        ))
    busy.sort()

    slots, cursor = [], start
    for bs, be in busy:
        if (bs - cursor).total_seconds() >= minutes * 60:
            slots.append(cursor.strftime("%-I:%M %p"))
        cursor = max(cursor, be)
    if (end - cursor).total_seconds() >= minutes * 60:
        slots.append(cursor.strftime("%-I:%M %p"))

    return {"date": day.strftime("%A %B %-d"), "free": slots[:8]}



BLOCKED_TERMS = {
    "sex", "sexual", "sexy", "porn", "pornography", "nude", "nudity", "naked",
    "erotic", "explicit", "intimacy", "intimate", "arousal", "arousing",
    "adultery", "affair", "underwear", "lingerie", "bikini", "puberty",
    "fertility", "dating", "tinder", "hookup", "romance", "romantic",
    "marriage counseling", "relationship advice",
    # Jewish religious subjects are ALLOWED - this service is for Jewish
    # callers. What stays blocked is weighing faiths against each other.
    # Only unambiguous phrases here: single words like "church" or
    # "christian" would block a Brooklyn street name or somebody's name.
    # These must be phrases nobody says innocently. "which religion" is
    # NOT one: "which religion is the name Raizi from" is etymology, and
    # blocking it refused a caller twice.
    "other religions", "compare religions", "religions compared",
    "other faiths", "which religion is right", "which religion is true",
    "which religion is the true", "which religion is better",
    "best religion", "true religion",
    "gossip", "celebrity", "gossip column",
    "addiction", "drugs", "rehab",
    "joke", "jokes", "humor", "funny",
    "news", "headlines", "sports", "score", "game", "movie", "movies",
    "netflix", "tv show", "music video", "entertainment",
}


def blocked_terms_in(text: str) -> set:
    """Which forbidden words actually appear. Counting them matters: one
    stray word in a web snippet is not the same as a page about it."""
    t = " " + (text or "").lower().replace("-", " ") + " "
    return {term for term in BLOCKED_TERMS
            if f" {term} " in t or t.strip() == term}


def is_blocked(text: str) -> bool:
    """For something the CALLER said, or an answer we are about to read
    out. One word is enough here - they chose those words."""
    return bool(blocked_terms_in(text))


# What a tool returns when a blocked topic comes up. The browser path has to
# refuse exactly like web search does - the rule is not the prompt's job.
BLOCKED_REPLY = ("BLOCKED. Say exactly: I am not allowed to talk to you "
                 "about this. Nothing else.")


def _search_serper(q: str) -> dict:
    payload = json.dumps({"q": q, "num": 5}).encode()
    req = urllib.request.Request(
        "https://google.serper.dev/search", data=payload,
        headers={"X-API-KEY": SERPER_API_KEY,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode())

    answer = ""
    if d.get("answerBox"):
        ab = d["answerBox"]
        answer = ab.get("answer") or ab.get("snippet") or ""
    elif d.get("knowledgeGraph"):
        kg = d["knowledgeGraph"]
        bits = [kg.get("title", ""), kg.get("description", "")]
        for k in ("address", "phone", "hours", "website"):
            if kg.get(k):
                bits.append(f"{k}: {kg[k]}")
        answer = ". ".join(b for b in bits if b)

    results = [{"title": x.get("title", ""),
                "snippet": (x.get("snippet") or "")[:300],
                "url": x.get("link", "")}
               for x in d.get("organic", [])[:4]]

    for p in d.get("places", [])[:3]:
        results.append({
            "title": p.get("title", ""),
            "snippet": " ".join(filter(None, [
                p.get("address", ""),
                f"phone {p['phoneNumber']}" if p.get("phoneNumber") else "",
            ]))[:300],
            "url": "",
        })

    return {"answer": answer[:800], "results": results}


def _search_tavily(q: str) -> dict:
    payload = json.dumps({
        "api_key": TAVILY_API_KEY, "query": q,
        "search_depth": "basic", "include_answer": True, "max_results": 4,
    }).encode()
    req = urllib.request.Request(
        "https://api.tavily.com/search", data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode())
    return {
        "answer": (d.get("answer") or "")[:800],
        "results": [{"title": x.get("title", ""),
                     "snippet": (x.get("content") or "")[:300],
                     "url": x.get("url", "")}
                    for x in d.get("results", [])[:4]],
    }


def tool_web_search(query: str, near: str = "") -> dict:
    """Google-backed web search with a content filter."""
    if is_blocked(query):
        return {"blocked": True,
                "answer": "I am not allowed to talk to you about this.",
                "results": []}

    q = f"{query} near {near}" if near else query
    try:
        if SERPER_API_KEY:
            out = _search_serper(q)
        elif TAVILY_API_KEY:
            out = _search_tavily(q)
        else:
            return {"answer": "Web search isn't configured.", "results": []}
    except Exception as e:
        return {"answer": f"Search failed: {e}", "results": []}

    # The direct answer is read out, so judge it as strictly as speech.
    if is_blocked(out.get("answer", "")):
        return {"blocked": True,
                "answer": "I am not allowed to talk to you about this.",
                "results": []}
    # Snippets are scraped web text and full of stray words. Refusing a
    # whole search because one result said "news" blocked a question about
    # security assessors. Two different forbidden words means the results
    # really are about something we don't discuss; one means nothing.
    snippets = " ".join(r.get("snippet", "") + " " + r.get("title", "")
                        for r in out.get("results", []))
    if len(blocked_terms_in(snippets)) >= 2:
        return {"blocked": True,
                "answer": "I am not allowed to talk to you about this.",
                "results": []}
    return out



def _digits_e164(number: str) -> str:
    d = "".join(ch for ch in (number or "") if ch.isdigit())
    if len(d) == 10:
        d = "1" + d
    return "+" + d


def tool_send_sms(to: str, message: str) -> dict:
    """Send a text message. Provider set by SMS_PROVIDER."""
    to = _digits_e164(to)
    if not SMS_PROVIDER or not SMS_FROM:
        return {"sent": False, "error": "SMS isn't configured."}

    try:
        if SMS_PROVIDER == "twilio":
            body = urllib.parse.urlencode({
                "To": to, "From": _digits_e164(SMS_FROM), "Body": message[:1500],
            }).encode()
            auth = _b64.b64encode(
                f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
            req = urllib.request.Request(
                f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}"
                f"/Messages.json",
                data=body,
                headers={"Authorization": f"Basic {auth}",
                         "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode())
            return {"sent": True, "id": d.get("sid")}

        if SMS_PROVIDER == "telnyx":
            body = {"from": _digits_e164(SMS_FROM), "to": to,
                    "text": message[:1500]}
            if TELNYX_PROFILE_ID:
                body["messaging_profile_id"] = TELNYX_PROFILE_ID
            req = urllib.request.Request(
                "https://api.telnyx.com/v2/messages",
                data=json.dumps(body).encode(),
                headers={"Authorization": f"Bearer {TELNYX_API_KEY}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode())
            return {"sent": True, "id": d.get("data", {}).get("id", "")}

        if SMS_PROVIDER == "bulkvs":
            payload = json.dumps({
                "From": _digits_e164(SMS_FROM).lstrip("+"),
                "To": [to.lstrip("+")],
                "Message": message[:1500],
            }).encode()  # BulkVS: To is a list, numbers without +
            auth = _b64.b64encode(
                f"{BULKVS_USER}:{BULKVS_PASS}".encode()).decode()
            req = urllib.request.Request(
                "https://portal.bulkvs.com/api/v1.0/messageSend",
                data=payload,
                headers={"Authorization": f"Basic {auth}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode())
            return {"sent": True, "raw": str(d)[:200]}

        return {"sent": False, "error": f"Unknown provider {SMS_PROVIDER}"}
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:500]
        except Exception:
            detail = ""
        return {"sent": False, "error": f"HTTP {e.code}", "detail": detail}
    except Exception as e:
        return {"sent": False, "error": str(e)[:200]}


def tool_text_link(account_id: int, to: str) -> dict:
    """Text the customer their personal Gmail-linking link."""
    url = (f"{PUBLIC_URL}/link/start?t="
           + urllib.parse.quote(_make_link_token(account_id)))
    msg = ("Tap this link to connect your email to your phone assistant. "
           "It only takes a moment: " + url)
    return tool_send_sms(to, msg)



def mem_add(account_id: int, channel: str, who: str, text: str):
    if not account_id or not text:
        return
    db = Session()
    db.add(Memory(account_id=account_id, channel=channel,
                  who=who, text=text[:2000]))
    db.commit()
    db.close()


def mem_recent(account_id: int, limit: int = 20) -> list:
    db = Session()
    rows = (db.query(Memory).filter_by(account_id=account_id)
              .order_by(Memory.id.desc()).limit(limit).all())
    db.close()
    return [{"channel": r.channel, "who": r.who, "text": r.text,
             "at": local_str(r.at) if r.at else ""}
            for r in reversed(rows)]


def account_for_number(number: str):
    d = "".join(ch for ch in (number or "") if ch.isdigit())[-10:]
    db = Session()
    for p in db.query(PhoneNumber).all():
        if "".join(ch for ch in p.number if ch.isdigit())[-10:] == d:
            acct = db.query(Account).filter_by(id=p.account_id).first()
            db.close()
            return acct
    db.close()
    return None







# Phone country code -> (proxy country, preferred region). Longest prefix
# wins, so +1876 Jamaica beats the generic +1.
DIAL_MAP = {
    "1876": ("JM", ""), "1809": ("DO", ""), "1829": ("DO", ""),
    "1849": ("DO", ""), "1868": ("TT", ""), "1246": ("BB", ""),
    "1242": ("BS", ""), "1441": ("BM", ""), "1345": ("KY", ""),
    "1264": ("AI", ""), "1721": ("SX", ""), "1758": ("LC", ""),
    "1473": ("GD", ""), "1784": ("VC", ""), "1268": ("AG", ""),
    "1670": ("MP", ""), "1671": ("GU", ""), "1787": ("PR", ""),
    "1939": ("PR", ""), "1340": ("VI", ""), "1684": ("AS", ""),
    "1204": ("CA", ""), "1226": ("CA", ""), "1236": ("CA", ""),
    "1249": ("CA", ""), "1250": ("CA", ""), "1289": ("CA", ""),
    "1306": ("CA", ""), "1343": ("CA", ""), "1365": ("CA", ""),
    "1367": ("CA", ""), "1403": ("CA", ""), "1416": ("CA", ""),
    "1418": ("CA", ""), "1431": ("CA", ""), "1437": ("CA", ""),
    "1438": ("CA", ""), "1450": ("CA", ""), "1506": ("CA", ""),
    "1514": ("CA", ""), "1519": ("CA", ""), "1548": ("CA", ""),
    "1579": ("CA", ""), "1581": ("CA", ""), "1587": ("CA", ""),
    "1604": ("CA", ""), "1613": ("CA", ""), "1639": ("CA", ""),
    "1647": ("CA", ""), "1672": ("CA", ""), "1705": ("CA", ""),
    "1709": ("CA", ""), "1778": ("CA", ""), "1780": ("CA", ""),
    "1782": ("CA", ""), "1807": ("CA", ""), "1819": ("CA", ""),
    "1825": ("CA", ""), "1867": ("CA", ""), "1873": ("CA", ""),
    "1902": ("CA", ""), "1905": ("CA", ""),
    "44": ("GB", ""), "353": ("IE", ""), "972": ("IL", ""),
    "61": ("AU", ""), "64": ("NZ", ""), "27": ("ZA", ""),
    "33": ("FR", ""), "49": ("DE", ""), "34": ("ES", ""),
    "39": ("IT", ""), "31": ("NL", ""), "32": ("BE", ""),
    "41": ("CH", ""), "43": ("AT", ""), "351": ("PT", ""),
    "46": ("SE", ""), "47": ("NO", ""), "45": ("DK", ""),
    "358": ("FI", ""), "48": ("PL", ""), "420": ("CZ", ""),
    "36": ("HU", ""), "30": ("GR", ""), "40": ("RO", ""),
    "380": ("UA", ""), "52": ("MX", ""), "55": ("BR", ""),
    "54": ("AR", ""), "56": ("CL", ""), "57": ("CO", ""),
    "51": ("PE", ""), "91": ("IN", ""), "63": ("PH", ""),
    "65": ("SG", ""), "60": ("MY", ""), "66": ("TH", ""),
    "62": ("ID", ""), "84": ("VN", ""), "81": ("JP", ""),
    "82": ("KR", ""), "852": ("HK", ""), "886": ("TW", ""),
    "971": ("AE", ""), "966": ("SA", ""), "90": ("TR", ""),
    "20": ("EG", ""), "234": ("NG", ""), "254": ("KE", ""),
    "1": ("US", PROXY_STATE),
}

# US area code -> state, so a New York caller browses from New York.
US_AREA_STATE = {
    "212": "NY", "315": "NY", "332": "NY", "347": "NY", "516": "NY",
    "518": "NY", "585": "NY", "607": "NY", "631": "NY", "646": "NY",
    "680": "NY", "716": "NY", "718": "NY", "838": "NY", "845": "NY",
    "914": "NY", "917": "NY", "929": "NY", "934": "NY",
    "201": "NJ", "551": "NJ", "609": "NJ", "640": "NJ", "732": "NJ",
    "848": "NJ", "856": "NJ", "862": "NJ", "908": "NJ", "973": "NJ",
    "203": "CT", "475": "CT", "860": "CT", "959": "CT",
    "215": "PA", "267": "PA", "412": "PA", "445": "PA", "484": "PA",
    "570": "PA", "610": "PA", "717": "PA", "724": "PA", "814": "PA",
    "878": "PA",
    "305": "FL", "321": "FL", "352": "FL", "386": "FL", "407": "FL",
    "561": "FL", "689": "FL", "727": "FL", "754": "FL", "772": "FL",
    "786": "FL", "813": "FL", "850": "FL", "863": "FL", "904": "FL",
    "941": "FL", "954": "FL",
    "213": "CA", "310": "CA", "323": "CA", "408": "CA", "415": "CA",
    "424": "CA", "510": "CA", "530": "CA", "559": "CA", "562": "CA",
    "619": "CA", "626": "CA", "650": "CA", "657": "CA", "661": "CA",
    "707": "CA", "714": "CA", "747": "CA", "760": "CA", "805": "CA",
    "818": "CA", "831": "CA", "858": "CA", "909": "CA", "916": "CA",
    "925": "CA", "949": "CA", "951": "CA",
    "312": "IL", "224": "IL", "331": "IL", "630": "IL", "708": "IL",
    "773": "IL", "779": "IL", "815": "IL", "847": "IL", "872": "IL",
    "214": "TX", "210": "TX", "281": "TX", "409": "TX", "469": "TX",
    "512": "TX", "682": "TX", "713": "TX", "737": "TX", "817": "TX",
    "832": "TX", "915": "TX", "936": "TX", "972": "TX",
    "404": "GA", "470": "GA", "678": "GA", "770": "GA", "706": "GA",
    "202": "DC", "410": "MD", "240": "MD", "301": "MD", "443": "MD",
    "617": "MA", "339": "MA", "351": "MA", "508": "MA", "774": "MA",
    "781": "MA", "857": "MA", "978": "MA",
    "216": "OH", "234": "OH", "330": "OH", "419": "OH", "440": "OH",
    "513": "OH", "614": "OH", "740": "OH", "937": "OH",
    "206": "WA", "253": "WA", "360": "WA", "425": "WA", "509": "WA",
    "303": "CO", "720": "CO", "970": "CO",
    "602": "AZ", "480": "AZ", "520": "AZ", "623": "AZ", "928": "AZ",
    "702": "NV", "725": "NV", "775": "NV",
    "704": "NC", "336": "NC", "252": "NC", "743": "NC", "910": "NC",
    "919": "NC", "980": "NC", "984": "NC",
    "313": "MI", "248": "MI", "269": "MI", "517": "MI", "586": "MI",
    "616": "MI", "734": "MI", "810": "MI", "947": "MI", "989": "MI",
}


def _where_for_phone(number: str):
    """Which country and state a browser should appear to be in for this
    caller. Falls back to the configured default."""
    digits = "".join(ch for ch in (number or "") if ch.isdigit())
    if not digits:
        return PROXY_COUNTRY, PROXY_STATE, ""
    for length in (4, 3, 2, 1):
        pre = digits[:length]
        if pre in DIAL_MAP:
            country, state = DIAL_MAP[pre]
            if country == "US":
                area = digits[1:4]
                state = US_AREA_STATE.get(area, PROXY_STATE)
            return country, state, ""
    return PROXY_COUNTRY, PROXY_STATE, ""


def _where_for_account(account_id):
    """Use the account's own phone number to decide where to browse from."""
    if not account_id:
        return PROXY_COUNTRY, PROXY_STATE, ""
    try:
        db = Session()
        pn = db.query(PhoneNumber).filter_by(account_id=account_id).first()
        db.close()
        if pn and pn.number:
            return _where_for_phone(pn.number)
    except Exception:
        pass
    return PROXY_COUNTRY, PROXY_STATE, ""


_LAST_PROXY_FLAG = {"at": None}



def _shape(secret: str) -> str:
    """Describe a password without revealing it: length and the pattern of
    character types. Lets us see a misheard password without storing one."""
    if not secret:
        return "(empty)"
    out = []
    for ch in secret:
        if ch.isupper():
            out.append("A")
        elif ch.islower():
            out.append("a")
        elif ch.isdigit():
            out.append("9")
        elif ch.isspace():
            out.append("_")
        else:
            out.append("#")
    return f"{len(secret)} chars, pattern {''.join(out)}"


PROXY_STATUS = {"proxies_enabled": None, "checked": None, "note": ""}


def _flag_proxy_unavailable(wanted: str):
    """Browserbase proxies aren't on this plan. Sessions still run from
    Browserbase's own datacenter, which is in the US."""
    PROXY_STATUS.update({"proxies_enabled": False,
                         "checked": datetime.utcnow(),
                         "note": "Browserbase returned 402 Payment Required"})
    emit("browser", "proxy", f"Proxies not enabled on the Browserbase plan. "
                             f"Wanted {wanted}; running from Browserbase's "
                             f"own US datacenter instead.", "warn")
    if wanted.upper() == "US":
        return              # US customers are unaffected, no alert needed
    now = datetime.utcnow()
    last = _LAST_PROXY_FLAG.get("at")
    if last and (now - last).total_seconds() < 3600:
        return
    _LAST_PROXY_FLAG["at"] = now
    msg = (f"A customer in {wanted} was browsed from a US address, because "
           f"proxies are not enabled on the Browserbase plan. Their sign-ins "
           f"will look foreign to Google. Turn on proxies in Browserbase to "
           f"fix this. US customers are not affected.")
    try:
        db = Session()
        db.add(Followup(reason="proxy_not_enabled", note=msg,
                        channel="system"))
        db.commit()
        db.close()
    except Exception:
        pass


_LAST_LIMIT_FLAG = {"at": None}


def _flag_account_limit(detail: str):
    """Browserbase refused a plain browser. Out of sessions, out of minutes,
    or a billing problem - nothing in this code can fix it."""
    msg = ("Browserbase will not start a browser at all (402). The account "
           "is out of sessions or minutes, or billing needs attention. "
           "Every browser job - site sign-ins, order lookups, ordering - "
           "will fail until it is sorted out in the Browserbase dashboard. "
           "Email, calendar, texts and normal conversation are unaffected.")
    emit("browser", "ACCOUNT LIMIT", f"{msg} ({detail[:120]})", "error")
    now = datetime.utcnow()
    last = _LAST_LIMIT_FLAG.get("at")
    if last and (now - last).total_seconds() < 1800:
        return
    _LAST_LIMIT_FLAG["at"] = now
    try:
        db = Session()
        db.add(Followup(reason="browser_account_limit", note=msg,
                        channel="system"))
        db.commit()
        db.close()
    except Exception:
        pass


def _flag_proxy_fallback(reason: str):
    """Loud: browsers are running outside the US until this is fixed."""
    msg = (f"Browser is not in the expected country. Sign-ins may look "
           f"foreign to Google and get blocked. Reason: {reason}")
    emit("browser", "PROXY FALLBACK", msg, "error")
    now = datetime.utcnow()
    last = _LAST_PROXY_FLAG.get("at")
    if last and (now - last).total_seconds() < 1800:
        return                      # don't spam the to-do list
    _LAST_PROXY_FLAG["at"] = now
    try:
        db = Session()
        db.add(Followup(reason="proxy_fallback", note=msg, channel="system"))
        db.commit()
        db.close()
    except Exception:
        pass


def _bb_session(context_id: str = "", country: str = "",
                state: str = "", city: str = "") -> str:
    """Create a Browserbase session pinned to the caller's own country.
    Returns the session id, or "" to fall back to a plain connection."""
    if not BROWSERBASE_API_KEY:
        return ""
    country = country or PROXY_COUNTRY
    geo = {"country": country}
    if state and country == "US":
        geo["state"] = state
    if city:
        geo["city"] = city
    body = {"projectId": BROWSERBASE_PROJECT_ID}
    if context_id:
        body["browserSettings"] = {"context": {"id": context_id,
                                               "persist": True}}

    # The SAME call asks for the proxy and creates the session that keeps
    # the customer logged in. Asking for a proxy we don't have used to fail
    # the whole call, so we lost the session too - and every job started
    # logged out, making the site demand a fresh code every single time.
    # Proxies are a nice-to-have; staying signed in is not.
    with_proxy = dict(body)
    with_proxy["proxies"] = [{"type": "browserbase", "geolocation": geo}]
    attempts = [(True, with_proxy), (False, body)]
    if PROXY_STATUS.get("proxies_enabled") is False:
        attempts = [(False, body)]          # already known, don't waste a call

    for wants_proxy, payload in attempts:
        try:
            req = urllib.request.Request(
                "https://api.browserbase.com/v1/sessions",
                data=json.dumps(payload).encode(),
                headers={"X-BB-API-Key": BROWSERBASE_API_KEY,
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                sid = json.loads(r.read().decode()).get("id", "")
            if sid:
                PROXY_STATUS.update({"proxies_enabled": wants_proxy,
                                     "checked": datetime.utcnow(),
                                     "note": "" if wants_proxy else
                                     "running without a proxy so the "
                                     "signed-in session survives"})
                return sid
        except Exception as e:
            detail = str(e)[:300]
            # urllib throws the response body away, and the body is where
            # Browserbase explains itself. Read it.
            try:
                if isinstance(e, urllib.error.HTTPError):
                    said = e.read().decode("utf-8", "replace")[:300]
                    if said:
                        detail = f"{detail} - {said}"
            except Exception:
                pass
            paid = "402" in detail or "Payment Required" in detail
            if wants_proxy and paid:
                _flag_proxy_unavailable(country)
                continue        # keep the session, drop the proxy
            if paid:
                # We asked for a plain browser and were still refused. That
                # is the ACCOUNT, not the geography - saying "wrong country"
                # here sent someone looking in completely the wrong place.
                _flag_account_limit(detail)
                return ""
            _flag_proxy_fallback(f"{detail} (wanted {country})")
            return ""
    return ""


def _bb_connect_url(context_id: str = "", account_id=None,
                    phone: str = "") -> str:
    """Prefer a session pinned to the caller's country; fall back to a
    direct connection."""
    if phone:
        country, state, city = _where_for_phone(phone)
    else:
        country, state, city = _where_for_account(account_id)
    sid = _bb_session(context_id, country, state, city)
    if sid:
        # _bb_session records whether a proxy was actually granted - don't
        # overwrite it here, or the status page reports proxies that the
        # plan never gave us.
        emit("browser", "proxy",
             f"browsing from {country}{'/' + state if state else ''}"
             f"{'' if PROXY_STATUS.get('proxies_enabled') else ' (no proxy)'}"
             f"{', session kept' if context_id else ''}",
             "info", account_id)
        return (f"wss://connect.browserbase.com?apiKey={BROWSERBASE_API_KEY}"
                f"&sessionId={sid}")
    url = (f"wss://connect.browserbase.com?apiKey={BROWSERBASE_API_KEY}"
           f"&projectId={BROWSERBASE_PROJECT_ID}")
    if context_id:
        url += f"&contextId={context_id}&persist=true"
    return url


# ---------------------------------------------------- safe page operations
# Pages navigate under us constantly (Google, checkout flows). Every read of
# a live page goes through these so a navigation is a retry, not a crash.

def _is_nav_error(e) -> bool:
    t = str(e).lower()
    return ("context was destroyed" in t or "navigation" in t
            or "target closed" in t or "frame was detached" in t)


def settle(page, ms: int = 1200):
    """Let any in-flight navigation finish."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    try:
        page.wait_for_timeout(ms)
    except Exception:
        pass


def q(page, selector):
    """query_selector that survives a navigation mid-check."""
    for _ in range(3):
        try:
            return page.query_selector(selector)
        except Exception as e:
            if _is_nav_error(e):
                settle(page)
                continue
            return None
    return None


def q_all(page, selector):
    for _ in range(3):
        try:
            return page.query_selector_all(selector)
        except Exception as e:
            if _is_nav_error(e):
                settle(page)
                continue
            return []
    return []


def page_text(page, limit: int = 4000) -> str:
    """The visible text, trimmed INSIDE the browser. Pulling a whole shop
    page across the network and then keeping the first 4000 characters was
    costing seconds per step.

    It takes the page's MAIN content where the page marks one. Otherwise
    the first thousand characters of every shop page are its menu -
    departments, sign-in, gift cards - and a model given that went hunting
    for a "details" section that had been in front of it all along."""
    js = (r"(n) => { const m = document.querySelector("
          r"'main, [role=main], #main-content, #content, #main, "
          r"[id*=product-detail], article, "
          r"#dp-container, #centerCol, #search') "
          r"|| document.body; "
          r"const t = (m && m.innerText ? m.innerText : "
          r"(document.body ? document.body.innerText : '')); "
          r"return t.replace(/\s+/g, ' ').slice(0, n); }")
    got = page_eval(page, js, limit)
    if got:
        return got
    for _ in range(2):
        try:
            return " ".join((page.inner_text("body") or "").split())[:limit]
        except Exception as e:
            if _is_nav_error(e):
                settle(page)
                continue
            return ""
    return ""


def page_url(page) -> str:
    try:
        return page.url
    except Exception:
        return ""


def do_click(page, el, wait_ms: int = 3500) -> bool:
    try:
        el.click()
    except Exception as e:
        if not _is_nav_error(e):
            return False
    settle(page, wait_ms)
    return True


def do_fill(page, el, value: str, press_enter: bool = False,
            wait_ms: int = 3500) -> bool:
    try:
        el.fill(value)
        if press_enter:
            page.keyboard.press("Enter")
    except Exception as e:
        if not _is_nav_error(e):
            return False
    settle(page, wait_ms)
    return True


def do_goto(page, url: str, wait_ms: int = 3500) -> bool:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        if not _is_nav_error(e):
            return False
    settle(page, wait_ms)
    return True


def page_eval(page, js, arg=None):
    """Run JavaScript inside the page, surviving a navigation.

    One call that does the work in the browser beats hundreds of calls that
    each cross the network to it."""
    for _ in range(3):
        try:
            return (page.evaluate(js, arg) if arg is not None
                    else page.evaluate(js))
        except Exception as e:
            if _is_nav_error(e):
                settle(page)
                continue
            return None
    return None


def page_shot(page) -> str:
    """A JPEG of what the page looks like right now, base64 encoded.
    Returns "" if it can't be taken - never raises, never blocks a job."""
    for _ in range(2):
        try:
            raw = page.screenshot(type="jpeg", quality=50, timeout=15000)
            return _b64.b64encode(raw).decode()
        except Exception as e:
            if _is_nav_error(e):
                settle(page)
                continue
            return ""
    return ""


def do_back(page, wait_ms: int = 3000) -> bool:
    """Back out of a dead end instead of getting stuck on it."""
    try:
        page.go_back(wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        if not _is_nav_error(e):
            return False
    settle(page, wait_ms)
    return True


# --------------------------------------------------- assisted Gmail sign-in
# The customer's Google password lives in memory for the length of one
# sign-in and is never written to the database or logged.

_PENDING = {}          # session_id -> {"password": str, "code": str|None}


def _ob_set(sid: int, state: str, message: str = "", reason: str = ""):
    emit("signin", f"signin {sid}", f"{state}: {message}",
         "error" if state == "failed" else "info")
    db = Session()
    row = db.query(Onboard).filter_by(id=sid).first()
    if row:
        row.state = state
        row.message = message[:2000]
        row.reason = reason[:40]
        stamp = datetime.utcnow().strftime("%H:%M:%S")
        line = f"[{stamp}] {state}: {message[:300]}"
        row.history = ((row.history or "") + line + "\n")[-6000:]
        db.commit()
    db.close()


def _run_signin(sid: int, account_id: int, email: str):
    """Drive a hosted browser through Google sign-in, any 2FA, and consent."""
    import re as _re
    from playwright.sync_api import sync_playwright

    creds = _PENDING.get(sid) or {}
    password = creds.get("password", "")
    if not password:
        _ob_set(sid, "failed", "No password supplied.")
        return

    ws = _bb_connect_url(account_id=account_id)

    EMAIL_SEL = ('input[type="email"], input#identifierId, '
                 'input[name="identifier"]')
    PW_SEL = ('input[type="password"], input[name="Passwd"], '
              'input[name="password"]')
    CODE_SEL = ('input[type="tel"], input[name="totpPin"], input#idvPin, '
                'input[name="Pin"], input[name="code"], '
                'input[autocomplete="one-time-code"], '
                'input[aria-label*="code" i]')

    def screen(page):
        return page_text(page, 6000)

    def where(page):
        return f"url={page_url(page)[:110]} | screen: {screen(page)[:260]}"

    def wants_tap(page):
        return q(page, 'text=/Check your device|Tap Yes on the notification|'
                       'Open the Gmail app|notification to your/i')

    def tap_number(page):
        """Google shows a two-digit number the caller must pick on their
        phone. It renders a moment after the screen appears, so look in a
        few places and don't give up on the first miss."""
        # The number is usually its own large element.
        for sel in ('div[jsname] span:text-matches("^[0-9]{1,3}$")',
                    'samp', 'strong:text-matches("^[0-9]{1,3}$")',
                    '*[aria-live] >> text=/^[0-9]{1,3}$/'):
            el = q(page, sel)
            if el:
                try:
                    t = (el.inner_text() or "").strip()
                    if t.isdigit() and 1 <= len(t) <= 3:
                        return t
                except Exception:
                    pass
        body = screen(page)
        for pat in (r"tap\s+(\d{1,3})\b",
                    r"select\s+(\d{1,3})\b",
                    r"number\s+(\d{1,3})\b",
                    r"\b(\d{1,3})\b\s*Check your device",
                    r"Check your device.{0,160}?\b(\d{1,3})\b",
                    r"tablet.{0,160}?\b(\d{1,3})\b"):
            m = _re.search(pat, body, _re.I)
            if m:
                return m.group(1)
        return ""

    def wait_for_tap_number(page, tries: int = 6):
        """The number can take a second or two to render."""
        for _ in range(tries):
            n = tap_number(page)
            if n:
                return n
            settle(page, 1200)
        return ""

    def describe_code_screen(page):
        """Say where the code went, so the caller knows what to look for."""
        body = screen(page)
        if _re.search(r"enter your (device |phone )?(pin|passcode)|"
                      r"screen lock|unlock your (phone|device)", body, _re.I):
            return ("Google wants the PIN or passcode they use to unlock "
                    "their own phone. If they don't want to give that, "
                    "offer try_another_way.")
        if _re.search(r"authenticator|Google Authenticator", body, _re.I):
            return ("Google wants the 6-digit code from their authenticator "
                    "app. Ask them to open it and read the current code.")
        if _re.search(r"backup code|recovery code", body, _re.I):
            return "Google wants one of their backup codes."
        if _re.search(r"security key|USB|tap your key", body, _re.I):
            return ("Google wants a physical security key, which we can't do. "
                    "Offer try_another_way.")
        tail = _re.search(r"(?:ending in|\u2022{2,}\s*)(\d{2,4})", body)
        if _re.search(r"call|voice", body, _re.I) and _re.search(
                r"code", body, _re.I):
            return ("Google is calling their phone with a spoken code. Ask "
                    "them to answer and read it out.")
        if tail:
            return (f"Google texted a code to the number ending {tail.group(1)}."
                    f" Ask them to read it out.")
        return "Google sent a code. Ask them to read it out."

    def pick_another_method(page):
        """Open 'Try another way' and choose a text/call option if offered."""
        try:
            alt = (q(page, 'text=/Try another way/i')
                   or q(page, 'text=/More ways to verify/i')
                   or q(page, 'text=/Try another method/i'))
            if not alt:
                return False
            do_click(page, alt, 3500)
            for sel in ('text=/Get a verification code at/i',
                        'text=/Text message/i',
                        'text=/Send code/i',
                        'text=/Get a code|verification code/i',
                        'text=/Phone call/i'):
                opt = q(page, sel)
                if opt:
                    do_click(page, opt, 3500)
                    return True
            return True          # menu is open; caller can be told options
        except Exception:
            return False

    def wait_for_code(page, sid, note):
        """Sit on a code screen until the caller supplies one."""
        _ob_set(sid, "needs_code", note)
        waited = 0
        while waited < 240:
            time.sleep(3)
            waited += 3
            st = _PENDING.get(sid) or {}
            if st.get("cancelled"):
                return "cancelled"
            if st.get("other_way"):
                _PENDING[sid]["other_way"] = False
                if pick_another_method(page):
                    page.wait_for_timeout(2000)
                    if wants_tap(page):
                        return "tap"
                    _ob_set(sid, "needs_code", describe_code_screen(page))
                continue
            code = st.get("code")
            if code:
                _PENDING[sid]["code"] = None
                code_el = q(page, CODE_SEL)
                if code_el:
                    do_fill(page, code_el, code, True, 5000)
                else:
                    settle(page, 4000)
                if q(page, 'text=/Wrong code|incorrect code|try again/i'):
                    _ob_set(sid, "needs_code",
                            "That code didn't work. Ask them to read it "
                            "again, or call try_another_way.")
                    continue
                return "ok"
        return "timeout"

    page = None
    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws)
            ctx = browser.contexts[0] if browser.contexts \
                else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(45000)

            db = Session()
            before_ids = {c.id for c in db.query(Connection)
                          .filter_by(account_id=account_id,
                                     provider="google").all()}
            db.close()

            _ob_set(sid, "signing_in", "Opening Google.")
            # trusted, server-side, so it mints its own ticket and skips
            # the confirmation page a person would see
            do_goto(page, f"{PUBLIC_URL}/link/start?go=1&t="
                          + urllib.parse.quote(_make_link_token(account_id)),
                    4000)

            other = q(page, 'text=/Use another account/i')
            if other:
                do_click(page, other, 2500)

            try:
                page.wait_for_selector(EMAIL_SEL, timeout=30000)
            except Exception:
                _ob_set(sid, "failed", "No email box. " + where(page))
                browser.close()
                return

            email_el = q(page, EMAIL_SEL)
            if not email_el:
                _ob_set(sid, "failed", "No email box. " + where(page))
                browser.close()
                return
            do_fill(page, email_el, email, True, 3500)

            try:
                page.wait_for_selector(PW_SEL, timeout=30000)
            except Exception:
                _ob_set(sid, "failed", "No password box. " + where(page))
                browser.close()
                return

            pw_el = q(page, PW_SEL)
            if not pw_el:
                _ob_set(sid, "failed", "No password box. " + where(page))
                browser.close()
                return
            emit("signin", f"signin {sid}",
                 f"typing password: {_shape(password)}")
            do_fill(page, pw_el, password, True, 5000)

            if q(page, 'text=/Wrong password/i'):
                emit("signin", f"signin {sid}",
                     f"Google rejected the password ({_shape(password)}). "
                     f"Compare that pattern with the real one - A is a "
                     f"capital, a is lowercase, 9 is a digit, # is a symbol.",
                     "warn")
                _ob_set(sid, "failed",
                        "Google says the password is wrong. Ask them to say "
                        "it again slowly, or have someone call them back.",
                        reason="bad_password")
                browser.close()
                return

            if q(page, 'text=/couldn.t sign you in|browser or app may not be '
                       'secure|unusual activity/i'):
                _ob_set(sid, "failed",
                        "Google blocked the automated sign-in. " + where(page))
                browser.close()
                return

            def on_challenge(page):
                """Still stuck on a Google verification screen?"""
                u = page_url(page)
                if "/challenge" in u or "/signin/v2/challenge" in u:
                    return True
                return bool(q(page, 'text=/Verify it.s you|Choose a way to '
                                    'verify|2-Step Verification/i'))

            def pick_from_selection(page):
                """On 'Choose a way to verify', pick something we can do:
                a texted code first, then a voice call, then anything."""
                for sel in ('text=/Get a verification code at/i',
                            'text=/Text message/i',
                            'text=/Send a text message/i',
                            'text=/Get a code.{0,40}(text|SMS)/i',
                            'text=/Phone call/i',
                            'text=/Call.{0,20}(instead|me)/i',
                            'text=/Google Authenticator/i',
                            'text=/backup code/i'):
                    el = q(page, sel)
                    if el:
                        emit("signin", f"signin {sid}",
                             f"Choosing verification method: {sel}")
                        do_click(page, el, 4000)
                        return True
                return False

            # ---- verification, whichever form it takes
            for _round in range(6):
                if "/link/callback" in page_url(page):
                    break
                if (q(page, 'text=/Choose a way to verify/i')
                        and not q(page, CODE_SEL)):
                    _ob_set(sid, "verifying",
                            "Google is asking how to verify. Picking a "
                            "texted code.")
                    if pick_from_selection(page):
                        settle(page, 3000)
                        continue
                    _ob_set(sid, "failed",
                            "Google offered no verification method we can "
                            "use. " + where(page))
                    browser.close()
                    return

                if wants_tap(page):
                    num = wait_for_tap_number(page)
                    if num:
                        _ob_set(sid, "needs_tap",
                                f"Google sent a prompt to their phone. Tell "
                                f"them to tap Yes and choose the number "
                                f"{num}.")
                    else:
                        emit("signin", f"signin {sid}",
                             f"No number found on the tap screen. Screen "
                             f"text: {screen(page)[:400]}", "warn")
                        _ob_set(sid, "needs_tap",
                                "Google sent a prompt to their phone. Tell "
                                "them to tap Yes, and to read out the number "
                                "shown on their own phone if it asks for one. "
                                "If they already missed it, use "
                                "try_another_way.")
                    waited = 0
                    switched = False
                    while waited < 200:
                        time.sleep(4)
                        waited += 4
                        if (_PENDING.get(sid) or {}).get("cancelled"):
                            _ob_set(sid, "failed", "The caller hung up.",
                                    reason="cancelled")
                            browser.close()
                            return
                        # the number sometimes renders after the first look
                        if not num:
                            num = tap_number(page)
                            if num:
                                _ob_set(sid, "needs_tap",
                                        f"The number is {num}. Tell them to "
                                        f"choose {num} on their phone.")
                        if (_PENDING.get(sid) or {}).get("other_way"):
                            _PENDING[sid]["other_way"] = False
                            if pick_another_method(page):
                                switched = True
                                break
                        # approving navigates the page — that's success
                        if "/link/callback" in page_url(page):
                            break
                        if not wants_tap(page):
                            settle(page, 2000)
                            if not wants_tap(page):
                                break
                    if switched:
                        continue
                    if wants_tap(page):
                        _ob_set(sid, "failed",
                                "They never approved the prompt.")
                        browser.close()
                        return
                    page.wait_for_timeout(3000)
                    continue

                if q(page, CODE_SEL):
                    res = wait_for_code(page, sid, describe_code_screen(page))
                    if res == "cancelled":
                        _ob_set(sid, "failed", "The caller hung up.",
                                reason="cancelled")
                        browser.close()
                        return
                    if res == "timeout":
                        _ob_set(sid, "failed", "Timed out waiting for a code.")
                        browser.close()
                        return
                    if res == "tap":
                        continue
                    page.wait_for_timeout(2000)
                    continue
                break

            _ob_set(sid, "consenting", "Approving access.")

            def consent_screen(page):
                """What Google is showing us right now."""
                t = screen(page)
                u = page_url(page)
                if "/link/callback" in u:
                    return "done", t
                if _re.search(r"hasn.t verified this app|being tested|"
                              r"unverified app", t, _re.I):
                    return "unverified", t
                if _re.search(r"wants access to your Google Account|"
                              r"Select what .* can access|"
                              r"See, edit, download", t, _re.I):
                    return "scopes", t
                if _re.search(r"Choose an account|Select an account", t,
                              _re.I):
                    return "chooser", t
                return "other", t

            approved = False
            for attempt in range(24):
                kind, text = consent_screen(page)
                emit("signin", f"signin {sid}",
                     f"consent screen [{kind}] {text[:160]}")
                if kind == "done":
                    approved = True
                    break

                if kind == "unverified":
                    # "Continue" is sometimes hidden behind "Advanced".
                    for sel in ('button:has-text("Continue")',
                                'span:has-text("Continue")',
                                'div[role="button"]:has-text("Continue")',
                                'text=/^Continue$/'):
                        el = q(page, sel)
                        if el:
                            do_click(page, el, 3000)
                            break
                    else:
                        adv = q(page, 'text=/^Advanced$/')
                        if adv:
                            do_click(page, adv, 2000)
                            unsafe = q(page, 'text=/Go to .*unsafe/i')
                            if unsafe:
                                do_click(page, unsafe, 3000)
                    continue

                if kind == "chooser":
                    acct = q(page, f'text=/{_re.escape(email)}/i')
                    if acct:
                        do_click(page, acct, 3000)
                    continue

                if kind == "scopes":
                    # tick "Select all" if the boxes aren't already on
                    for sel in ('text=/^Select all$/',
                                'input[type="checkbox"][aria-label*="all" i]'):
                        el = q(page, sel)
                        if el:
                            do_click(page, el, 1200)
                            break
                    for box in q_all(page, 'input[type="checkbox"]'):
                        try:
                            if not box.is_checked():
                                box.check(timeout=3000)
                        except Exception:
                            pass
                    for sel in ('button:has-text("Continue")',
                                'button:has-text("Allow")',
                                'span:has-text("Continue")',
                                'span:has-text("Allow")',
                                'div[role="button"]:has-text("Continue")'):
                        el = q(page, sel)
                        if el:
                            do_click(page, el, 3000)
                            break
                    continue

                # unknown screen: try the usual buttons, then wait
                clicked = False
                for sel in ('button:has-text("Continue")',
                            'button:has-text("Allow")',
                            'button:has-text("Next")',
                            'span:has-text("Continue")',
                            'span:has-text("Allow")',
                            'div[role="button"]:has-text("Continue")'):
                    el = q(page, sel)
                    if el:
                        do_click(page, el, 3000)
                        clicked = True
                        break
                if not clicked:
                    settle(page, 2500)

            db = Session()
            rows = (db.query(Connection)
                      .filter_by(account_id=account_id, provider="google")
                      .all())
            want = (email or "").strip().lower()
            match = next((c for c in rows
                          if (c.email or "").lower() == want), None)
            fresh = [c for c in rows if c.id not in before_ids]
            db.close()
            final = where(page)
            browser.close()

            if match:
                _ob_set(sid, "done", f"Connected {match.email}.")
            elif fresh:
                _ob_set(sid, "failed",
                        f"Signed in as {fresh[0].email}, not {email}. "
                        f"Google was already signed into another account. "
                        f"Try again.")
            elif _re.search(r"hasn.t verified this app|being tested", final,
                            _re.I):
                _ob_set(sid, "failed",
                        "Stuck on Google's 'app not verified' warning. The "
                        "Continue button could not be clicked. This account "
                        "may not be on the app's test-user list in Google "
                        "Cloud Console. " + final[:250])
            elif "/challenge" in final or "Verify it" in final:
                _ob_set(sid, "failed",
                        "Google is still asking to verify and we ran out of "
                        "attempts. The phone prompt expired. Try again and "
                        "tap Yes as soon as it appears. " + final[:300])
            else:
                _ob_set(sid, "failed", "Consent not completed. " + final)
    except Exception as e:
        detail = ""
        try:
            if page:
                detail = " " + where(page)
        except Exception:
            pass
        _ob_set(sid, "failed", f"Browser error: {str(e)[:150]}{detail}")
        try:
            if browser:
                browser.close()
        except Exception:
            pass
    finally:
        _PENDING.pop(sid, None)      # password gone from memory


def revoke_google(blob: str) -> bool:
    """Tell Google to invalidate the token, so access really ends."""
    try:
        tok = vault_get(blob)
        t = tok.get("refresh_token") or tok.get("token")
        if not t:
            return False
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/revoke",
            data=urllib.parse.urlencode({"token": t}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False


def disconnect_mailbox(account_id: int, which: str = "") -> dict:
    """Revoke and remove one mailbox."""
    db = Session()
    rows = (db.query(Connection)
              .filter_by(account_id=account_id, provider="google").all())
    if not rows:
        db.close()
        return {"removed": False, "reason": "nothing connected"}

    target = None
    if which:
        w = which.strip().lower()
        target = next((r for r in rows
                       if w == (r.email or "").lower()
                       or w == (r.label or "").lower()), None)
        if not target:
            target = next((r for r in rows
                           if w in (r.email or "").lower()
                           or w in (r.label or "").lower()), None)
        if not target:
            db.close()
            return {"removed": False, "reason": f"no mailbox like '{which}'"}
    else:
        if len(rows) > 1:
            db.close()
            return {"removed": False, "reason": "several mailboxes — ask which",
                    "mailboxes": [r.email for r in rows]}
        target = rows[0]

    email = target.email
    was_default = bool(target.is_default)
    revoked = revoke_google(target.secret_blob)
    db.delete(target)
    db.commit()

    left = (db.query(Connection)
              .filter_by(account_id=account_id, provider="google").all())
    if was_default and left:
        left[0].is_default = 1
        db.commit()
    db.close()
    return {"removed": True, "email": email, "revoked_at_google": revoked,
            "remaining": len(left)}


def delete_everything(account_id: int) -> dict:
    """Remove the customer and every trace of them."""
    db = Session()
    conns = db.query(Connection).filter_by(account_id=account_id).all()
    for c in conns:
        revoke_google(c.secret_blob)
        db.delete(c)

    for model in (Memory, Onboard, PhoneNumber, SiteLogin,
                  SiteSession, Job, Address, PaymentCard, Order):
        for row in db.query(model).filter_by(account_id=account_id).all():
            db.delete(row)

    calls = db.query(Call).filter_by(account_id=account_id).all()
    for call in calls:
        for t in db.query(CallTurn).filter_by(call_id=call.id).all():
            db.delete(t)
        db.delete(call)

    acct = db.query(Account).filter_by(id=account_id).first()
    if acct:
        db.delete(acct)
    db.commit()
    db.close()
    return {"deleted": True, "mailboxes": len(conns), "calls": len(calls)}



def save_site_login(account_id: int, site: str, username: str,
                    password: str) -> dict:
    """Store or replace one site login.

    A blank password NEVER overwrites a stored one. A caller asked to
    change only his username; the assistant called this with an empty
    password, and his real password was replaced with nothing - so the
    next sign-in reported 'no saved login' for an account that was there
    all along."""
    db = Session()
    row = (db.query(SiteLogin)
             .filter_by(account_id=account_id, site=site.lower()).first())

    if not (password or "").strip():
        if not row:
            db.close()
            raise HTTPException(
                400, "A new login needs a password as well as a username.")
        if username.strip():
            row.username = username.strip()
        row.at = datetime.utcnow()
        db.commit()
        out = {"saved": True, "site": site.lower(), "username": row.username,
               "password_unchanged": True}
        db.close()
        return out

    blob = vault_put({"password": password})
    if row:
        row.username = username
        row.secret_blob = blob
        row.at = datetime.utcnow()
    else:
        db.add(SiteLogin(account_id=account_id, site=site.lower(),
                         username=username, secret_blob=blob))
    db.commit()
    db.close()
    return {"saved": True, "site": site.lower(), "username": username}


def use_site_login(account_id: int, site: str, purpose: str = "") -> dict:
    """Decrypt for one use. Server-side only — never sent to a client."""
    db = Session()
    row = (db.query(SiteLogin)
             .filter_by(account_id=account_id, site=site.lower()).first())
    if not row:
        db.close()
        return {}
    data = vault_get(row.secret_blob)
    row.last_used = datetime.utcnow()
    row.use_count = (row.use_count or 0) + 1
    db.add(SecretAccess(account_id=account_id, site=site.lower(),
                        purpose=purpose[:120]))
    db.commit()
    out = {"username": row.username, "password": data.get("password", "")}
    db.close()
    return out


def list_site_logins(account_id: int) -> list:
    db = Session()
    rows = db.query(SiteLogin).filter_by(account_id=account_id).all()
    out = [{"site": r.site, "username": r.username,
            "saved": local_str(r.at, "day") if r.at else "",
            "used": r.use_count or 0} for r in rows]
    db.close()
    return out


def forget_site_login(account_id: int, site: str) -> dict:
    db = Session()
    rows = (db.query(SiteLogin)
              .filter_by(account_id=account_id, site=site.lower()).all())
    for r in rows:
        db.delete(r)
    db.commit()
    db.close()
    return {"removed": len(rows), "site": site.lower()}



# ------------------------------------------------------------ site logins
# Per-site hints. Selectors are deliberately loose; Google/Amazon change
# their pages often, so we try several and fail with the screen text.

SITES = {
    "amazon": {
        "login_url": "https://www.amazon.com/ap/signin?openid.mode=checkid_setup"
                     "&openid.identity=http://specs.openid.net/auth/2.0/"
                     "identifier_select&openid.claimed_id=http://specs.openid"
                     ".net/auth/2.0/identifier_select&openid.assoc_handle="
                     "usflex&openid.ns=http://specs.openid.net/auth/2.0"
                     "&openid.return_to=https://www.amazon.com/",
        "user_sel": 'input[type="email"], input#ap_email, input[name="email"]',
        "pass_sel": 'input[type="password"], input#ap_password',
        "next_sel": 'input#continue, input#signInSubmit',
        "ok_sel": '#nav-link-accountList, text=/Hello,/i',
        "otp_sel": 'input#auth-mfa-otpcode, input[name="otpCode"], '
                   'input[autocomplete="one-time-code"]',
    },
    "walmart": {
        "login_url": "https://www.walmart.com/account/login",
        "user_sel": 'input[type="email"], input#email',
        "pass_sel": 'input[type="password"], input#password',
        "next_sel": 'button[type="submit"]',
        "ok_sel": 'text=/Account|Sign out/i',
        "otp_sel": 'input[autocomplete="one-time-code"], input[name="code"]',
    },
    "temu": {
        "login_url": "https://www.temu.com/login.html",
        "user_sel": 'input[type="email"], input[name="email"], '
                    'input[placeholder*="mail" i]',
        "pass_sel": 'input[type="password"]',
        "next_sel": 'button[type="submit"], div[role="button"]:has-text("Continue")',
        "ok_sel": 'text=/Account|Sign out|Orders/i',
        "otp_sel": 'input[autocomplete="one-time-code"], input[name="code"]',
    },
}

_JOBS = {}          # job_id -> {"code": str|None}

# How many browsers may run at once. Keep at or below your Browserbase plan's
# concurrency limit; everything else waits in line.
MAX_BROWSERS = int(os.environ.get("MAX_BROWSERS", "5"))
_slots = threading.Semaphore(MAX_BROWSERS)
_queue_lock = threading.Lock()
_waiting = 0


_JOB_STARTED = {}


def _job_set(jid: int, state: str, message: str = "", reason: str = ""):
    if state in ("opening", "signing_in") and jid not in _JOB_STARTED:
        _JOB_STARTED[jid] = time.time()
    if state in ("done", "failed") and jid in _JOB_STARTED:
        secs = int(time.time() - _JOB_STARTED.pop(jid))
        try:
            db2 = Session()
            job = db2.query(Job).filter_by(id=jid).first()
            acc = job.account_id if job else None
            db2.close()
            record_usage(account_id=acc, kind="browser",
                         browser_seconds=secs)
        except Exception:
            pass
    emit("job", f"job {jid}", f"{state}: {message}",
         "error" if state == "failed" else "info")
    db = Session()
    row = db.query(Job).filter_by(id=jid).first()
    if row:
        row.state = state
        # 500 characters silently cut every answer that was a list: the
        # saved Amazon addresses stopped mid-word, and a list of cards
        # stopped at "Visa ending 6125, expi". The column is TEXT.
        row.message = message[:4000]
        row.reason = reason[:40]
        stamp = datetime.utcnow().strftime("%H:%M:%S")
        row.history = ((row.history or "") +
                       f"[{stamp}] {state}: {message[:300]}\n")[-6000:]
        if state in ("done", "failed"):
            row.done_at = datetime.utcnow()
        db.commit()
    db.close()


def _get_context(account_id: int, site: str):
    """Reuse a saved browser context so cookies persist between runs."""
    db = Session()
    row = (db.query(SiteSession)
             .filter_by(account_id=account_id, site=site).first())
    ctx_id = row.context_id if row else ""
    db.close()
    return ctx_id


def _forget_context(account_id: int, site: str) -> bool:
    """Throw away a saved browser identity so the next run starts clean.

    A context keeps cookies - including whatever the site decided about
    you at the time. Ours were created from a datacenter in Seattle, so
    Target kept showing a Seattle store to a New York customer long after
    the proxy was fixed."""
    db = Session()
    rows = (db.query(SiteSession)
              .filter_by(account_id=account_id, site=(site or "").lower())
              .all())
    for r in rows:
        db.delete(r)
    db.commit()
    db.close()
    return bool(rows)


def _save_context(account_id: int, site: str, ctx_id: str):
    db = Session()
    row = (db.query(SiteSession)
             .filter_by(account_id=account_id, site=site).first())
    if row:
        row.context_id = ctx_id
        row.last_ok = datetime.utcnow()
    else:
        db.add(SiteSession(account_id=account_id, site=site,
                           context_id=ctx_id, last_ok=datetime.utcnow()))
    db.commit()
    db.close()


def _new_browserbase_context() -> str:
    """Ask Browserbase for a persistent context id."""
    try:
        req = urllib.request.Request(
            "https://api.browserbase.com/v1/contexts",
            data=json.dumps({"projectId": BROWSERBASE_PROJECT_ID}).encode(),
            headers={"X-BB-API-Key": BROWSERBASE_API_KEY,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode()).get("id", "")
    except Exception:
        return ""


def looks_like_pdf(url: str) -> bool:
    u = (url or "").lower().split("?")[0]
    return u.endswith(".pdf") or "/pdf/" in u


def read_pdf(url: str, limit: int = 12000) -> str:
    """Pull the text out of a PDF.

    Appliance manuals, statements and bills are all PDFs, and a PDF has no
    readable text in a browser at all - document.innerText is empty. We
    were scraping videos for something the manufacturer's own manual says
    plainly."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; PhoneAssistant/1.0)"})
        with urllib.request.urlopen(req, timeout=40) as r:
            raw = r.read(8 * 1024 * 1024)
    except Exception as e:
        emit("browse", "pdf", f"could not fetch {url[:80]}: {str(e)[:90]}",
             "warn")
        return ""
    try:
        from pypdf import PdfReader
        import io as _io
        reader = PdfReader(_io.BytesIO(raw))
        out = []
        for page in reader.pages:
            out.append(page.extract_text() or "")
            if sum(len(x) for x in out) > limit:
                break
        return " ".join(" ".join(out).split())[:limit]
    except Exception as e:
        emit("browse", "pdf", f"could not read {url[:80]}: {str(e)[:90]}",
             "warn")
        return ""


def _agent_fallback(jid: int, account_id: int, site: str, goal: str,
                    url: str = ""):
    """No hand-written setup for this site - hand it to the general agent
    and let it work the page out. This is what stops every new site the
    customers ask for needing someone to configure it first."""
    emit("job", f"job {jid}",
         f"no saved setup for {site or 'this site'} - working it out")
    db = Session()
    row = db.query(Job).filter_by(id=jid).first()
    if row:
        payload = json.loads(row.payload or "{}")
        payload["goal"] = goal
        if url:
            payload["url"] = url
        row.payload = json.dumps(payload)
        db.commit()
    db.close()
    _run_browse(jid, account_id, site)


def _run_site_login(jid: int, account_id: int, site: str):
    """Log this customer in, handing over to the general agent if the
    hand-written setup doesn't fit the page any more.

    The handover happens HERE, after the browser work has finished and
    Playwright has closed. Starting a second Playwright inside the first
    one crashes with "Sync API inside the asyncio loop"."""
    try:
        handover = _do_site_login(jid, account_id, site)
        if handover:
            goal, url = handover
            _agent_fallback(jid, account_id, site, goal, url)
    finally:
        # only once EVERYTHING is finished. _do_site_login used to drop it
        # here, so a handed-over job could never be answered: the caller's
        # reply came back "that job is no longer running".
        _JOBS.pop(jid, None)


def _do_site_login(jid: int, account_id: int, site: str):
    """Returns (goal, url) if the agent should take over, else None."""
    from playwright.sync_api import sync_playwright

    creds = use_site_login(account_id, site, purpose=f"job {jid} login")
    if not creds or not creds.get("password"):
        _job_set(jid, "failed", "No saved login for that site.")
        return

    cfg = SITES.get(site.lower())
    if not cfg:
        # never seen this site - the agent signs in the way a person would
        return (f"sign in to {site} with the saved username and password, "
                f"then confirm you are signed in by naming what you can see "
                f"on the account page",
                f"https://www.{site}.com")

    ctx_id = _get_context(account_id, site.lower()) or \
        _new_browserbase_context()
    ws = _bb_connect_url(ctx_id, account_id)

    page = browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws)
            bctx = browser.contexts[0] if browser.contexts \
                else browser.new_context()
            page = bctx.pages[0] if bctx.pages else bctx.new_page()
            page.set_default_timeout(45000)

            _job_set(jid, "opening", f"Opening {site}.")
            do_goto(page, cfg["login_url"], 4000)

            # Already signed in from a previous session?
            if signed_in(page, f"{site} already open")[0]:
                if ctx_id:
                    _save_context(account_id, site.lower(), ctx_id)
                _job_set(jid, "done", f"Already signed in to {site}.")
                browser.close()
                return

            _job_set(jid, "signing_in", "Entering their details.")
            user_el = q(page, cfg["user_sel"])
            if not user_el:
                # The hand-written selectors are an optimisation, not the
                # plan. Walmart swapped its email box for a combined
                # phone-or-email one and this simply gave up; sites change
                # their pages and nobody should have to notice.
                browser.close()
                return (f"sign in to {site} with the saved username and "
                        f"password, then confirm you are signed in by "
                        f"naming what you can see on the account page",
                        cfg["login_url"])
            do_fill(page, user_el, creds["username"])
            nxt = q(page, cfg["next_sel"])
            if nxt:
                do_click(page, nxt)
            else:
                try:
                    page.keyboard.press("Enter")
                except Exception:
                    pass
                settle(page, 3500)

            pw_el = q(page, cfg["pass_sel"])
            if not pw_el:
                where = page_url(page) or cfg["login_url"]
                browser.close()
                return (f"finish signing in to {site} with the saved "
                        f"username and password, then confirm you are "
                        f"signed in", where)
            do_fill(page, pw_el, creds["password"])
            nxt = q(page, cfg["next_sel"])
            if nxt:
                do_click(page, nxt, 6000)
            else:
                try:
                    page.keyboard.press("Enter")
                except Exception:
                    pass
                settle(page, 6000)

            # One-time code? Up to three goes: a misheard digit is the
            # normal case, and one wrong code used to end the sign-in.
            code_since = int(time.time() * 1000)
            for code_try in range(3):
                if not q(page, cfg["otp_sel"]):
                    break
                seen = page_text(page, 1500)
                where = code_destination(seen)

                # If the site emailed the code, fetch it ourselves. The
                # caller has no screen; asking them to go and find it is
                # asking for the one thing they rang us to avoid.
                mailed = ""
                if "mail" in (where or "").lower() or not where:
                    _job_set(jid, "working",
                             f"{site.title()} wants a code - looking in "
                             f"their email for it.")
                    for _ in range(6):
                        time.sleep(5)
                        mailed = code_from_email(account_id, site, code_since)
                        if mailed:
                            break
                if mailed:
                    otp_el = q(page, cfg["otp_sel"])
                    if otp_el:
                        do_fill(page, otp_el, mailed, True, 6000)
                        settle(page, 4000)
                        emit("signin", site,
                             "read the sign-in code from their email",
                             "info", account_id)
                        if not CODE_BAD.search(page_text(page, 1500) or ""):
                            break
                    seen = page_text(page, 1500)
                    where = code_destination(seen)
                if CODE_BAD.search(seen or ""):
                    note = (f"That code wasn't accepted by {site.title()}. "
                            f"Ask them to read the newest code again, "
                            f"digit by digit.")
                elif where:
                    note = (f"{site.title()} sent a code - {where}. Ask them "
                            f"to read it out.")
                else:
                    note = (f"{site.title()} is asking for a code but does "
                            f"not say where it sent it. Tell them that, and "
                            f"ask them to check their phone and their email.")
                _job_set(jid, "needs_code", note)
                waited = 0
                code = None
                while waited < 240:
                    time.sleep(3)
                    waited += 3
                    if (_JOBS.get(jid) or {}).get("cancelled"):
                        break
                    code = (_JOBS.get(jid) or {}).get("code")
                    if code:
                        _JOBS[jid]["code"] = None
                        break
                if not code:
                    _job_set(jid, "failed", "Timed out waiting for the code.",
                             reason="no_code")
                    browser.close()
                    return
                otp_el = q(page, cfg["otp_sel"])
                if not otp_el:
                    break
                do_fill(page, otp_el, code, True, 6000)
                settle(page, 4000)
                if not CODE_BAD.search(page_text(page, 1500) or ""):
                    break
            else:
                _job_set(jid, "failed",
                         f"{site.title()} refused the code three times.",
                         reason="bad_code")
                browser.close()
                return

            settle(page, 3000)
            screen = page_text(page, 1500)
            if _re_scrub.search(r"(?i)(password is incorrect|wrong password|"
                                r"incorrect password|password you entered)",
                                screen or ""):
                _job_set(jid, "failed",
                         f"{site.title()} says the password is wrong.",
                         reason="bad_password")
                browser.close()
                return

            ok, why = signed_in(page, f"{site} sign-in")
            if ok:
                if ctx_id:
                    _save_context(account_id, site.lower(), ctx_id)
                _job_set(jid, "done",
                         f"Signed in to {site} and saved the session.")
                record_change(account_id, "login", "signed in",
                              f"signed in to {site} and saved the session "
                              f"so it isn't needed again")
            else:
                seen = page_text(page, 1500) or ""
                if q(page, cfg["otp_sel"]) or CODE_BAD.search(seen):
                    where = code_destination(seen)
                    _job_set(jid, "failed",
                             f"{site.title()} is still asking for a code"
                             + (f" - {where}" if where else "")
                             + ". The code we tried wasn't accepted.",
                             reason="bad_code")
                else:
                    _job_set(jid, "failed",
                             f"Sign-in didn't complete - {why}.",
                             reason="stuck")
            browser.close()
    except Exception as e:
        detail = " url=" + page_url(page)[:100] if page else ""
        _job_set(jid, "failed", f"Browser error: {str(e)[:150]}{detail}")
        try:
            if browser:
                browser.close()
        except Exception:
            pass
    # NB: the job is NOT removed from _JOBS here - the handover that may
    # follow still needs to receive the caller's answers.


def _open_with_session(p, account_id: int, site: str):
    """Connect to Browserbase reusing this customer's saved session."""
    ctx_id = _get_context(account_id, site) or _new_browserbase_context()
    browser = p.chromium.connect_over_cdp(
        _bb_connect_url(ctx_id, account_id))
    bctx = browser.contexts[0] if browser.contexts else browser.new_context()
    page = bctx.pages[0] if bctx.pages else bctx.new_page()
    page.set_default_timeout(45000)
    return browser, page, ctx_id


ORDER_PAGES = {
    "walmart": "https://www.walmart.com/orders",
    "amazon": "https://www.amazon.com/gp/css/order-history",
    "temu": "https://www.temu.com/orders.html",
}

SEARCH_PAGES = {
    "walmart": "https://www.walmart.com/search?q=",
    "amazon": "https://www.amazon.com/s?k=",
    "temu": "https://www.temu.com/search_result.html?search_key=",
}


# A shop page starts with a hundred menu items. Handing the first 1800
# characters to the model means handing it "Alexa Skills, Amazon Autos,
# Amazon Devices..." - so it reported, honestly enough, that it could not
# find anything. Read more of the page and let the summariser find the
# part that answers the question.
NAV_NOISE = _re_scrub.compile(
    r"(?i)(skip to main content|all departments|alexa skills|"
    r"customer service|registry|gift cards|sell on |your account|"
    r"hello, sign in|deliver to|shop by category)")


def page_answer(page, question: str, limit: int = 9000) -> tuple:
    """(answer, raw) for one loaded page. The answer is empty when the page
    genuinely doesn't say - never menu text dressed up as a result."""
    raw = page_text(page, limit)
    if not (raw or "").strip():
        return "", ""
    answer = _summarise_page(raw, question)
    if not answer or "NOTHING_RELEVANT" in answer:
        return "", raw
    # a "summary" that is only chrome is not an answer
    words = [w for w in answer.split() if len(w) > 2]
    if len(words) < 6 or len(NAV_NOISE.findall(answer)) >= 2:
        return "", raw
    return answer, raw


def _run_site_orders(jid: int, account_id: int, site: str):
    """Read the customer's recent orders from a site they're signed into."""
    from playwright.sync_api import sync_playwright
    url = ORDER_PAGES.get(site)
    if not url:
        _agent_fallback(
            jid, account_id, site,
            "find my recent orders and read back what was ordered, the "
            "status of each, and the date",
            f"https://www.{site}.com")
        return

    browser = page = None
    try:
        with sync_playwright() as p:
            browser, page, ctx_id = _open_with_session(p, account_id, site)
            _job_set(jid, "opening", f"Opening {site} orders.")
            do_goto(page, url, 5000)

            ok, why = signed_in(page, f"{site} orders")
            if not ok:
                emit("job", f"job {jid}", f"{site} is signed out - {why}",
                     "warn")
                _job_set(jid, "failed",
                         f"Not signed in to {site} - {why}. The saved "
                         f"session has expired. Sign in again first.",
                         reason="signed_out")
                browser.close()
                return
            settle(page, 2500)          # let the list actually render
            answer, raw = page_answer(page, "their recent orders: what was "
                                            "ordered, when, and the status")
            if answer:
                _job_set(jid, "done", answer)
            else:
                _job_set(jid, "failed",
                         f"The {site} orders page opened but didn't show any "
                         f"orders we could read. Say that, rather than that "
                         f"they have no orders.",
                         reason="no_results")
            if ctx_id:
                _save_context(account_id, site, ctx_id)
            browser.close()
    except Exception as e:
        _job_set(jid, "failed", _browser_error(e))
        try:
            if browser:
                browser.close()
        except Exception:
            pass
    finally:
        _JOBS.pop(jid, None)


def _run_site_search(jid: int, account_id: int, site: str):
    """Search a site for a product, using the customer's session."""
    from playwright.sync_api import sync_playwright
    db = Session()
    row = db.query(Job).filter_by(id=jid).first()
    payload = json.loads(row.payload or "{}") if row else {}
    db.close()
    query = (payload.get("query") or "").strip()
    if not query:
        _job_set(jid, "failed", "Nothing to search for.")
        return
    base = SEARCH_PAGES.get(site)
    if not base:
        _agent_fallback(
            jid, account_id, site,
            f"search this site for {query} and read back the best few "
            f"matches with their prices",
            f"https://www.{site}.com")
        return

    browser = page = None
    try:
        with sync_playwright() as p:
            browser, page, ctx_id = _open_with_session(p, account_id, site)
            _job_set(jid, "opening", f"Searching {site} for {query}.")
            do_goto(page, base + urllib.parse.quote_plus(query), 5000)
            settle(page, 2500)          # results load after the shell
            answer, raw = page_answer(
                page, f"the best few matches for '{query}', with prices")
            if looks_signed_out(raw or ""):
                _job_set(jid, "failed",
                         f"{site} wants them signed in before it will "
                         f"search. Sign in to {site} first.",
                         reason="signed_out")
                browser.close()
                return
            if looks_like_bot_check(raw or ""):
                wall = record_block(account_id, site, raw or "",
                                    page_url(page), jid)
                _job_set(jid, "failed",
                         f"{site} refused us: {wall['what']}. "
                         f"{wall['advice']}", reason="bot_check")
                browser.close()
                return
            if not answer:
                _job_set(jid, "failed",
                         f"The {site} search page opened but no results came "
                         f"back that we could read. Say that, rather than "
                         f"that the item doesn't exist.",
                         reason="no_results")
                browser.close()
                return
            _job_set(jid, "done", answer)
            if ctx_id:
                _save_context(account_id, site, ctx_id)
            browser.close()
    except Exception as e:
        _job_set(jid, "failed", _browser_error(e))
        try:
            if browser:
                browser.close()
        except Exception:
            pass
    finally:
        _JOBS.pop(jid, None)



# --------------------------------------------------- general browser agent
# Give it a goal and a starting URL. It reads the page, decides the next
# action, and repeats. No per-site configuration.

BROWSE_SYSTEM = """You are operating a web browser for someone on a phone
call. You get the page's text, a numbered list of things you can interact
with, and usually a picture of the page as it looks right now. Use the
picture to see the layout - which box is the search box, where the total
sits, what a button actually says. Act only on the numbered list; the
picture is for understanding, the numbers are for clicking.
Reply with ONE action as JSON and nothing else.

Actions:
{"action":"click","index":N,"why":"..."}
{"action":"type","index":N,"text":"...","enter":true,"why":"..."}
{"action":"goto","url":"https://...","why":"..."}
{"action":"back","why":"..."}                         the last step led
                                                      nowhere - go back and
                                                      try a different way
{"action":"scroll","why":"..."}
{"action":"wait","why":"..."}
{"action":"ask_user","question":"...","why":"..."}   when you need a code,
                                                      a choice, or anything
                                                      only they can answer
{"action":"done","answer":"what to say out loud","why":"..."}
Any action may also carry "found":"..." - a fact this page told you that
the goal needs: a price, a size, what something is made of. It is kept and
given back to you on every later step, so once you have written something
down you never need to open that page again.
{"action":"give_up","answer":"why it can't be done","why":"..."}

Rules:
- Work towards the goal in as few steps as possible.
- Never buy, pay, submit an order, or send anything irreversible. If the goal
  needs that, stop with ask_user and describe exactly what you would do.
- If the page wants a login and there are saved details, use them; if it
  wants a one-time code, use ask_user. Signing in is a normal step towards
  the goal on any site - work it out from the page in front of you.
- If a click led somewhere useless, use back rather than repeating it. If
  the same approach has failed twice, try a different route to the goal.
- If you can already answer the goal from the page, use done. PAGE TEXT is
  what the page says: read it before clicking anything to "see details".
- Write down what you read, with "found", BEFORE you leave a page. To
  compare two things: open the first, note what matters with "found", go
  back, open the second, note that too, then answer from your notes. Never
  open a page you have already noted.
- The answer field is read aloud, so keep it to two or three sentences with
  plain names, prices and dates. Never include a URL."""


_SNAPSHOT_JS = r"""
(args) => {
  const limit = args.limit, want = (args.want || '').toLowerCase();
  const words = want.split(/[^a-z0-9]+/).filter(w => w.length > 3);
  const sel = 'a, button, input, textarea, select, [role=button], ' +
              '[role=link], [role=combobox], [contenteditable="true"]';
  const typed = ['input', 'textarea', 'select'];
  // Clear the markers left by the last look at this page. A page that
  // only partly redraws - or a page we came BACK to - kept its old
  // numbers, so [12] could still match something from the previous
  // screen: the click landed on the wrong thing, or on nothing, and the
  // model concluded the product page "wasn't opening properly".
  for (const old of document.querySelectorAll('[data-pa-idx]'))
    old.removeAttribute('data-pa-idx');
  const cand = [];
  const dupes = new Set();
  let seen = 0;
  for (const el of document.querySelectorAll(sel)) {
    // Amazon keeps thousands of hidden menu links at the TOP of the page.
    // A budget counted over everything scanned was spent entirely on those,
    // and the visible page was never reached: "the page offers 7 things you
    // can use" on a shop full of products. Count what we can actually use.
    if (cand.length >= 600 || ++seen > 12000) break;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    const tag = el.tagName.toLowerCase();
    let label = el.getAttribute('aria-label') || el.getAttribute('placeholder')
      || (el.innerText || '').trim() || el.getAttribute('name')
      || el.getAttribute('value') || el.getAttribute('title') || '';
    label = label.replace(/\s+/g, ' ').slice(0, 70);
    if (!label && !typed.includes(tag)) continue;
    const type = (el.getAttribute('type') || '').toLowerCase();

    // What matters on a shop page is buried under a hundred menu links,
    // so rank rather than take the first ones in the page's own order.
    let score = 0;
    if (tag === 'button' || el.getAttribute('role') === 'button'
        || type === 'submit' || type === 'button') score += 4;
    if (typed.includes(tag)) score += 3;
    const low = label.toLowerCase();
    if (/add to (cart|basket|bag)|buy now|check ?out|place .*order|continue|proceed|sign in|log in|search|save|next|submit|apply|pay/
        .test(low)) score += 4;
    if (words.some(w => low.includes(w))) score += 3;
    if (el.closest('nav, header, footer, [role=navigation], [role=banner], ' +
                   '[role=contentinfo]')) score -= 4;
    if (el.closest('main, [role=main], form, [id*=cart], [id*=checkout]'))
      score += 2;
    if (r.top >= 0 && r.top < 1400) score += 1;
    // One "Add to cart" button per product means sixty identical entries.
    // They outranked the product names, filled every slot, and were then
    // collapsed into one - leaving ten things on a page of hundreds.
    const key = tag + '|' + type + '|' + label.toLowerCase();
    if (dupes.has(key)) continue;
    dupes.add(key);
    cand.push({el: el, order: cand.length, score: score,
               tag: tag, type: type, label: label});
  }
  cand.sort((a, b) => b.score - a.score || a.order - b.order);
  const keep = cand.slice(0, limit).sort((a, b) => a.order - b.order);
  const out = [];
  for (const c of keep) {
    c.el.setAttribute('data-pa-idx', String(out.length));
    out.push({tag: c.tag, type: c.type, label: c.label});
  }
  return out;
}
"""


def _body_mark(page) -> str:
    """A fingerprint of what a page is showing. Deliberately ignores the
    first part: every page on a shop starts with the same menu, and
    comparing that told us nothing had happened when the whole page had
    just changed."""
    import hashlib
    body = page_text(page, 6000) or ""
    return hashlib.md5(body[600:4000].encode("utf-8",
                                             "ignore")).hexdigest()


def _page_snapshot(page, limit: int = 80, want: str = ""):
    """Page text plus a numbered list of things you can interact with.

    This used to ask the browser about each element one at a time - is it
    visible, what tag, what label - which is seven network round trips per
    element. On a big shop that took over two minutes for a single step,
    and often timed out with an empty list, so the model was choosing
    numbers for elements that weren't there. Now the browser does the whole
    job once and hands back the finished list.

    It used to hand back the first 60 things in the page's own order. On
    Amazon that is the menu - Alexa Skills, Amazon Autos, Amazon Fresh -
    so "Add to Cart" never appeared and the model pressed things at random
    until it was declared stuck. The page is ranked now: buttons and boxes
    first, words from the goal next, menus and footers last."""
    raw = page_eval(page, _SNAPSHOT_JS, {"limit": limit, "want": want}) or []
    items, seen = [], set()
    for i, it in enumerate(raw):
        tag = it.get("tag", "")
        typ = it.get("type", "")
        desc = f"{tag}{'/' + typ if typ else ''}: {it.get('label', '')}"
        if desc in seen:
            continue
        seen.add(desc)
        items.append({"idx": i, "desc": desc})
    return items, page_text(page, 4000)


def _handle(page, item):
    """The live element for a snapshot entry, found by the marker the
    snapshot left on it."""
    if not item:
        return None
    return q(page, f'[data-pa-idx="{item["idx"]}"]')


def _first_json(raw: str) -> dict:
    """Pull the first JSON object out of a model's reply.

    The old code demanded the whole reply be nothing but JSON. Newer models
    often add a sentence before or after it, and that used to be read as
    'I have no idea what to do' - the agent gave up on a good answer."""
    if not raw:
        return {}
    s = raw.replace("```json", " ").replace("```", " ")
    start = s.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except Exception:
                        break
        start = s.find("{", start + 1)
    return {}


def _user_turn(msg: str, shot: str = ""):
    """A user message, with a picture of the page attached when we have one."""
    if not shot:
        return {"role": "user", "content": msg}
    return {"role": "user", "content": [
        {"type": "text", "text": msg},
        {"type": "image_url",
         "image_url": {"url": f"data:image/jpeg;base64,{shot}"}}]}


# The model sometimes answers a shopping page in prose - "I'm unable to
# perform the checkout" - instead of an action. It is not being blocked by
# the site: it has lost track of whose browser this is. One plain reminder
# gets it going again; a second refusal is reported honestly rather than
# dressed up as "I couldn't work out what to do next".
REFUSAL_HINT = """You are not browsing on your own behalf and you are not
being asked to buy anything. You are operating the customer's OWN browser,
already signed into their own account, at their spoken request, on a phone
call where they cannot see the screen. Working through a cart and a
checkout page is the job. Follow the GOAL exactly, including any limit it
sets on what you must not click. If the goal says to stop before placing
the order, stop there and report what the page shows. Reply with ONE action
as JSON and nothing else - never prose, never an explanation."""


def _decide(goal: str, url: str, text: str, items: list, history: list,
            answer_hint: str = "", shot: str = "", account_id=None,
            call_id=None, findings=None, model: str = ""):
    listing = "\n".join(f"[{i}] {it['desc']}" for i, it in enumerate(items))
    steps = "\n".join(history[-8:]) or "(none yet)"
    # What it has already read. Without this it walked into a product page,
    # walked out again, and had nothing - so it walked back in.
    notes = "\n".join(f"- {n}" for n in (findings or [])[-12:])
    msg = (f"GOAL: {goal}\n"
           f"URL: {url}\n"
           + (f"WHAT YOU HAVE WRITTEN DOWN SO FAR:\n{notes}\n"
              if notes else "")
           + f"STEPS SO FAR:\n{steps}\n"
           f"{answer_hint}\n"
           f"ELEMENTS:\n{listing}\n\n"
           f"PAGE TEXT:\n{text}")
    turns = [{"role": "system", "content": BROWSE_SYSTEM},
             _user_turn(msg, shot)]
    use = model or MODEL_BROWSER
    d = _openai_chat(turns, model=use, account_id=account_id,
                     call_id=call_id, cheap=False)
    raw = (d["choices"][0]["message"].get("content") or "").strip()
    act = _first_json(raw)
    if act.get("action"):
        return act

    emit("browse", "decide", f"{MODEL_BROWSER} answered in words instead of "
                             f"an action: {raw[:160]} - reminding it",
         "warn", account_id)
    try:
        d = _openai_chat(turns + [{"role": "assistant", "content": raw[:400]},
                                  {"role": "system", "content": REFUSAL_HINT}],
                         model=use, account_id=account_id,
                         call_id=call_id, cheap=False)
        again = (d["choices"][0]["message"].get("content") or "").strip()
        act = _first_json(again)
        if act.get("action"):
            return act
        raw = again or raw
    except Exception:
        pass

    emit("browse", "decide", f"{MODEL_BROWSER} would not act: {raw[:200]}",
         "error", account_id)
    return {"action": "give_up", "refused": True,
            "answer": (f"The part that reads web pages wouldn't carry on "
                       f"with this. It said: {raw[:200]}")}


_TASK_CACHE = {}


def _action_index(act: dict) -> int:
    """Which numbered element the model chose, or -1 if it didn't say.

    This used to read the index with an `or -1` fallback, and in Python
    `0 or -1` is -1 - so element [0], the first link on every page, could
    never be clicked. On Kohl's that was the sign-in link, and the agent
    spent seven steps being told "there is no [-1]"."""
    raw = act.get("index")
    if isinstance(raw, bool):
        return -1
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


def _action_sig(act: dict) -> str:
    """A short fingerprint of what the model just decided to do, so the
    same decision can be recognised when it comes round again."""
    a = act.get("action", "")
    if a == "goto":
        return f"goto:{act.get('url', '')}"
    return f"{a}:{_action_index(act)}:{str(act.get('text', ''))[:20]}"


def _going_in_circles(sigs: list, window: int = 6, times: int = 3) -> bool:
    """Is it doing the same thing over and over?

    Comparing the page before and after an action misses a loop that
    alternates - Home Depot went search, error, refresh, search, error,
    refresh six times, and the page 'changed' on every single step."""
    if len(sigs) < times:
        return False
    recent = sigs[-window:]
    return any(recent.count(s) >= times for s in set(recent))


def _stuck_note(n: int) -> str:
    """What to tell the agent when the page hasn't reacted.

    Without this it repeated 'fill in the username, fill in the password'
    twenty-four times on Target and called it a day. It has no memory that
    an action did nothing, so it has to be told."""
    if n == 1:
        return "That changed nothing on the page."
    if n == 2:
        return ("That changed nothing again. Do not click the same thing a "
                "third time. If you were trying to reach a sign-in page, "
                "use goto with the site's own sign-in address instead of "
                "hunting for the link.")
    return ("Nothing you have tried has changed the page. Stop repeating "
            "these steps. Either go back, try a completely different route, "
            "or give_up and say what you could see.")


STUCK_LIMIT = int(os.environ.get("BROWSE_STUCK_LIMIT", "4"))


def _task_shape(goal: str) -> dict:
    """Split a goal into the KIND of task and its SUBJECT, in one call.

    The kind keys the recipe, so 'where's my order' and 'check my order
    status' share one. The subject is the part that changes between
    callers - 'paper towels', 'milk' - and is what gets typed. Recording
    the subject as a placeholder is what lets a search recipe be reused."""
    key = goal.strip().lower()
    if key in _TASK_CACHE:
        return _TASK_CACHE[key]
    out = {"label": "misc", "subject": ""}
    if OPENAI_API_KEY:
        try:
            d = _openai_chat(model=MODEL_SUMMARY, messages=[
                {"role": "system",
                 "content": ('Split the task into its kind and its subject. '
                             'Reply with JSON only: {"label": "...", '
                             '"subject": "..."}. label is a snake_case kind '
                             'of 1-3 words describing the type of task, not '
                             'the specifics: order_status, product_price, '
                             'account_balance, store_hours, track_package. '
                             'subject is the specific thing being looked '
                             'for, or "" when the task has no variable '
                             'subject. Examples: "find snow blowers and '
                             'their prices" -> {"label": "product_price", '
                             '"subject": "snow blowers"}. "where is my '
                             'order" -> {"label": "order_status", '
                             '"subject": ""}.')},
                {"role": "user", "content": goal[:300]}])
            raw = (d["choices"][0]["message"].get("content") or "").strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            got = json.loads(raw)
            label = "".join(ch if ch.isalnum() or ch == "_" else "_"
                            for ch in str(got.get("label", "")).lower())
            label = label.strip("_")[:60]
            if label:
                out = {"label": label,
                       "subject": str(got.get("subject", ""))[:120].strip()}
        except Exception:
            pass
    _TASK_CACHE[key] = out
    return out


def _task_label(goal: str) -> str:
    return _task_shape(goal)["label"]


def _recipe_value(val: str, creds: dict, subject: str) -> str:
    """Turn a recorded placeholder back into a real value at replay time."""
    if val == "SAVED_PASSWORD":
        return creds.get("password", "")
    if val == "SAVED_USERNAME":
        return creds.get("username", "")
    if val == "TASK_SUBJECT":
        return subject
    return val


def _as_placeholder(typed: str, subject: str) -> str:
    """When the agent typed the subject of the task, record it as a
    placeholder so the next caller's subject goes in instead."""
    a, b = (typed or "").strip().lower(), (subject or "").strip().lower()
    if len(a) >= 2 and b and (a == b or a in b or b in a):
        return "TASK_SUBJECT"
    return typed


def _record_request(site, task, goal, path, outcome, seconds, jid):
    db = Session()
    db.add(SiteRequest(site=site or "generic", task=task, goal=goal[:500],
                       path=path, outcome=outcome, seconds=int(seconds),
                       job_id=jid))
    db.commit()
    db.close()


# Goals that require DOING something. A saved shortcut replays a few
# steps and then summarises whatever page it lands on - fine for "what
# does it cost", never right for "put it in the basket". Replaying one
# for an action goal is how a Best Buy search page became "the item
# successfully went into the cart".
DOING_GOAL = _re_scrub.compile(
    r"(?i)(add .{0,20}to (the |your )?(cart|basket|bag)|"
    r"put .{0,20}in (the |your )?(cart|basket)|"
    r"check ?out|proceed to|place .{0,12}order|sign in|log in|"
    r"send|book|cancel|change|update|remove|delete|reply|pay)")


def claims_action(answer: str) -> bool:
    """Does this answer say something was DONE, rather than seen?"""
    return bool(_re_scrub.search(
        r"(?i)(added to (the )?(cart|basket)|went into the (cart|basket)|"
        r"is now in (the |your )?(cart|basket)|signed (you )?in|"
        r"order (was )?placed|has been (sent|placed|ordered|added)|"
        r"i (have |'ve )?(added|sent|placed|ordered|signed))", answer or ""))


def _find_recipe(site: str, task: str):
    db = Session()
    row = (db.query(Recipe)
             .filter_by(site=site or "generic", task=task, retired=0)
             .order_by(Recipe.times_ok.desc()).first())
    out = None
    if row:
        out = {"id": row.id, "steps": json.loads(row.steps or "[]")}
    db.close()
    return out


def _save_recipe(site: str, task: str, goal: str, steps: list):
    """Store the steps that worked. Existing recipe -> refresh it."""
    db = Session()
    row = (db.query(Recipe)
             .filter_by(site=site or "generic", task=task).first())
    if row:
        row.steps = json.dumps(steps)
        row.retired = 0
        row.times_ok = (row.times_ok or 0) + 1
        row.last_ok = datetime.utcnow()
    else:
        db.add(Recipe(site=site or "generic", task=task,
                      example_goal=goal[:500], steps=json.dumps(steps),
                      times_ok=1, last_ok=datetime.utcnow()))
    db.commit()
    db.close()


def _recipe_result(rid: int, ok: bool):
    db = Session()
    row = db.query(Recipe).filter_by(id=rid).first()
    if row:
        if ok:
            row.times_ok = (row.times_ok or 0) + 1
            row.last_ok = datetime.utcnow()
        else:
            row.times_failed = (row.times_failed or 0) + 1
            # three failures in a row with few successes -> retire it
            if (row.times_failed or 0) >= 3 and \
                    (row.times_failed or 0) > (row.times_ok or 0):
                row.retired = 1
        db.commit()
    db.close()


def _match_element(items: list, desc: str):
    """Find today's version of an element recorded by description."""
    if not desc:
        return None
    want = desc.lower()
    for it in items:
        if it["desc"].lower() == want:
            return it
    tail = want.split(":", 1)[-1].strip()
    if tail:
        for it in items:
            if tail in it["desc"].lower():
                return it
    return None


def _replay_recipe(page, steps: list, creds: dict, log_fn,
                   subject: str = ""):
    """Run recorded steps without the model. Returns (ok, answer_text)."""
    needs = any(st.get("text") == "TASK_SUBJECT" for st in steps)
    if needs and not subject:
        log_fn("saved steps need a subject and this task has none")
        return False, ""
    for i, st in enumerate(steps):
        a = st.get("action")
        try:
            if a in ("click", "type"):
                items, _ = _page_snapshot(page)
                el = _handle(page, _match_element(items, st.get("desc", "")))
                if not el:
                    log_fn(f"replay step {i + 1}: couldn't find "
                           f"'{st.get('desc', '')}'")
                    return False, ""
                if a == "click":
                    do_click(page, el)
                else:
                    val = _recipe_value(st.get("text", ""), creds, subject)
                    do_fill(page, el, val, bool(st.get("enter")))
            elif a == "goto":
                do_goto(page, st["url"])
            elif a == "back":
                do_back(page)
            elif a == "scroll":
                page.mouse.wheel(0, 1400)
                settle(page, 2000)
            elif a == "wait":
                settle(page, 3000)
            log_fn(f"replay step {i + 1}: {a} ok")
        except Exception as e:
            log_fn(f"replay step {i + 1}: {a} failed — {str(e)[:80]}")
            return False, ""
    return True, page_text(page, 5000)


# Pressing this is spending someone else's money. A prompt saying "do not
# buy" is a wish; this is a rule. A browsing job may never press it, and an
# order may only press it once the caller has said yes out loud and the job
# was started with may_buy set.
BUY_BUTTONS = _re_scrub.compile(
    r"(?i)(place (your )?order|buy now|complete (the )?(purchase|order)|"
    r"confirm (and )?(pay|purchase|order)|submit (my |your )?order|"
    r"pay now|place order)")


def _run_browse(jid: int, account_id: int, site: str):
    """Pursue a goal on any site. Try the learned recipe first; fall back to
    the step-by-step agent; record what worked."""
    from playwright.sync_api import sync_playwright

    db = Session()
    row = db.query(Job).filter_by(id=jid).first()
    payload = json.loads(row.payload or "{}") if row else {}
    call_id = row.call_id if row else None
    db.close()
    goal = payload.get("goal", "")
    start = payload.get("url") or ""
    if not start and not site:
        # "Look this up properly": do the search here and start on the best
        # result. Asking the model to search and then decide to read a page
        # never worked - it searched, narrated, and answered from thin air.
        try:
            found = tool_web_search(goal)
            hits = [r.get("url") for r in found.get("results", [])
                    if r.get("url")]
            # the manual beats a video, and both beat a shop listing -
            # a product page for the thing they already own tells them
            # nothing and is usually the first result
            def _rank(u: str) -> int:
                low = (u or "").lower()
                if looks_like_pdf(low):
                    return 0                     # the manual itself
                if "manual" in low or "use-and-care" in low:
                    return 1                     # a page hosting one
                if "/p/" in low or "/shop" in low or "buy" in low:
                    return 3                     # somewhere to buy it
                return 2
            hits.sort(key=_rank)
            if hits:
                start = hits[0]
                payload.setdefault("urls", [])
                payload["urls"] = hits[1:4] + list(payload["urls"])
                _job_set(jid, "opening", f"Looking up: {goal[:90]}")
        except Exception as e:
            emit("browse", f"job {jid}", f"search first failed: {e}", "warn")
    if not start:
        start = (f"https://www.{site}.com" if site
                 else "https://www.google.com")
    # Other pages to try if this one turns out to refuse robots. A
    # manufacturer's own support page is usually the first search result
    # and usually the one that blocks, so going back to the agent to pick
    # again costs the caller half a minute of silence.
    spares = [u for u in (payload.get("urls") or []) if u and u != start]
    # a sign-in plus a lookup does not fit in twelve
    max_steps = int(payload.get("max_steps", 24))
    site_key = site or "generic"
    shape = _task_shape(goal)
    task = shape["label"]
    # the part that changes between callers, e.g. "paper towels"
    subject = (payload.get("query") or shape["subject"] or "").strip()
    t0 = time.time()

    creds = use_site_login(account_id, site, purpose=f"job {jid} browse") \
        if site else {}
    hint = ""
    if creds.get("username"):
        hint = (f"Saved login for this site: username {creds['username']}. "
                f"To fill the username field type SAVED_USERNAME; to fill "
                f"the password field type SAVED_PASSWORD. Both get replaced "
                f"with the real values.")

    def log_fn(msg):
        _job_set(jid, "working", msg)

    # A PDF has nothing for a browser to read - fetch and extract it
    # instead. No browser session, no bot check, and it is the manual.
    if looks_like_pdf(start):
        text = read_pdf(start)
        if text:
            answer = _summarise_page(text, goal)
            if answer and "NOTHING_RELEVANT" not in answer:
                _job_set(jid, "done", answer[:1500])
                _record_request(site_key, task, goal, "pdf", "ok",
                                time.time() - t0, jid)
                return
        nxt = [u for u in (payload.get("urls") or []) if u]
        if nxt:
            start, payload["urls"] = nxt[0], nxt[1:]
        else:
            _job_set(jid, "failed", "That document could not be read.")
            return

    browser = page = None
    recorded = []          # steps with element descriptions, for the recipe
    history = []
    path_used = "agent"
    stuck = 0              # actions in a row that changed nothing
    sigs = []              # what it has been trying, to spot a loop
    try:
        with sync_playwright() as p:
            browser, page, ctx_id = _open_with_session(p, account_id, site_key)
            _job_set(jid, "opening", f"Opening {start}")
            do_goto(page, start, 4000)

            # ---- 1. learned recipe first, but never for a goal that
            # asks for something to be DONE
            recipe = (None if DOING_GOAL.search(goal)
                      else _find_recipe(site_key, task))
            if recipe and recipe["steps"]:
                _job_set(jid, "working",
                         f"Using what worked before for {task}.")
                ok, text = _replay_recipe(page, recipe["steps"], creds,
                                          log_fn, subject)
                if ok and text:
                    answer = _summarise_page(text, goal)
                    if is_blocked(answer):
                        _job_set(jid, "done", BLOCKED_REPLY)
                        browser.close()
                        return
                    if answer and "NOTHING_RELEVANT" not in answer:
                        _recipe_result(recipe["id"], True)
                        _job_set(jid, "done", answer[:1500])
                        _record_request(site_key, task, goal, "recipe", "ok",
                                        time.time() - t0, jid)
                        if ctx_id:
                            _save_context(account_id, site_key, ctx_id)
                        browser.close()
                        return
                _recipe_result(recipe["id"], False)
                path_used = "fallback"
                _job_set(jid, "working",
                         "The saved steps didn't work — working it out fresh.")
                do_goto(page, start, 4000)

            # ---- 2. step-by-step agent
            outcome = "failed"
            findings = []
            for step in range(max_steps):
                if (_JOBS.get(jid) or {}).get("cancelled"):
                    _job_set(jid, "failed", "The caller hung up.",
                             reason="cancelled")
                    break
                # A shop draws its results after the shell, and we were
                # reading the page in between: "the page offers 9 things
                # you can use" on a search results page full of products.
                items, text = _page_snapshot(page, want=goal)
                for _ in range(3):
                    if len(items) >= 15 and len(text) >= 800:
                        break
                    settle(page, 1500)
                    items, text = _page_snapshot(page, want=goal)
                if looks_like_bot_check(text):
                    wall = record_block(account_id, site_key, text,
                                        page_url(page), jid)
                    if spares:
                        nxt = spares.pop(0)
                        _job_set(jid, "working",
                                 "that page wants a human check - trying "
                                 "another source")
                        do_goto(page, nxt, 4000)
                        continue
                    _job_set(jid, "failed",
                             f"{site_key} refused us: {wall['what']}"
                             + (f" ({wall['vendor']})" if wall["vendor"]
                                else "") + f". {wall['advice']}",
                             reason="bot_check")
                    break
                shot = page_shot(page) if BROWSER_VISION else ""
                url_before, body_before = page_url(page), _body_mark(page)
                act = _decide(goal, page_url(page), text, items, history,
                              hint, shot, account_id, call_id, findings,
                              payload.get("model", ""))
                noted = (act.get("found") or "").strip()
                if noted and noted not in findings:
                    findings.append(noted[:300])
                    _job_set(jid, "working", f"Noted: {noted[:120]}")
                a = act.get("action")
                why = act.get("why", "")[:120]

                if a == "done":
                    answer = act.get("answer", "")[:1500]
                    if is_blocked(answer):
                        _job_set(jid, "done", BLOCKED_REPLY)
                        break
                    # Saying it was done does not make it done. If the
                    # answer claims an action and nothing in this job
                    # clicked anything, it is being imagined.
                    if claims_action(answer) and not any(
                            r.get("action") == "click" for r in recorded):
                        note = ("You said something had been done, but "
                                "nothing on this page has been clicked in "
                                "this job. Do it, or say only what the page "
                                "shows.")
                        emit("browse", f"job {jid}",
                             "refused an answer claiming an action that "
                             "never happened", "warn", account_id)
                        history.append(note)
                        _job_set(jid, "working", "checking that before "
                                                 "saying it")
                        continue
                    _job_set(jid, "done", answer)
                    outcome = "ok"
                    if recorded:
                        _save_recipe(site_key, task, goal, recorded)
                    break
                if a == "give_up":
                    _job_set(jid, "failed", act.get("answer", "")[:600],
                             reason="model_refused" if act.get("refused")
                             else "gave_up")
                    break
                if a == "ask_user":
                    # never name this 'q' - that shadows the page helper q()
                    question = act.get("question", "")[:300]
                    if looks_like_bot_check(question):
                        _job_set(jid, "failed",
                                 f"{site_key} wants a human to complete a "
                                 f"check by hand, which a caller on the "
                                 f"phone cannot do for us.",
                                 reason="bot_check")
                        break
                    _job_set(jid, "needs_input", question)
                    waited, reply = 0, None
                    while waited < 240:
                        time.sleep(3)
                        waited += 3
                        reply = (_JOBS.get(jid) or {}).get("code")
                        if reply:
                            _JOBS[jid]["code"] = None
                            break
                    if not reply:
                        _job_set(jid, "failed", "No answer from the caller.")
                        break
                    history.append(f"asked: {question} -> they said: {reply}")
                    _job_set(jid, "working", "Carrying on.")
                    continue

                if a in ("click", "type"):
                    idx = _action_index(act)
                    if idx < 0 or idx >= len(items):
                        note = (f"There is no [{idx}] - the page offers "
                                f"{len(items)} things you can use."
                                + (" Nothing was found on the page at all; "
                                   "it may still be loading, so wait or "
                                   "scroll before choosing again."
                                   if not items else ""))
                        history.append(note)
                        _job_set(jid, "working", note)
                        settle(page, 2500)
                        continue

                try:
                    if a == "click":
                        it = items[int(act["index"])]
                        if (BUY_BUTTONS.search(it["desc"])
                                and not payload.get("may_buy")):
                            note = (f"Refused to press '{it['desc'][:60]}'. "
                                    f"Nothing here may complete a purchase. "
                                    f"Report what the page shows instead.")
                            emit("browse", f"job {jid}",
                                 f"refused to press {it['desc'][:60]}",
                                 "warn", account_id)
                            history.append(note)
                            _job_set(jid, "working", "stopped short of "
                                                     "buying anything")
                            continue
                        do_click(page, _handle(page, it))
                        recorded.append({"action": "click",
                                         "desc": it["desc"]})
                    elif a == "type":
                        it = items[int(act["index"])]
                        val = act.get("text", "")
                        real = _recipe_value(val, creds, subject)
                        do_fill(page, _handle(page, it), real,
                                bool(act.get("enter")))
                        # store what it meant, not what it said
                        recorded.append({"action": "type", "desc": it["desc"],
                                         "text": _as_placeholder(val, subject),
                                         "enter": bool(act.get("enter"))})
                    elif a == "goto":
                        do_goto(page, act["url"])
                        recorded.append({"action": "goto", "url": act["url"]})
                    elif a == "back":
                        do_back(page)
                        # a dead end is not worth recording as a step
                    elif a == "scroll":
                        page.mouse.wheel(0, 1400)
                        recorded.append({"action": "scroll"})
                        settle(page, 2000)
                    else:
                        settle(page, 3000)
                except Exception as e:
                    history.append(f"{a} failed: {str(e)[:90]}")
                    _job_set(jid, "working", f"Retrying after: {str(e)[:80]}")
                    continue

                sigs.append(_action_sig(act))
                if _going_in_circles(sigs):
                    stuck += 1
                    history.append(
                        "You have done that same thing several times now "
                        "and are going round in circles. Try a completely "
                        "different route, or give_up and say what you saw.")
                    _job_set(jid, "working", "going round in circles")
                    if stuck >= STUCK_LIMIT:
                        _job_set(jid, "failed",
                                 f"It kept repeating the same steps without "
                                 f"getting anywhere. Last screen: "
                                 f"{text[:200]}", reason="stuck")
                        break
                    sigs.clear()
                    continue

                # Did any of that actually do something? The first 200
                # characters of a shop page are its menu and never change,
                # so "nothing happened" was reported after a search, after
                # opening a product, and after going back - three real
                # steps in a row, and the job was declared stuck.
                was_url, was_body = url_before, body_before
                now_url = page_url(page)
                now_body = _body_mark(page)
                if now_url == was_url and now_body == was_body:
                    stuck += 1
                    history.append(_stuck_note(stuck))
                    if stuck >= STUCK_LIMIT:
                        # A stale identity is one reason a site ignores
                        # everything - but so is a button we never saw.
                        # Throwing the session away costs the customer
                        # another sign-in and another code read out over
                        # the phone, so only do it when the page itself
                        # says they are signed out.
                        fresh = (_forget_context(account_id, site_key)
                                 if looks_signed_out(text) else False)
                        record_block(account_id, site_key, text,
                                     page_url(page), jid)
                        _job_set(jid, "failed",
                                 f"The page stopped responding to anything "
                                 f"it tried"
                                 + (" - the saved browser session has been "
                                    "cleared, so trying again starts fresh"
                                    if fresh else "")
                                 + f". Last screen: {text[:200]}",
                                 reason="stuck")
                        break
                else:
                    stuck = 0

                shown = act.get("text", "")
                if shown in ("SAVED_PASSWORD",):
                    shown = "(password)"
                history.append(f"{a} {act.get('index', act.get('url', ''))}"
                               f" {shown} — {why}")
                _job_set(jid, "working", f"Step {step + 1}: {why}")
            else:
                # Notes taken along the way are worth more than nothing,
                # and the caller is waiting.
                if findings:
                    _job_set(jid, "done",
                             "It didn't get all the way, but here is what "
                             "it read: " + " ".join(findings)[:1200])
                else:
                    _job_set(jid, "failed",
                             "Ran out of steps before finishing.")

            _record_request(site_key, task, goal, path_used, outcome,
                            time.time() - t0, jid)
            if ctx_id:
                _save_context(account_id, site_key, ctx_id)
            browser.close()
    except Exception as e:
        _job_set(jid, "failed", _browser_error(e))
        _record_request(site_key, task, goal, path_used, "failed",
                        time.time() - t0, jid)
        try:
            if browser:
                browser.close()
        except Exception:
            pass
    finally:
        _JOBS.pop(jid, None)



CHECKOUT_SYSTEM = """You are placing an order on a website for a customer
who has already confirmed every detail on the phone. You get the page text
and numbered interactive elements. Reply with ONE JSON action and nothing
else.

Actions:
{"action":"click","index":N,"why":"..."}
{"action":"type","index":N,"text":"...","enter":false,"why":"..."}
{"action":"goto","url":"https://...","why":"..."}
{"action":"scroll","why":"..."}
{"action":"wait","why":"..."}
{"action":"ask_user","question":"...","why":"..."}
{"action":"place_order","index":N,"total":"12.34","why":"..."}
{"action":"done","confirmation":"...","total":"12.34","answer":"...","why":"..."}
{"action":"give_up","answer":"...","why":"..."}

THE ORDER (already confirmed by the customer — use exactly these):
{spec}

Placeholders you may type: SHIP_LINE1, SHIP_LINE2, SHIP_CITY, SHIP_STATE,
SHIP_ZIP, SHIP_NAME, CARD_NUMBER, CARD_EXP_MM, CARD_EXP_YY, CARD_CVV,
CARD_NAME, SAVED_USERNAME, SAVED_PASSWORD. They are swapped for real values.

Rules, in order of importance:
1. Add ONLY the item described, in the quantity given. If you cannot find a
   product that clearly matches, give_up — do not substitute.
2. If the site already has a saved address or card that matches the
   customer's, use it. Otherwise enter the customer's details.
3. Before the final purchase, you must be on a review/summary screen. Read
   the item, quantity, shipping address, and total. Then use place_order
   with the index of the final purchase button and the total shown.
   If the total is more than 20% above the expected price, use ask_user
   instead, stating the total.
4. After purchasing, find the confirmation/order number and use done.
5. If anything asks for a code or a choice only the customer can make,
   use ask_user.
6. Never buy anything else, never add extras, never change quantity."""


def _run_checkout(jid: int, account_id: int, site: str):
    """Place a confirmed order. Every value comes from the confirmed spec."""
    from playwright.sync_api import sync_playwright

    db = Session()
    job = db.query(Job).filter_by(id=jid).first()
    payload = json.loads(job.payload or "{}") if job else {}
    oid = payload.get("order_id")
    order = db.query(Order).filter_by(id=oid).first() if oid else None
    if not order:
        db.close()
        _job_set(jid, "failed", "No order attached.")
        return
    addr = (db.query(Address).filter_by(id=order.address_id).first()
            if order.address_id else None)
    card = (db.query(PaymentCard).filter_by(id=order.card_id).first()
            if order.card_id else None)
    acct = db.query(Account).filter_by(id=account_id).first()
    order_call_id = order.call_id
    spec = {
        "site": order.site, "item": order.item, "quantity": order.quantity,
        "expected_price": order.expected_price,
        "ship_to": _fmt_address(addr) if addr else "(use the site's saved address)",
        "pay_with": (f"{card.brand} ending {card.last4}" if card
                     else "(use the site's saved payment method)"),
    }
    values = {
        "SHIP_LINE1": addr.line1 if addr else "",
        "SHIP_LINE2": addr.line2 if addr else "",
        "SHIP_CITY": addr.city if addr else "",
        "SHIP_STATE": addr.state if addr else "",
        "SHIP_ZIP": addr.zip if addr else "",
        "SHIP_NAME": (card.name_on_card if card and card.name_on_card
                      else (acct.name if acct else "")),
        "CARD_NAME": (card.name_on_card if card and card.name_on_card
                      else (acct.name if acct else "")),
    }
    db.close()

    if card:
        secret = vault_get(card.secret_blob)
        values["CARD_NUMBER"] = secret.get("number", "")
        values["CARD_CVV"] = secret.get("cvv", "")
        if secret.get("stripe_pm") and not values["CARD_NUMBER"]:
            # Held by Stripe, so there are no digits to type into a form.
            # Say so plainly rather than silently typing nothing.
            spec["pay_with"] = (
                f"{card.brand} ending {card.last4}, held securely and NOT "
                f"available to type in. Use a card the site already has "
                f"saved for them. If the site has none, stop with ask_user "
                f"and say the card cannot be entered on this site.")
        mm, _, yy = (card.exp or "").partition("/")
        values["CARD_EXP_MM"] = mm.strip()
        values["CARD_EXP_YY"] = yy.strip()[-2:]
        db = Session()
        db.add(SecretAccess(account_id=account_id, site="card",
                            purpose=f"order {oid} checkout"))
        db.commit()
        db.close()

    creds = use_site_login(account_id, site, purpose=f"order {oid} checkout") \
        if site else {}
    values["SAVED_USERNAME"] = creds.get("username", "")
    values["SAVED_PASSWORD"] = creds.get("password", "")

    system = CHECKOUT_SYSTEM.replace("{spec}", json.dumps(spec, indent=1))

    def decide(url, text, items, history, shot=""):
        listing = "\n".join(f"[{i}] {it['desc']}"
                             for i, it in enumerate(items))
        msg = (f"URL: {url}\nSTEPS SO FAR:\n" +
               ("\n".join(history[-10:]) or "(none)") +
               f"\n\nELEMENTS:\n{listing}\n\nPAGE TEXT:\n{text}")
        d = _openai_chat([{"role": "system", "content": system},
                          _user_turn(msg, shot)],
                         model=MODEL_BROWSER, account_id=account_id,
                         call_id=order_call_id, cheap=False)
        raw = (d["choices"][0]["message"].get("content") or "").strip()
        act = _first_json(raw)
        if act.get("action"):
            return act
        emit("order", f"order {oid}", f"{MODEL_BROWSER} gave no usable "
                                      f"action: {raw[:200]}", "warn")
        return {"action": "give_up", "answer": "Lost track of the page."}

    browser = page = None
    history = []
    try:
        with sync_playwright() as p:
            browser, page, ctx_id = _open_with_session(p, account_id, site)
            _job_set(jid, "opening", f"Opening {site}.")
            _order_set(oid, "placing", f"Opening {site}.")
            do_goto(page, f"https://www.{site}.com", 4000)

            for step in range(30):
                if (_JOBS.get(jid) or {}).get("cancelled"):
                    _job_set(jid, "failed", "The caller hung up.",
                             reason="cancelled")
                    break
                items, text = _page_snapshot(
                    page, limit=80,
                    want=f"{spec.get('item', '')} add to cart checkout")
                shot = page_shot(page) if BROWSER_VISION else ""
                act = decide(page_url(page), text, items, history, shot)
                a = act.get("action")
                why = act.get("why", "")[:120]

                if a == "done":
                    conf = act.get("confirmation", "")[:120]
                    total = act.get("total", "")[:20]
                    _order_set(oid, "placed",
                               act.get("answer", "Order placed.")[:400],
                               confirmation=conf, final_total=total)
                    _job_set(jid, "done", f"Placed. Confirmation {conf}, "
                                          f"total {total}.")
                    break
                if a == "give_up":
                    _order_set(oid, "failed", act.get("answer", "")[:400])
                    _job_set(jid, "failed", act.get("answer", "")[:400])
                    break
                if a == "ask_user":
                    # never name this 'q' - that shadows the page helper q()
                    question = act.get("question", "")[:300]
                    _job_set(jid, "needs_input", question)
                    _order_set(oid, "placing",
                               f"Needs the customer: {question}")
                    waited, reply = 0, None
                    while waited < 240:
                        time.sleep(3)
                        waited += 3
                        reply = (_JOBS.get(jid) or {}).get("code")
                        if reply:
                            _JOBS[jid]["code"] = None
                            break
                    if not reply:
                        _order_set(oid, "failed", "No answer from the customer.")
                        _job_set(jid, "failed", "No answer from the caller.")
                        break
                    history.append(f"asked: {question} -> they said: {reply}")
                    continue
                if a in ("click", "type", "place_order"):
                    idx = _action_index(act)
                    if idx < 0 or idx >= len(items):
                        note = (f"There is no [{idx}] - the page offers "
                                f"{len(items)} things you can use.")
                        history.append(note)
                        _job_set(jid, "working", note)
                        settle(page, 2500)
                        continue

                if a == "place_order":
                    total = str(act.get("total", ""))[:20]
                    _order_set(oid, "placing",
                               f"On the review screen, total {total}. "
                               f"Placing now.")
                    history.append(f"place_order total {total} — {why}")
                    ok = do_click(page, _handle(page, items[int(act["index"])]),
                                  8000)
                    if not ok:
                        history.append("place_order click failed")
                    continue

                try:
                    if a == "click":
                        do_click(page, _handle(page, items[int(act["index"])]))
                    elif a == "type":
                        val = act.get("text", "")
                        real = values.get(val, val)
                        do_fill(page, _handle(page, items[int(act["index"])]),
                                real, bool(act.get("enter")))
                    elif a == "goto":
                        do_goto(page, act["url"])
                    elif a == "scroll":
                        page.mouse.wheel(0, 1400)
                        settle(page, 2000)
                    else:
                        settle(page, 3000)
                except Exception as e:
                    history.append(f"{a} failed: {str(e)[:90]}")
                    continue

                shown = act.get("text", "")
                if shown in values and shown.startswith(("CARD", "SAVED_P")):
                    shown = f"({shown.lower()})"
                history.append(f"{a} {act.get('index', act.get('url', ''))}"
                               f" {shown} — {why}")
                _job_set(jid, "working", f"Step {step + 1}: {why}")
                _order_set(oid, "placing", f"Step {step + 1}: {why}")
            else:
                _order_set(oid, "failed", "Ran out of steps.")
                _job_set(jid, "failed", "Ran out of steps.")

            if ctx_id:
                _save_context(account_id, site, ctx_id)
            browser.close()
    except Exception as e:
        _order_set(oid, "failed", f"Browser error: {str(e)[:200]}")
        _job_set(jid, "failed", f"Browser error: {str(e)[:200]}")
        try:
            if browser:
                browser.close()
        except Exception:
            pass
    finally:
        values.clear()
        _JOBS.pop(jid, None)


RUNNERS = {"site_login": _run_site_login,
           "browse": _run_browse,
           "checkout": _run_checkout,
           "site_orders": _run_site_orders,
           "site_search": _run_site_search}


def _queued_run(jid: int, kind: str, account_id: int, site: str):
    """Wait for a free browser slot, then run. Keeps us inside the plan."""
    global _waiting
    with _queue_lock:
        _waiting += 1
        ahead = _waiting - 1
    if ahead > 0:
        _job_set(jid, "waiting",
                 f"{ahead} ahead in the queue. Starting shortly.")
    _slots.acquire()
    with _queue_lock:
        _waiting -= 1
    try:
        fn = RUNNERS.get(kind)
        if not fn:
            _job_set(jid, "failed", f"Unknown job type '{kind}'.")
            return
        fn(jid, account_id, site)
    finally:
        _slots.release()


def start_job(account_id: int, kind: str, site: str = "",
              call_id=None, payload=None) -> int:
    # One live job per customer per site — no duplicate browsers.
    db = Session()
    live = (db.query(Job)
              .filter(Job.account_id == account_id,
                      Job.site == site.lower(),
                      Job.kind == kind,
                      Job.state.notin_(["done", "failed"]))
              .order_by(Job.id.desc()).first())
    if live and kind == "site_login":
        jid = live.id
        db.close()
        return jid

    row = Job(account_id=account_id, call_id=call_id, kind=kind,
              site=site.lower(), payload=json.dumps(payload or {}))
    db.add(row)
    db.commit()
    db.refresh(row)
    jid = row.id
    db.close()

    _JOBS[jid] = {"code": None}
    threading.Thread(target=_queued_run,
                     args=(jid, kind, account_id, site.lower()),
                     daemon=True).start()
    return jid


def _summarise_page(text: str, question: str) -> str:
    """Turn a scraped page into a short spoken answer."""
    if not OPENAI_API_KEY:
        return text[:600]
    try:
        d = _openai_chat(model=MODEL_SUMMARY, messages=[
            {"role": "system",
             "content": ("You turn a scraped web page into a short answer to "
                         "be read aloud on a phone call. Two or three "
                         "sentences. Give names, prices and dates plainly. "
                         "No URLs. If the page does not answer the question, "
                         "reply with exactly NOTHING_RELEVANT and nothing "
                         "else.")},
            {"role": "user",
             "content": f"Question: {question}\n\nPage text:\n{text[:6000]}"},
        ])
        return (d["choices"][0]["message"].get("content") or "")[:900]
    except Exception:
        return text[:600]


def queue_health() -> dict:
    db = Session()
    running = (db.query(Job)
                 .filter(Job.state.notin_(["done", "failed", "waiting"]))
                 .count())
    waiting = db.query(Job).filter(Job.state == "waiting").count()
    stale_cut = datetime.utcnow() - timedelta(minutes=20)
    stale = (db.query(Job)
               .filter(Job.state.notin_(["done", "failed"]),
                       Job.at < stale_cut).count())
    sessions = db.query(SiteSession).count()
    db.close()
    return {"max_browsers": MAX_BROWSERS, "running": running,
            "waiting": waiting, "stuck_over_20min": stale,
            "saved_sessions": sessions}



# ------------------------------------------------------------- ordering

def _order_set(oid: int, state: str, message: str = "", **fields):
    emit("order", f"order {oid}", f"{state}: {message}",
         "error" if state == "failed" else "info")
    db = Session()
    row = db.query(Order).filter_by(id=oid).first()
    if row:
        row.state = state
        row.message = message[:600]
        for k, v in fields.items():
            setattr(row, k, v)
        stamp = datetime.utcnow().strftime("%H:%M:%S")
        row.history = ((row.history or "") +
                       f"[{stamp}] {state}: {message[:300]}\n")[-6000:]
        if state == "placed":
            row.placed_at = datetime.utcnow()
        db.commit()
    db.close()


def _fmt_address(a) -> str:
    parts = [a.line1, a.line2, f"{a.city}, {a.state} {a.zip}".strip(", ")]
    return ", ".join(p for p in parts if p)


class StripeError(Exception):
    def __init__(self, code: str, message: str, decline: str = ""):
        super().__init__(message)
        self.code, self.message, self.decline = code, message, decline


def _stripe_call(method: str, path: str, fields: dict | None = None,
                 idem: str = "") -> dict:
    """One call to Stripe, plain urllib like every other service here.
    Failures keep Stripe's own error code, so nothing reads the message."""
    data = urllib.parse.urlencode(fields or {}, doseq=True)
    url = f"https://api.stripe.com/v1/{path}"
    headers = {"Authorization": f"Bearer {STRIPE_SECRET_KEY}"}
    body = None
    if method == "GET":
        url += ("?" + data) if data else ""
    else:
        body = data.encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if idem:
        headers["Idempotency-Key"] = idem[:255]
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        err = {}
        try:
            err = json.loads(e.read().decode()).get("error", {}) or {}
        except Exception:
            pass
        raise StripeError(err.get("code") or err.get("type") or str(e.code),
                          err.get("message") or f"Stripe said {e.code}.",
                          err.get("decline_code", "")) from None


def _stripe(path: str, fields: dict) -> dict:
    try:
        return _stripe_call("POST", path, fields)
    except StripeError as e:
        raise HTTPException(400, e.message)


def stripe_customer_for(account_id: int) -> str:
    """Each customer gets one Stripe customer, made the first time."""
    db = Session()
    acct = db.query(Account).filter_by(id=account_id).first()
    if not acct:
        db.close()
        raise HTTPException(404, "No such customer.")
    if acct.stripe_customer:
        cus = acct.stripe_customer
        db.close()
        return cus
    name = acct.name or ""
    db.close()
    made = _stripe_call("POST", "customers", {
        "name": name, "metadata[account_id]": str(account_id)},
        idem=f"customer-{account_id}")
    db = Session()
    acct = db.query(Account).filter_by(id=account_id).first()
    acct.stripe_customer = made["id"]
    db.commit()
    db.close()
    return made["id"]


def stripe_card_page_url(account_id: int) -> str:
    """Stripe's own hosted page, set up to SAVE a card, not charge it. The
    card is typed into Stripe, never into us."""
    cus = stripe_customer_for(account_id)
    session = _stripe_call("POST", "checkout/sessions", {
        "mode": "setup",
        "customer": cus,
        "payment_method_types[0]": "card",
        "client_reference_id": str(account_id),
        "metadata[account_id]": str(account_id),
        "setup_intent_data[metadata][account_id]": str(account_id),
        "success_url": f"{PUBLIC_URL}/card/done?session_id="
                       "{CHECKOUT_SESSION_ID}",
        "cancel_url": f"{PUBLIC_URL}/card?cancelled=1",
    })
    return session["url"]


def stripe_save_finished(session_id: str) -> dict:
    """Stripe says the card page was completed: record the card. Asks
    Stripe, never trusts the address bar, and is safe to run twice."""
    sess = _stripe_call("GET", f"checkout/sessions/{session_id}",
                        {"expand[]": "setup_intent.payment_method"})
    if sess.get("status") != "complete" or sess.get("mode") != "setup":
        raise HTTPException(400, "not_finished")
    try:
        account_id = int(sess.get("client_reference_id") or 0)
    except ValueError:
        account_id = 0
    db = Session()
    acct = db.query(Account).filter_by(id=account_id).first()
    if not acct or not acct.stripe_customer \
            or acct.stripe_customer != sess.get("customer"):
        db.close()
        raise HTTPException(400, "not_finished")
    pm = ((sess.get("setup_intent") or {}).get("payment_method") or {})
    card = pm.get("card") or {}
    pm_id = pm.get("id", "")
    for c in db.query(PaymentCard).filter_by(account_id=account_id).all():
        try:
            if vault_get(c.secret_blob).get("stripe_pm") == pm_id:
                out = {"brand": c.brand, "last4": c.last4, "new": False}
                db.close()
                return out
        except Exception:
            continue
    for c in db.query(PaymentCard).filter_by(account_id=account_id).all():
        c.is_default = 0
    brand = (card.get("brand") or "card").title()
    row = PaymentCard(
        account_id=account_id, brand=brand, last4=card.get("last4", ""),
        exp=f"{card.get('exp_month', '')}/{str(card.get('exp_year', ''))[-2:]}",
        name_on_card=((pm.get("billing_details") or {}).get("name") or "")[:120],
        secret_blob=vault_put({"stripe_pm": pm_id,
                               "stripe_customer": acct.stripe_customer}),
        is_default=1)
    db.add(row)
    db.commit()
    out = {"brand": brand, "last4": row.last4, "new": True,
           "account_id": account_id}
    db.close()
    record_change(account_id, "card", "saved",
                  f"added a {brand} ending {out['last4']} on the card page")
    emit("cards", "saved", f"card saved at Stripe ({brand} {out['last4']})",
         "info", account_id)
    return out


CHARGE_LIMIT_CENTS = int(os.environ.get("CHARGE_LIMIT_CENTS", "50000"))


def stripe_charge(account_id: int, card_id: int, cents: int,
                  what_for: str, key: str) -> dict:
    """Charge a card saved at Stripe. key makes it safe to retry: the same
    key never charges twice. Outcomes are reason codes, not sentences."""
    if cents < 50:
        raise HTTPException(400, "too_small")
    if cents > CHARGE_LIMIT_CENTS:
        raise HTTPException(400, "over_limit")
    db = Session()
    card = db.query(PaymentCard).filter_by(id=card_id,
                                           account_id=account_id).first()
    db.close()
    if not card:
        raise HTTPException(404, "no_card")
    secret = vault_get(card.secret_blob)
    pm, cus = secret.get("stripe_pm"), secret.get("stripe_customer")
    if not pm or not cus:
        raise HTTPException(400, "card_not_at_stripe")
    try:
        pi = _stripe_call("POST", "payment_intents", {
            "amount": str(int(cents)), "currency": "usd",
            "customer": cus, "payment_method": pm,
            "off_session": "true", "confirm": "true",
            "description": (what_for or "")[:300],
            "metadata[account_id]": str(account_id)}, idem=key)
    except StripeError as e:
        reason = {"authentication_required": "needs_authentication",
                  "card_declined": "declined",
                  "expired_card": "card_expired",
                  "insufficient_funds": "declined"}.get(e.code, "stripe_error")
        if e.decline == "authentication_required":
            reason = "needs_authentication"
        emit("cards", "charge", f"charge of ${cents / 100:.2f} failed: "
                                f"{reason}", "warn", account_id)
        return {"charged": False, "reason": reason, "message": e.message}
    ok = pi.get("status") == "succeeded"
    if ok:
        record_change(account_id, "payment", "charged",
                      f"charged ${cents / 100:.2f} to {card.brand} ending "
                      f"{card.last4} for {what_for[:120]}",
                      undo="refundable from the Stripe dashboard")
    emit("cards", "charge",
         f"{'charged' if ok else 'charge ' + pi.get('status', '')} "
         f"${cents / 100:.2f} on {card.brand} {card.last4}: {what_for[:80]}",
         "info" if ok else "warn", account_id)
    return {"charged": ok, "reason": "" if ok else pi.get("status", ""),
            "id": pi.get("id"), "amount": f"${cents / 100:.2f}",
            "card": f"{card.brand} ending {card.last4}"}


class StripeNeedsRawCardAccess(Exception):
    """Stripe will not take card digits from a server until the account is
    approved for it. Phone orders have no browser to collect a card in, so
    this has to be requested - until then we keep storing cards ourselves."""


def stripe_hold_card(number: str, exp: str, cvv: str, name: str) -> dict:
    """Give the card to Stripe, get back a token. We never store the digits.

    Stripe is also the authority on the brand and last four, so we stop
    guessing those ourselves."""
    mm, _, yy = (exp or "").partition("/")
    yy = yy.strip()
    year = int(yy) + 2000 if len(yy) == 2 else int(yy or 0)
    fields = {"type": "card", "card[number]": number,
              "card[exp_month]": (mm.strip() or "0"), "card[exp_year]": year}
    if cvv:
        fields["card[cvc]"] = cvv
    if name:
        fields["billing_details[name]"] = name
    try:
        pm = _stripe("payment_methods", fields)
    except HTTPException as e:
        said = str(getattr(e, "detail", ""))
        if "raw card" in said.lower() or "directly to the stripe api"                 in said.lower():
            raise StripeNeedsRawCardAccess(said) from None
        raise
    card = pm.get("card", {})
    return {"id": pm.get("id", ""), "brand": (card.get("brand") or "").title(),
            "last4": card.get("last4", ""),
            "exp": f"{card.get('exp_month', '')}/"
                   f"{str(card.get('exp_year', ''))[-2:]}"}


def _luhn_ok(num: str) -> bool:
    d = [int(c) for c in num if c.isdigit()]
    if len(d) < 13:
        return False
    total, alt = 0, False
    for x in reversed(d):
        if alt:
            x *= 2
            if x > 9:
                x -= 9
        total += x
        alt = not alt
    return total % 10 == 0


def _card_brand(num: str) -> str:
    if num.startswith("4"):
        return "Visa"
    if num[:2] in ("51", "52", "53", "54", "55") or 2221 <= int(num[:4] or 0) <= 2720:
        return "Mastercard"
    if num[:2] in ("34", "37"):
        return "Amex"
    if num.startswith("6"):
        return "Discover"
    return "Card"


# ------------------------------------------------------- text brain (SMS)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# Which model does which job. All three are Railway variables, so you can
# change the browser's brain without a code change or a deploy.
#   MODEL_BROWSER  decides every click on a website - the one that matters
#   MODEL_SUMMARY  turns a finished page into a spoken sentence - easy work
#   MODEL_TEXT     answers incoming text messages
# GET /models lists what your OpenAI account can actually use.
MODEL_BROWSER = os.environ.get("MODEL_BROWSER", "gpt-4o")
MODEL_SUMMARY = os.environ.get("MODEL_SUMMARY", "gpt-4o-mini")
MODEL_TEXT = os.environ.get("MODEL_TEXT", "gpt-4o-mini")
# Send the browser a picture of the page as well as its text. Set to 0 to
# go back to text only.
BROWSER_VISION = os.environ.get("BROWSER_VISION", "1") not in ("0", "false")

TEXT_TOOLS = [
    {"type": "function", "function": {
        "name": "check_email",
        "description": "Their unread emails — count, senders, subjects.",
        "parameters": {"type": "object", "properties": {
            "how_many": {"type": "integer"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "search_email",
        "description": "Search the whole mailbox. Gmail syntax, e.g. from:chaim.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "send_email",
        "description": "Send an email. Confirm with the user first.",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "string"}, "subject": {"type": "string"},
            "body": {"type": "string"}},
            "required": ["to", "subject", "body"]}}},
    {"type": "function", "function": {
        "name": "find_contact",
        "description": "Find someone's email address from past mail.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "check_calendar",
        "description": "What's scheduled. days=1 today, 7 this week.",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "create_event",
        "description": "Book something. start_iso is YYYY-MM-DDTHH:MM:SS.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"}, "start_iso": {"type": "string"},
            "minutes": {"type": "integer"}},
            "required": ["title", "start_iso"]}}},
    {"type": "function", "function": {
        "name": "leave_note_for_office",
        "description": ("Record something for staff: a failure, a request "
                        "you can't handle, or anything to pass on."),
        "parameters": {"type": "object", "properties": {
            "note": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["note"]}}},
    {"type": "function", "function": {
        "name": "disconnect_email",
        "description": "Remove one connected mailbox and revoke it at Google.",
        "parameters": {"type": "object", "properties": {
            "mailbox": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "delete_my_account",
        "description": ("Erase this person entirely. Only after they have "
                        "typed the word DELETE."),
        "parameters": {"type": "object", "properties": {
            "confirmation": {"type": "string"}},
            "required": ["confirmation"]}}},
    {"type": "function", "function": {
        "name": "list_mailboxes",
        "description": "Which email addresses this person has connected.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "name_mailbox",
        "description": ("Give a mailbox a short name they can say, and/or "
                        "make it their main one."),
        "parameters": {"type": "object", "properties": {
            "mailbox": {"type": "string"}, "name": {"type": "string"},
            "make_main": {"type": "boolean"}}, "required": ["mailbox"]}}},
    {"type": "function", "function": {
        "name": "connect_email",
        "description": ("Connect this person's Gmail using an address and "
                        "password they sent. Confirm both back first."),
        "parameters": {"type": "object", "properties": {
            "email": {"type": "string"}, "password": {"type": "string"}},
            "required": ["email", "password"]}}},
    {"type": "function", "function": {
        "name": "check_connect",
        "description": "How the email sign-in is going.",
        "parameters": {"type": "object", "properties": {
            "session_id": {"type": "integer"}}, "required": ["session_id"]}}},
    {"type": "function", "function": {
        "name": "submit_code",
        "description": "Give Google the verification code they sent you.",
        "parameters": {"type": "object", "properties": {
            "session_id": {"type": "integer"}, "code": {"type": "string"}},
            "required": ["session_id", "code"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web — addresses, hours, phone numbers, facts.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
]

TEXT_RULES = """You are a personal assistant reachable by phone call and by
text. This is the text channel, so keep replies under 300 characters, plain
and clear. No emoji.

You share one memory with the phone side. The history below includes both, so
if they discussed something on a call, you already know it.

TOPICS YOU DO NOT DISCUSS: gossip, sex, adultery, intimacy, explicit
material, addiction, humor, culture, dating, underwear, nudity, fertility,
puberty, marriage, relationships, anything arousing, news, sports,
entertainment, personal feelings, jokes. Reply to any of these with exactly:
"I am not allowed to talk to you about this." Nothing more. Never explain
the rules.

Jewish religious subjects ARE allowed - Shabbos and Yom Tov, kashrus, zmanim,
davening, brochos, the parsha, minhagim. This service is for Jewish people.
What stays out is DISCUSSING other religions or comparing faiths. Practical
things that merely mention one are fine and you just do them: store hours on
Christmas, directions to a church, an email that mentions a holiday. A place,
a date or a name is not a discussion.
You are not a rav: relay what a source says and look things up, but for an
actual shailah say they should ask their rav.
If someone is in danger or a medical emergency, help them reach emergency
services — that comes first.

Only say you've done something after the tool actually did it. If you can't
do what they ask, say so and use leave_note_for_office, then tell them it's
been passed on. Never promise a follow-up you haven't recorded.

Before sending an email or booking anything, state what you're about to do
and wait for a yes.

They can undo anything. "Disconnect my work email" -> disconnect_email.
"Delete everything" -> explain that every mailbox, all history and their
account go, that it can't be undone, and ask them to reply with the word
DELETE. Only then call delete_my_account. Never ask why.

If they have several mailboxes, ask which one they mean when it isn't
obvious, and you can name them with name_mailbox if they'd like.

If they have no email connected yet, you can connect it. Ask for their Gmail
address and password, read both back, then use connect_email. Poll
check_connect. If it says needs_code, ask them for the code Google just sent
and use submit_code. Never repeat their password back after the sign-in is
done, and never include it in any later message."""


def _run_text_tool(account_id: int, name: str, args: dict):
    try:
        if name == "check_email":
            return tool_unread_summary(account_id, args.get("how_many", 5))
        if name == "search_email":
            return tool_search_email(account_id, args["query"], 5)
        if name == "send_email":
            return tool_send_email(account_id, args["to"],
                                   args["subject"], args["body"])
        if name == "find_contact":
            return tool_find_contact(account_id, args["name"])
        if name == "check_calendar":
            return tool_list_events(account_id, args.get("days", 1))
        if name == "create_event":
            return tool_create_event(account_id, args["title"],
                                     args["start_iso"],
                                     args.get("minutes", 60))
        if name == "leave_note_for_office":
            db = Session()
            db.add(Followup(account_id=account_id,
                            reason=(args.get("reason") or "general")[:60],
                            note=args.get("note", "")[:2000], channel="sms"))
            db.commit()
            db.close()
            return {"ok": True}
        if name == "disconnect_email":
            return disconnect_mailbox(account_id, args.get("mailbox", ""))
        if name == "delete_my_account":
            if (args.get("confirmation") or "").strip().upper() != "DELETE":
                return {"error": "they did not type DELETE — do nothing"}
            return delete_everything(account_id)
        if name == "list_mailboxes":
            return {"mailboxes": list_mailboxes(account_id)}
        if name == "name_mailbox":
            rows = list_mailboxes(account_id)
            w = (args.get("mailbox") or "").strip().lower()
            m = next((r for r in rows if w in (r["email"] or "").lower()
                      or w == (r["label"] or "").lower()), None)
            if not m:
                return {"error": "no mailbox matched"}
            db = Session()
            row = db.query(Connection).filter_by(id=m["id"]).first()
            if args.get("name"):
                row.label = args["name"][:60]
            if args.get("make_main"):
                for o in (db.query(Connection)
                            .filter_by(account_id=account_id).all()):
                    o.is_default = 1 if o.id == m["id"] else 0
            db.commit()
            db.close()
            return {"ok": True, "email": m["email"]}
        if name == "connect_email":
            db = Session()
            row = Onboard(account_id=account_id, email=args["email"],
                          state="starting")
            db.add(row)
            db.commit()
            db.refresh(row)
            sid = row.id
            db.close()
            _PENDING[sid] = {"password": args["password"],
                             "code": None, "other_way": False}
            threading.Thread(target=_run_signin,
                             args=(sid, account_id, args["email"]),
                             daemon=True).start()
            return {"session_id": sid,
                    "note": "Started. Tell them it takes about a minute."}
        if name == "check_connect":
            db = Session()
            row = db.query(Onboard).filter_by(id=args["session_id"]).first()
            db.close()
            return {"state": row.state, "message": row.message} if row \
                else {"error": "unknown session"}
        if name == "submit_code":
            sid = args["session_id"]
            if sid in _PENDING:
                _PENDING[sid]["code"] = "".join(
                    ch for ch in args["code"] if ch.isdigit())
                return {"ok": True}
            return {"error": "that sign-in is no longer running"}
        if name == "web_search":
            return tool_web_search(args["query"])
    except Exception as e:
        return {"error": str(e)[:200]}
    return {"error": "unknown tool"}


def _openai_chat(messages: list, tools=None, model: str = "",
                 account_id=None, call_id=None, cheap: bool = True) -> dict:
    """One chat call. 'cheap' decides which column the tokens are billed to,
    so the Costs tab separates the browser's brain from the helpers."""
    payload = {"model": model or MODEL_TEXT, "messages": messages}
    if tools:
        payload["tools"] = tools
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        # Mark it, so a browser job doesn't report this as a Browserbase
        # fault, and shout - a bad key here silently breaks every job.
        try:
            e._from_openai = True
        except Exception:
            pass
        emit("openai", "chat", f"{payload['model']} call failed: "
                               f"{str(e)[:180]}", "error", account_id)
        raise

    # Until now these tokens were never counted anywhere.
    try:
        u = data.get("usage") or {}
        got_in = int(u.get("prompt_tokens", 0) or 0)
        got_out = int(u.get("completion_tokens", 0) or 0)
        if got_in or got_out:
            fields = ({"mini_in": got_in, "mini_out": got_out} if cheap
                      else {"brain_in": got_in, "brain_out": got_out})
            record_usage(account_id=account_id, call_id=call_id,
                         kind="browser", **fields)
    except Exception:
        pass
    return data


# ------------------------------------------------------------- the advisor
# The voice model is fast and a poor judge: told "my phone isn't with me",
# it answered "I'm handling that" while nothing at all was running. Rules
# were added one call at a time and there is no end to them.
#
# So judgement moves here. The backend reads the real state - what is
# running, what failed and why, what is connected - and a slower model
# decides the next step and the words. The voice model speaks them.
#
# What it may NOT decide is permission: reading an order total back,
# getting a yes before sending or charging, stopping at a human check.
# Those stay in code, where a model cannot talk itself past them.

MODEL_ADVISOR = os.environ.get("MODEL_ADVISOR", MODEL_BROWSER)

WORKING_CLAIMS = _re_scrub.compile(
    r"(?i)(i.?m (working|handling|looking|checking|signing|placing|getting)"
    r"|i am (working|handling|looking|checking)|let me (check|look|see)"
    r"|hold on while|one moment while|i.?ll (check|look) (on )?that now"
    r"|still (working|trying|waiting)|in progress|handling (that|it))")


def call_state(account_id: int, call_id: int = 0) -> dict:
    """Everything true about this caller right now, read from the database
    and the running jobs - never from what the model believes."""
    now = datetime.now(_tz())
    out = {"now": f"{now:%A, %B} {now.day} at {_clock(now)}",
           "account_id": account_id, "running": [], "finished": [],
           "mailboxes": [], "saved_logins": [], "cards": [], "addresses": []}
    db = Session()
    acct = db.query(Account).filter_by(id=account_id).first()
    out["name"] = acct.name if acct else ""

    q = db.query(Job).filter_by(account_id=account_id)
    if call_id:
        q = q.filter_by(call_id=call_id)
    for j in q.order_by(Job.id.desc()).limit(6).all():
        row = {"job_id": j.id, "what": j.kind, "site": j.site,
               "state": j.state, "reason": j.reason or "",
               # what the page actually said, so "what does it say?" has an
               # answer instead of "the page content isn't known"
               "message": (j.message or "")[:900]}
        if j.state in ("done", "failed"):
            out["finished"].append(row)
        else:
            row["still_running"] = j.id in _JOBS
            out["running"].append(row)

    for c in (db.query(Connection)
                .filter_by(account_id=account_id, provider="google").all()):
        days = ((datetime.utcnow() - c.linked_at).total_seconds() / 86400.0
                if c.linked_at else 999)
        out["mailboxes"].append({
            "email": c.email, "label": c.label or "",
            "connected_days_ago": round(days, 1),
            "probably_expired": days >= 7})
    out["saved_logins"] = [
        s.site for s in db.query(SiteLogin)
                          .filter_by(account_id=account_id).all()]
    out["cards"] = [f"{c.brand} ending {c.last4}"
                    for c in db.query(PaymentCard)
                               .filter_by(account_id=account_id).all()]
    out["addresses"] = [a.label or "home"
                        for a in db.query(Address)
                                   .filter_by(account_id=account_id).all()]
    db.close()
    out["anything_running"] = bool(out["running"])
    return out


ADVISOR_SYSTEM = """You decide what a telephone assistant does next, and the
exact words it says.

The caller is usually elderly, has no internet, no screen, and often cannot
read a text message. Everything must be doable by voice alone.

You are given FACTS read from the system a moment ago. They are the only
truth. What the assistant said earlier may be wrong; the facts are not.

Rules:
- Never say or imply that something is being worked on unless the facts show
  a job still running. If nothing is running, say plainly what the position
  is.
- Never invent a detail. If the facts don't say where a code went, or what a
  page said, say that it isn't known.
- Say what you DID, or what you are about to do, and then either do it or
  ask one clear question. Never leave the caller waiting with nothing said.
- One question at a time. Short sentences. No jargon, no menus, no lists of
  options longer than two.
- Money and sending: never say anything is sent, ordered or charged unless
  the facts show it happened.
- If the right next step needs a tool the assistant has, name it.

Answer as JSON only:
{"say": "<the words to speak, at most 45 words>",
 "next": "<one tool name, or empty if nothing to call>",
 "why": "<one short line for the office log>"}"""


def advise(account_id: int, call_id: int, situation: str,
           heard: str = "") -> dict:
    """What should happen next, decided from the facts."""
    state = call_state(account_id, call_id)
    msgs = [{"role": "system", "content": ADVISOR_SYSTEM},
            {"role": "user", "content":
                f"FACTS:\n{json.dumps(state, default=str)[:4000]}\n\n"
                f"WHAT IS HAPPENING: {situation[:600]}\n"
                f"WHAT THE CALLER JUST SAID: {heard[:300] or '(nothing)'}"}]
    try:
        d = _openai_chat(msgs, model=MODEL_ADVISOR, account_id=account_id,
                         call_id=call_id, cheap=False)
        raw = (d["choices"][0]["message"].get("content") or "").strip()
    except Exception as e:
        emit("advisor", f"call {call_id}", f"advisor failed: {str(e)[:150]}",
             "error", account_id)
        return {"say": "", "next": "", "why": "advisor unavailable",
                "error": True}
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{"):]
    try:
        out = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    except Exception:
        out = {"say": raw[:300], "next": "", "why": "unparsed"}

    said = out.get("say", "")
    # The one thing the voice model kept getting wrong, now checked in code
    # rather than trusted to a prompt: you cannot claim to be working on
    # something when nothing is running.
    if said and not state["anything_running"] and WORKING_CLAIMS.search(said):
        emit("advisor", f"call {call_id}",
             "advice claimed work was in progress while nothing was running "
             "- replaced", "warn", account_id)
        out["say"] = ("Nothing is running at the moment. " + said)
        out["corrected"] = True
    if is_blocked(said):
        out["say"] = BLOCKED_REPLY
    out["facts"] = {"anything_running": state["anything_running"],
                    "running": state["running"],
                    "mailboxes": state["mailboxes"]}
    return out


REVIEW_SYSTEM = """You are checking a finished telephone call for one thing
only: did the assistant tell the caller anything the record does not support?

You get the spoken turns and the record of what the system actually did,
including each step a job went through. A job's own steps are evidence: if
the record shows a sign-in was opened, then "I'm signing in now" was true,
even if the sign-in later failed. Only flag what the record contradicts or
does not mention at all.

Count as a problem:
- claiming something was being worked on when no job was running
- claiming an email was sent, an order placed, or a card charged, with no
  record of it
- inventing a detail: a time, an address, a price, where a code was sent
- leaving the caller waiting with no answer, or ending with their question
  unanswered
- asking the caller to do something they cannot do by phone

Do NOT report tone, politeness, or wording you merely dislike, and do not
report a refusal to discuss a blocked subject.

Answer as JSON only:
{"problems": [{"quote": "<what the assistant said>",
               "why": "<one line>",
               "severity": "high|low"}],
 "verdict": "<one line for the office>"}"""


PROFILE_SYSTEM = """You keep the notes a telephone assistant reads before
speaking to someone it has helped before.

You are given the notes so far and a transcript of the call that just
finished. Return the notes as they should now stand.

Keep only STANDING facts - things likely to matter on a future call:
- how they need to be spoken to (hard of hearing, prefers Yiddish words,
  speaks slowly, gets tired)
- who their people are and how they refer to them ("my son Moshe",
  "the office" = their bookkeeper)
- which mailbox is which, which shops they use, what they buy regularly
- how they like things done ("always read the total twice", "never order
  before Sunday")
- what has gone wrong for them before, and what worked instead
- anything they asked us to remember

Never keep:
- passwords, PINs, codes, card numbers, or anything secret
- one-off details of a single call (what was said, an order number)
- guesses. If you are not sure it is true, leave it out.
- anything about health, religion or family circumstances beyond what is
  needed to do the job

Rules: one short fact per line, plain English, no bullets or numbering.
At most 25 lines. Keep every line from the existing notes that is still
true; drop what the call shows is wrong; add what is new.

If the call adds nothing, return the notes exactly as they were. If there
were no notes and the call gives you nothing worth keeping, return NOTHING
and nothing else. Never write a sentence about the notes themselves, such
as "no notes available" - that is not a fact about the person."""


def learn_about_caller(call_id: int) -> dict:
    """Update what we know about this customer from the call that just
    ended. Runs on its own after every call."""
    db = Session()
    call = db.query(Call).filter_by(id=call_id).first()
    if not call or not call.account_id:
        db.close()
        return {"skipped": "no account"}
    account_id = call.account_id
    turns = (db.query(CallTurn).filter_by(call_id=call_id)
               .order_by(CallTurn.id).all())
    row = db.query(Profile).filter_by(account_id=account_id).first()
    before = (row.notes if row else "") or ""
    by_hand = (row.by_hand if row else "") or ""
    db.close()
    said = [f"{t.who}: {(t.text or '')[:300]}" for t in turns
            if t.who in ("caller", "agent")]
    if len(said) < 4 or not OPENAI_API_KEY:
        return {"skipped": "too short"}
    msgs = [{"role": "system", "content": PROFILE_SYSTEM},
            {"role": "user", "content":
                f"NOTES SO FAR:\n{before or '(none yet)'}\n\n"
                f"THE CALL:\n" + "\n".join(said)[:6000]}]
    try:
        d = _openai_chat(msgs, model=MODEL_ADVISOR, account_id=account_id,
                         call_id=call_id, cheap=False)
        notes = (d["choices"][0]["message"].get("content") or "").strip()
    except Exception as e:
        emit("profile", f"call {call_id}", f"could not update the notes: "
                                           f"{str(e)[:150]}", "warn",
             account_id)
        return {"error": str(e)[:200]}

    # Same rule as everywhere else: a secret never gets written down, even
    # if a model decides it is worth remembering.
    notes = scrub(notes)[:4000]
    lines = [ln.strip(" -*\t") for ln in notes.splitlines() if ln.strip()]
    notes = "\n".join(lines[:25])
    # A model asked for notes will often write a sentence ABOUT the notes.
    # "No notes available." was stored as a fact and read back on the next
    # call as if it were something we knew about the person.
    if len(lines) <= 1 and _re_scrub.match(
            r"(?i)^\W*(nothing|none|no notes?|no standing|n/?a|unchanged)\b",
            notes or ""):
        return {"skipped": "nothing worth keeping"}
    if not notes:
        return {"skipped": "nothing to keep"}
    if notes.strip() == (before or "").strip():
        return {"account_id": account_id, "notes": notes,
                "unchanged": True}

    db = Session()
    row = db.query(Profile).filter_by(account_id=account_id).first()
    if row:
        row.notes = notes
        row.updated = datetime.utcnow()
    else:
        db.add(Profile(account_id=account_id, notes=notes))
    db.commit()
    db.close()
    fresh = [ln for ln in notes.splitlines()
             if ln.strip() and ln.strip() not in before]
    if fresh:
        record_change(account_id, "notes", "learned",
                      "now also knows: " + "; ".join(fresh)[:300],
                      call_id=call_id)
    emit("profile", f"call {call_id}", "what we know about them was updated",
         "info", account_id)
    return {"account_id": account_id, "notes": notes, "by_hand": by_hand}


def profile_for(account_id: int) -> str:
    """What the assistant is told before it speaks to them. Staff notes
    first: a person who took the trouble to write something knows more
    than the model does."""
    db = Session()
    row = db.query(Profile).filter_by(account_id=account_id).first()
    db.close()
    if not row:
        return ""
    parts = []
    if (row.by_hand or "").strip():
        parts.append("FROM THE OFFICE:\n" + row.by_hand.strip())
    if (row.notes or "").strip():
        parts.append("FROM EARLIER CALLS:\n" + row.notes.strip())
    return "\n\n".join(parts)


def review_call(call_id: int) -> dict:
    """Read a finished call and flag anything the assistant said that the
    record doesn't back up. Runs on its own after every call, so a customer
    doesn't have to ring back and report it."""
    db = Session()
    call = db.query(Call).filter_by(id=call_id).first()
    if not call:
        db.close()
        return {"skipped": "no such call"}
    turns = (db.query(CallTurn).filter_by(call_id=call_id)
               .order_by(CallTurn.id).all())
    jobs = db.query(Job).filter_by(call_id=call_id).all()
    account_id = call.account_id
    said = [f"{t.who}: {(t.text or '')[:300]}" for t in turns]
    # The step-by-step history, not just the final state. Without it the
    # reviewer called "I'm signing in to Amazon now" a false claim, when a
    # sign-in really was running - it just ended badly.
    did = []
    for j in jobs:
        did.append(f"job {j.id} {j.kind} {j.site}: ended {j.state} "
                   f"{j.reason or ''} {(j.message or '')[:200]}")
        for line in (j.history or "").strip().splitlines()[-12:]:
            did.append(f"    {line.strip()[:200]}")
    db.close()
    if len([t for t in turns if t.who == "agent"]) < 2:
        return {"skipped": "too short to review"}
    if not OPENAI_API_KEY:
        return {"skipped": "no model configured"}
    msgs = [{"role": "system", "content": REVIEW_SYSTEM},
            {"role": "user", "content":
                "WHAT WAS SAID:\n" + "\n".join(said)[:6000]
                + "\n\nWHAT THE SYSTEM ACTUALLY DID:\n"
                + ("\n".join(did)[:2000] or "(nothing ran)")}]
    try:
        d = _openai_chat(msgs, model=MODEL_ADVISOR, account_id=account_id,
                         call_id=call_id, cheap=False)
        raw = (d["choices"][0]["message"].get("content") or "").strip()
        out = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    except Exception as e:
        emit("review", f"call {call_id}", f"review failed: {str(e)[:150]}",
             "warn", account_id)
        return {"error": str(e)[:200]}

    problems = [p for p in out.get("problems", []) if p.get("quote")]
    if problems:
        note = (f"CALL REVIEW - call {call_id}\n"
                + out.get("verdict", "") + "\n\n"
                + "\n".join(f"- [{p.get('severity', 'low')}] "
                             f"\"{p.get('quote', '')[:160]}\" - "
                             f"{p.get('why', '')[:200]}" for p in problems))
        db = Session()
        db.add(Followup(account_id=account_id, call_id=call_id,
                        reason="call_review", note=note[:4000],
                        channel="voice"))
        db.commit()
        db.close()
        worst = ("error" if any(p.get("severity") == "high" for p in problems)
                 else "warn")
        emit("review", f"call {call_id}",
             f"{len(problems)} thing(s) the assistant said aren't backed by "
             f"the record: {out.get('verdict', '')[:160]}", worst,
             account_id)
    else:
        emit("review", f"call {call_id}", "call reviewed - nothing said that "
                                          "the record doesn't support",
             "info", account_id)
    out["problems"] = problems
    out["call_id"] = call_id
    return out


def text_brain(account_id: int, incoming: str) -> str:
    """Answer one incoming text, using shared memory and the same tools."""
    if is_blocked(incoming):
        return "I am not allowed to talk to you about this."
    if not OPENAI_API_KEY:
        return "The assistant isn't configured yet."

    today = datetime.now().strftime("%A, %B %-d, %Y")
    history = mem_recent(account_id, 16)
    msgs = [{"role": "system",
             "content": TEXT_RULES + f"\n\nToday is {today}."}]
    for h in history:
        msgs.append({
            "role": "user" if h["who"] == "user" else "assistant",
            "content": f"({h['channel']}) {h['text']}",
        })
    msgs.append({"role": "user", "content": incoming})

    for _ in range(4):
        try:
            data = _openai_chat(msgs, TEXT_TOOLS, model=MODEL_TEXT,
                                account_id=account_id)
        except Exception as e:
            return f"Something went wrong: {str(e)[:80]}"

        choice = data["choices"][0]["message"]
        calls = choice.get("tool_calls") or []
        if not calls:
            return (choice.get("content") or "").strip()[:600]

        msgs.append(choice)
        for c in calls:
            fn = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            result = _run_text_tool(account_id, fn, args)
            if isinstance(result, dict) and result.get("error"):
                db = Session()
                db.add(Followup(account_id=account_id, reason=fn[:60],
                                note=f"{fn} failed: {result['error']}"[:2000],
                                channel="sms"))
                db.commit()
                db.close()
            msgs.append({"role": "tool", "tool_call_id": c["id"],
                         "content": json.dumps(result)[:3000]})

    return "I couldn't finish that one — try asking a different way."


# ----------------------------------------------------------------- api

app = FastAPI(title="Phone Assistant")


# NB: this file must NOT be called site.py - Python has a built-in module
# of that name and shadowing it breaks the interpreter's startup.
from site_pages import (HOME, SIGNUP, PRIVACY, TERMS, CONNECT,
                        LINK_EXPIRED, NOT_CONNECTED, connect_confirm,
                        connected, CARD, CARD_NOT_SAVED, card_saved)


@app.get("/health")
def health():
    return {"ok": True, "service": "phone-assistant", "step": "gmail"}


@app.get("/", response_class=HTMLResponse)
def home():
    """The public front page. Google's reviewers land here, and so do the
    families who set an elderly customer up."""
    return HTMLResponse(HOME)


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    """Required for Google verification, and it has to be on a domain you
    own - a railway.app address will not pass."""
    return HTMLResponse(PRIVACY)


@app.get("/terms", response_class=HTMLResponse)
def terms():
    return HTMLResponse(TERMS)


@app.get("/connect", response_class=HTMLResponse)
def connect_page():
    """Where a son, daughter or neighbour connects a customer's email, with
    the code the assistant read out on the phone."""
    return HTMLResponse(CONNECT)


class ConnectBody(BaseModel):
    phone: str = ""
    code: str = ""


@app.post("/connect")
def connect_with_code(b: ConnectBody, request: Request):
    """Phone number plus code -> the signed link for that customer. The
    same answer for a wrong code and an unknown number, so the page can't
    be used to find out who is a customer."""
    digits = "".join(ch for ch in (b.phone or "") if ch.isdigit())[-10:]
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
          or (request.client.host if request.client else "?"))
    if (_connect_too_many("p" + digits, CONNECT_MAX_FAILS)
            or _connect_too_many("i" + ip, CONNECT_MAX_FAILS_IP)):
        raise HTTPException(429, "Too many tries. Please wait an hour, or "
                                 "ask the assistant for a new code.")
    acct = account_for_number(digits) if len(digits) == 10 else None
    if not acct or not _connect_code_ok(acct.id, b.code):
        _connect_too_many("p" + digits, CONNECT_MAX_FAILS, add=True)
        _connect_too_many("i" + ip, CONNECT_MAX_FAILS_IP, add=True)
        emit("signin", "connect page", "a connect code didn't match", "warn")
        raise HTTPException(400, "That code doesn't match that phone number. "
                                 "Check both and try again.")
    emit("signin", "connect page", f"code accepted for {acct.name}", "info",
         acct.id)
    t = _make_link_token(acct.id)
    return {"url": f"/link/start?t={urllib.parse.quote(t)}"}


@app.get("/link/code")
def link_code(request: Request, account_id: int):
    """A connect code for the assistant to read out. Agent and staff only."""
    require_auth(request)
    db = Session()
    row = db.query(Account).filter_by(id=account_id).first()
    db.close()
    if not row:
        raise HTTPException(404, "No such customer.")
    return {"code": _connect_code(account_id), "valid_minutes": 60,
            "page": f"{PUBLIC_URL}/connect", "for": row.name}


@app.get("/card", response_class=HTMLResponse)
def card_page():
    """Where a card is added, on Stripe's own page, with a code the
    assistant read out."""
    return HTMLResponse(CARD)


@app.post("/card")
def card_with_code(b: ConnectBody, request: Request):
    digits = "".join(ch for ch in (b.phone or "") if ch.isdigit())[-10:]
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
          or (request.client.host if request.client else "?"))
    if (_connect_too_many("cp" + digits, CONNECT_MAX_FAILS)
            or _connect_too_many("ci" + ip, CONNECT_MAX_FAILS_IP)):
        raise HTTPException(429, "Too many tries. Please wait an hour, or "
                                 "ask the assistant for a new code.")
    acct = account_for_number(digits) if len(digits) == 10 else None
    if not acct or not _connect_code_ok(acct.id, b.code, "card"):
        _connect_too_many("cp" + digits, CONNECT_MAX_FAILS, add=True)
        _connect_too_many("ci" + ip, CONNECT_MAX_FAILS_IP, add=True)
        emit("cards", "card page", "a card code didn't match", "warn")
        raise HTTPException(400, "That code doesn't match that phone number. "
                                 "Check both and try again.")
    if not STRIPE_SECRET_KEY:
        raise HTTPException(503, "Cards can't be added just yet. Please call "
                                 "us.")
    try:
        url = stripe_card_page_url(acct.id)
    except StripeError as e:
        emit("cards", "card page", f"Stripe refused: {e.message[:150]}",
             "error", acct.id)
        raise HTTPException(502, "Something went wrong. Please try again "
                                 "shortly.")
    emit("cards", "card page", f"card page opened for {acct.name}", "info",
         acct.id)
    return {"url": url}


@app.get("/card/done", response_class=HTMLResponse)
def card_done(session_id: str = ""):
    try:
        got = stripe_save_finished(session_id)
    except (HTTPException, StripeError):
        return HTMLResponse(CARD_NOT_SAVED, status_code=400)
    return HTMLResponse(card_saved(got["brand"], got["last4"]))


@app.get("/card/code")
def card_code(request: Request, account_id: int):
    """A card code for the assistant to read out. Agent and staff only."""
    require_auth(request)
    db = Session()
    row = db.query(Account).filter_by(id=account_id).first()
    db.close()
    if not row:
        raise HTTPException(404, "No such customer.")
    return {"code": _connect_code(account_id, purpose="card"),
            "valid_minutes": 60, "page": f"{PUBLIC_URL}/card"}


class ChargeBody(BaseModel):
    account_id: int
    card_id: int
    amount: str                 # "12.50"
    what_for: str
    key: str                    # the same key never charges twice


@app.post("/charges")
def charge(b: ChargeBody, request: Request):
    require_auth(request)
    try:
        cents = int(round(float(b.amount.replace("$", "").strip()) * 100))
    except ValueError:
        raise HTTPException(400, "bad_amount")
    if not b.key.strip() or not b.what_for.strip():
        raise HTTPException(400, "A reason and a key are needed.")
    return stripe_charge(b.account_id, b.card_id, cents, b.what_for, b.key)


@app.get("/signup", response_class=HTMLResponse)
def signup_page():
    return HTMLResponse(SIGNUP)


class SignupBody(BaseModel):
    name: str = ""
    phone: str = ""
    helper: str = ""
    note: str = ""


@app.post("/signup")
def signup_request(b: SignupBody):
    """Somebody asking to be set up. Deliberately not an account - accounts
    need a phone number we have confirmed, and most of these will be a
    daughter arranging it for her father."""
    who = (b.name or "").strip()[:120]
    phone = (b.phone or "").strip()[:40]
    if not who or not phone:
        raise HTTPException(400, "A name and a telephone number are needed.")
    note = (f"NEW SIGNUP REQUEST\nFor: {who}\nTheir number: {phone}\n"
            f"Arranged by: {(b.helper or '(themselves)').strip()[:120]}\n"
            f"Notes: {(b.note or '').strip()[:600]}")
    db = Session()
    db.add(Followup(reason="signup", note=note, channel="web"))
    db.commit()
    db.close()
    emit("signup", "web", f"someone asked to be set up: {who} ({phone})",
         "warn")
    return {"ok": True}


class NewAccount(BaseModel):
    name: str
    phone: str | None = None
    pin: str = "1234"


@app.post("/accounts")
def create_account(a: NewAccount, request: Request):
    require_auth(request)
    db = Session()
    acct = Account(name=a.name, pin=a.pin)
    db.add(acct)
    db.commit()
    db.refresh(acct)
    if a.phone:
        db.add(PhoneNumber(number=a.phone, account_id=acct.id))
        db.commit()
    out = {"account_id": acct.id, "name": acct.name, "phone": a.phone}
    db.close()
    return out


@app.get("/accounts")
def list_accounts(request: Request, q: str = ""):
    require_auth(request)
    db = Session()
    rows = []
    for acct in db.query(Account).all():
        nums = [p.number for p in
                db.query(PhoneNumber).filter_by(account_id=acct.id).all()]
        conns = (db.query(Connection)
                   .filter_by(account_id=acct.id, provider="google").all())
        rows.append({
            "account_id": acct.id,
            "name": acct.name,
            "phones": nums,
            "gmail": conns[0].email if conns else None,
            "mailboxes": [{"id": c.id, "email": c.email, "label": c.label,
                           "default": bool(c.is_default),
                           "used": c.use_count or 0} for c in conns],
        })
    if q:
        w = q.strip().lower()
        rows = [r for r in rows
                if w in (r["name"] or "").lower()
                or any(w in p for p in r["phones"])
                or any(w in (m["email"] or "").lower()
                       for m in r["mailboxes"])
                or str(r["account_id"]) == w]
    db.close()
    return rows


class PinCheck(BaseModel):
    account_id: int
    pin: str


@app.post("/accounts/verify_pin")
def verify_account_pin(b: PinCheck, request: Request):
    """Say whether a spoken PIN matches, without ever sending it out.

    The agent gets a yes or a no and nothing else. Doing the comparison
    here keeps the PIN out of the voice process, and out of /accounts -
    which has never returned one, so the agent's own fallback of "1234"
    was the value every caller was checked against.
    """
    require_auth(request)
    db = Session()
    acct = db.query(Account).filter_by(id=b.account_id).first()
    stored = (acct.pin or "") if acct else ""
    db.close()
    if not stored:
        return {"ok": False}
    import hmac
    said = "".join(ch for ch in (b.pin or "") if ch.isdigit())
    ok = bool(said) and hmac.compare_digest(said, stored)
    if not ok:
        # What was said stays out of the log; that it was refused doesn't.
        emit("call", f"account {b.account_id}", "PIN refused", "warn",
             account_id=b.account_id)
    return {"ok": ok}


@app.get("/mailboxes")
def mailboxes(request: Request, account_id: int):
    require_auth(request)
    return list_mailboxes(account_id)


@app.post("/mailboxes/label")
def mailbox_label(request: Request, connection_id: int, label: str = "",
                  make_default: int = 0):
    require_auth(request)
    db = Session()
    row = db.query(Connection).filter_by(id=connection_id).first()
    if not row:
        db.close()
        raise HTTPException(404, "Unknown mailbox.")
    if label:
        row.label = label[:60]
    if make_default:
        for other in (db.query(Connection)
                        .filter_by(account_id=row.account_id).all()):
            other.is_default = 1 if other.id == connection_id else 0
    db.commit()
    db.close()
    return {"ok": True}


@app.delete("/mailboxes")
def mailbox_remove(request: Request, connection_id: int):
    require_auth(request)
    db = Session()
    row = db.query(Connection).filter_by(id=connection_id).first()
    if not row:
        db.close()
        raise HTTPException(404, "Unknown mailbox.")
    acct_id, email = row.account_id, row.email
    db.close()
    out = disconnect_mailbox(acct_id, email)
    return out


@app.post("/mailboxes/disconnect")
def mailbox_disconnect(request: Request, account_id: int, which: str = ""):
    require_auth(request)
    return disconnect_mailbox(account_id, which)


@app.delete("/account")
def account_delete(request: Request, account_id: int, confirm: str = ""):
    """Erase a customer completely. confirm must be the word DELETE."""
    require_auth(request)
    if confirm != "DELETE":
        raise HTTPException(400, "Pass confirm=DELETE.")
    return delete_everything(account_id)


@app.get("/link/start")
def link_start(request: Request, t: str = "", account_id: int = 0):
    """The page a customer lands on to connect their email.

    It used to take account_id straight from the address, with nothing
    checking it. Anyone could send someone a link with THEIR account
    number in it: the customer would sign into their own Gmail, and the
    mailbox would be attached to the sender's account instead. They could
    then ring in and hear that person's email read to them.

    Links are now signed and expire, and the customer is shown whose
    account they are about to connect to before anything happens."""
    acc = _check_link_token(t)
    if not acc:
        return HTMLResponse(LINK_EXPIRED, status_code=400)
    db = Session()
    row = db.query(Account).filter_by(id=acc).first()
    name = row.name if row else ""
    db.close()
    if not name:
        return HTMLResponse(LINK_EXPIRED, status_code=400)
    if request.query_params.get("go") != "1":
        return HTMLResponse(connect_confirm(
            name, f"/link/start?t={urllib.parse.quote(t)}&go=1"))
    # The state Google hands back is the signed ticket, not a bare account
    # number - otherwise anyone could build a Google link carrying THEIR
    # account number and skip our checks entirely.
    url, _ = _flow(state=t).authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true")
    return RedirectResponse(url)


@app.get("/link/new")
def link_new(request: Request, account_id: int, minutes: int = 30):
    """Make a signed link for one customer. Staff and the voice agent only."""
    require_auth(request)
    db = Session()
    row = db.query(Account).filter_by(id=account_id).first()
    db.close()
    if not row:
        raise HTTPException(404, "No such customer.")
    t = _make_link_token(account_id, minutes)
    return {"url": f"{PUBLIC_URL}/link/start?t={urllib.parse.quote(t)}",
            "for": row.name, "valid_minutes": minutes}


@app.get("/link/callback")
def link_callback(request: Request):
    state = request.query_params.get("state") or ""
    if request.query_params.get("error"):
        return HTMLResponse(NOT_CONNECTED)
    account_id = _check_link_token(state)
    if not account_id:
        return HTMLResponse(LINK_EXPIRED, status_code=400)

    flow = _flow(state=state)
    flow.fetch_token(authorization_response=str(request.url).replace(
        "http://", "https://", 1))
    creds = flow.credentials

    email = ""
    try:
        info = build("oauth2", "v2", credentials=creds,
                     cache_discovery=False).userinfo().get().execute()
        email = info.get("email", "")
    except Exception:
        pass

    blob = vault_put({
        "token": creds.token,
        "refresh_token": creds.refresh_token,
    })

    db = Session()
    existing = (db.query(Connection)
                  .filter_by(account_id=account_id, provider="google").all())
    same = next((c for c in existing
                 if (c.email or "").lower() == (email or "").lower()), None)
    if same:
        same.secret_blob = blob
        same.linked_at = datetime.utcnow()
    else:
        db.add(Connection(account_id=account_id, provider="google",
                          email=email, secret_blob=blob,
                          is_default=0 if existing else 1))
    db.commit()
    db.close()

    record_change(account_id, "mailbox", "connected",
                  f"connected {email or 'a Google account'}")
    emit("signin", "connected", f"mailbox connected: {email}", "info",
         account_id)
    return HTMLResponse(connected(email))


@app.get("/test/unread")
def test_unread(request: Request, account_id: int, limit: int = 5,
                which: str = "", primary_only: bool = False):
    require_auth(request)
    return tool_unread_summary(account_id, limit, which, primary_only)


@app.post("/email/mark_read")
def email_mark_read(request: Request, account_id: int, msg_ids: str = "",
                    read: bool = True, which: str = "",
                    all_unread: bool = False, primary_only: bool = False):
    """Mark one, several, or every unread message as read."""
    require_auth(request)
    if all_unread:
        out = tool_mark_all_read(account_id, which, primary_only)
        emit("email", "mark read", f"marked {out['marked_read']} read "
                                   f"({out['scope']})", "info", account_id)
        return out
    ids = [i for i in msg_ids.split(",") if i.strip()]
    if not ids:
        raise HTTPException(400, "Give msg_ids, or set all_unread=true.")
    return tool_mark_read(account_id, ids, read, which)


class ReplyBody(BaseModel):
    account_id: int
    msg_id: str
    body: str
    which: str = ""
    all_recipients: bool = False


@app.post("/email/reply")
def email_reply(b: ReplyBody, request: Request):
    require_auth(request)
    out = tool_reply_email(b.account_id, b.msg_id, b.body, b.which,
                           b.all_recipients)
    record_change(b.account_id, "email", "replied",
                  f"replied to {out.get('to', '')} about "
                  f"\"{out.get('subject', '')}\"")
    emit("email", "reply", f"replied to {out.get('to', '')}", "info",
         b.account_id)
    return out


class ForwardBody(BaseModel):
    account_id: int
    msg_id: str
    to: str
    note: str = ""
    which: str = ""


@app.post("/email/forward")
def email_forward(b: ForwardBody, request: Request):
    require_auth(request)
    out = tool_forward_email(b.account_id, b.msg_id, b.to, b.note, b.which)
    record_change(b.account_id, "email", "forwarded",
                  f"forwarded \"{out.get('subject', '')}\" to {b.to}")
    emit("email", "forward", f"forwarded to {b.to}", "info", b.account_id)
    return out


class DraftBody(BaseModel):
    account_id: int
    to: str
    subject: str
    body: str
    which: str = ""


@app.post("/email/draft")
def email_draft(b: DraftBody, request: Request):
    require_auth(request)
    out = tool_draft_email(b.account_id, b.to, b.subject, b.body, b.which)
    record_change(b.account_id, "email", "drafted",
                  f"saved a draft to {b.to}: \"{b.subject}\" (not sent)")
    return out


@app.post("/email/action")
def email_action(request: Request, account_id: int, msg_ids: str,
                 action: str, which: str = ""):
    """archive, unarchive, star, unstar, important, spam, not_spam,
    trash, untrash. Nothing here is permanent."""
    require_auth(request)
    ids = [i for i in msg_ids.split(",") if i.strip()]
    if not ids:
        raise HTTPException(400, "No messages given.")
    out = tool_message_action(account_id, ids, action, which)
    record_change(account_id, "email", action,
                  f"{action} on {out.get('changed', len(ids))} message(s)",
                  undo=out.get("undo", ""))
    emit("email", action, f"{action} on {len(ids)} message(s)", "info",
         account_id)
    return out


@app.get("/email/attachments")
def email_attachments(request: Request, account_id: int, msg_id: str,
                      which: str = ""):
    require_auth(request)
    return tool_attachments(account_id, msg_id, which)


@app.get("/email/attachment")
def email_attachment(request: Request, account_id: int, msg_id: str,
                     attachment_id: str, which: str = ""):
    require_auth(request)
    return tool_attachment_text(account_id, msg_id, attachment_id, which)


@app.get("/test/read")
def test_read(request: Request, account_id: int, msg_id: str,
              which: str = ""):
    require_auth(request)
    return tool_read_email(account_id, msg_id, which)


@app.get("/test/search")
def test_search(request: Request, account_id: int, q: str, limit: int = 5,
                which: str = "", newest_first: bool = False):
    require_auth(request)
    return tool_search_email(account_id, q, limit, which, newest_first)


from googleapiclient.errors import HttpError
from google.auth.exceptions import RefreshError


@app.exception_handler(RefreshError)
async def google_token_dead(request: Request, exc: RefreshError):
    """Google has stopped honouring the saved permission - revoked, or the
    seven-day limit that applies while the app is unverified. Every email
    and calendar tool used to answer this with a 500, which the assistant
    reads out as "something went wrong"."""
    emit("google", request.url.path,
         "the Google connection has expired - the customer has to connect "
         "again", "warn")
    return JSONResponse({"detail": "connection_expired", "status": 401},
                        status_code=403)


@app.exception_handler(HttpError)
async def google_refused(request: Request, exc: HttpError):
    """Google said no. Say WHY in a word the assistant can act on, instead
    of a 500 that reads as "something broke"."""
    status = getattr(getattr(exc, "resp", None), "status", 500) or 500
    try:
        body = (exc.content or b"").decode("utf-8", "ignore")
    except Exception:
        body = ""
    if ("ACCESS_TOKEN_SCOPE_INSUFFICIENT" in body
            or "insufficient authentication scopes" in body.lower()):
        reason, level = "needs_reconnect", "warn"
    elif ("SERVICE_DISABLED" in body or "accessNotConfigured" in body
          or "has not been used in project" in body):
        reason, level = "api_not_enabled", "error"
    elif status == 404:
        reason, level = "not_found", "warn"
    else:
        reason, level = "google_error", "error"
    emit("google", request.url.path,
         f"Google refused ({status}): {reason}", level)
    code = 403 if reason in ("needs_reconnect", "api_not_enabled") else int(
        status)
    return JSONResponse({"detail": reason, "status": int(status)},
                        status_code=code)


@app.get("/contacts/search")
def contacts_search(request: Request, account_id: int, name: str,
                    which: str = ""):
    require_auth(request)
    return tool_contacts_search(account_id, name, which)


class NewContact(BaseModel):
    account_id: int
    name: str
    phone: str = ""
    email: str = ""
    which: str = ""


@app.post("/contacts/add")
def contacts_add(b: NewContact, request: Request):
    require_auth(request)
    if not b.name.strip() or not (b.phone.strip() or b.email.strip()):
        raise HTTPException(400, "A name and a phone number or email needed.")
    out = tool_contact_add(b.account_id, b.name, b.phone, b.email, b.which)
    record_change(b.account_id, "contacts", "saved",
                  f"saved {b.name}"
                  + (f", {b.phone}" if b.phone else "")
                  + (f", {b.email}" if b.email else ""))
    emit("contacts", "saved", f"contact saved: {b.name}", "info",
         b.account_id)
    return out


@app.get("/drive/search")
def drive_search(request: Request, account_id: int, words: str = "",
                 limit: int = 5, which: str = ""):
    require_auth(request)
    return tool_drive_search(account_id, words, limit, which)


@app.get("/drive/read")
def drive_read(request: Request, account_id: int, file_id: str,
               which: str = ""):
    require_auth(request)
    return tool_drive_read(account_id, file_id, which)


class DocBody(BaseModel):
    account_id: int
    title: str = ""
    text: str = ""
    file_id: str = ""
    find: str = ""
    replace_with: str = ""
    all_of_them: bool = False
    which: str = ""


class SheetBody(BaseModel):
    account_id: int
    title: str = ""
    columns: list = []
    rows: list = []
    file_id: str = ""
    values: list = []
    row: int = 0
    column: str = ""
    value: str = ""
    which: str = ""


class FileBody(BaseModel):
    account_id: int
    file_id: str
    to: str = ""
    note: str = ""
    which: str = ""


def _drive_did(account_id: int, what: str):
    """Every change to a customer's files is recorded, in the live log for
    debugging and in the change list for a person to read."""
    emit("drive", "changed", what[:300], "info", account_id)
    record_change(account_id, "drive", what.split(":")[0][:40], what,
                  undo="Google keeps the file's own version history")


@app.post("/drive/doc/create")
def drive_doc_create(b: DocBody, request: Request):
    require_auth(request)
    out = tool_doc_create(b.account_id, b.title, b.text, b.which)
    _drive_did(b.account_id, f"created doc: {out['file']['name']}")
    return out


@app.post("/drive/doc/add")
def drive_doc_add(b: DocBody, request: Request):
    require_auth(request)
    out = tool_doc_add(b.account_id, b.file_id, b.text, b.which)
    _drive_did(b.account_id, f"added to {out['name']}")
    return out


@app.post("/drive/doc/replace")
def drive_doc_replace(b: DocBody, request: Request):
    require_auth(request)
    out = tool_doc_replace(b.account_id, b.file_id, b.find, b.replace_with,
                           b.all_of_them, b.which)
    if out.get("changed"):
        _drive_did(b.account_id, f"changed words in {out['name']} "
                                 f"({out['times']}x)")
    return out


@app.post("/drive/sheet/create")
def drive_sheet_create(b: SheetBody, request: Request):
    require_auth(request)
    out = tool_sheet_create(b.account_id, b.title, b.columns, b.rows, b.which)
    _drive_did(b.account_id, f"created sheet: {out['file']['name']}")
    return out


@app.get("/drive/sheet")
def drive_sheet_read(request: Request, account_id: int, file_id: str,
                     which: str = ""):
    require_auth(request)
    return tool_sheet_read(account_id, file_id, which)


@app.post("/drive/sheet/add_row")
def drive_sheet_add_row(b: SheetBody, request: Request):
    require_auth(request)
    out = tool_sheet_add_row(b.account_id, b.file_id, b.values, b.which)
    _drive_did(b.account_id, f"added a row to {out['name']}")
    return out


@app.post("/drive/sheet/update")
def drive_sheet_update(b: SheetBody, request: Request):
    require_auth(request)
    out = tool_sheet_update(b.account_id, b.file_id, b.row, b.column,
                            b.value, b.which)
    _drive_did(b.account_id, f"changed {out['name']} row {out['row']} "
                             f"{out['column']}")
    return out


@app.post("/drive/copy_editable")
def drive_copy_editable(b: FileBody, request: Request):
    require_auth(request)
    out = tool_drive_editable_copy(b.account_id, b.file_id, b.which)
    _drive_did(b.account_id, f"editable copy of {out['original']}")
    return out


@app.post("/drive/save_pdf")
def drive_save_pdf(b: FileBody, request: Request):
    require_auth(request)
    out = tool_drive_save_pdf(b.account_id, b.file_id, b.which)
    _drive_did(b.account_id, f"saved {out['file']['name']}")
    return out


@app.post("/drive/email")
def drive_email(b: FileBody, request: Request):
    require_auth(request)
    if "@" not in b.to:
        raise HTTPException(400, "Who should it go to?")
    out = tool_email_drive_file(b.account_id, b.file_id, b.to, b.note,
                                b.which)
    _drive_did(b.account_id, f"emailed {out['file']} to {b.to}")
    return out


@app.get("/todo")
def todo_list(request: Request, account_id: int, which: str = ""):
    require_auth(request)
    return tool_tasks_list(account_id, which)


class NewTask(BaseModel):
    account_id: int
    title: str
    due_date: str = ""
    notes: str = ""
    which: str = ""


@app.post("/todo/add")
def todo_add(b: NewTask, request: Request):
    require_auth(request)
    if not b.title.strip():
        raise HTTPException(400, "What is the task?")
    out = tool_task_add(b.account_id, b.title, b.due_date, b.notes, b.which)
    record_change(b.account_id, "to-do", "added",
                  f"added \"{b.title}\""
                  + (f" for {out.get('due_spoken')}"
                     if out.get("due_spoken") else ""))
    return out


@app.post("/todo/done")
def todo_done(request: Request, account_id: int, task_id: str,
              done: bool = True, which: str = ""):
    require_auth(request)
    out = tool_task_done(account_id, task_id, done, which)
    record_change(account_id, "to-do", "ticked off" if done else "reopened",
                  f"{out.get('title', 'a task')}",
                  undo="can be reopened")
    return out


@app.get("/test/contact")
def test_contact(request: Request, account_id: int, name: str,
                 which: str = ""):
    require_auth(request)
    return tool_find_contact(account_id, name, which)


@app.get("/sms/status")
def sms_status(request: Request, message_id: str):
    """Ask Telnyx what actually happened to a message."""
    require_auth(request)
    if SMS_PROVIDER != "telnyx" or not TELNYX_API_KEY:
        return {"error": "Only available for Telnyx."}
    req = urllib.request.Request(
        f"https://api.telnyx.com/v2/messages/{message_id}",
        headers={"Authorization": f"Bearer {TELNYX_API_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode()).get("data", {})
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}",
                "detail": e.read().decode()[:400]}
    tos = d.get("to") or []
    return {
        "id": d.get("id"),
        "status": tos[0].get("status") if tos else d.get("type"),
        "carrier": tos[0].get("carrier") if tos else "",
        "errors": d.get("errors", []),
        "sent_at": d.get("sent_at"),
        "completed_at": d.get("completed_at"),
    }


@app.post("/sms/dlr")
async def sms_dlr(request: Request):
    """Delivery receipt webhook. Records whatever the carrier reports."""
    try:
        body = await request.json()
    except Exception:
        try:
            form = await request.form()
            body = dict(form)
        except Exception:
            body = {"raw": (await request.body()).decode("utf-8", "replace")}

    def pick(*names):
        for n in names:
            for k, v in body.items():
                if k.lower() == n.lower() and v:
                    return v
        return ""

    to = pick("To", "Destination", "msisdn")
    if isinstance(to, list):
        to = to[0] if to else ""

    db = Session()
    db.add(Dlr(ref_id=str(pick("RefId", "MessageId", "id") or ""),
               to_number=str(to or ""),
               status=str(pick("Status", "DeliveryStatus", "state") or ""),
               raw=json.dumps(body)[:3000]))
    db.commit()
    db.close()
    return {"ok": True}


@app.get("/sms/dlr")
def dlr_list(request: Request, limit: int = 30):
    require_auth(request)
    db = Session()
    rows = db.query(Dlr).order_by(Dlr.id.desc()).limit(limit).all()
    out = [{"at": local_str(r.at, "full") if r.at else "",
            "ref_id": r.ref_id, "to": r.to_number,
            "status": r.status, "raw": r.raw} for r in rows]
    db.close()
    return out


@app.post("/sms/incoming")
async def sms_incoming(request: Request):
    """Inbound text webhook.

    BulkVS posts JSON: To (list), From (string), Message (URL-encoded),
    MediaURLs (null for SMS, list for MMS). Also tolerates Twilio-style
    form posts.
    """
    try:
        body = await request.json()
    except Exception:
        form = await request.form()
        body = dict(form)

    # Telnyx wraps everything in data.payload
    tel = None
    if isinstance(body.get("data"), dict):
        tel = body["data"].get("payload") or {}
        flat = dict(tel)
        f = flat.get("from")
        if isinstance(f, dict):
            flat["From"] = f.get("phone_number", "")
        t = flat.get("to")
        if isinstance(t, list) and t:
            flat["To"] = t[0].get("phone_number", "") \
                if isinstance(t[0], dict) else t[0]
            if isinstance(t[0], dict) and t[0].get("status"):
                flat["Status"] = t[0]["status"]
        flat["Message"] = flat.get("text", "")
        body = flat

    def pick(*names):
        for n in names:
            for k, v in body.items():
                if k.lower() == n.lower() and v:
                    return v
        return None

    raw_from = pick("From", "Sender", "source", "msisdn")
    raw_to = pick("To", "Destination")
    raw_msg = pick("Message", "Body", "text")
    media = pick("MediaURLs", "MediaUrl0")

    # BulkVS sends To as a list
    if isinstance(raw_to, list):
        raw_to = raw_to[0] if raw_to else ""
    frm = str(raw_from or "")
    text = urllib.parse.unquote_plus(str(raw_msg or ""))

    # A delivery receipt, not a customer message: no body, has a status.
    status = pick("Status", "DeliveryStatus", "state", "MessageStatus")
    if status and not raw_msg:
        db = Session()
        db.add(Dlr(ref_id=str(pick("RefId", "MessageId", "id") or ""),
                   to_number=str(raw_to or frm or ""),
                   status=str(status),
                   raw=json.dumps(body)[:3000]))
        db.commit()
        db.close()
        return {"ok": True, "dlr": True}

    if not frm:
        return {"ok": False, "error": "no sender"}

    # MMS: BulkVS blanks the text and sends media instead
    if media and not text.strip():
        acct = account_for_number(frm)
        if acct:
            tool_send_sms(frm, "I can't open pictures yet — "
                               "send it as text and I'll help.")
        return {"ok": True, "mms": True}

    if not text.strip():
        return {"ok": False, "error": "empty message"}

    acct = account_for_number(frm)
    if not acct:
        tool_send_sms(frm, "This number isn't set up yet. "
                           "Please contact the office.")
        return {"ok": True, "known": False}

    mem_add(acct.id, "sms", "user", text)
    reply = text_brain(acct.id, text)
    mem_add(acct.id, "sms", "assistant", reply)
    tool_send_sms(frm, reply)
    return {"ok": True}


@app.get("/memory")
def memory_get(request: Request, account_id: int, limit: int = 20):
    require_auth(request)
    return mem_recent(account_id, limit)


class MemBody(BaseModel):
    account_id: int
    channel: str = "voice"
    who: str = "user"
    text: str = ""


@app.post("/memory")
def memory_add(m: MemBody, request: Request):
    require_auth(request)
    mem_add(m.account_id, m.channel, m.who, m.text)
    return {"ok": True}


class CallStart(BaseModel):
    account_id: int | None = None
    from_number: str = ""
    room: str = ""


@app.post("/calls/start")
def call_start(c: CallStart, request: Request):
    require_auth(request)
    emit("call", "incoming", f"call from {c.from_number}",
         "info", c.account_id)
    db = Session()
    row = Call(account_id=c.account_id, from_number=c.from_number,
               room=c.room)
    db.add(row)
    db.commit()
    db.refresh(row)
    out = {"call_id": row.id}
    db.close()
    return out


class TurnBody(BaseModel):
    call_id: int
    who: str
    text: str = ""
    tool: str = ""
    latency_ms: int = 0


@app.post("/calls/turn")
def call_turn(t: TurnBody, request: Request):
    require_auth(request)
    t.text = scrub(t.text or "")
    label = {"caller": "caller", "agent": "agent",
             "tool": t.tool or "tool", "problem": "PROBLEM"}.get(t.who, t.who)
    emit("call", f"call {t.call_id}", f"{label}: {t.text}",
         "error" if t.who == "problem" else "info")
    db = Session()
    db.add(CallTurn(call_id=t.call_id, who=t.who, text=t.text[:4000],
                    tool=t.tool, latency_ms=t.latency_ms))
    db.commit()
    db.close()
    return {"ok": True}


@app.post("/calls/end")
def call_end(request: Request, call_id: int, background: BackgroundTasks,
             verified: int = 0):
    require_auth(request)
    emit("call", f"call {call_id}", "call ended")
    db = Session()
    row = db.query(Call).filter_by(id=call_id).first()
    if row:
        row.ended_at = datetime.utcnow()
        row.duration_sec = int(
            (row.ended_at - row.started_at).total_seconds())
        row.verified = verified
        db.commit()
    db.close()
    # Read the call back and flag anything said that the record doesn't
    # support. Nobody should have to ring in to report it.
    try:
        background.add_task(review_call, call_id)
        background.add_task(learn_about_caller, call_id)
    except Exception:
        pass
    return {"ok": True}


class AdviceBody(BaseModel):
    account_id: int
    call_id: int = 0
    situation: str
    heard: str = ""


@app.post("/advise")
def advise_now(b: AdviceBody, request: Request):
    """What to do and say next, decided from the facts, not from memory."""
    require_auth(request)
    return advise(b.account_id, b.call_id, b.situation, b.heard)


@app.get("/state")
def state_now(request: Request, account_id: int, call_id: int = 0):
    """The facts the advisor is given. Useful when a call goes wrong."""
    require_auth(request)
    return call_state(account_id, call_id)


@app.post("/calls/review")
def calls_review(request: Request, call_id: int):
    """Review one finished call by hand. Every call is reviewed anyway."""
    require_auth(request)
    return review_call(call_id)


@app.get("/profiles")
def profiles_list(request: Request, limit: int = 100):
    """What we know about every customer, for the office to read."""
    require_auth(request)
    db = Session()
    names = {a.id: a.name for a in db.query(Account).all()}
    rows = (db.query(Profile).order_by(Profile.updated.desc())
              .limit(min(limit, 300)).all())
    out = [{"account_id": r.account_id, "who": names.get(r.account_id, ""),
            "notes": r.notes or "", "by_hand": r.by_hand or "",
            "updated": local_str(r.updated) if r.updated else ""}
           for r in rows]
    db.close()
    return out


@app.get("/profile")
def profile_get(request: Request, account_id: int):
    """What we know about one customer."""
    require_auth(request)
    db = Session()
    row = db.query(Profile).filter_by(account_id=account_id).first()
    acct = db.query(Account).filter_by(id=account_id).first()
    out = {"account_id": account_id, "name": acct.name if acct else "",
           "notes": (row.notes if row else ""),
           "by_hand": (row.by_hand if row else ""),
           "updated": local_str(row.updated) if row and row.updated else ""}
    db.close()
    out["for_the_assistant"] = profile_for(account_id)
    return out


class ProfileBody(BaseModel):
    account_id: int
    by_hand: str = ""


@app.post("/profile")
def profile_set(b: ProfileBody, request: Request):
    """Staff notes. The system never overwrites these."""
    require_auth(request)
    db = Session()
    row = db.query(Profile).filter_by(account_id=b.account_id).first()
    if not row:
        row = Profile(account_id=b.account_id)
        db.add(row)
    row.by_hand = scrub(b.by_hand or "")[:4000]
    row.updated = datetime.utcnow()
    db.commit()
    db.close()
    emit("profile", f"account {b.account_id}", "the office changed their "
                                               "notes", "info", b.account_id)
    return {"ok": True}


@app.post("/profile/learn")
def profile_learn(request: Request, call_id: int):
    """Update the notes from one call by hand. Happens anyway after every
    call."""
    require_auth(request)
    return learn_about_caller(call_id)


@app.get("/blocks")
def blocks_list(request: Request, days: int = 30, site: str = ""):
    """Which sites refused us, and which kind of wall each one is. This is
    what decides where ordering work goes: a puzzle or a fingerprint wall
    will not open, an address refusal or a login wall might."""
    require_auth(request)
    since = datetime.utcnow() - timedelta(days=max(1, min(days, 365)))
    db = Session()
    q = db.query(Block).filter(Block.at >= since)
    if site:
        q = q.filter(Block.site == site.lower())
    rows = q.order_by(Block.id.desc()).limit(400).all()
    out, summary = [], {}
    for r in rows:
        out.append({"at": local_str(r.at), "site": r.site, "kind": r.kind,
                    "vendor": r.vendor, "url": r.url, "job_id": r.job_id,
                    "saw": (r.saw or "")[:200]})
        key = f"{r.site}/{r.kind}" + (f"/{r.vendor}" if r.vendor else "")
        summary[key] = summary.get(key, 0) + 1
    db.close()
    worst = []
    for key, n in sorted(summary.items(), key=lambda kv: -kv[1]):
        bits = key.split("/")
        kind = bits[1] if len(bits) > 1 else ""
        what, retry, advice = BLOCK_KINDS.get(
            kind, BLOCK_KINDS["unknown"])
        worst.append({"site": bits[0], "kind": kind,
                      "vendor": bits[2] if len(bits) > 2 else "",
                      "times": n, "what": what,
                      "worth_retrying": retry, "advice": advice})
    return {"days": days, "by_site": worst, "recent": out[:100],
            "kinds": {k: v[0] for k, v in BLOCK_KINDS.items()}}


@app.get("/changes")
def changes_list(request: Request, account_id: int = 0, limit: int = 100,
                 area: str = ""):
    """Everything done on customers' behalf, newest first."""
    require_auth(request)
    db = Session()
    q = db.query(Change)
    if account_id:
        q = q.filter_by(account_id=account_id)
    if area:
        q = q.filter_by(area=area)
    rows = q.order_by(Change.id.desc()).limit(min(limit, 500)).all()
    names = {a.id: a.name for a in db.query(Account).all()}
    out = [{"id": r.id, "at": local_str(r.at), "account_id": r.account_id,
            "who": names.get(r.account_id, ""), "call_id": r.call_id,
            "area": r.area, "what": r.what, "detail": r.detail,
            "undo": r.undo} for r in rows]
    db.close()
    return out


@app.get("/reviews")
def reviews_list(request: Request, limit: int = 20):
    """What the reviewer found, newest first."""
    require_auth(request)
    db = Session()
    rows = (db.query(Followup).filter_by(reason="call_review")
              .order_by(Followup.id.desc()).limit(limit).all())
    out = [{"id": r.id, "call_id": r.call_id, "account_id": r.account_id,
            "at": local_str(r.at), "note": r.note} for r in rows]
    db.close()
    return out


@app.get("/calls")
def calls_list(request: Request, limit: int = 50, q: str = ""):
    require_auth(request)
    db = Session()
    rows = db.query(Call).order_by(Call.id.desc()).limit(400).all()
    names = {a.id: a.name for a in db.query(Account).all()}
    ids = [r.id for r in rows]
    turns = (db.query(CallTurn).filter(CallTurn.call_id.in_(ids)).all()
             if ids else [])
    db.close()

    by_call = {}
    for t in turns:
        by_call.setdefault(t.call_id, []).append(t)

    out = []
    for r in rows:
        ts = by_call.get(r.id, [])
        tools = []
        problems = []
        slowest = 0
        for t in ts:
            if t.tool and t.tool not in tools:
                tools.append(t.tool)
            slowest = max(slowest, t.latency_ms or 0)
            low = (t.text or "").lower()
            if any(p in low for p in (
                    "didn't go", "couldn't", "not verified", "failed",
                    "nothing matched", "no address found", "didn't get added",
                    "did not go through", "i'm sorry", "error",
                    "didn't work", "not allowed")):
                problems.append(t.text or "")
        out.append({
            "call_id": r.id,
            "who": names.get(r.account_id) or "unknown",
            "from": r.from_number,
            "started": local_str(r.started_at)
                       if r.started_at else "",
            "seconds": r.duration_sec,
            "verified": bool(r.verified),
            "tasks": tools,
            "turns": len(ts),
            "slowest_ms": slowest,
            "problems": problems[:5],
        })

    if q:
        w = q.strip().lower()
        out = [c for c in out
               if w in (c["who"] or "").lower()
               or w in (c["from"] or "").lower()
               or w in " ".join(c["tasks"]).lower()
               or str(c["call_id"]) == w]
    return out[:limit]


@app.get("/stats")
def stats(request: Request, days: int = 7):
    require_auth(request)
    since = datetime.utcnow() - timedelta(days=days)
    db = Session()
    rows = db.query(Call).filter(Call.started_at >= since).all()
    ids = [r.id for r in rows]
    turns = (db.query(CallTurn).filter(CallTurn.call_id.in_(ids)).all()
             if ids else [])
    db.close()

    tool_counts = {}
    lat = []
    for t in turns:
        if t.tool:
            tool_counts[t.tool] = tool_counts.get(t.tool, 0) + 1
        if t.latency_ms:
            lat.append(t.latency_ms)

    durations = [r.duration_sec for r in rows if r.duration_sec]
    return {
        "days": days,
        "calls": len(rows),
        "verified": sum(1 for r in rows if r.verified),
        "avg_seconds": int(sum(durations) / len(durations)) if durations else 0,
        "avg_tool_ms": int(sum(lat) / len(lat)) if lat else 0,
        "top_tasks": sorted(tool_counts.items(),
                            key=lambda x: -x[1])[:6],
    }


@app.get("/calls/{call_id}")
def call_detail(call_id: int, request: Request):
    require_auth(request)
    db = Session()
    turns = (db.query(CallTurn).filter_by(call_id=call_id)
               .order_by(CallTurn.id).all())
    out = [{"who": t.who, "text": t.text, "tool": t.tool,
            "latency_ms": t.latency_ms,
            "at": local_str(t.at, "seconds") if t.at else ""}
           for t in turns]
    db.close()
    return out


class LoginBody2(BaseModel):
    account_id: int
    site: str
    username: str
    password: str


@app.post("/logins")
def logins_save(b: LoginBody2, request: Request):
    require_auth(request)
    out = save_site_login(b.account_id, b.site, b.username, b.password)
    record_change(b.account_id, "login", "saved",
                  f"saved the {b.site} login for {b.username} "
                  f"(password encrypted, never shown)")
    return out


@app.get("/logins")
def logins_list(request: Request, account_id: int):
    """Which sites they've saved. Never returns passwords."""
    require_auth(request)
    return list_site_logins(account_id)


@app.delete("/logins")
def logins_forget(request: Request, account_id: int, site: str):
    require_auth(request)
    return forget_site_login(account_id, site)


@app.get("/logins/audit")
def logins_audit(request: Request, limit: int = 50):
    require_auth(request)
    db = Session()
    rows = (db.query(SecretAccess).order_by(SecretAccess.id.desc())
              .limit(limit).all())
    names = {a.id: a.name for a in db.query(Account).all()}
    out = [{"at": local_str(r.at) if r.at else "",
            "who": names.get(r.account_id) or "?", "site": r.site,
            "purpose": r.purpose} for r in rows]
    db.close()
    return out


@app.post("/jobs/site-login")
def job_site_login(request: Request, account_id: int, site: str,
                   call_id: int = 0):
    require_auth(request)
    if not BROWSERBASE_API_KEY:
        raise HTTPException(400, "Browserbase isn't configured.")
    jid = start_job(account_id, "site_login", site,
                    call_id=call_id or None)
    return {"job_id": jid, "state": "queued"}


@app.post("/jobs/site-orders")
def job_site_orders(request: Request, account_id: int, site: str,
                    call_id: int = 0):
    require_auth(request)
    if not BROWSERBASE_API_KEY:
        raise HTTPException(400, "Browserbase isn't configured.")
    return {"job_id": start_job(account_id, "site_orders", site,
                                call_id=call_id or None)}


@app.post("/jobs/site-search")
def job_site_search(request: Request, account_id: int, site: str,
                    query: str, call_id: int = 0):
    require_auth(request)
    if is_blocked(query) or is_blocked(site):
        emit("browse", "blocked", f"refused: {query[:120]}", "warn",
             account_id)
        return {"blocked": True, "answer": BLOCKED_REPLY}
    if not BROWSERBASE_API_KEY:
        raise HTTPException(400, "Browserbase isn't configured.")
    return {"job_id": start_job(account_id, "site_search", site,
                                call_id=call_id or None,
                                payload={"query": query})}


CHECKOUT_GOAL = """The account is already signed in. Open the shopping
cart and press Proceed to checkout, then stop on the checkout or order
review page.

Some carts tick each item separately and start with nothing ticked - the
page says something like "No items selected" and checkout does nothing
when pressed. If you see that, use "Select all items", or tick the items
one by one, before pressing Proceed to checkout.

{changes}

Then READ the page and report exactly what it shows. Do not guess at
anything that is not written there.

You must NOT place the order. Do not press Place your order, Buy now, Pay
now or anything that completes a purchase - pressing them is blocked
anyway, and trying wastes the call.

Reply done with, each on its own line: every item with its quantity and
price; the delivery address; the payment method exactly as written, such
as "Visa ending 1234"; the delivery date or shipping choice; and the
order total. If the page does not show one of them, say which one."""

CHECKOUT_CHANGES = """If a delivery address is wanted: open Change beside
the delivery address, pick the saved address matching "{deliver_to}", and
use it. If a payment method is wanted: open Change under the payment
section and pick the saved card matching "{pay_with}". If you cannot find
the Change control after two tries, leave it as it is and say so in your
answer - do not keep trying."""


@app.post("/jobs/checkout")
def job_checkout(request: Request, account_id: int, site: str,
                 deliver_to: str = "", pay_with: str = "",
                 call_id: int = 0):
    """Take a cart as far as the review page and read everything back.
    Never completes a purchase: the buttons that would are blocked."""
    require_auth(request)
    if not BROWSERBASE_API_KEY:
        raise HTTPException(400, "Browserbase isn't configured.")
    changes = ""
    if deliver_to or pay_with:
        changes = CHECKOUT_CHANGES.format(
            deliver_to=deliver_to or "the one already chosen",
            pay_with=pay_with or "the one already chosen")
    goal = CHECKOUT_GOAL.format(changes=changes)
    jid = start_job(account_id, "browse", site, call_id=call_id or None,
                    payload={"goal": goal, "url": "",
                             "max_steps": 18, "may_buy": False})
    return {"job_id": jid, "state": "queued"}


@app.post("/jobs/browse")
def job_browse(request: Request, account_id: int, goal: str,
               site: str = "", url: str = "", call_id: int = 0,
               max_steps: int = 0, urls: str = "", model: str = ""):
    """Pursue any goal on any site. No per-site setup. max_steps lets a
    quick probe stay quick."""
    require_auth(request)
    # checked before anything opens a browser - a blocked topic must not be
    # reachable just because it's on a web page rather than in a search
    if is_blocked(goal) or is_blocked(site):
        emit("browse", "blocked", f"refused: {goal[:120]}", "warn", account_id)
        return {"blocked": True, "answer": BLOCKED_REPLY}
    if not BROWSERBASE_API_KEY:
        raise HTTPException(400, "Browserbase isn't configured.")
    payload = {"goal": goal, "url": url}
    if model:
        # try a different brain for one job, without changing Railway
        payload["model"] = model
    if urls:
        # space separated - a URL can't contain a space
        payload["urls"] = [u for u in urls.split() if u]
    if max_steps:
        payload["max_steps"] = max(3, min(int(max_steps), 30))
    return {"job_id": start_job(account_id, "browse", site,
                                call_id=call_id or None, payload=payload)}


@app.get("/sites/report")
def sites_report(request: Request, days: int = 30):
    """What's being asked for, per site, and how it's going."""
    require_auth(request)
    since = datetime.utcnow() - timedelta(days=days)
    db = Session()
    reqs = db.query(SiteRequest).filter(SiteRequest.at >= since).all()
    recipes = db.query(Recipe).all()
    db.close()

    by_site = {}
    for r in reqs:
        b = by_site.setdefault(r.site, {"site": r.site, "requests": 0,
                                        "ok": 0, "failed": 0,
                                        "via_recipe": 0, "fallbacks": 0,
                                        "avg_seconds": 0, "tasks": {}})
        b["requests"] += 1
        b["ok" if r.outcome == "ok" else "failed"] += 1
        if r.path == "recipe":
            b["via_recipe"] += 1
        if r.path == "fallback":
            b["fallbacks"] += 1
        b["avg_seconds"] += r.seconds or 0
        t = b["tasks"].setdefault(r.task or "misc", 0)
        b["tasks"][r.task or "misc"] = t + 1
    for b in by_site.values():
        if b["requests"]:
            b["avg_seconds"] = int(b["avg_seconds"] / b["requests"])
        b["tasks"] = sorted(b["tasks"].items(), key=lambda x: -x[1])[:6]

    rec_out = [{"id": r.id, "site": r.site, "task": r.task,
                "example": r.example_goal, "steps": json.loads(r.steps or "[]"),
                "ok": r.times_ok or 0, "failed": r.times_failed or 0,
                "retired": bool(r.retired),
                "last_ok": local_str(r.last_ok)
                           if r.last_ok else ""} for r in recipes]
    return {"sites": sorted(by_site.values(), key=lambda x: -x["requests"]),
            "recipes": rec_out}


@app.get("/sites/events")
def sites_events(request: Request, limit: int = 60):
    """Recent requests, including every fallback and failure."""
    require_auth(request)
    db = Session()
    rows = (db.query(SiteRequest).order_by(SiteRequest.id.desc())
              .limit(limit).all())
    out = [{"at": local_str(r.at) if r.at else "",
            "site": r.site, "task": r.task, "goal": r.goal,
            "path": r.path, "outcome": r.outcome, "seconds": r.seconds,
            "job_id": r.job_id} for r in rows]
    db.close()
    return out


@app.get("/jobs/answer")
def job_answer(request: Request, job_id: int, question: str = ""):
    """A spoken-length summary of what a finished job found."""
    require_auth(request)
    db = Session()
    row = db.query(Job).filter_by(id=job_id).first()
    db.close()
    if not row:
        raise HTTPException(404, "Unknown job.")
    if row.state != "done":
        return {"state": row.state, "message": row.message}
    if row.kind == "browse":
        return {"state": "done", "answer": row.message}
    if row.kind == "site_login":
        # Summarising "signed in and saved the session" against "what were
        # my recent orders" produced "I couldn't find any order details" -
        # right after a sign-in that had just worked.
        return {"state": "done",
                "answer": ("They are signed in now. Say so, then start what "
                           "they originally asked for again.")}
    # These runners already store a spoken-length answer. Summarising a
    # summary is how a search that really did find three house numbers was
    # read back to the caller as "I couldn't get any results" - the second
    # pass had nothing left to work with and said so.
    answer = (row.message or "").strip()
    if len(answer) > 1200 or answer.count(" ") > 220:
        asked = question or ("their recent orders"
                             if row.kind == "site_orders"
                             else "the search results")
        answer = _summarise_page(answer, asked)
    if "NOTHING_RELEVANT" in answer:
        return {"state": "done",
                "answer": ("The page opened but nothing readable came back. "
                           "Say that - do not say they have none and do not "
                           "say it doesn't exist.")}
    if is_blocked(answer):
        return {"state": "done", "answer": BLOCKED_REPLY}
    return {"state": "done", "answer": answer}


class JobCode(BaseModel):
    job_id: int
    code: str


@app.post("/jobs/code")
def job_code(b: JobCode, request: Request):
    require_auth(request)
    if b.job_id in _JOBS:
        # ASCII only. isalnum() is true for Chinese numerals and the like,
        # and speech-to-text does produce those from a spoken code - we
        # were typing them into the site verbatim.
        clean = "".join(ch for ch in b.code
                        if ("0" <= ch <= "9") or ("a" <= ch <= "z")
                        or ("A" <= ch <= "Z"))
        # It has to LOOK like a code. The model once sent the words
        # "another way" here; they survived as "anotherway" and were
        # typed into Amazon's code box, which then said the code was
        # wrong - and the caller was blamed for it.
        digits = sum(ch.isdigit() for ch in clean)
        if not clean or digits < 3 or len(clean) > 10:
            emit("job", f"job {b.job_id}",
                 "that wasn't a code - ask them to say the digits again, "
                 "slowly", "warn")
            return {"ok": False, "reason": "unreadable"}
        _JOBS[b.job_id]["code"] = clean
        return {"ok": True}
    raise HTTPException(400, "That job is no longer running.")


@app.post("/jobs/cancel")
def job_cancel(request: Request, job_id: int, why: str = "stopped"):
    """Stop one job. A sign-in waiting for a code the caller cannot get
    would otherwise sit there for four minutes, holding a browser open,
    while the caller listens to silence."""
    require_auth(request)
    if job_id in _JOBS:
        _JOBS[job_id]["cancelled"] = True
        _job_set(job_id, "failed", f"Stopped: {why[:120]}",
                 reason="cancelled")
        emit("job", f"job {job_id}", f"stopped: {why[:120]}", "warn")
        return {"ok": True}
    return {"ok": False, "reason": "not_running"}


@app.post("/jobs/cancel_for_call")
def jobs_cancel_for_call(request: Request, call_id: int):
    """The caller hung up - stop anything still running for them. A browser
    job that outlives the call keeps spending on browser time and model
    calls for an answer nobody will ever hear."""
    require_auth(request)
    db = Session()
    rows = (db.query(Job)
              .filter(Job.call_id == call_id,
                      Job.state.notin_(["done", "failed", "cancelled"]))
              .all())
    ids = [r.id for r in rows]
    db.close()
    for jid in ids:
        if jid in _JOBS:
            _JOBS[jid]["cancelled"] = True
        _job_set(jid, "failed", "The caller hung up before this finished.",
                 reason="cancelled")
    return {"cancelled": ids}


@app.get("/jobs/status")
def job_status(request: Request, job_id: int):
    require_auth(request)
    db = Session()
    row = db.query(Job).filter_by(id=job_id).first()
    db.close()
    if not row:
        raise HTTPException(404, "Unknown job.")
    return {"job_id": row.id, "kind": row.kind, "site": row.site,
            "state": row.state, "message": row.message,
            "reason": row.reason or "", "history": row.history or ""}


@app.get("/jobs/health")
def jobs_health(request: Request):
    require_auth(request)
    return queue_health()


@app.post("/sessions/forget")
def sessions_forget(request: Request, account_id: int, site: str = ""):
    """Discard saved browser identities, so the next run starts clean."""
    require_auth(request)
    db = Session()
    rows = db.query(SiteSession).filter_by(account_id=account_id)
    if site:
        rows = rows.filter_by(site=site.lower())
    sites = [r.site for r in rows.all()]
    db.close()
    for s in sites:
        _forget_context(account_id, s)
    emit("browser", "context", f"cleared saved browser session(s) for "
                               f"{', '.join(sites) or 'nothing'}",
         "info", account_id)
    return {"cleared": sites}


@app.get("/sessions")
def sessions_list(request: Request, limit: int = 100):
    """Which customers have a live session on which sites."""
    require_auth(request)
    db = Session()
    rows = db.query(SiteSession).order_by(SiteSession.id.desc()).limit(
        limit).all()
    names = {a.id: a.name for a in db.query(Account).all()}
    out = [{"who": names.get(r.account_id) or "?", "site": r.site,
            "last_ok": local_str(r.last_ok)
                       if r.last_ok else "never"} for r in rows]
    db.close()
    return out


@app.get("/jobs")
def jobs_list(request: Request, limit: int = 30):
    require_auth(request)
    db = Session()
    rows = db.query(Job).order_by(Job.id.desc()).limit(limit).all()
    names = {a.id: a.name for a in db.query(Account).all()}
    out = [{"job_id": r.id, "who": names.get(r.account_id) or "?",
            "kind": r.kind, "site": r.site, "state": r.state,
            "message": r.message, "history": r.history or "",
            "at": local_str(r.at) if r.at else ""}
           for r in rows]
    db.close()
    return out


@app.get("/events")
def events_list(request: Request, after_id: int = 0, limit: int = 200,
                kind: str = ""):
    """Live log. Pass after_id to get only what's new."""
    require_auth(request)
    db = Session()
    q = db.query(Event)
    if after_id:
        q = q.filter(Event.id > after_id)
    if kind:
        q = q.filter(Event.kind == kind)
    rows = q.order_by(Event.id.desc()).limit(limit).all()
    names = {a.id: a.name for a in db.query(Account).all()}
    out = [{"id": r.id,
            "at": local_str(r.at, "seconds") if r.at else "",
            "kind": r.kind, "ref": r.ref, "level": r.level,
            "who": names.get(r.account_id, "") if r.account_id else "",
            "text": r.text} for r in reversed(rows)]
    db.close()
    return out


@app.get("/browser/peek")
def browser_peek(request: Request, url: str, account_id: int = 0,
                 site: str = "", want: str = "", after: str = ""):
    """What the browser actually sees on one page: how much text, how many
    controls, and the first of each. Built while chasing "the page offers 6
    things you can use" on a shop full of products - guessing at that from
    a job's history wastes an hour every time."""
    require_auth(request)
    from playwright.sync_api import sync_playwright
    out = {"url": url}
    browser = None
    try:
        with sync_playwright() as p:
            if account_id and site:
                browser, page, _ = _open_with_session(p, account_id, site)
            else:
                browser = p.chromium.connect_over_cdp(_bb_connect_url())
                ctx = (browser.contexts[0] if browser.contexts
                       else browser.new_context())
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
            do_goto(page, url, 5000)
            settle(page, 2500)
            if after:
                el = q(page, after)
                if el:
                    do_click(page, el, 5000)
                    settle(page, 2500)
                out["clicked"] = bool(el)
            items, text = _page_snapshot(page, want=want)
            # counted in the page itself, so a missing product link can be
            # told apart from a page that never loaded
            out["raw"] = page_eval(page, """() => {
                const all = document.querySelectorAll('a, button, input');
                let visible = 0, product = 0;
                for (const el of all) {
                  const r = el.getBoundingClientRect();
                  if (r.width && r.height) visible++;
                  const href = el.getAttribute('href') || '';
                  if (href.includes('/dp/') || href.includes('/gp/product'))
                    product++;
                }
                return {clickable: all.length, visible: visible,
                        product_links: product,
                        body_text: (document.body.innerText || '').length};
            }""") or {}
            # where the snapshot loses them, step by step
            out["stages"] = page_eval(page, """() => {
                const sel = 'a, button, input, textarea, select, ' +
                  '[role=button], [role=link], [role=combobox], ' +
                  '[contenteditable="true"]';
                const typed = ['input', 'textarea', 'select'];
                let matched = 0, sized = 0, shown = 0, labelled = 0;
                for (const el of document.querySelectorAll(sel)) {
                  matched++;
                  const r = el.getBoundingClientRect();
                  if (!r.width || !r.height) continue;
                  sized++;
                  const st = getComputedStyle(el);
                  if (st.visibility === 'hidden' || st.display === 'none')
                    continue;
                  shown++;
                  const tag = el.tagName.toLowerCase();
                  let label = el.getAttribute('aria-label')
                    || el.getAttribute('placeholder')
                    || (el.innerText || '').trim()
                    || el.getAttribute('name') || el.getAttribute('value')
                    || el.getAttribute('title') || '';
                  if (!label && !typed.includes(tag)) continue;
                  labelled++;
                }
                return {matched: matched, with_size: sized,
                        not_hidden: shown, with_label: labelled};
            }""") or {}
            out["snapshot_all"] = len(_page_snapshot(page, limit=999)[0])
            out["landed_on"] = page_url(page)
            out["text_length"] = len(text or "")
            out["text_start"] = (text or "")[:400]
            out["control_count"] = len(items)
            out["first_controls"] = [i["desc"][:60] for i in items[:25]]
            out["pages_open"] = len(page.context.pages)
            browser.close()
    except Exception as e:
        out["error"] = str(e)[:300]
        try:
            if browser:
                browser.close()
        except Exception:
            pass
    return out


@app.get("/browser/where")
def browser_where(request: Request, phone: str = ""):
    """Open a browser and report where it appears to be. Pass a phone
    number to test what a caller from there would get."""
    require_auth(request)
    if not BROWSERBASE_API_KEY:
        raise HTTPException(400, "Browserbase isn't configured.")
    from playwright.sync_api import sync_playwright
    want_c, want_s, want_city = _where_for_phone(phone) if phone else \
        (PROXY_COUNTRY, PROXY_STATE, PROXY_CITY)
    out = {"for_phone": phone or "(default)",
           "wanted": {"country": want_c, "state": want_s, "city": want_city}}
    PROXY_STATUS["proxies_enabled"] = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(
                _bb_connect_url(phone=phone) if phone else _bb_connect_url())
            ctx = browser.contexts[0] if browser.contexts \
                else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            do_goto(page, "https://ipinfo.io/json", 2000)
            raw = page_text(page, 800)
            browser.close()
        try:
            i = raw.index("{")
            out["actual"] = json.loads(raw[i:raw.rindex("}") + 1])
            got = (out["actual"].get("country") or "").upper()
            used = PROXY_STATUS.get("proxies_enabled")
            out["proxy_used"] = bool(used)
            if used is False:
                out["warning"] = (
                    "Proxies are not enabled on your Browserbase plan. This "
                    "IP is Browserbase's own datacenter, which happens to be "
                    "in the US. Non-US customers cannot be matched to their "
                    "country until you enable proxies.")
            out["ok"] = (got == want_c.upper())
            if not out["ok"] and used is not False:
                _flag_proxy_fallback(
                    f"browser is reporting country {got}, expected "
                    f"{want_c}")
        except Exception:
            out["actual_raw"] = raw[:400]
    except Exception as e:
        out["error"] = str(e)[:300]
    return out


class UsageBody(BaseModel):
    call_id: int | None = None
    account_id: int | None = None
    kind: str = "voice"
    audio_in: int = 0
    audio_out: int = 0
    text_in: int = 0
    text_out: int = 0
    cached_in: int = 0
    mini_in: int = 0
    mini_out: int = 0
    brain_in: int = 0
    brain_out: int = 0
    call_seconds: int = 0
    browser_seconds: int = 0
    searches: int = 0
    texts: int = 0


@app.post("/usage")
def usage_add(b: UsageBody, request: Request):
    require_auth(request)
    dollars = record_usage(**b.model_dump())
    return {"ok": True, "cost_usd": dollars}


@app.get("/usage/call")
def usage_call(request: Request, call_id: int):
    """What one call cost, itemised."""
    require_auth(request)
    db = Session()
    row = db.query(Usage).filter_by(call_id=call_id).first()
    db.close()
    if not row:
        return {"call_id": call_id, "cost_usd": 0, "note": "nothing recorded"}
    dollars, parts = price_usage(row)
    return {
        "call_id": call_id,
        "minutes": round((row.call_seconds or 0) / 60.0, 2),
        "tokens": {"audio_in": row.audio_in, "audio_out": row.audio_out,
                   "text_in": row.text_in, "text_out": row.text_out,
                   "cached_in": row.cached_in,
                   "helper_in": row.mini_in, "helper_out": row.mini_out},
        "browser_minutes": round((row.browser_seconds or 0) / 60.0, 2),
        "searches": row.searches, "texts": row.texts,
        "cost_usd": round(dollars, 4),
        "breakdown_usd": parts,
    }


@app.get("/usage/summary")
def usage_summary(request: Request, days: int = 30):
    """What calls are costing you, and what that means per customer."""
    require_auth(request)
    since = datetime.utcnow() - timedelta(days=days)
    db = Session()
    rows = db.query(Usage).filter(Usage.at >= since).all()
    names = {a.id: a.name for a in db.query(Account).all()}
    db.close()

    total = sum((r.cost_cents or 0) for r in rows) / 100.0
    mins = sum((r.call_seconds or 0) for r in rows) / 60.0
    calls = len([r for r in rows if r.kind == "voice"])

    per_account = {}
    for r in rows:
        k = names.get(r.account_id, "unknown")
        d = per_account.setdefault(k, {"calls": 0, "minutes": 0.0,
                                       "cost_usd": 0.0})
        d["calls"] += 1
        d["minutes"] += (r.call_seconds or 0) / 60.0
        d["cost_usd"] += (r.cost_cents or 0) / 100.0

    combined = {}
    for r in rows:
        try:
            for k, v in json.loads(r.breakdown or "{}").items():
                combined[k] = combined.get(k, 0) + v
        except Exception:
            pass

    return {
        "days": days,
        "calls": calls,
        "total_minutes": round(mins, 1),
        "total_cost_usd": round(total, 2),
        "cost_per_call_usd": round(total / calls, 4) if calls else 0,
        "cost_per_minute_usd": round(total / mins, 4) if mins else 0,
        "by_component_usd": {k: round(v, 4) for k, v in
                             sorted(combined.items(), key=lambda x: -x[1])},
        "by_customer": {k: {kk: round(vv, 3) for kk, vv in v.items()}
                        for k, v in sorted(
                            per_account.items(),
                            key=lambda x: -x[1]["cost_usd"])},
    }


@app.get("/models")
def models_list(request: Request):
    """What each job uses now, and what your OpenAI account can actually
    run. Change MODEL_BROWSER in Railway to switch - no deploy needed."""
    require_auth(request)
    out = {"in_use": {"browser": MODEL_BROWSER, "summary": MODEL_SUMMARY,
                      "text": MODEL_TEXT, "advisor": MODEL_ADVISOR,
                      "browser_sees_pictures": BROWSER_VISION},
           "openai_key_set_on_backend": bool(OPENAI_API_KEY),
           "openai_key_length": len(OPENAI_API_KEY),
           "rates_per_million": {"browser_in": RATES["brain_in"],
                                 "browser_out": RATES["brain_out"]}}
    try:
        req = urllib.request.Request(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
        names = sorted(m.get("id", "") for m in d.get("data", []))
        out["available"] = [n for n in names
                            if n.startswith(("gpt", "o1", "o3", "o4", "chatgpt"))]
        out["browser_model_exists"] = MODEL_BROWSER in names
    except Exception as e:
        out["error"] = f"could not list models: {str(e)[:200]}"
    return out


@app.get("/browser/account")
def browser_account(request: Request):
    """Ask Browserbase for a plain browser and report exactly what it says.
    Turns 'browsing is broken' into the actual reason, in their words."""
    require_auth(request)
    out = {"api_key_set": bool(BROWSERBASE_API_KEY),
           "project_set": bool(BROWSERBASE_PROJECT_ID)}
    if not BROWSERBASE_API_KEY:
        out["verdict"] = "No BROWSERBASE_API_KEY on the backend."
        return out
    try:
        req = urllib.request.Request(
            "https://api.browserbase.com/v1/sessions",
            data=json.dumps({"projectId": BROWSERBASE_PROJECT_ID}).encode(),
            headers={"X-BB-API-Key": BROWSERBASE_API_KEY,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            body = json.loads(r.read().decode())
        out["status"] = 200
        out["session_id"] = body.get("id", "")
        out["verdict"] = ("Browserbase started a browser - the account is "
                          "fine right now.")
    except urllib.error.HTTPError as e:
        try:
            out["browserbase_said"] = e.read().decode("utf-8", "replace")[:600]
        except Exception:
            out["browserbase_said"] = ""
        out["status"] = e.code
        if e.code == 402:
            out["verdict"] = ("Browserbase is refusing to start a browser "
                              "(402). The account is out of sessions or "
                              "minutes, or billing needs attention. Check "
                              "usage and billing at browserbase.com. No "
                              "code change will fix this.")
        elif e.code == 401:
            out["verdict"] = ("Browserbase rejected the API key (401). Check "
                              "BROWSERBASE_API_KEY on the backend service.")
        elif e.code == 429:
            out["verdict"] = ("Too many browsers at once (429). Lower "
                              "MAX_BROWSERS or wait for jobs to finish.")
        else:
            out["verdict"] = f"Browserbase returned {e.code}."
    except Exception as e:
        out["verdict"] = f"Could not reach Browserbase: {str(e)[:200]}"
    return out


@app.get("/stripe/status")
def stripe_status(request: Request):
    """Is Stripe wired up, is it the test key, and does it answer?"""
    require_auth(request)
    key = STRIPE_SECRET_KEY
    out = {"configured": bool(key),
           "mode": ("test" if key.startswith("sk_test_")
                    else "LIVE" if key.startswith("sk_live_")
                    else "restricted/unknown" if key else "none")}
    if not key:
        out["verdict"] = ("No STRIPE_SECRET_KEY on the backend. Cards are "
                          "still stored encrypted here instead of at "
                          "Stripe.")
        return out
    try:
        # a real tokenisation with Stripe's own test card
        held = stripe_hold_card("4242424242424242", "12/34", "123", "Probe")
        out["probe"] = {"brand": held["brand"], "last4": held["last4"],
                        "token_looks_like": held["id"][:8] + "..."}
        out["verdict"] = ("Stripe is holding cards. The digits no longer "
                          "reach this database.")
    except StripeNeedsRawCardAccess:
        out["card_page"] = ("works - cards added on /card go through "
                            "Stripe's own page and need no approval")
        out["needs"] = "raw card data API access (only for cards read out)"
        out["verdict"] = (
            "The key works, but Stripe has not approved this account to "
            "accept card numbers from a server. That is the normal state - "
            "ask Stripe support to enable raw card data APIs and explain "
            "that customers read their card out over the phone, so there "
            "is no browser to collect it in. Until then cards keep being "
            "stored encrypted here, and nothing is broken.")
    except HTTPException as e:
        out["verdict"] = f"Stripe refused: {e.detail}"
    except Exception as e:
        out["verdict"] = f"Could not reach Stripe: {str(e)[:200]}"
    return out


@app.get("/browser/proxy_status")
def proxy_status(request: Request):
    require_auth(request)
    st = dict(PROXY_STATUS)
    if st.get("checked"):
        st["checked"] = local_str(st["checked"])
    st["default"] = {"country": PROXY_COUNTRY, "state": PROXY_STATE}
    return st


@app.get("/permissions")
def token_permissions(request: Request, account_id: int = 0):
    """Ask Google what each stored token is actually allowed to do.

    Not what we believe, not what we asked for - what Google says the
    token carries. Scopes are enforced at Google's end, so this is the
    real answer to "does this give you their whole account"."""
    require_auth(request)
    db = Session()
    q = db.query(Connection).filter_by(provider="google")
    if account_id:
        q = q.filter_by(account_id=account_id)
    rows = [(r.account_id, r.email, r.secret_blob, r.linked_at)
            for r in q.all()]
    db.close()

    plain = {
        "https://www.googleapis.com/auth/gmail.modify":
            "read, send and organise their Gmail",
        "https://www.googleapis.com/auth/calendar":
            "read and change their calendar",
        "https://www.googleapis.com/auth/contacts":
            "look up and add to their contacts",
        "https://www.googleapis.com/auth/drive.readonly":
            "find and read files in their Google Drive (not change them)",
        "https://www.googleapis.com/auth/drive":
            "find, read, create and edit files in their Google Drive",
        "https://www.googleapis.com/auth/tasks":
            "read and add to their to-do list",
        "https://www.googleapis.com/auth/userinfo.email":
            "see which email address they are",
        "openid": "confirm who they are",
    }
    out = []
    for acc, email, blob, linked in rows:
        row = {"account_id": acc, "email": email,
               "connected": local_str(linked)}
        # While the Google app is unverified, refresh tokens die after
        # seven days and the customer has to connect all over again.
        if linked:
            days = (datetime.utcnow() - linked).total_seconds() / 86400.0
            row["days_connected"] = round(days, 1)
            if days >= 7:
                row["expiry_warning"] = ("PROBABLY DEAD - over 7 days, and "
                                         "unverified apps lose the token "
                                         "at 7 days. They must reconnect.")
            elif days >= 5:
                row["expiry_warning"] = (f"Expires in about "
                                         f"{round(7 - days, 1)} days unless "
                                         f"the app is verified.")
        try:
            tok = vault_get(blob)
            creds = Credentials(
                token=tok.get("token"),
                refresh_token=tok.get("refresh_token"),
                token_uri="https://oauth2.googleapis.com/token",
                client_id=GOOGLE_CLIENT_ID,
                client_secret=GOOGLE_CLIENT_SECRET, scopes=None)
            from google.auth.transport.requests import Request as GReq
            creds.refresh(GReq())
            req = urllib.request.Request(
                "https://oauth2.googleapis.com/tokeninfo?access_token="
                + urllib.parse.quote(creds.token))
            with urllib.request.urlopen(req, timeout=20) as r:
                info = json.loads(r.read().decode())
            got = (info.get("scope") or "").split()
            row["granted"] = [plain.get(s, s) for s in got]
            row["raw_scopes"] = got
            row["can_reach_drive"] = any("drive" in s for s in got)
            row["can_reach_contacts"] = any("contacts" in s for s in got)
            row["can_delete_mail_permanently"] = \
                "https://mail.google.com/" in got
        except Exception as e:
            row["error"] = str(e)[:200]
        out.append(row)
    return {"mailboxes": out,
            "note": ("Google enforces these. A token without a scope is "
                     "refused by that API, not merely unused here."),
            "testing_mode_warning": (
                "While the Google app is in Testing rather than published, "
                "every connection dies after 7 days and the customer has "
                "to sign in again. Publishing the app is what stops that.")}


@app.get("/vault/status")
def vault_status(request: Request):
    """Which key protects what."""
    require_auth(request)
    db = Session()
    counts = {"azure": 0, "aws": 0, "local": 0}
    for row in db.query(Connection).all():
        b = row.secret_blob or ""
        counts["azure" if b.startswith("akv:") else
               "aws" if b.startswith("kms:") else "local"] += 1
    login_counts = {"azure": 0, "aws": 0, "local": 0}
    for row in db.query(SiteLogin).all():
        b = row.secret_blob or ""
        login_counts["azure" if b.startswith("akv:") else
                     "aws" if b.startswith("kms:") else "local"] += 1
    db.close()
    return {"active_key": ("azure" if AZURE_KEY_ID else
                           "aws" if KMS_KEY_ID else "local"),
            "mailbox_tokens": counts, "site_logins": login_counts}


@app.get("/vault/test")
def vault_test(request: Request):
    """Try a real encrypt/decrypt and report exactly what fails."""
    require_auth(request)
    out = {
        "azure_key_id_set": bool(AZURE_KEY_ID),
        "azure_tenant_set": bool(os.environ.get("AZURE_TENANT_ID")),
        "azure_client_set": bool(os.environ.get("AZURE_CLIENT_ID")),
        "azure_secret_set": bool(os.environ.get("AZURE_CLIENT_SECRET")),
        "kms_key_set": bool(KMS_KEY_ID),
    }
    if AZURE_KEY_ID:
        kid = AZURE_KEY_ID.rstrip("/")
        parts = kid.split("/keys/")
        out["key_id_has_version"] = (len(parts) == 2
                                     and parts[1].count("/") == 1)
    try:
        blob = vault_put({"probe": "ok"})
        out["encrypt"] = "ok"
        out["format"] = blob.split(":", 1)[0] if ":" in blob else "local"
        out["decrypt"] = ("ok" if vault_get(blob).get("probe") == "ok"
                          else "mismatch")
    except Exception as e:
        out["encrypt"] = "failed"
        out["error"] = str(e)[:800]
    return out


@app.post("/vault/migrate")
def vault_migrate(request: Request, confirm: str = ""):
    """Re-encrypt every stored secret with the key that's active now."""
    require_auth(request)
    if confirm != "MIGRATE":
        raise HTTPException(400, "Pass confirm=MIGRATE.")
    if not (AZURE_KEY_ID or KMS_KEY_ID):
        raise HTTPException(400, "No cloud key is configured.")

    moved, failed = 0, []
    db = Session()
    for model in (Connection, SiteLogin):
        for row in db.query(model).all():
            blob = row.secret_blob or ""
            if AZURE_KEY_ID and blob.startswith("akv:"):
                continue
            if KMS_KEY_ID and not AZURE_KEY_ID and blob.startswith("kms:"):
                continue
            try:
                row.secret_blob = vault_put(vault_get(blob))
                moved += 1
            except Exception as e:
                failed.append(f"{model.__tablename__}#{row.id}: "
                              f"{str(e)[:600]}")
    db.commit()
    db.close()
    return {"re_encrypted": moved, "failed": failed}



class AddressBody(BaseModel):
    account_id: int
    label: str = "home"
    line1: str
    line2: str = ""
    city: str
    state: str
    zip: str
    make_default: bool = True


@app.post("/addresses")
def address_add(b: AddressBody, request: Request):
    require_auth(request)
    db = Session()
    if b.make_default:
        for a in db.query(Address).filter_by(account_id=b.account_id).all():
            a.is_default = 0
    row = Address(account_id=b.account_id, label=b.label[:40],
                  line1=b.line1, line2=b.line2, city=b.city,
                  state=b.state, zip=b.zip,
                  is_default=1 if b.make_default else 0)
    db.add(row)
    db.commit()
    db.refresh(row)
    out = {"id": row.id, "address": _fmt_address(row)}
    db.close()
    return out


@app.get("/addresses")
def address_list(request: Request, account_id: int):
    require_auth(request)
    db = Session()
    rows = db.query(Address).filter_by(account_id=account_id).all()
    out = [{"id": a.id, "label": a.label, "address": _fmt_address(a),
            "default": bool(a.is_default)} for a in rows]
    db.close()
    return out


@app.delete("/addresses")
def address_delete(request: Request, address_id: int):
    require_auth(request)
    db = Session()
    row = db.query(Address).filter_by(id=address_id).first()
    if row:
        db.delete(row)
        db.commit()
    db.close()
    return {"ok": True}


class CardBody(BaseModel):
    account_id: int
    number: str
    exp: str                    # MM/YY
    cvv: str = ""
    name_on_card: str = ""
    label: str = ""
    make_default: bool = True


@app.post("/cards")
def card_add(b: CardBody, request: Request):
    require_auth(request)
    num = "".join(ch for ch in b.number if ch.isdigit())
    if not _luhn_ok(num):
        raise HTTPException(400, "That card number doesn't check out.")

    held = None
    if STRIPE_SECRET_KEY:
        try:
            held = stripe_hold_card(num, b.exp, b.cvv, b.name_on_card)
        except StripeNeedsRawCardAccess:
            # Never fail a caller's card save over this - keep the old way
            # until Stripe approves the account for phone orders.
            emit("cards", "stripe",
                 "Stripe has not approved this account for card numbers "
                 "taken over the phone, so the card was stored here "
                 "instead. Request raw card data API access from Stripe.",
                 "warn", b.account_id)
    if held:
        blob = vault_put({"stripe_pm": held["id"]})
        brand, last4 = held["brand"], held["last4"]
        exp = held["exp"] or b.exp[:7]
        record_change(b.account_id, "card", "saved",
                      f"saved a {brand} ending {last4}")
        emit("cards", "saved", f"card held by Stripe ({brand} {last4})",
             "info", b.account_id)
    else:
        blob = vault_put({"number": num, "cvv": b.cvv})
        brand, last4, exp = _card_brand(num), num[-4:], b.exp[:7]
    del num

    db = Session()
    if b.make_default:
        for c in db.query(PaymentCard).filter_by(account_id=b.account_id).all():
            c.is_default = 0
    row = PaymentCard(account_id=b.account_id, label=b.label[:40],
                      last4=last4, brand=brand,
                      exp=exp, name_on_card=b.name_on_card[:120],
                      secret_blob=blob,
                      is_default=1 if b.make_default else 0)
    db.add(row)
    db.commit()
    db.refresh(row)
    out = {"id": row.id, "brand": row.brand, "last4": row.last4}
    db.close()
    return out


@app.get("/cards")
def card_list(request: Request, account_id: int):
    """Never returns the number."""
    require_auth(request)
    db = Session()
    rows = db.query(PaymentCard).filter_by(account_id=account_id).all()
    out = [{"id": c.id, "label": c.label, "brand": c.brand,
            "last4": c.last4, "exp": c.exp, "default": bool(c.is_default)}
           for c in rows]
    db.close()
    return out


@app.delete("/cards")
def card_delete(request: Request, card_id: int):
    require_auth(request)
    db = Session()
    row = db.query(PaymentCard).filter_by(id=card_id).first()
    if row:
        db.delete(row)
        db.commit()
    db.close()
    return {"ok": True}


class OrderBody(BaseModel):
    account_id: int
    site: str
    item: str
    quantity: int = 1
    expected_price: str = ""
    address_id: int | None = None
    card_id: int | None = None
    call_id: int | None = None


@app.post("/orders/draft")
def order_draft(b: OrderBody, request: Request):
    """Create the order. Nothing is placed until /orders/confirm."""
    require_auth(request)
    db = Session()
    addr = None
    if b.address_id:
        addr = db.query(Address).filter_by(id=b.address_id).first()
    else:
        addr = (db.query(Address).filter_by(account_id=b.account_id)
                  .order_by(Address.is_default.desc()).first())
    card = None
    if b.card_id:
        card = db.query(PaymentCard).filter_by(id=b.card_id).first()
    else:
        card = (db.query(PaymentCard).filter_by(account_id=b.account_id)
                  .order_by(PaymentCard.is_default.desc()).first())
    row = Order(account_id=b.account_id, call_id=b.call_id,
                site=b.site.lower(), item=b.item, quantity=b.quantity,
                expected_price=b.expected_price,
                address_id=addr.id if addr else None,
                card_id=card.id if card else None, state="draft")
    db.add(row)
    db.commit()
    db.refresh(row)
    out = {"order_id": row.id,
           "site": row.site, "item": row.item, "quantity": row.quantity,
           "expected_price": row.expected_price,
           "address": _fmt_address(addr) if addr else "",
           "card": f"{card.brand} ending {card.last4}" if card else "",
           "ready": bool(addr and (card or b.site.lower() in
                                  ("walmart", "amazon", "temu")))}
    db.close()
    return out


@app.post("/orders/confirm")
def order_confirm(request: Request, order_id: int, confirmed: str = ""):
    """Caller said yes out loud. Starts the checkout job."""
    require_auth(request)
    if confirmed.strip().lower() not in ("yes", "confirmed", "place it"):
        raise HTTPException(400, "Needs an explicit yes.")
    if not BROWSERBASE_API_KEY:
        raise HTTPException(400, "Browserbase isn't configured.")
    db = Session()
    row = db.query(Order).filter_by(id=order_id).first()
    if not row or row.state not in ("draft", "failed"):
        db.close()
        raise HTTPException(400, "Order isn't in a state to confirm.")
    row.state = "confirmed"
    acct_id, site = row.account_id, row.site
    db.commit()
    db.close()
    jid = start_job(acct_id, "checkout", site, payload={"order_id": order_id})
    _order_set(order_id, "placing", "Checkout started.", job_id=jid)
    return {"order_id": order_id, "job_id": jid, "state": "placing"}


@app.post("/orders/cancel")
def order_cancel(request: Request, order_id: int):
    require_auth(request)
    _order_set(order_id, "cancelled", "Cancelled by the customer.")
    return {"ok": True}


@app.get("/orders/status")
def order_status(request: Request, order_id: int):
    require_auth(request)
    db = Session()
    row = db.query(Order).filter_by(id=order_id).first()
    db.close()
    if not row:
        raise HTTPException(404, "Unknown order.")
    return {"order_id": row.id, "state": row.state, "message": row.message,
            "confirmation": row.confirmation, "final_total": row.final_total,
            "history": row.history or ""}


@app.get("/orders")
def orders_list(request: Request, account_id: int = 0, limit: int = 50):
    require_auth(request)
    db = Session()
    q = db.query(Order)
    if account_id:
        q = q.filter_by(account_id=account_id)
    rows = q.order_by(Order.id.desc()).limit(limit).all()
    names = {a.id: a.name for a in db.query(Account).all()}
    out = [{"order_id": r.id, "who": names.get(r.account_id) or "?",
            "site": r.site, "item": r.item, "quantity": r.quantity,
            "expected_price": r.expected_price, "state": r.state,
            "confirmation": r.confirmation, "final_total": r.final_total,
            "message": r.message, "history": r.history or "",
            "at": local_str(r.at) if r.at else ""}
           for r in rows]
    db.close()
    return out


class FollowupBody(BaseModel):
    account_id: int | None = None
    call_id: int | None = None
    reason: str = ""
    note: str = ""
    channel: str = "voice"


@app.post("/followups")
def followup_add(b: FollowupBody, request: Request):
    require_auth(request)
    b.note = scrub(b.note or "")
    emit("followup", b.reason or "note", b.note, "warn", b.account_id)
    db = Session()
    row = Followup(account_id=b.account_id, call_id=b.call_id,
                   reason=b.reason[:60], note=b.note[:2000],
                   channel=b.channel)
    db.add(row)
    db.commit()
    db.refresh(row)
    out = {"id": row.id}
    db.close()
    return out


@app.get("/followups")
def followups_list(request: Request, include_done: int = 0,
                   limit: int = 50):
    require_auth(request)
    db = Session()
    q = db.query(Followup)
    if not include_done:
        q = q.filter(Followup.done == 0)
    rows = q.order_by(Followup.id.desc()).limit(limit).all()
    names = {a.id: a.name for a in db.query(Account).all()}
    phones = {}
    for p in db.query(PhoneNumber).all():
        phones.setdefault(p.account_id, p.number)
    out = [{"id": r.id, "who": names.get(r.account_id) or "unknown",
            "phone": phones.get(r.account_id, ""),
            "call_id": r.call_id, "reason": r.reason, "note": r.note,
            "channel": r.channel, "done": bool(r.done),
            "at": local_str(r.at) if r.at else ""}
           for r in rows]
    db.close()
    return out


@app.post("/followups/done")
def followup_done(request: Request, followup_id: int, undo: int = 0):
    require_auth(request)
    db = Session()
    row = db.query(Followup).filter_by(id=followup_id).first()
    if row:
        row.done = 0 if undo else 1
        db.commit()
    db.close()
    return {"ok": True}


class SmsBody(BaseModel):
    to: str
    message: str


@app.post("/sms/send")
def sms_send(s: SmsBody, request: Request):
    require_auth(request)
    return tool_send_sms(s.to, s.message)


@app.post("/sms/link")
def sms_link(request: Request, account_id: int, to: str = ""):
    require_auth(request)
    """Text a customer their linking link. Uses their stored number if blank."""
    if not to:
        db = Session()
        row = db.query(PhoneNumber).filter_by(account_id=account_id).first()
        db.close()
        if not row:
            raise HTTPException(400, "No phone number on file.")
        to = row.number
    return tool_text_link(account_id, to)


ASK_SYSTEM = """You are answering a question for someone on a phone call.
They are older, often not technical, and they cannot see a screen.

Answer in two or three short spoken sentences. No lists, no markdown, no
URLs.

Be honest about how sure you are, in plain words:
- Sure: just say it.
- It varies by model, version or place: say what it usually is AND say it
  varies, e.g. "on most of those it's X, but it does differ between
  models".
- You don't know: say so plainly. Never invent a specific button
  combination, part number, price or step. A wrong specific answer is far
  worse than "I'm not certain" - they will go and try it.

If the answer depends on something that changes - today's price, this
week's hours, whether a shop has it in stock - say it needs checking."""


@app.get("/ask")
def ask_ai(request: Request, q: str, model: str = ""):
    """Ask a bigger model a general-knowledge question.

    The voice model is tuned for speech, not for knowing things, and it was
    inventing appliance instructions rather than admitting it didn't know.
    This is a few seconds and a fraction of a cent - far better than a
    minute of browsing for something a good model simply knows."""
    require_auth(request)
    if is_blocked(q):
        return {"blocked": True, "answer": BLOCKED_REPLY}
    if not OPENAI_API_KEY:
        return {"answer": "", "error": "no model configured"}
    try:
        # model= lets you compare what different models actually know
        # before committing to one in Railway
        use = model.strip() or MODEL_BROWSER
        d = _openai_chat(model=use, cheap=False, messages=[
            {"role": "system", "content": ASK_SYSTEM},
            {"role": "user", "content": q[:600]}])
        said = (d["choices"][0]["message"].get("content") or "").strip()
    except Exception as e:
        return {"answer": "", "error": str(e)[:200]}
    if is_blocked(said):
        return {"blocked": True, "answer": BLOCKED_REPLY}
    return {"answer": said[:900], "model": use}


@app.get("/web/search")
def web_search(request: Request, q: str, near: str = ""):
    require_auth(request)
    return tool_web_search(q, near)


@app.get("/cal/events")
def cal_events(request: Request, account_id: int, days: int = 1,
               which: str = ""):
    require_auth(request)
    return tool_list_events(account_id, days, which)


@app.get("/cal/free")
def cal_free(request: Request, account_id: int, date: str, minutes: int = 60):
    require_auth(request)
    return tool_find_free(account_id, date, minutes)


class NewEvent(BaseModel):
    account_id: int
    title: str
    start_iso: str
    minutes: int = 60
    location: str = ""
    notes: str = ""


@app.post("/cal/create")
def cal_create(e: NewEvent, request: Request):
    require_auth(request)
    record_change(e.account_id, "calendar", "booked",
                  f"{e.title} on {e.start_iso[:16].replace('T', ' at ')}")
    return tool_create_event(e.account_id, e.title, e.start_iso,
                             e.minutes, e.location, e.notes)


@app.post("/cal/cancel")
def cal_cancel(request: Request, account_id: int, event_id: str):
    require_auth(request)
    out = tool_cancel_event(account_id, event_id)
    record_change(account_id, "calendar", "cancelled",
                  "cancelled a calendar entry")
    return out


class SendBody(BaseModel):
    account_id: int
    to: str
    subject: str
    body: str
    which: str = ""


@app.post("/test/send")
def test_send(s: SendBody, request: Request):
    require_auth(request)
    out = tool_send_email(s.account_id, s.to, s.subject, s.body, s.which)
    record_change(s.account_id, "email", "sent",
                  f"sent to {s.to}: \"{s.subject}\"")
    return out


# ----------------------------------------------------------------- admin

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")
SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "")


def _session_value() -> str:
    """Cookie value tied to the password and encryption key."""
    import hashlib
    return hashlib.sha256(
        (ADMIN_PASSWORD + ENCRYPTION_KEY).encode()).hexdigest()[:40]


def require_auth(request: Request):
    """Allow the admin browser session or the agent's service token."""
    if request.cookies.get("pa_session") == _session_value():
        return True
    auth = request.headers.get("authorization", "")
    if SERVICE_TOKEN and auth == f"Bearer {SERVICE_TOKEN}":
        return True
    raise HTTPException(401, "Not authorised.")

ADMIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Phone Assistant &mdash; Admin</title>
<style>
 *{box-sizing:border-box}
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;
      color:#e6e6e6;margin:0;padding:0;}
 header{display:flex;align-items:center;gap:18px;padding:16px 24px;
        border-bottom:1px solid #262b36;background:#141821;
        position:sticky;top:0;z-index:5;}
 header h1{font-size:17px;margin:0;}
 nav{display:flex;gap:4px;margin-left:auto;flex-wrap:wrap;}
 nav a{padding:8px 14px;border-radius:6px;color:#8b94a7;text-decoration:none;
       font-size:14px;cursor:pointer;}
 nav a.on{background:#232936;color:#fff;}
 main{padding:22px 24px;max-width:1150px;}
 .card{background:#171a21;border:1px solid #262b36;border-radius:10px;
       padding:18px;margin-bottom:18px;}
 .card h2{font-size:15px;margin:0 0 4px;}
 .hint{color:#8b94a7;font-size:13px;margin-bottom:10px;}
 label{display:block;font-size:12px;color:#8b94a7;margin:10px 0 4px;}
 input{width:100%;padding:9px 10px;background:#0f1115;border:1px solid #2c3240;
       border-radius:6px;color:#e6e6e6;font-size:14px;}
 .search{max-width:340px;display:inline-block;margin-right:8px;}
 button{padding:9px 15px;background:#3b82f6;border:0;border-radius:6px;
        color:#fff;font-size:14px;cursor:pointer;}
 button.sec{background:#2c3240;padding:6px 12px;}
 table{width:100%;border-collapse:collapse;margin-top:12px;font-size:14px;}
 th{text-align:left;color:#8b94a7;font-weight:500;font-size:12px;
    padding:8px 6px;border-bottom:1px solid #262b36;}
 td{padding:10px 6px;border-bottom:1px solid #1c212b;vertical-align:top;}
 tr.det td{background:#12151c;}
 .ok{color:#4ade80;} .no{color:#f87171;} .warn{color:#fbbf24;}
 .tag{display:inline-block;background:#232936;border-radius:4px;
      padding:2px 7px;margin:2px 3px 2px 0;font-size:12px;color:#c3cad8;}
 .err{color:#f87171;font-size:13px;display:block;margin-top:5px;
      white-space:pre-wrap;line-height:1.5;}
 a.btn{display:inline-block;padding:6px 12px;background:#2c3240;color:#e6e6e6;
       border-radius:6px;text-decoration:none;font-size:13px;}
 .msg{margin-top:10px;font-size:13px;color:#8b94a7;}
 .nums{display:flex;gap:26px;flex-wrap:wrap;}
 .num b{display:block;font-size:24px;color:#fff;}
 .num span{font-size:12px;color:#8b94a7;}
 pre{white-space:pre-wrap;background:#0f1115;padding:14px;border-radius:6px;
     font-size:13px;line-height:1.65;max-height:460px;overflow:auto;
     margin:10px 0 0;}
 .page{display:none;} .page.on{display:block;}
</style></head><body>
<header>
  <h1>Phone Assistant</h1>
  <nav>
    <a data-p="live">Live</a>
    <a data-p="changes">What it did</a>
    <a data-p="reviews">Call checks</a>
    <a data-p="know">Who they are</a>
    <a data-p="blocks">Blocked by</a>
    <a data-p="costs">Costs</a>
    <a data-p="overview" class="on">Overview</a>
    <a data-p="calls">Calls</a>
    <a data-p="customers">Customers</a>
    <a data-p="followups">To do</a>
    <a data-p="signins">Sign-ins</a>
    <a data-p="orders">Orders</a>
    <a data-p="sites">Websites</a>
    <a data-p="jobs">Site logins</a>
    <a data-p="texts">Texts</a>
  </nav>
  <button class="sec" onclick="fetch('/admin/logout',{method:'POST'})
    .then(()=>location.reload())">Sign out</button>
</header>
<main>

<section class="page" id="p-changes">
  <div class="card"><h2>What the assistant did for customers</h2>
    <div class="hint">Every email sent, file changed, card charged, contact
      saved. In plain words, newest first. Passwords and card numbers never
      appear here.</div>
    <label style="display:inline-block;margin:8px 14px 8px 0">Show
      <select id="charea" style="width:auto;margin-left:6px"
              onchange="loadChanges()">
        <option value="">everything</option>
        <option value="email">email</option>
        <option value="drive">documents</option>
        <option value="calendar">calendar</option>
        <option value="contacts">contacts</option>
        <option value="to-do">to-do list</option>
        <option value="payment">payments</option>
        <option value="card">cards</option>
        <option value="login">logins</option>
        <option value="mailbox">mailboxes</option>
      </select></label>
    <button class="sec" onclick="loadChanges()">Refresh</button>
    <table><thead><tr><th>When</th><th>Who</th><th>What</th>
    <th>Details</th><th>Call</th><th>Can it be undone?</th></tr></thead>
    <tbody id="chrows"><tr><td colspan="6" class="hint">Loading&hellip;</td></tr>
    </tbody></table>
  </div>
</section>

<section class="page" id="p-know">
  <div class="card"><h2>What we know about each customer</h2>
    <div class="hint">Built up after every call: how they need to be spoken
      to, who their people are, what they order. The assistant reads this
      before it speaks to them. Passwords and PINs are never kept here.
      Anything you write in "the office says" is yours - the system never
      overwrites it, and it is read first.</div>
    <button class="sec" onclick="loadKnow()">Refresh</button>
    <div id="knowrows" class="hint">Loading&hellip;</div>
  </div>
</section>

<section class="page" id="p-blocks">
  <div class="card"><h2>Which sites refuse us, and why</h2>
    <div class="hint">Every refusal, named. A puzzle or a fingerprint wall
      will not open for anyone - those shops need a sanctioned route or a
      person. An address refusal, a rate limit or a login wall might open,
      and the advice column says what would change it.</div>
    <button class="sec" onclick="loadBlocks()">Refresh</button>
    <table><thead><tr><th>Site</th><th>Kind</th><th>Who blocks</th>
    <th>Times</th><th>Worth retrying?</th><th>What would change it</th>
    </tr></thead>
    <tbody id="blkrows"><tr><td colspan="6" class="hint">Loading&hellip;</td></tr>
    </tbody></table>
    <h2 style="font-size:16px;margin-top:22px">Most recent</h2>
    <table><thead><tr><th>When</th><th>Site</th><th>Kind</th>
    <th>What the page said</th></tr></thead>
    <tbody id="blkrecent"></tbody></table>
  </div>
</section>

<section class="page" id="p-reviews">
  <div class="card"><h2>Calls the system checked itself</h2>
    <div class="hint">After every call, the assistant's own words are read
      back against what actually happened. Anything it said that the record
      doesn't support is listed here, so nobody has to ring in to report
      it.</div>
    <button class="sec" onclick="loadReviews()">Refresh</button>
    <table><thead><tr><th>When</th><th>Call</th><th>Who</th>
    <th>What it found</th></tr></thead>
    <tbody id="rvrows"><tr><td colspan="4" class="hint">Loading&hellip;</td></tr>
    </tbody></table>
  </div>
</section>

<section class="page" id="p-costs">
  <div class="card"><h2>What calls cost</h2>
    <div class="hint">Real token counts from each call, priced with the
      rates in Railway. Verify the rates against your own invoices before
      you price customers.</div>
    <label style="display:inline-block;margin:8px 14px 8px 0">Period
      <select id="costdays" style="width:auto;margin-left:6px">
        <option value="1">today</option>
        <option value="7">7 days</option>
        <option value="30" selected>30 days</option>
        <option value="90">90 days</option>
      </select></label>
    <div id="costtop" style="margin:14px 0"></div>
    <h2 class="no">Where the money goes</h2>
    <table><tbody id="costparts"></tbody></table>
    <h2 class="no" style="margin-top:18px">By customer</h2>
    <table><thead><tr><th>Customer</th><th>Calls</th><th>Minutes</th>
      <th>Cost</th></tr></thead><tbody id="costcust"></tbody></table>
  </div>
</section>

<section class="page" id="p-live">
  <div class="card"><h2>Live log</h2>
    <div class="hint">Everything as it happens: sign-in steps, what Google
      says, browser jobs, orders, tool errors. Updates every 2 seconds.
      Errors in red, notes for the office in amber.</div>
    <label style="display:inline-block;margin-right:14px">
      <input type="checkbox" id="live_pause" style="width:auto"> pause</label>
    <label style="display:inline-block;margin-right:14px">
      <input type="checkbox" id="live_err" style="width:auto"> errors only</label>
    <button class="sec" onclick="liveClear()">Clear view</button>
    <pre id="livelog" style="max-height:70vh;min-height:300px;margin-top:12px">
Waiting for events…</pre>
  </div>
</section>

<section class="page on" id="p-overview">
  <div class="card" id="alertcard" style="display:none;
       border-color:#7f1d1d;background:#1b1113">
    <h2 class="no">Needs attention</h2>
    <div id="alerts"></div>
  </div>
  <div class="card"><h2>Last 7 days</h2>
    <div class="nums" id="stats"><span class="hint">Loading&hellip;</span></div>
  </div>
  <div class="card"><h2>Latest calls</h2>
    <table><thead><tr><th>#</th><th>Who</th><th>When</th><th>Length</th>
    <th>What they wanted</th></tr></thead>
    <tbody id="mini"><tr><td colspan="5" class="hint">Loading&hellip;</td></tr>
    </tbody></table>
  </div>
</section>

<section class="page" id="p-calls">
  <div class="card"><h2>Calls</h2>
    <div class="hint">Search by name, number, call id, or what they asked for.</div>
    <div class="search"><input id="q_calls" placeholder="e.g. David, 3476, send_email"
      onkeydown="if(event.key==='Enter')loadCalls()"></div>
    <button onclick="loadCalls()">Search</button>
    <button class="sec" onclick="document.getElementById('q_calls').value='';loadCalls()">
      Clear</button>
    <table><thead><tr><th>#</th><th>Who</th><th>When</th><th>Length</th>
    <th>PIN</th><th>What they wanted</th><th></th></tr></thead>
    <tbody id="calls"><tr><td colspan="7" class="hint">Loading&hellip;</td></tr>
    </tbody></table>
  </div>
</section>

<section class="page" id="p-customers">
  <div class="card"><h2>Customers</h2>
    <div class="search"><input id="q_cust" placeholder="name, number or email"
      onkeydown="if(event.key==='Enter')load()"></div>
    <button onclick="load()">Search</button>
    <button class="sec" onclick="document.getElementById('q_cust').value='';load()">
      Clear</button>
    <table><thead><tr><th>ID</th><th>Name</th><th>Phone</th>
    <th>Mailboxes</th><th></th></tr></thead>
    <tbody id="rows"><tr><td colspan="5" class="hint">Loading&hellip;</td></tr>
    </tbody></table>
  </div>
  <div class="card"><h2>Add a customer</h2>
    <div class="hint">Customers normally set themselves up by phone. This is
      for when you need to add someone by hand.</div>
    <label>Name</label><input id="n">
    <label>Their phone number</label><input id="p" placeholder="+18455551234">
    <label>PIN</label><input id="k" value="1234">
    <button onclick="add()">Create</button>
    <div class="msg" id="msg"></div>
  </div>
</section>

<section class="page" id="p-followups">
  <div class="card"><h2>Needs attention</h2>
    <div class="hint">Anything the assistant couldn't finish, or that a
      caller asked to be passed on.</div>
    <button class="sec" onclick="loadFu()">Refresh</button>
    <button class="sec" onclick="fuAll=!fuAll;loadFu()">Show/hide done</button>
    <table><thead><tr><th>When</th><th>Who</th><th>Why</th><th>Note</th>
    <th>Call</th><th></th></tr></thead>
    <tbody id="furows"><tr><td colspan="6" class="hint">Loading&hellip;</td></tr>
    </tbody></table>
  </div>
</section>

<section class="page" id="p-signins">
  <div class="card"><h2>Email sign-in attempts</h2>
    <div class="hint">Every step of each attempt, with the reason it stopped.</div>
    <button class="sec" onclick="loadOb()">Refresh</button>
    <table><thead><tr><th>#</th><th>Who</th><th>Address</th><th>When</th>
    <th>Result</th><th></th></tr></thead>
    <tbody id="obrows"><tr><td colspan="6" class="hint">Loading&hellip;</td></tr>
    </tbody></table>
  </div>
</section>

<section class="page" id="p-orders">
  <div class="card"><h2>Orders</h2>
    <div class="hint">Every order a customer confirmed on the phone, and what
      happened to it. Card numbers never appear here.</div>
    <button class="sec" onclick="loadOrders()">Refresh</button>
    <table><thead><tr><th>#</th><th>Who</th><th>Site</th><th>Item</th>
    <th>Expected</th><th>Status</th><th>Confirmation</th><th>When</th>
    <th></th></tr></thead>
    <tbody id="orderrows"><tr><td colspan="9" class="hint">Loading&hellip;</td>
    </tr></tbody></table>
  </div>
</section>

<section class="page" id="p-sites">
  <div class="card"><h2>What customers ask for, by site</h2>
    <div class="hint">Last 30 days. "Learned" means it ran from saved steps
      with no thinking; "fell back" means the saved steps broke and it
      worked it out fresh. Nothing here needs you to do anything.</div>
    <button class="sec" onclick="loadSites()">Refresh</button>
    <table><thead><tr><th>Site</th><th>Requests</th><th>Worked</th>
    <th>Learned</th><th>Fell back</th><th>Avg time</th>
    <th>Most asked</th></tr></thead>
    <tbody id="siterows"><tr><td colspan="7" class="hint">Loading&hellip;</td>
    </tr></tbody></table>
  </div>
  <div class="card"><h2>What it has learned</h2>
    <div class="hint">Steps it recorded after succeeding once. It retires a
      recipe by itself after repeated failures and re-learns.</div>
    <table><thead><tr><th>Site</th><th>Task</th><th>Example</th>
    <th>Worked</th><th>Failed</th><th>Last ok</th><th></th></tr></thead>
    <tbody id="reciperows"><tr><td colspan="7" class="hint">Loading&hellip;</td>
    </tr></tbody></table>
  </div>
  <div class="card"><h2>Recent website activity</h2>
    <div class="hint">Every request, including fallbacks and failures.</div>
    <table><thead><tr><th>When</th><th>Site</th><th>Task</th>
    <th>What they asked</th><th>How</th><th>Result</th><th>Time</th>
    <th></th></tr></thead>
    <tbody id="siteevents"><tr><td colspan="8" class="hint">Loading&hellip;</td>
    </tr></tbody></table>
  </div>
</section>

<section class="page" id="p-jobs">
  <div class="card"><h2>Browser queue</h2>
    <div class="nums" id="qhealth"><span class="hint">Loading&hellip;</span></div>
  </div>
  <div class="card"><h2>Site sign-ins</h2>
    <div class="hint">Amazon, Walmart, Temu. Sessions are kept so customers
      aren't asked to sign in again each time.</div>
    <button class="sec" onclick="loadJobs()">Refresh</button>
    <table><thead><tr><th>#</th><th>Who</th><th>Site</th><th>When</th>
    <th>Result</th><th></th></tr></thead>
    <tbody id="jobrows"><tr><td colspan="6" class="hint">Loading&hellip;</td></tr>
    </tbody></table>
  </div>
</section>

<section class="page" id="p-texts">
  <div class="card"><h2>Text delivery</h2>
    <div class="hint">What the carrier reported for each message.</div>
    <button class="sec" onclick="loadDlr()">Refresh</button>
    <table><thead><tr><th>When</th><th>To</th><th>Status</th>
    <th>Detail</th></tr></thead>
    <tbody id="dlr"><tr><td colspan="4" class="hint">Loading&hellip;</td></tr>
    </tbody></table>
  </div>
</section>

</main>
<script>
function esc(x){ return String(x==null?'':x)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

document.querySelectorAll('nav a').forEach(function(a){
  a.onclick = function(){
    document.querySelectorAll('nav a').forEach(function(b){
      b.classList.remove('on'); });
    a.classList.add('on');
    document.querySelectorAll('.page').forEach(function(s){
      s.classList.remove('on'); });
    document.getElementById('p-'+a.dataset.p).classList.add('on');
    if(a.dataset.p === 'changes') loadChanges();
    if(a.dataset.p === 'reviews') loadReviews();
    if(a.dataset.p === 'know') loadKnow();
    if(a.dataset.p === 'blocks') loadBlocks();
  };
});

function taskCell(c){
  var t = (c.tasks||[]).map(function(x){
    return '<span class="tag">'+esc(x)+'</span>'; }).join('');
  if(!t) t = '<span class="hint">just talking</span>';
  if(c.slowest_ms > 2500) t += '<span class="warn"> slow '+c.slowest_ms+'ms</span>';
  (c.problems||[]).forEach(function(p){
    t += '<span class="err">'+esc(p)+'</span>'; });
  return t;
}

async function loadStats(){
  try{
    const d = await (await fetch('/stats?days=7')).json();
    const tasks = (d.top_tasks||[]).map(function(t){
      return '<span class="tag">'+esc(t[0])+' '+t[1]+'</span>'; }).join('')
      || '&mdash;';
    document.getElementById('stats').innerHTML =
      '<div class="num"><b>'+d.calls+'</b><span>calls</span></div>'+
      '<div class="num"><b>'+d.verified+'</b><span>passed PIN</span></div>'+
      '<div class="num"><b>'+d.avg_seconds+'s</b><span>avg length</span></div>'+
      '<div class="num"><b>'+d.avg_tool_ms+'ms</b><span>avg lookup</span></div>'+
      '<div class="num" style="flex:1"><span>most asked for</span>'+
      '<div style="margin-top:6px">'+tasks+'</div></div>';
  }catch(e){
    document.getElementById('stats').innerHTML =
      '<span class="no">'+esc(e.message)+'</span>'; }
}

async function loadCalls(){
  const q = (document.getElementById('q_calls')||{}).value || '';
  const tb = document.getElementById('calls');
  const mini = document.getElementById('mini');
  try{
    const d = await (await fetch('/calls?limit=50&q='+
      encodeURIComponent(q))).json();
    if(!d.length){
      tb.innerHTML = '<tr><td colspan="7" class="hint">No calls found.</td></tr>';
      mini.innerHTML = '<tr><td colspan="5" class="hint">No calls yet.</td></tr>';
      return;
    }
    tb.innerHTML = d.map(function(c){
      return '<tr><td>'+c.call_id+'</td>'+
        '<td>'+esc(c.who)+'<br><span class="hint">'+esc(c.from)+'</span></td>'+
        '<td>'+esc(c.started)+'</td>'+
        '<td>'+c.seconds+'s<br><span class="hint">'+c.turns+' turns</span></td>'+
        '<td>'+(c.verified?'<span class="ok">ok</span>'
                          :'<span class="no">no</span>')+'</td>'+
        '<td>'+taskCell(c)+'</td>'+
        '<td><button class="sec" onclick="showTx('+c.call_id+',this)">'+
        'Transcript</button></td></tr>'+
        '<tr class="det" id="det'+c.call_id+'" style="display:none">'+
        '<td colspan="7"><pre id="tx'+c.call_id+'"></pre></td></tr>';
    }).join('');
    mini.innerHTML = d.slice(0,6).map(function(c){
      return '<tr><td>'+c.call_id+'</td><td>'+esc(c.who)+'</td>'+
        '<td>'+esc(c.started)+'</td><td>'+c.seconds+'s</td>'+
        '<td>'+taskCell(c)+'</td></tr>'; }).join('');
  }catch(e){
    tb.innerHTML = '<tr><td colspan="7" class="no">'+esc(e.message)+'</td></tr>'; }
}

async function showTx(id, btn){
  const row = document.getElementById('det'+id);
  const pre = document.getElementById('tx'+id);
  if(row.style.display === 'table-row'){
    row.style.display = 'none'; btn.textContent = 'Transcript'; return; }
  row.style.display = 'table-row';
  btn.textContent = 'Hide';
  pre.textContent = 'Loading…';
  try{
    const r = await fetch('/calls/'+id);
    if(!r.ok){ pre.innerHTML = '<span class="no">Server said '+r.status+
      '. '+esc(await r.text())+'</span>'; return; }
    const d = await r.json();
    if(!d.length){
      pre.innerHTML = '<span class="warn">Nothing was recorded for this '+
        'call. If this keeps happening, SERVICE_TOKEN may be missing on '+
        'the agent service.</span>'; return; }
    pre.textContent = d.map(function(t){
      return '['+t.at+'] '+t.who+(t.tool?' ('+t.tool+')':'')+
             (t.latency_ms?' '+t.latency_ms+'ms':'')+': '+t.text;
    }).join(String.fromCharCode(10));
  }catch(e){ pre.innerHTML = '<span class="no">'+esc(e.message)+'</span>'; }
}

function mboxes(a){
  var m = a.mailboxes || [];
  if(!m.length) return '<span class="no">none connected</span>';
  return m.map(function(b){
    return '<div style="margin-bottom:4px"><span class="ok">'+
      esc(b.email)+'</span>'+
      (b.label?' <span class="tag">'+esc(b.label)+'</span>':'')+
      (b.default?' <span class="tag">main</span>':'')+
      ' <span class="hint">'+b.used+' uses</span></div>'; }).join('');
}

async function load(){
  const q = (document.getElementById('q_cust')||{}).value || '';
  const tb = document.getElementById('rows');
  try{
    const d = await (await fetch('/accounts?q='+
      encodeURIComponent(q))).json();
    if(!d.length){
      tb.innerHTML='<tr><td colspan="5" class="hint">None found.</td></tr>';
      return; }
    tb.innerHTML = d.map(function(a){
      return '<tr><td>'+a.account_id+'</td><td>'+esc(a.name)+'</td>'+
        '<td>'+esc((a.phones||[]).join(', '))+'</td>'+
        '<td>'+mboxes(a)+'</td>'+
        '<td><button class="sec" onclick="copyLink('+a.account_id+
        ')">Link</button> '+
        '<button class="sec" onclick="copyLink('+a.account_id+')">Copy</button> '+
        '<button class="sec" onclick="textLink('+a.account_id+')">Text</button>'+
        '</td></tr>'; }).join('');
  }catch(e){
    tb.innerHTML='<tr><td colspan="5" class="no">'+esc(e.message)+'</td></tr>'; }
}

var fuAll = false;
async function loadCosts(){
  try{
    const days = document.getElementById('costdays').value;
    const d = await (await fetch('/usage/summary?days='+days)).json();
    document.getElementById('costtop').innerHTML =
      '<b style="font-size:26px">$'+d.total_cost_usd.toFixed(2)+'</b>'+
      '<span class="hint"> over '+d.calls+' calls, '+
      d.total_minutes+' minutes</span><br>'+
      '<span class="hint">$'+d.cost_per_call_usd.toFixed(3)+
      ' per call &middot; $'+d.cost_per_minute_usd.toFixed(3)+
      ' per minute</span>';
    document.getElementById('costparts').innerHTML =
      Object.entries(d.by_component_usd).map(function(e){
        return '<tr><td>'+esc(e[0])+'</td><td>$'+e[1].toFixed(4)+
               '</td></tr>'; }).join('') ||
      '<tr><td class="hint">Nothing recorded yet.</td></tr>';
    document.getElementById('costcust').innerHTML =
      Object.entries(d.by_customer).map(function(e){
        return '<tr><td>'+esc(e[0])+'</td><td>'+e[1].calls+'</td><td>'+
               e[1].minutes.toFixed(1)+'</td><td>$'+
               e[1].cost_usd.toFixed(3)+'</td></tr>'; }).join('') ||
      '<tr><td class="hint">Nothing recorded yet.</td></tr>';
  }catch(e){}
}
document.getElementById('costdays').addEventListener('change', loadCosts);
async function loadAlerts(){
  try{
    const d = await (await fetch('/followups?include_done=0')).json();
    const urgent = d.filter(function(f){
      return f.reason==='proxy_fallback' || f.channel==='system'; });
    const card = document.getElementById('alertcard');
    if(!urgent.length){ card.style.display='none'; return; }
    card.style.display='block';
    document.getElementById('alerts').innerHTML = urgent.map(function(f){
      return '<div class="err" style="margin-bottom:8px">'+esc(f.at)+
        ' — '+esc(f.note)+
        ' <button class="sec" onclick="fuDone('+f.id+',0)">Fixed</button>'+
        '</div>'; }).join('');
  }catch(e){}
}
async var chArea = "";
function loadChanges(){
  var sel = document.getElementById('charea');
  chArea = sel ? sel.value : "";
  fetch('/changes?limit=200' + (chArea ? '&area=' + chArea : ''))
    .then(function(r){ return r.json(); })
    .then(function(rows){
      var b = document.getElementById('chrows');
      if(!rows.length){ b.innerHTML =
        '<tr><td colspan="6" class="hint">Nothing yet.</td></tr>'; return; }
      b.innerHTML = rows.map(function(c){
        return '<tr><td>' + esc(c.at) + '</td>' +
          '<td>' + esc(c.who || ('#' + (c.account_id||''))) + '</td>' +
          '<td><span class="tag">' + esc(c.area) + '</span> ' +
            esc(c.what) + '</td>' +
          '<td>' + esc(c.detail) + '</td>' +
          '<td>' + (c.call_id ? esc(c.call_id) : '') + '</td>' +
          '<td class="hint">' + esc(c.undo || '') + '</td></tr>';
      }).join('');
    });
}
function loadKnow(){
  fetch('/profiles?limit=100').then(function(r){ return r.json(); })
    .then(function(rows){
      var b = document.getElementById('knowrows');
      if(!rows.length){ b.innerHTML =
        'Nothing learned yet. It fills in after calls.'; return; }
      b.innerHTML = rows.map(function(p){
        return '<div class="card" style="margin-top:14px">' +
          '<h2 style="font-size:16px">' + esc(p.who || ('#'+p.account_id)) +
          ' <span class="hint" style="font-weight:400">updated ' +
          esc(p.updated) + '</span></h2>' +
          '<div class="hint">From earlier calls</div>' +
          '<pre style="max-height:200px">' +
          esc(p.notes || '(nothing yet)') + '</pre>' +
          '<div class="hint" style="margin-top:10px">The office says' +
          ' &mdash; the assistant reads this first</div>' +
          '<textarea id="byhand' + p.account_id + '" rows="4" ' +
          'style="width:100%;background:#0f1115;color:#e6e6e6;border:' +
          '1px solid #262b36;border-radius:6px;padding:10px;' +
          'font-size:14px">' + esc(p.by_hand) + '</textarea>' +
          '<button class="sec" style="margin-top:8px" onclick="saveKnow(' +
          p.account_id + ')">Save</button>' +
          '<span class="msg" id="knowmsg' + p.account_id + '"></span>' +
          '</div>';
      }).join('');
    });
}
function saveKnow(id){
  var box = document.getElementById('byhand' + id);
  var msg = document.getElementById('knowmsg' + id);
  fetch('/profile', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({account_id:id, by_hand:box.value})})
    .then(function(r){ msg.textContent = r.ok ? ' Saved.' : ' Did not save.';
                       setTimeout(function(){ msg.textContent=''; }, 2500); });
}
function loadBlocks(){
  fetch('/blocks?days=60').then(function(r){ return r.json(); })
    .then(function(d){
      var b = document.getElementById('blkrows');
      var rows = d.by_site || [];
      b.innerHTML = rows.length ? rows.map(function(x){
        return '<tr><td>' + esc(x.site) + '</td>' +
          '<td><span class="tag">' + esc(x.kind) + '</span></td>' +
          '<td>' + esc(x.vendor || '') + '</td>' +
          '<td>' + esc(x.times) + '</td>' +
          '<td class="' + (x.worth_retrying ? 'ok' : 'no') + '">' +
            (x.worth_retrying ? 'maybe' : 'no') + '</td>' +
          '<td class="hint">' + esc(x.advice) + '</td></tr>';
      }).join('') :
        '<tr><td colspan="6" class="hint">No refusals recorded.</td></tr>';
      var r2 = document.getElementById('blkrecent');
      r2.innerHTML = (d.recent || []).slice(0, 25).map(function(x){
        return '<tr><td>' + esc(x.at) + '</td><td>' + esc(x.site) +
          '</td><td>' + esc(x.kind) + '</td><td class="hint">' +
          esc(x.saw) + '</td></tr>';
      }).join('');
    });
}
function loadReviews(){
  fetch('/reviews?limit=50').then(function(r){ return r.json(); })
    .then(function(rows){
      var b = document.getElementById('rvrows');
      if(!rows.length){ b.innerHTML =
        '<tr><td colspan="4" class="hint">No calls flagged.</td></tr>';
        return; }
      b.innerHTML = rows.map(function(r){
        return '<tr><td>' + esc(r.at) + '</td>' +
          '<td>' + esc(r.call_id || '') + '</td>' +
          '<td>' + esc(r.account_id || '') + '</td>' +
          '<td><pre style="margin:0;max-height:200px">' +
            esc(r.note) + '</pre></td></tr>';
      }).join('');
    });
}
function loadFu(){
  const tb = document.getElementById('furows');
  try{
    const d = await (await fetch('/followups?include_done='+
      (fuAll?1:0))).json();
    if(!d.length){
      tb.innerHTML='<tr><td colspan="6" class="hint">Nothing outstanding.'+
        '</td></tr>'; return; }
    tb.innerHTML = d.map(function(f){
      return '<tr'+(f.done?' style="opacity:.45"':'')+'>'+
        '<td>'+esc(f.at)+'</td>'+
        '<td>'+esc(f.who)+'<br><span class="hint">'+esc(f.phone)+'</span></td>'+
        '<td><span class="tag">'+esc(f.reason)+'</span><br>'+
        '<span class="hint">'+esc(f.channel)+'</span></td>'+
        '<td>'+esc(f.note)+'</td>'+
        '<td>'+(f.call_id?('#'+f.call_id):'&mdash;')+'</td>'+
        '<td><button class="sec" onclick="fuDone('+f.id+','+
        (f.done?1:0)+')">'+(f.done?'Reopen':'Done')+'</button></td></tr>';
    }).join('');
  }catch(e){
    tb.innerHTML='<tr><td colspan="6" class="no">'+esc(e.message)+'</td></tr>'; }
}
async function fuDone(id, isDone){
  await fetch('/followups/done?followup_id='+id+'&undo='+(isDone?1:0),
              {method:'POST'});
  loadFu();
}
async function loadOb(){
  const tb = document.getElementById('obrows');
  try{
    const d = await (await fetch('/onboard/sessions?limit=20')).json();
    if(!d.length){
      tb.innerHTML='<tr><td colspan="6" class="hint">None yet.</td></tr>';
      return; }
    tb.innerHTML = d.map(function(o){
      var cls = o.state==='done'?'ok':(o.state==='failed'?'no':'warn');
      return '<tr><td>'+o.session_id+'</td><td>'+esc(o.who)+'</td>'+
        '<td>'+esc(o.email)+'</td><td>'+esc(o.at)+'</td>'+
        '<td class="'+cls+'">'+esc(o.state)+
        (o.message?'<span class="'+(cls==='no'?'err':'hint')+'">'+
          esc(o.message)+'</span>':'')+'</td>'+
        '<td><button class="sec" onclick="showOb('+o.session_id+',this)">'+
        'Steps</button></td></tr>'+
        '<tr class="det" id="ob'+o.session_id+'" style="display:none">'+
        '<td colspan="6"><pre>'+esc(o.history||'No steps recorded.')+
        '</pre></td></tr>'; }).join('');
  }catch(e){
    tb.innerHTML='<tr><td colspan="6" class="no">'+esc(e.message)+'</td></tr>'; }
}
function showOb(id, btn){
  const row = document.getElementById('ob'+id);
  const open = row.style.display === 'table-row';
  row.style.display = open ? 'none' : 'table-row';
  btn.textContent = open ? 'Steps' : 'Hide';
}

var liveLast = 0, liveLines = [];
function liveClear(){ liveLines = []; render(); }
function render(){
  const errOnly = document.getElementById('live_err').checked;
  const el = document.getElementById('livelog');
  const shown = liveLines.filter(function(l){ return !errOnly || l.level!=='info'; });
  el.innerHTML = shown.length ? shown.map(function(l){
    var color = l.level==='error' ? '#f87171' : (l.level==='warn' ? '#fbbf24' : '#c3cad8');
    return '<span style="color:#8b94a7">'+esc(l.at)+'</span> '+
      '<span style="color:#60a5fa">'+esc(l.ref)+'</span>'+
      (l.who?' <span style="color:#8b94a7">'+esc(l.who)+'</span>':'')+
      ' <span style="color:'+color+'">'+esc(l.text)+'</span>';
  }).join(String.fromCharCode(10)) : 'Nothing yet.';
  el.scrollTop = el.scrollHeight;
}
async function pollLive(){
  if(document.getElementById('live_pause').checked) return;
  try{
    const d = await (await fetch('/events?after_id='+liveLast+'&limit=200')).json();
    if(d.length){
      const seen = {};
      liveLines.forEach(function(l){ seen[l.id] = 1; });
      const fresh = d.filter(function(l){
        if(seen[l.id]) return false; seen[l.id] = 1; return true; });
      if(fresh.length){
        liveLines = liveLines.concat(fresh).slice(-800);
        liveLast = Math.max(liveLast, fresh[fresh.length-1].id);
        render();
      }
    }
  }catch(e){}
}
document.getElementById('live_err').addEventListener('change', render);
async function loadOrders(){
  const tb = document.getElementById('orderrows');
  try{
    const d = await (await fetch('/orders?limit=50')).json();
    if(!d.length){ tb.innerHTML='<tr><td colspan="9" class="hint">'+
      'No orders yet.</td></tr>'; return; }
    tb.innerHTML = d.map(function(o){
      var cls = o.state==='placed'?'ok':(o.state==='failed'?'no':
                (o.state==='cancelled'?'hint':'warn'));
      return '<tr><td>'+o.order_id+'</td><td>'+esc(o.who)+'</td>'+
        '<td><span class="tag">'+esc(o.site)+'</span></td>'+
        '<td>'+o.quantity+' x '+esc(o.item)+'</td>'+
        '<td>'+(o.expected_price?'$'+esc(o.expected_price):'')+'</td>'+
        '<td class="'+cls+'">'+esc(o.state)+
        (o.message?'<span class="'+(cls==='no'?'err':'hint')+'">'+
          esc(o.message)+'</span>':'')+'</td>'+
        '<td>'+esc(o.confirmation||'')+(o.final_total?'<br><span class="hint">$'+
          esc(o.final_total)+'</span>':'')+'</td>'+
        '<td>'+esc(o.at)+'</td>'+
        '<td><button class="sec" onclick="showOrd('+o.order_id+',this)">'+
        'Steps</button></td></tr>'+
        '<tr class="det" id="od'+o.order_id+'" style="display:none">'+
        '<td colspan="9"><pre>'+esc(o.history||'No steps.')+'</pre></td></tr>';
    }).join('');
  }catch(e){ tb.innerHTML='<tr><td colspan="9" class="no">'+esc(e.message)+
    '</td></tr>'; }
}
function showOrd(id, btn){
  const row = document.getElementById('od'+id);
  const open = row.style.display === 'table-row';
  row.style.display = open ? 'none' : 'table-row';
  btn.textContent = open ? 'Steps' : 'Hide';
}
async function loadSites(){
  const tb = document.getElementById('siterows');
  const rb = document.getElementById('reciperows');
  const eb = document.getElementById('siteevents');
  try{
    const d = await (await fetch('/sites/report?days=30')).json();
    tb.innerHTML = d.sites.length ? d.sites.map(function(x){
      var tasks = (x.tasks||[]).map(function(t){
        return '<span class="tag">'+esc(t[0])+' '+t[1]+'</span>'; }).join('');
      return '<tr><td><b>'+esc(x.site)+'</b></td><td>'+x.requests+'</td>'+
        '<td class="ok">'+x.ok+'</td><td>'+x.via_recipe+'</td>'+
        '<td class="'+(x.fallbacks?'warn':'')+'">'+x.fallbacks+'</td>'+
        '<td>'+x.avg_seconds+'s</td><td>'+tasks+'</td></tr>'; }).join('')
      : '<tr><td colspan="7" class="hint">No website requests yet.</td></tr>';
    rb.innerHTML = d.recipes.length ? d.recipes.map(function(r){
      var steps = (r.steps||[]).map(function(st, i){
        var t = (i+1)+'. '+st.action;
        if(st.desc) t += ' → '+st.desc;
        if(st.url) t += ' → '+st.url;
        if(st.text) t += ' ["'+st.text+'"]';
        return t; }).join(String.fromCharCode(10));
      return '<tr'+(r.retired?' style="opacity:.45"':'')+'>'+
        '<td>'+esc(r.site)+'</td><td><span class="tag">'+esc(r.task)+
        '</span>'+(r.retired?' <span class="no">retired</span>':'')+'</td>'+
        '<td class="hint">'+esc(r.example)+'</td>'+
        '<td class="ok">'+r.ok+'</td><td class="'+(r.failed?'no':'')+'">'+
        r.failed+'</td><td>'+esc(r.last_ok)+'</td>'+
        '<td><button class="sec" onclick="showRec('+r.id+',this)">Steps'+
        '</button></td></tr>'+
        '<tr class="det" id="rc'+r.id+'" style="display:none"><td colspan="7">'+
        '<pre>'+esc(steps||'No steps.')+'</pre></td></tr>'; }).join('')
      : '<tr><td colspan="7" class="hint">Nothing learned yet.</td></tr>';
  }catch(e){ tb.innerHTML='<tr><td colspan="7" class="no">'+esc(e.message)+
    '</td></tr>'; }
  try{
    const ev = await (await fetch('/sites/events?limit=40')).json();
    eb.innerHTML = ev.length ? ev.map(function(x){
      var how = x.path==='recipe' ? '<span class="ok">learned</span>'
        : x.path==='fallback' ? '<span class="warn">fell back</span>'
        : 'worked it out';
      var res = x.outcome==='ok' ? '<span class="ok">ok</span>'
        : '<span class="no">failed</span>';
      return '<tr><td>'+esc(x.at)+'</td><td>'+esc(x.site)+'</td>'+
        '<td><span class="tag">'+esc(x.task)+'</span></td>'+
        '<td class="hint">'+esc(x.goal)+'</td><td>'+how+'</td>'+
        '<td>'+res+'</td><td>'+x.seconds+'s</td>'+
        '<td>'+(x.job_id?'<button class="sec" onclick="jumpJob('+x.job_id+
        ')">Details</button>':'')+'</td></tr>'; }).join('')
      : '<tr><td colspan="8" class="hint">Nothing yet.</td></tr>';
  }catch(e){ eb.innerHTML='<tr><td colspan="8" class="no">'+esc(e.message)+
    '</td></tr>'; }
}
function showRec(id, btn){
  const row = document.getElementById('rc'+id);
  const open = row.style.display === 'table-row';
  row.style.display = open ? 'none' : 'table-row';
  btn.textContent = open ? 'Steps' : 'Hide';
}
function jumpJob(id){
  document.querySelector('nav a[data-p="jobs"]').click();
  setTimeout(function(){
    var row = document.getElementById('jb'+id);
    if(row){ row.style.display='table-row'; row.scrollIntoView(); }
  }, 300);
}
async function loadHealth(){
  try{
    const d = await (await fetch('/jobs/health')).json();
    document.getElementById('qhealth').innerHTML =
      '<div class="num"><b>'+d.running+'</b><span>running now</span></div>'+
      '<div class="num"><b>'+d.waiting+'</b><span>waiting</span></div>'+
      '<div class="num"><b>'+d.max_browsers+'</b><span>max at once</span></div>'+
      '<div class="num"><b>'+d.saved_sessions+'</b><span>saved sessions</span></div>'+
      (d.stuck_over_20min ? '<div class="num"><b class="no">'+
        d.stuck_over_20min+'</b><span>stuck 20min+</span></div>' : '');
  }catch(e){ document.getElementById('qhealth').innerHTML =
    '<span class="no">'+esc(e.message)+'</span>'; }
}
async function loadJobs(){
  const tb = document.getElementById('jobrows');
  try{
    const d = await (await fetch('/jobs?limit=25')).json();
    if(!d.length){
      tb.innerHTML='<tr><td colspan="6" class="hint">None yet.</td></tr>';
      return; }
    tb.innerHTML = d.map(function(j){
      var cls = j.state==='done'?'ok':(j.state==='failed'?'no':'warn');
      return '<tr><td>'+j.job_id+'</td><td>'+esc(j.who)+'</td>'+
        '<td><span class="tag">'+esc(j.site)+'</span> '+
        '<span class="hint">'+esc(j.kind)+'</span></td>'+
        '<td>'+esc(j.at)+'</td>'+
        '<td class="'+cls+'">'+esc(j.state)+
        (j.message?'<span class="'+(cls==='no'?'err':'hint')+'">'+
          esc(j.message)+'</span>':'')+'</td>'+
        '<td><button class="sec" onclick="showJob('+j.job_id+',this)">'+
        'Steps</button></td></tr>'+
        '<tr class="det" id="jb'+j.job_id+'" style="display:none">'+
        '<td colspan="6"><pre>'+esc(j.history||'No steps.')+'</pre></td></tr>';
    }).join('');
  }catch(e){
    tb.innerHTML='<tr><td colspan="6" class="no">'+esc(e.message)+'</td></tr>'; }
}
function showJob(id, btn){
  const row = document.getElementById('jb'+id);
  const open = row.style.display === 'table-row';
  row.style.display = open ? 'none' : 'table-row';
  btn.textContent = open ? 'Steps' : 'Hide';
}
async function loadDlr(){
  const tb = document.getElementById('dlr');
  try{
    const d = await (await fetch('/sms/dlr?limit=25')).json();
    if(!d.length){
      tb.innerHTML='<tr><td colspan="4" class="hint">'+
        'No delivery receipts yet.</td></tr>'; return; }
    tb.innerHTML = d.map(function(x){
      var good = /deliver|success|ok/i.test(x.status||'');
      return '<tr><td>'+esc(x.at)+'</td><td>'+esc(x.to)+'</td>'+
        '<td class="'+(good?'ok':'no')+'">'+esc(x.status||'?')+'</td>'+
        '<td class="hint">'+esc(x.raw||'')+'</td></tr>'; }).join('');
  }catch(e){
    tb.innerHTML='<tr><td colspan="4" class="no">'+esc(e.message)+'</td></tr>'; }
}

function copyLink(id){
  fetch('/link/new?account_id=' + id).then(function(r){ return r.json(); })
   .then(function(d){
     navigator.clipboard.writeText(d.url);
     document.getElementById('msg').textContent =
       'Copied a link for ' + d.for + ', good for ' + d.valid_minutes +
       ' minutes.';
   }).catch(function(e){
     document.getElementById('msg').textContent = 'Could not make a link.';
   });
}
async function textLink(id){
  const m = document.getElementById('msg');
  m.textContent = 'Sending…';
  try{
    const d = await (await fetch('/sms/link?account_id='+id,
                                 {method:'POST'})).json();
    m.textContent = d.sent ? 'Text sent.'
      : ('Not sent: ' + (d.detail || d.error || 'check SMS settings'));
  }catch(e){ m.textContent = 'Not sent: ' + e.message; }
}
async function add(){
  const m = document.getElementById('msg');
  const r = await fetch('/accounts',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name:document.getElementById('n').value,
      phone:document.getElementById('p').value,
      pin:document.getElementById('k').value})});
  if(r.ok){ m.textContent='Created.';
    document.getElementById('n').value='';
    document.getElementById('p').value=''; load(); }
  else { m.textContent='Failed — that number may already exist.'; }
}

load(); loadCalls(); loadStats(); loadDlr(); loadOb(); loadFu(); loadJobs(); loadHealth(); loadSites(); loadOrders(); loadAlerts(); loadCosts();
if(!window._livePoller){
  pollLive(); window._livePoller = setInterval(pollLive, 2000);
}
setInterval(function(){ loadCalls(); loadStats(); loadDlr(); loadOb();
                        loadFu(); loadJobs(); loadHealth(); loadSites();
                        loadOrders(); loadAlerts(); loadCosts(); }, 25000);
</script></body></html>"""


LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in</title>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;
      color:#e6e6e6;display:flex;align-items:center;justify-content:center;
      height:100vh;margin:0;}
 .box{background:#171a21;border:1px solid #262b36;border-radius:10px;
      padding:28px;width:300px;}
 h2{margin:0 0 16px;font-size:17px;}
 input{width:100%;padding:10px;background:#0f1115;border:1px solid #2c3240;
       border-radius:6px;color:#e6e6e6;font-size:14px;box-sizing:border-box;}
 button{width:100%;margin-top:14px;padding:10px;background:#3b82f6;border:0;
        border-radius:6px;color:#fff;font-size:14px;cursor:pointer;}
 .err{color:#f87171;font-size:13px;margin-top:10px;min-height:18px;}
</style></head><body>
<div class="box">
  <h2>Phone Assistant</h2>
  <input id="p" type="password" placeholder="Password"
         onkeydown="if(event.key==='Enter')go()">
  <button onclick="go()">Sign in</button>
  <div class="err" id="e"></div>
</div>
<script>
async function go(){
  const r = await fetch('/admin/login', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({password: document.getElementById('p').value})});
  if(r.ok){ location.href = '/admin'; }
  else { document.getElementById('e').textContent = 'Wrong password.'; }
}
</script></body></html>"""


class LoginBody(BaseModel):
    password: str


@app.post("/admin/login")
def admin_login(b: LoginBody):
    if b.password != ADMIN_PASSWORD:
        raise HTTPException(401, "Wrong password.")
    r = JSONResponse({"ok": True})
    r.set_cookie("pa_session", _session_value(), httponly=True,
                 secure=True, samesite="lax", max_age=60 * 60 * 12)
    return r


@app.post("/admin/logout")
def admin_logout():
    r = JSONResponse({"ok": True})
    r.delete_cookie("pa_session")
    return r


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    if request.cookies.get("pa_session") != _session_value():
        return HTMLResponse(LOGIN_HTML,
                            headers={"Cache-Control": "no-store, max-age=0"})
    return HTMLResponse(ADMIN_HTML,
                        headers={"Cache-Control": "no-store, max-age=0"})


class OnboardStart(BaseModel):
    account_id: int
    email: str
    password: str


@app.post("/onboard/start")
def onboard_start(b: OnboardStart, request: Request,
                  background: BackgroundTasks):
    require_auth(request)
    if not BROWSERBASE_API_KEY:
        raise HTTPException(400, "Browserbase isn't configured.")

    db = Session()
    row = Onboard(account_id=b.account_id, email=b.email, state="starting")
    db.add(row)
    db.commit()
    db.refresh(row)
    sid = row.id
    db.close()

    _PENDING[sid] = {"password": b.password, "code": None,
                     "other_way": False}
    background.add_task(_run_signin, sid, b.account_id, b.email)
    return {"session_id": sid, "state": "starting"}


@app.post("/onboard/another-way")
def onboard_another_way(request: Request, session_id: int):
    """Caller can't tap the phone prompt — ask Google for a texted code."""
    require_auth(request)
    if session_id in _PENDING:
        _PENDING[session_id]["other_way"] = True
        return {"ok": True}
    raise HTTPException(400, "That sign-in is no longer running.")


class OnboardCode(BaseModel):
    session_id: int
    code: str


@app.post("/onboard/code")
def onboard_code(b: OnboardCode, request: Request):
    require_auth(request)
    if b.session_id in _PENDING:
        _PENDING[b.session_id]["code"] = b.code.strip()
        return {"ok": True}
    raise HTTPException(400, "That sign-in is no longer running.")


@app.post("/onboard/cancel")
def onboard_cancel(request: Request, session_id: int):
    """The caller hung up mid sign-in - stop driving the browser."""
    require_auth(request)
    if session_id in _PENDING:
        _PENDING[session_id]["cancelled"] = True
        return {"cancelled": True}
    return {"cancelled": False}


@app.get("/onboard/status")
def onboard_status(request: Request, session_id: int):
    require_auth(request)
    db = Session()
    row = db.query(Onboard).filter_by(id=session_id).first()
    db.close()
    if not row:
        raise HTTPException(404, "Unknown session.")
    return {"session_id": row.id, "state": row.state,
            "message": row.message, "email": row.email,
            "reason": row.reason or "", "history": row.history or ""}


@app.get("/onboard/sessions")
def onboard_sessions(request: Request, limit: int = 20):
    require_auth(request)
    db = Session()
    rows = db.query(Onboard).order_by(Onboard.id.desc()).limit(limit).all()
    names = {a.id: a.name for a in db.query(Account).all()}
    out = [{"session_id": r.id, "who": names.get(r.account_id) or "?",
            "email": r.email, "state": r.state, "message": r.message,
            "history": r.history or "",
            "at": local_str(r.at) if r.at else ""}
           for r in rows]
    db.close()
    return out


