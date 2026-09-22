"""
The foundations every other part of the backend stands on: what is
configured, where things are stored, and the few helpers that must behave
the same everywhere - the scrubber that keeps secrets out of storage, the
live log, the vault, and the clock that turns a stored UTC time into the
caller's own.

Nothing here knows about email, browsers, orders or the web app. Keep it
that way: this file is imported by everything, so anything that reaches
back the other way becomes a circular import.
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

# Which model does which job. Change these in Railway, not here: the
# browser and the advisor do the thinking, the other two are small
# helpers where an older model is cheaper and good enough.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
MODEL_BROWSER = os.environ.get("MODEL_BROWSER", "gpt-4o")
MODEL_SUMMARY = os.environ.get("MODEL_SUMMARY", "gpt-4o-mini")
MODEL_TEXT = os.environ.get("MODEL_TEXT", "gpt-4o-mini")
# Send the browser a picture of the page as well as its text. Set to 0 to
# go back to text only.
BROWSER_VISION = os.environ.get("BROWSER_VISION", "1") not in ("0", "false")
MODEL_ADVISOR = os.environ.get("MODEL_ADVISOR", MODEL_BROWSER)

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


