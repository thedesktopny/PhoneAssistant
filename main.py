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
    at = Column(DateTime, default=datetime.utcnow)


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
    }
    with engine.begin() as c:
        for table, cols in wanted.items():
            for name, decl in cols:
                try:
                    c.execute(_sql(
                        f"ALTER TABLE {table} ADD COLUMN {name} {decl}"))
                except Exception:
                    pass          # already there


_ensure_columns()

# ----------------------------------------------------------------- vault
# Swap the two functions below for AWS KMS before real customers.
# Everything else in the app stays the same.

def vault_put(data: dict) -> str:
    return fernet.encrypt(json.dumps(data).encode()).decode()


def vault_get(blob: str) -> dict:
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

    for model in (Memory, Onboard, PhoneNumber):
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
def list_accounts(request: Request):
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
def calls_list(request: Request, limit: int = 50):
    require_auth(request)
    db = Session()
    rows = db.query(Call).order_by(Call.id.desc()).limit(limit).all()
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
                    "did not go through", "i'm sorry", "not allowed")):
                problems.append((t.text or "")[:120])
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
            "problems": problems[:3],
        })
    return out


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
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;
      color:#e6e6e6;margin:0;padding:24px;}
 h1{font-size:20px;margin:0 0 6px;}
 .sub{color:#8b94a7;font-size:13px;margin-bottom:20px;}
 .card{background:#171a21;border:1px solid #262b36;border-radius:10px;
       padding:18px;margin-bottom:18px;max-width:1000px;}
 .card b{font-size:15px;}
 label{display:block;font-size:12px;color:#8b94a7;margin:10px 0 4px;}
 input{width:100%;padding:9px 10px;background:#0f1115;border:1px solid #2c3240;
       border-radius:6px;color:#e6e6e6;font-size:14px;box-sizing:border-box;}
 button{margin-top:14px;padding:9px 16px;background:#3b82f6;border:0;
        border-radius:6px;color:#fff;font-size:14px;cursor:pointer;}
 button.sec{background:#2c3240;margin:0 0 0 6px;padding:6px 12px;}
 table{width:100%;border-collapse:collapse;margin-top:10px;font-size:14px;}
 th{text-align:left;color:#8b94a7;font-weight:500;font-size:12px;
    padding:8px 6px;border-bottom:1px solid #262b36;}
 td{padding:10px 6px;border-bottom:1px solid #1c212b;vertical-align:top;}
 .ok{color:#4ade80;} .no{color:#f87171;} .warn{color:#fbbf24;}
 .tag{display:inline-block;background:#232936;border-radius:4px;
      padding:2px 7px;margin:2px 3px 2px 0;font-size:12px;color:#c3cad8;}
 .prob{color:#f87171;font-size:12px;display:block;margin-top:4px;}
 a.btn{display:inline-block;padding:6px 12px;background:#2c3240;color:#e6e6e6;
       border-radius:6px;text-decoration:none;font-size:13px;}
 .msg{margin-top:10px;font-size:13px;color:#8b94a7;}
 .nums{display:flex;gap:26px;flex-wrap:wrap;margin-top:12px;}
 .num b{display:block;font-size:24px;color:#fff;}
 .num span{font-size:12px;color:#8b94a7;}
 pre{white-space:pre-wrap;background:#0f1115;padding:14px;border-radius:6px;
     margin-top:12px;display:none;font-size:13px;max-height:420px;
     overflow:auto;line-height:1.6;}
</style></head><body>
<h1>Phone Assistant
  <button class="sec" style="float:right;margin:0"
    onclick="fetch('/admin/logout',{method:'POST'}).then(()=>location.reload())">
    Sign out</button></h1>
<div class="sub" id="clock"></div>

<div class="card">
  <b>Last 7 days</b>
  <div class="nums" id="stats"><span style="color:#8b94a7">Loading…</span></div>
</div>

<div class="card">
  <b>Recent calls</b>
  <button class="sec" onclick="loadCalls()">Refresh</button>
  <table><thead><tr><th>#</th><th>Who</th><th>When</th><th>Length</th>
  <th>PIN</th><th>What they wanted</th><th></th></tr></thead>
  <tbody id="calls"><tr><td colspan="7" style="color:#8b94a7">Loading…</td></tr>
  </tbody></table>
  <pre id="tx"></pre>
</div>

<div class="card">
  <b>Text delivery</b>
  <button class="sec" onclick="loadDlr()">Refresh</button>
  <table><thead><tr><th>When</th><th>To</th><th>Status</th>
  <th>Detail</th></tr></thead>
  <tbody id="dlr"><tr><td colspan="4" style="color:#8b94a7">
  Nothing yet.</td></tr></tbody></table>
</div>

<div class="card">
  <b>Add a customer</b>
  <label>Name</label><input id="n" placeholder="Chaim Weiss">
  <label>Their phone number (the one they'll call from)</label>
  <input id="p" placeholder="+18455551234">
  <label>PIN</label><input id="k" value="1234">
  <button onclick="add()">Create</button>
  <div class="msg" id="msg"></div>
</div>

<div class="card">
  <b>Connect a customer's Gmail for them</b>
  <div style="color:#8b94a7;font-size:13px;margin-top:6px">
    For customers with no internet. Take their email and password on the
    phone, type them here. The password is used once and never saved.
  </div>
  <label>Customer ID</label><input id="ob_id" placeholder="2">
  <label>Their Gmail address</label>
  <input id="ob_email" placeholder="name@gmail.com">
  <label>Their Google password</label>
  <input id="ob_pw" type="password">
  <button onclick="obStart()">Start sign-in</button>
  <div class="msg" id="ob_msg"></div>
  <div id="ob_code" style="display:none;margin-top:12px">
    <label>Google sent them a code &mdash; type it here</label>
    <input id="ob_codeval" placeholder="123456">
    <button onclick="obCode()">Submit code</button>
  </div>
</div>

<div class="card">
  <b>Customers</b>
  <table><thead><tr><th>ID</th><th>Name</th><th>Phone</th>
  <th>Gmail</th><th></th></tr></thead>
  <tbody id="rows"><tr><td colspan="5" style="color:#8b94a7">Loading…</td></tr>
  </tbody></table>
</div>

<script>
function esc(x){ return String(x==null?'':x)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

async function loadStats(){
  try{
    const d = await (await fetch('/stats?days=7')).json();
    const tasks = (d.top_tasks||[]).map(t =>
      '<span class="tag">'+esc(t[0])+' '+t[1]+'</span>').join('') || '&mdash;';
    document.getElementById('stats').innerHTML =
      '<div class="num"><b>'+d.calls+'</b><span>calls</span></div>'+
      '<div class="num"><b>'+d.verified+'</b><span>passed PIN</span></div>'+
      '<div class="num"><b>'+d.avg_seconds+'s</b><span>avg length</span></div>'+
      '<div class="num"><b>'+d.avg_tool_ms+'ms</b><span>avg lookup</span></div>'+
      '<div class="num" style="flex:1"><span>most asked for</span>'+
      '<div style="margin-top:6px">'+tasks+'</div></div>';
  }catch(e){
    document.getElementById('stats').innerHTML =
      '<span class="no">Could not load stats.</span>';
  }
}

async function loadCalls(){
  const tb = document.getElementById('calls');
  try{
    const d = await (await fetch('/calls?limit=25')).json();
    if(!d.length){
      tb.innerHTML = '<tr><td colspan="7" style="color:#8b94a7">'+
        'No calls yet.</td></tr>'; return;
    }
    tb.innerHTML = d.map(function(c){
      var tasks = (c.tasks||[]).map(function(t){
        return '<span class="tag">'+esc(t)+'</span>'; }).join('');
      if(!tasks) tasks = '<span style="color:#8b94a7">just talking</span>';
      var probs = (c.problems||[]).map(function(p){
        return '<span class="prob">stuck: '+esc(p)+'</span>'; }).join('');
      var slow = c.slowest_ms > 2500
        ? '<span class="warn"> slow '+c.slowest_ms+'ms</span>' : '';
      return '<tr><td>'+c.call_id+'</td><td>'+esc(c.who)+'<br>'+
        '<span style="color:#8b94a7;font-size:12px">'+esc(c.from)+'</span></td>'+
        '<td>'+esc(c.started)+'</td><td>'+c.seconds+'s<br>'+
        '<span style="color:#8b94a7;font-size:12px">'+c.turns+' turns</span></td>'+
        '<td>'+(c.verified?'<span class="ok">ok</span>':'<span class="no">no</span>')+
        '</td><td>'+tasks+slow+probs+'</td>'+
        '<td><button class="sec" onclick="showTx('+c.call_id+')">'+
        'Transcript</button></td></tr>';
    }).join('');
  }catch(e){
    tb.innerHTML = '<tr><td colspan="7" class="no">Error loading calls: '+
      esc(e.message)+'</td></tr>';
  }
}

async function showTx(id){
  const el = document.getElementById('tx');
  el.style.display = 'block';
  el.textContent = 'Loading…';
  try{
    const d = await (await fetch('/calls/'+id)).json();
    el.textContent = d.length
      ? d.map(function(t){
          return '['+t.at+'] '+t.who+(t.tool?' ('+t.tool+')':'')+
                 (t.latency_ms?' '+t.latency_ms+'ms':'')+': '+t.text;
        }).join(String.fromCharCode(10))
      : 'No transcript recorded for this call.';
  }catch(e){ el.textContent = 'Could not load transcript.'; }
}

async function load(){
  const tb = document.getElementById('rows');
  try{
    const d = await (await fetch('/accounts')).json();
    if(!d.length){
      tb.innerHTML = '<tr><td colspan="5" style="color:#8b94a7">'+
        'No customers yet.</td></tr>'; return;
    }
    tb.innerHTML = d.map(function(a){
      return '<tr><td>'+a.account_id+'</td><td>'+esc(a.name)+'</td>'+
        '<td>'+esc((a.phones||[]).join(', '))+'</td>'+
        '<td>'+mboxes(a)+'</td>'+
        '<td><a class="btn" target="_blank" href="/link/start?account_id='+
        a.account_id+'">Link Gmail</a>'+
        '<button class="sec" onclick="copyLink('+a.account_id+')">Copy</button>'+
        '<button class="sec" onclick="textLink('+a.account_id+')">Text</button>'+
        '</td></tr>';
    }).join('');
  }catch(e){
    tb.innerHTML = '<tr><td colspan="5" class="no">Error: '+
      esc(e.message)+'</td></tr>';
  }
}

function mboxes(a){
  var m = a.mailboxes || [];
  if(!m.length) return '<span class="no">not linked</span>';
  return m.map(function(b){
    return '<div style="margin-bottom:4px">'+
      '<span class="ok">'+esc(b.email)+'</span>'+
      (b.label?' <span class="tag">'+esc(b.label)+'</span>':'')+
      (b.default?' <span class="tag">main</span>':'')+
      ' <span style="color:#8b94a7;font-size:11px">'+b.used+' uses</span>'+
      ' <a href="#" style="font-size:11px;color:#8b94a7" '+
      'onclick="labelBox('+b.id+');return false">rename</a>'+
      (b.default?'':' <a href="#" style="font-size:11px;color:#8b94a7" '+
      'onclick="defaultBox('+b.id+');return false">make main</a>')+
      '</div>';
  }).join('');
}
async function labelBox(id){
  var l = prompt('Short name for this mailbox (work, personal, shul):');
  if(l === null) return;
  await fetch('/mailboxes/label?connection_id='+id+
              '&label='+encodeURIComponent(l), {method:'POST'});
  load();
}
async function defaultBox(id){
  await fetch('/mailboxes/label?connection_id='+id+'&make_default=1',
              {method:'POST'});
  load();
}
function copyLink(id){
  const url = location.origin + '/link/start?account_id=' + id;
  navigator.clipboard.writeText(url);
  document.getElementById('msg').textContent = 'Link copied: ' + url;
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

var obSid = null, obTimer = null;
async function obStart(){
  const m = document.getElementById('ob_msg');
  m.textContent = 'Starting…';
  document.getElementById('ob_code').style.display = 'none';
  try{
    const r = await fetch('/onboard/start', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        account_id: parseInt(document.getElementById('ob_id').value),
        email: document.getElementById('ob_email').value,
        password: document.getElementById('ob_pw').value})});
    const d = await r.json();
    if(!r.ok){ m.textContent = d.detail || 'Could not start.'; return; }
    obSid = d.session_id;
    document.getElementById('ob_pw').value = '';
    if(obTimer) clearInterval(obTimer);
    obTimer = setInterval(obPoll, 3000);
    obPoll();
  }catch(e){ m.textContent = 'Error: ' + e.message; }
}
async function obPoll(){
  if(!obSid) return;
  try{
    const d = await (await fetch('/onboard/status?session_id='+obSid)).json();
    document.getElementById('ob_msg').textContent =
      d.state + (d.message ? ' — ' + d.message : '');
    document.getElementById('ob_code').style.display =
      (d.state === 'needs_code') ? 'block' : 'none';
    if(d.state === 'done' || d.state === 'failed'){
      clearInterval(obTimer); obTimer = null; load();
    }
  }catch(e){}
}
async function obCode(){
  await fetch('/onboard/code', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({session_id: obSid,
      code: document.getElementById('ob_codeval').value})});
  document.getElementById('ob_codeval').value = '';
  document.getElementById('ob_msg').textContent = 'Code submitted…';
}
async function add(){
  const body = {name:document.getElementById('n').value,
                phone:document.getElementById('p').value,
                pin:document.getElementById('k').value};
  const m = document.getElementById('msg');
  const r = await fetch('/accounts',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(r.ok){ m.textContent='Created. Now click Link Gmail or Text on their row.';
            document.getElementById('n').value='';
            document.getElementById('p').value=''; load(); }
  else { m.textContent='Failed &mdash; that phone number may already exist.'; }
}

document.getElementById('clock').textContent =
  'Updated ' + new Date().toLocaleTimeString();
async function loadDlr(){
  const tb = document.getElementById('dlr');
  try{
    const d = await (await fetch('/sms/dlr?limit=20')).json();
    if(!d.length){ tb.innerHTML='<tr><td colspan="4" style="color:#8b94a7">'+
      'No delivery receipts yet.</td></tr>'; return; }
    tb.innerHTML = d.map(function(x){
      var good = /deliver|success|ok/i.test(x.status||'');
      return '<tr><td>'+esc(x.at)+'</td><td>'+esc(x.to)+'</td>'+
        '<td class="'+(good?'ok':'no')+'">'+esc(x.status||'?')+'</td>'+
        '<td style="font-size:12px;color:#8b94a7">'+esc((x.raw||'').slice(0,180))+
        '</td></tr>';
    }).join('');
  }catch(e){ tb.innerHTML='<tr><td colspan="4" class="no">'+esc(e.message)+
    '</td></tr>'; }
}
load(); loadCalls(); loadStats(); loadDlr();
setInterval(function(){ loadCalls(); loadStats(); loadDlr(); }, 20000);
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
            "message": row.message, "email": row.email}


