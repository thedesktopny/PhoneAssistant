"""
Phone Assistant — Step 1: Gmail over API, no voice yet.

Endpoints:
  GET  /                      health check
  POST /accounts              create an account  {"name": "...", "phone": "+1..."}
  GET  /accounts              list accounts
  GET  /link/start?account_id=1     -> open in browser, links a Gmail account
  GET  /link/callback               (Google redirects here, don't call it yourself)
  GET  /test/unread?account_id=1    -> what the voice agent will read out
  GET  /test/read?account_id=1&msg_id=...
  POST /test/send             {"account_id":1,"to":"...","subject":"...","body":"..."}
"""

import os
import base64
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

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

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
    history = Column(Text, default="")
    at = Column(DateTime, default=datetime.utcnow)
    done_at = Column(DateTime, nullable=True)


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
        scopes=SCOPES,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)

# ----------------------------------------------------------------- tools
# These are the functions the voice agent will call later.

def tool_unread_summary(account_id: int, limit: int = 5, which: str = "") -> dict:
    svc = gmail_client(account_id, which)
    res = svc.users().messages().list(
        userId="me", q="is:unread in:inbox", maxResults=limit).execute()
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
            "snippet": m.get("snippet", "")[:200],
        })

    total = res.get("resultSizeEstimate", len(items))
    return {"unread_count": total, "messages": items}


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
        "body": body[:4000],
    }


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


def tool_search_email(account_id: int, query: str, limit: int = 5, which: str = "") -> dict:
    """Search the whole mailbox, not just unread."""
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
            "date": h.get("Date", ""),
            "snippet": d.get("snippet", "")[:200],
        })
    return {"found": len(items), "messages": items}


def tool_find_contact(account_id: int, name: str, which: str = "") -> dict:
    """Find someone's email address from past messages, by name or partial."""
    svc = gmail_client(account_id, which)
    seen = {}
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
        scopes=SCOPES,
    )
    return build(api, version, credentials=creds, cache_discovery=False)


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
    "idolatry", "idol worship", "halacha", "halachot", "jewish law",
    "gossip", "celebrity", "gossip column",
    "addiction", "drugs", "rehab",
    "joke", "jokes", "humor", "funny",
    "news", "headlines", "sports", "score", "game", "movie", "movies",
    "netflix", "tv show", "music video", "entertainment",
}


def is_blocked(text: str) -> bool:
    t = " " + (text or "").lower().replace("-", " ") + " "
    for term in BLOCKED_TERMS:
        if f" {term} " in t or t.strip() == term:
            return True
    return False


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

    combined = out.get("answer", "") + " " + " ".join(
        r.get("snippet", "") for r in out.get("results", []))
    if is_blocked(combined):
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
    url = f"{PUBLIC_URL}/link/start?account_id={account_id}"
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
             "at": r.at.strftime("%b %-d %-I:%M %p") if r.at else ""}
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




# --------------------------------------------------- assisted Gmail sign-in
# The customer's Google password lives in memory for the length of one
# sign-in and is never written to the database or logged.

_PENDING = {}          # session_id -> {"password": str, "code": str|None}


def _ob_set(sid: int, state: str, message: str = ""):
    db = Session()
    row = db.query(Onboard).filter_by(id=sid).first()
    if row:
        row.state = state
        row.message = message[:500]
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

    ws = (f"wss://connect.browserbase.com?apiKey={BROWSERBASE_API_KEY}"
          f"&projectId={BROWSERBASE_PROJECT_ID}")

    EMAIL_SEL = ('input[type="email"], input#identifierId, '
                 'input[name="identifier"]')
    PW_SEL = ('input[type="password"], input[name="Passwd"], '
              'input[name="password"]')
    CODE_SEL = ('input[type="tel"], input[name="totpPin"], input#idvPin, '
                'input[name="Pin"], input[name="code"], '
                'input[autocomplete="one-time-code"], '
                'input[aria-label*="code" i]')

    def screen(page):
        try:
            return (page.inner_text("body") or "").replace("\n", " ")
        except Exception:
            return ""

    def where(page):
        return f"url={page.url[:110]} | screen: {screen(page)[:260]}"

    def wants_tap(page):
        return page.query_selector(
            'text=/Check your device|Tap Yes on the notification|'
            'Open the Gmail app|notification to your/i')

    def tap_number(page):
        body = screen(page)
        m = (_re.search(r"\b(\d{2})\b\s*Check your device", body)
             or _re.search(r"Check your device.{0,120}?\b(\d{2})\b", body)
             or _re.search(r"tap\s+(\d{2})\b", body, _re.I))
        return m.group(1) if m else ""

    def describe_code_screen(page):
        """Say where the code went, so the caller knows what to look for."""
        body = screen(page)
        m = _re.search(r"\(?\s*[•\*\u2022]{0,3}\s*(\d{2,4})\s*\)?\s*$", "")
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
            alt = (page.query_selector('text=/Try another way/i')
                   or page.query_selector('text=/More ways to verify/i')
                   or page.query_selector('text=/Try another method/i'))
            if not alt:
                return False
            alt.click()
            page.wait_for_timeout(3500)
            for sel in ('text=/Get a verification code at/i',
                        'text=/Text message/i',
                        'text=/Send code/i',
                        'text=/Get a code|verification code/i',
                        'text=/Phone call/i'):
                opt = page.query_selector(sel)
                if opt:
                    opt.click()
                    page.wait_for_timeout(4000)
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
                try:
                    page.fill(CODE_SEL, code, timeout=10000)
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(6000)
                except Exception:
                    pass
                if page.query_selector(
                        'text=/Wrong code|incorrect code|try again/i'):
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
            page.goto(f"{PUBLIC_URL}/link/start?account_id={account_id}",
                      wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)

            try:
                other = page.query_selector('text=/Use another account/i')
                if other:
                    other.click()
                    page.wait_for_timeout(2500)
            except Exception:
                pass

            try:
                page.wait_for_selector(EMAIL_SEL, timeout=30000)
            except Exception:
                _ob_set(sid, "failed", "No email box. " + where(page))
                browser.close()
                return

            page.fill(EMAIL_SEL, email)
            page.keyboard.press("Enter")
            page.wait_for_timeout(4000)

            try:
                page.wait_for_selector(PW_SEL, timeout=30000)
            except Exception:
                _ob_set(sid, "failed", "No password box. " + where(page))
                browser.close()
                return

            page.fill(PW_SEL, password)
            page.keyboard.press("Enter")
            page.wait_for_timeout(6000)

            if page.query_selector('text=/Wrong password/i'):
                _ob_set(sid, "failed",
                        "Google says the password is wrong. Ask them to say "
                        "it again slowly, or have someone call them back.")
                browser.close()
                return

            if page.query_selector(
                    'text=/couldn.t sign you in|browser or app may not be '
                    'secure|unusual activity/i'):
                _ob_set(sid, "failed",
                        "Google blocked the automated sign-in. " + where(page))
                browser.close()
                return

            # ---- verification, whichever form it takes, possibly twice
            for _round in range(3):
                if "/link/callback" in page.url:
                    break
                if wants_tap(page):
                    num = tap_number(page)
                    _ob_set(sid, "needs_tap",
                            (f"Google sent a prompt to their phone. They tap "
                             f"Yes and choose {num}." if num else
                             "Google sent a prompt to their phone. They tap "
                             "Yes on the notification."))
                    waited = 0
                    switched = False
                    while waited < 200:
                        time.sleep(4)
                        waited += 4
                        if (_PENDING.get(sid) or {}).get("other_way"):
                            _PENDING[sid]["other_way"] = False
                            if pick_another_method(page):
                                switched = True
                                break
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

                if page.query_selector(CODE_SEL):
                    res = wait_for_code(page, sid, describe_code_screen(page))
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
            for _ in range(8):
                page.wait_for_timeout(2500)
                if "/link/callback" in page.url or "Linked" in page.content():
                    break
                for sel in ('text=/^Advanced$/',
                            'text=/Go to .*unsafe/i',
                            'button:has-text("Continue")',
                            'button:has-text("Allow")',
                            'span:has-text("Continue")',
                            'div[role="button"]:has-text("Continue")'):
                    try:
                        el = page.query_selector(sel)
                        if el:
                            el.click()
                            page.wait_for_timeout(2000)
                    except Exception:
                        pass

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
                  SiteSession, Job):
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
    """Store or replace one site login."""
    db = Session()
    row = (db.query(SiteLogin)
             .filter_by(account_id=account_id, site=site.lower()).first())
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
            "saved": r.at.strftime("%b %-d") if r.at else "",
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


def _job_set(jid: int, state: str, message: str = ""):
    db = Session()
    row = db.query(Job).filter_by(id=jid).first()
    if row:
        row.state = state
        row.message = message[:500]
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


def _run_site_login(jid: int, account_id: int, site: str):
    """Log this customer into a site and keep the session for next time."""
    from playwright.sync_api import sync_playwright

    cfg = SITES.get(site.lower())
    if not cfg:
        _job_set(jid, "failed", f"No setup for '{site}' yet.")
        return

    creds = use_site_login(account_id, site, purpose=f"job {jid} login")
    if not creds or not creds.get("password"):
        _job_set(jid, "failed", "No saved login for that site.")
        return

    ctx_id = _get_context(account_id, site.lower()) or \
        _new_browserbase_context()

    ws = (f"wss://connect.browserbase.com?apiKey={BROWSERBASE_API_KEY}"
          f"&projectId={BROWSERBASE_PROJECT_ID}")
    if ctx_id:
        ws += f"&contextId={ctx_id}&persist=true"

    page = browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws)
            bctx = browser.contexts[0] if browser.contexts \
                else browser.new_context()
            page = bctx.pages[0] if bctx.pages else bctx.new_page()
            page.set_default_timeout(45000)

            _job_set(jid, "opening", f"Opening {site}.")
            page.goto(cfg["login_url"], wait_until="domcontentloaded",
                      timeout=60000)
            page.wait_for_timeout(4000)

            # Already signed in from a previous session?
            if page.query_selector(cfg["ok_sel"]):
                if ctx_id:
                    _save_context(account_id, site.lower(), ctx_id)
                _job_set(jid, "done", f"Already signed in to {site}.")
                browser.close()
                return

            _job_set(jid, "signing_in", "Entering their details.")
            try:
                page.fill(cfg["user_sel"], creds["username"], timeout=25000)
            except Exception:
                body = (page.inner_text("body") or "")[:220]
                _job_set(jid, "failed", f"No username box. {body}")
                browser.close()
                return
            try:
                nxt = page.query_selector(cfg["next_sel"])
                if nxt:
                    nxt.click()
                else:
                    page.keyboard.press("Enter")
            except Exception:
                page.keyboard.press("Enter")
            page.wait_for_timeout(4000)

            try:
                page.fill(cfg["pass_sel"], creds["password"], timeout=25000)
            except Exception:
                body = (page.inner_text("body") or "")[:220]
                _job_set(jid, "failed", f"No password box. {body}")
                browser.close()
                return
            try:
                nxt = page.query_selector(cfg["next_sel"])
                if nxt:
                    nxt.click()
                else:
                    page.keyboard.press("Enter")
            except Exception:
                page.keyboard.press("Enter")
            page.wait_for_timeout(7000)

            # One-time code?
            if page.query_selector(cfg["otp_sel"]):
                _job_set(jid, "needs_code",
                         f"{site.title()} sent them a code. Ask them to read "
                         f"it out.")
                waited = 0
                got = False
                while waited < 240:
                    time.sleep(3)
                    waited += 3
                    code = (_JOBS.get(jid) or {}).get("code")
                    if code:
                        _JOBS[jid]["code"] = None
                        try:
                            page.fill(cfg["otp_sel"], code, timeout=10000)
                            page.keyboard.press("Enter")
                            page.wait_for_timeout(7000)
                            got = True
                        except Exception:
                            pass
                        break
                if not got:
                    _job_set(jid, "failed", "Timed out waiting for the code.")
                    browser.close()
                    return

            page.wait_for_timeout(3000)
            if page.query_selector(cfg["ok_sel"]):
                if ctx_id:
                    _save_context(account_id, site.lower(), ctx_id)
                _job_set(jid, "done",
                         f"Signed in to {site} and saved the session.")
            else:
                body = (page.inner_text("body") or "")[:250].replace("\n", " ")
                _job_set(jid, "failed",
                         f"Sign-in didn't complete. screen: {body}")
            browser.close()
    except Exception as e:
        detail = ""
        try:
            if page:
                detail = " url=" + page.url[:100]
        except Exception:
            pass
        _job_set(jid, "failed", f"Browser error: {str(e)[:150]}{detail}")
        try:
            if browser:
                browser.close()
        except Exception:
            pass
    finally:
        _JOBS.pop(jid, None)


RUNNERS = {"site_login": _run_site_login}


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
    if live:
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


# ------------------------------------------------------- text brain (SMS)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

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

TOPICS YOU DO NOT DISCUSS: religious discussions, gossip, sex, adultery,
intimacy, explicit material, addiction, humor, culture, Jewish law, dating,
Halachot, underwear, nudity, fertility, idolatry, worship, puberty, marriage,
relationships, anything arousing, news, sports, entertainment, personal
feelings, jokes. Reply to any of these with exactly: "I am not allowed to
talk to you about this." Nothing more. Never explain the rules.
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


def _openai_chat(messages: list, tools=None) -> dict:
    payload = {"model": "gpt-4o-mini", "messages": messages}
    if tools:
        payload["tools"] = tools
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode())


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
            data = _openai_chat(msgs, TEXT_TOOLS)
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


@app.get("/")
def health():
    return {"ok": True, "service": "phone-assistant", "step": "gmail"}


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
def link_start(account_id: int):
    url, _ = _flow(state=str(account_id)).authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true")
    return RedirectResponse(url)


@app.get("/link/callback")
def link_callback(request: Request):
    state = request.query_params.get("state")
    if not state:
        raise HTTPException(400, "missing state")
    account_id = int(state)

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

    return HTMLResponse(
        f"<h2>Linked{' — ' + email if email else ''}</h2>"
        f"<p>Account {account_id} is connected. You can close this window.</p>")


@app.get("/test/unread")
def test_unread(request: Request, account_id: int, limit: int = 5,
                which: str = ""):
    require_auth(request)
    return tool_unread_summary(account_id, limit, which)


@app.get("/test/read")
def test_read(request: Request, account_id: int, msg_id: str,
              which: str = ""):
    require_auth(request)
    return tool_read_email(account_id, msg_id, which)


@app.get("/test/search")
def test_search(request: Request, account_id: int, q: str, limit: int = 5,
                which: str = ""):
    require_auth(request)
    return tool_search_email(account_id, q, limit, which)


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
    out = [{"at": r.at.strftime("%b %-d %-I:%M:%S %p") if r.at else "",
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
    db = Session()
    db.add(CallTurn(call_id=t.call_id, who=t.who, text=t.text[:4000],
                    tool=t.tool, latency_ms=t.latency_ms))
    db.commit()
    db.close()
    return {"ok": True}


@app.post("/calls/end")
def call_end(request: Request, call_id: int, verified: int = 0):
    require_auth(request)
    db = Session()
    row = db.query(Call).filter_by(id=call_id).first()
    if row:
        row.ended_at = datetime.utcnow()
        row.duration_sec = int(
            (row.ended_at - row.started_at).total_seconds())
        row.verified = verified
        db.commit()
    db.close()
    return {"ok": True}


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
            "started": r.started_at.strftime("%b %-d %-I:%M %p")
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
            "at": t.at.strftime("%-I:%M:%S %p") if t.at else ""}
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
    return save_site_login(b.account_id, b.site, b.username, b.password)


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
    out = [{"at": r.at.strftime("%b %-d %-I:%M %p") if r.at else "",
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


class JobCode(BaseModel):
    job_id: int
    code: str


@app.post("/jobs/code")
def job_code(b: JobCode, request: Request):
    require_auth(request)
    if b.job_id in _JOBS:
        _JOBS[b.job_id]["code"] = "".join(
            ch for ch in b.code if ch.isalnum())
        return {"ok": True}
    raise HTTPException(400, "That job is no longer running.")


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
            "history": row.history or ""}


@app.get("/jobs/health")
def jobs_health(request: Request):
    require_auth(request)
    return queue_health()


@app.get("/sessions")
def sessions_list(request: Request, limit: int = 100):
    """Which customers have a live session on which sites."""
    require_auth(request)
    db = Session()
    rows = db.query(SiteSession).order_by(SiteSession.id.desc()).limit(
        limit).all()
    names = {a.id: a.name for a in db.query(Account).all()}
    out = [{"who": names.get(r.account_id) or "?", "site": r.site,
            "last_ok": r.last_ok.strftime("%b %-d %-I:%M %p")
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
            "at": r.at.strftime("%b %-d %-I:%M %p") if r.at else ""}
           for r in rows]
    db.close()
    return out


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


class FollowupBody(BaseModel):
    account_id: int | None = None
    call_id: int | None = None
    reason: str = ""
    note: str = ""
    channel: str = "voice"


@app.post("/followups")
def followup_add(b: FollowupBody, request: Request):
    require_auth(request)
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
            "at": r.at.strftime("%b %-d %-I:%M %p") if r.at else ""}
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
    return tool_create_event(e.account_id, e.title, e.start_iso,
                             e.minutes, e.location, e.notes)


@app.post("/cal/cancel")
def cal_cancel(request: Request, account_id: int, event_id: str):
    require_auth(request)
    return tool_cancel_event(account_id, event_id)


class SendBody(BaseModel):
    account_id: int
    to: str
    subject: str
    body: str
    which: str = ""


@app.post("/test/send")
def test_send(s: SendBody, request: Request):
    require_auth(request)
    return tool_send_email(s.account_id, s.to, s.subject, s.body, s.which)


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
    <a data-p="overview" class="on">Overview</a>
    <a data-p="calls">Calls</a>
    <a data-p="customers">Customers</a>
    <a data-p="followups">To do</a>
    <a data-p="signins">Sign-ins</a>
    <a data-p="jobs">Site logins</a>
    <a data-p="texts">Texts</a>
  </nav>
  <button class="sec" onclick="fetch('/admin/logout',{method:'POST'})
    .then(()=>location.reload())">Sign out</button>
</header>
<main>

<section class="page on" id="p-overview">
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
        '<td><a class="btn" target="_blank" href="/link/start?account_id='+
        a.account_id+'">Link</a> '+
        '<button class="sec" onclick="copyLink('+a.account_id+')">Copy</button> '+
        '<button class="sec" onclick="textLink('+a.account_id+')">Text</button>'+
        '</td></tr>'; }).join('');
  }catch(e){
    tb.innerHTML='<tr><td colspan="5" class="no">'+esc(e.message)+'</td></tr>'; }
}

var fuAll = false;
async function loadFu(){
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
        '<td><span class="tag">'+esc(j.site)+'</span></td>'+
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
  const url = location.origin + '/link/start?account_id=' + id;
  navigator.clipboard.writeText(url);
  document.getElementById('msg').textContent = 'Copied: ' + url;
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

load(); loadCalls(); loadStats(); loadDlr(); loadOb(); loadFu(); loadJobs(); loadHealth();
setInterval(function(){ loadCalls(); loadStats(); loadDlr(); loadOb();
                        loadFu(); loadJobs(); loadHealth(); }, 25000);
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
            "history": row.history or ""}


@app.get("/onboard/sessions")
def onboard_sessions(request: Request, limit: int = 20):
    require_auth(request)
    db = Session()
    rows = db.query(Onboard).order_by(Onboard.id.desc()).limit(limit).all()
    names = {a.id: a.name for a in db.query(Account).all()}
    out = [{"session_id": r.id, "who": names.get(r.account_id) or "?",
            "email": r.email, "state": r.state, "message": r.message,
            "history": r.history or "",
            "at": r.at.strftime("%b %-d %-I:%M %p") if r.at else ""}
           for r in rows]
    db.close()
    return out


