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

import urllib.request
import urllib.parse
import urllib.error
import base64 as _b64
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, HTMLResponse
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
    __tablename__ = "connections"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"))
    provider = Column(String(30), default="google")
    email = Column(String(200))
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


Base.metadata.create_all(engine)

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


def gmail_client(account_id: int):
    """Returns an authorised Gmail client for this account."""
    db = Session()
    conn = (db.query(Connection)
              .filter_by(account_id=account_id, provider="google")
              .first())
    db.close()
    if not conn:
        raise HTTPException(400, "This account has no Gmail linked yet.")

    tok = vault_get(conn.secret_blob)
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

def tool_unread_summary(account_id: int, limit: int = 5) -> dict:
    svc = gmail_client(account_id)
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


def tool_read_email(account_id: int, msg_id: str) -> dict:
    svc = gmail_client(account_id)
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


def tool_send_email(account_id: int, to: str, subject: str, body: str) -> dict:
    svc = gmail_client(account_id)
    msg = MIMEText(body)
    msg["to"] = to
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    sent = svc.users().messages().send(
        userId="me", body={"raw": raw}).execute()
    return {"sent": True, "id": sent.get("id")}


def tool_search_email(account_id: int, query: str, limit: int = 5) -> dict:
    """Search the whole mailbox, not just unread."""
    svc = gmail_client(account_id)
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


def tool_find_contact(account_id: int, name: str) -> dict:
    """Find someone's email address from past messages, by name or partial."""
    svc = gmail_client(account_id)
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



def google_client(account_id: int, api: str, version: str):
    """Same credentials, different Google API."""
    db = Session()
    conn = (db.query(Connection)
              .filter_by(account_id=account_id, provider="google").first())
    db.close()
    if not conn:
        raise HTTPException(400, "This account has no Google account linked.")
    tok = vault_get(conn.secret_blob)
    creds = Credentials(
        token=tok.get("token"),
        refresh_token=tok.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )
    return build(api, version, credentials=creds, cache_discovery=False)


def _cal(account_id: int):
    return google_client(account_id, "calendar", "v3")


def tool_list_events(account_id: int, days: int = 1) -> dict:
    """Upcoming events over the next N days."""
    svc = _cal(account_id)
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
and wait for a yes."""


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
def create_account(a: NewAccount):
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
def list_accounts():
    db = Session()
    rows = []
    for acct in db.query(Account).all():
        nums = [p.number for p in
                db.query(PhoneNumber).filter_by(account_id=acct.id).all()]
        conn = (db.query(Connection)
                  .filter_by(account_id=acct.id, provider="google").first())
        rows.append({
            "account_id": acct.id,
            "name": acct.name,
            "phones": nums,
            "gmail": conn.email if conn else None,
        })
    db.close()
    return rows


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
    conn = (db.query(Connection)
              .filter_by(account_id=account_id, provider="google").first())
    if conn:
        conn.secret_blob = blob
        conn.email = email
        conn.linked_at = datetime.utcnow()
    else:
        db.add(Connection(account_id=account_id, provider="google",
                          email=email, secret_blob=blob))
    db.commit()
    db.close()

    return HTMLResponse(
        f"<h2>Linked{' — ' + email if email else ''}</h2>"
        f"<p>Account {account_id} is connected. You can close this window.</p>")


@app.get("/test/unread")
def test_unread(account_id: int, limit: int = 5):
    return tool_unread_summary(account_id, limit)


@app.get("/test/read")
def test_read(account_id: int, msg_id: str):
    return tool_read_email(account_id, msg_id)


@app.get("/test/search")
def test_search(account_id: int, q: str, limit: int = 5):
    return tool_search_email(account_id, q, limit)


@app.get("/test/contact")
def test_contact(account_id: int, name: str):
    return tool_find_contact(account_id, name)


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
def memory_get(account_id: int, limit: int = 20):
    return mem_recent(account_id, limit)


class MemBody(BaseModel):
    account_id: int
    channel: str = "voice"
    who: str = "user"
    text: str = ""


@app.post("/memory")
def memory_add(m: MemBody):
    mem_add(m.account_id, m.channel, m.who, m.text)
    return {"ok": True}


class CallStart(BaseModel):
    account_id: int | None = None
    from_number: str = ""
    room: str = ""


@app.post("/calls/start")
def call_start(c: CallStart):
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
def call_turn(t: TurnBody):
    db = Session()
    db.add(CallTurn(call_id=t.call_id, who=t.who, text=t.text[:4000],
                    tool=t.tool, latency_ms=t.latency_ms))
    db.commit()
    db.close()
    return {"ok": True}


@app.post("/calls/end")
def call_end(call_id: int, verified: int = 0):
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
def calls_list(limit: int = 50):
    db = Session()
    rows = (db.query(Call).order_by(Call.id.desc()).limit(limit).all())
    names = {a.id: a.name for a in db.query(Account).all()}
    out = [{
        "call_id": r.id,
        "who": names.get(r.account_id) or "unknown",
        "from": r.from_number,
        "started": r.started_at.strftime("%b %-d %-I:%M %p")
                   if r.started_at else "",
        "seconds": r.duration_sec,
        "verified": bool(r.verified),
    } for r in rows]
    db.close()
    return out


@app.get("/calls/{call_id}")
def call_detail(call_id: int):
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
def sms_send(s: SmsBody):
    return tool_send_sms(s.to, s.message)


@app.post("/sms/link")
def sms_link(account_id: int, to: str = ""):
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
def web_search(q: str, near: str = ""):
    return tool_web_search(q, near)


@app.get("/cal/events")
def cal_events(account_id: int, days: int = 1):
    return tool_list_events(account_id, days)


@app.get("/cal/free")
def cal_free(account_id: int, date: str, minutes: int = 60):
    return tool_find_free(account_id, date, minutes)


class NewEvent(BaseModel):
    account_id: int
    title: str
    start_iso: str
    minutes: int = 60
    location: str = ""
    notes: str = ""


@app.post("/cal/create")
def cal_create(e: NewEvent):
    return tool_create_event(e.account_id, e.title, e.start_iso,
                             e.minutes, e.location, e.notes)


@app.post("/cal/cancel")
def cal_cancel(account_id: int, event_id: str):
    return tool_cancel_event(account_id, event_id)


class SendBody(BaseModel):
    account_id: int
    to: str
    subject: str
    body: str


@app.post("/test/send")
def test_send(s: SendBody):
    return tool_send_email(s.account_id, s.to, s.subject, s.body)


# ----------------------------------------------------------------- admin

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

ADMIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Phone Assistant — Admin</title>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;
      color:#e6e6e6;margin:0;padding:24px;}
 h1{font-size:20px;margin:0 0 20px;}
 .card{background:#171a21;border:1px solid #262b36;border-radius:10px;
       padding:18px;margin-bottom:18px;max-width:900px;}
 label{display:block;font-size:12px;color:#8b94a7;margin:10px 0 4px;}
 input{width:100%;padding:9px 10px;background:#0f1115;border:1px solid #2c3240;
       border-radius:6px;color:#e6e6e6;font-size:14px;box-sizing:border-box;}
 button{margin-top:14px;padding:9px 16px;background:#3b82f6;border:0;
        border-radius:6px;color:#fff;font-size:14px;cursor:pointer;}
 button.sec{background:#2c3240;}
 table{width:100%;border-collapse:collapse;margin-top:8px;font-size:14px;}
 th{text-align:left;color:#8b94a7;font-weight:500;font-size:12px;
    padding:8px 6px;border-bottom:1px solid #262b36;}
 td{padding:10px 6px;border-bottom:1px solid #1c212b;}
 .ok{color:#4ade80;} .no{color:#f87171;}
 a.btn{display:inline-block;padding:6px 12px;background:#2c3240;color:#e6e6e6;
       border-radius:6px;text-decoration:none;font-size:13px;}
 .msg{margin-top:10px;font-size:13px;color:#8b94a7;}
</style></head><body>
<h1>Phone Assistant — Admin</h1>

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
  <b>Recent calls</b>
  <table><thead><tr><th>#</th><th>Who</th><th>From</th><th>When</th>
  <th>Length</th><th>PIN</th><th></th></tr></thead>
  <tbody id="calls"></tbody></table>
  <pre id="tx" style="white-space:pre-wrap;background:#0f1115;padding:12px;
    border-radius:6px;margin-top:12px;display:none;font-size:13px;
    max-height:400px;overflow:auto"></pre>
</div>

<div class="card">
  <b>Customers</b>
  <table><thead><tr><th>ID</th><th>Name</th><th>Phone</th>
  <th>Gmail</th><th></th></tr></thead><tbody id="rows"></tbody></table>
</div>

<script>
const q = new URLSearchParams(location.search).get('key') || '';
async function load(){
  const r = await fetch('/accounts');
  const d = await r.json();
  document.getElementById('rows').innerHTML = d.map(a =>
    `<tr><td>${a.account_id}</td><td>${a.name||''}</td>
     <td>${(a.phones||[]).join(', ')}</td>
     <td>${a.gmail ? '<span class=ok>'+a.gmail+'</span>'
                   : '<span class=no>not linked</span>'}</td>
     <td><a class="btn" target="_blank"
        href="/link/start?account_id=${a.account_id}">Link Gmail</a>
      <button class="sec" style="margin:0 0 0 6px;padding:6px 12px"
        onclick="copyLink(${a.account_id})">Copy link</button>
      <button class="sec" style="margin:0 0 0 6px;padding:6px 12px"
        onclick="textLink(${a.account_id})">Text link</button></td></tr>`
  ).join('') || '<tr><td colspan=5 style="color:#8b94a7">None yet.</td></tr>';
}
function copyLink(id){
  const url = location.origin + '/link/start?account_id=' + id;
  navigator.clipboard.writeText(url);
  document.getElementById('msg').textContent =
    'Link copied — text it to the customer: ' + url;
}
async function textLink(id){
  const m = document.getElementById('msg');
  m.textContent = 'Sending...';
  const r = await fetch('/sms/link?account_id=' + id, {method:'POST'});
  const d = await r.json().catch(()=>({}));
  m.textContent = d.sent ? 'Text sent.'
                         : ('Not sent: ' + (d.error || 'check SMS settings'));
}
async function add(){
  const body = {name:document.getElementById('n').value,
                phone:document.getElementById('p').value,
                pin:document.getElementById('k').value};
  const r = await fetch('/accounts',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const m = document.getElementById('msg');
  if(r.ok){ m.textContent='Created. Now click Link Gmail on their row.';
            document.getElementById('n').value='';
            document.getElementById('p').value=''; load(); }
  else { m.textContent='Failed — that phone number may already exist.'; }
}
async function loadCalls(){
  const r = await fetch('/calls?limit=25');
  const d = await r.json();
  document.getElementById('calls').innerHTML = d.map(c =>
    `<tr><td>${c.call_id}</td><td>${c.who}</td><td>${c['from']||''}</td>
     <td>${c.started}</td><td>${c.seconds}s</td>
     <td>${c.verified?'<span class=ok>ok</span>':'<span class=no>no</span>'}</td>
     <td><button class="sec" style="margin:0;padding:6px 12px"
        onclick="showTx(${c.call_id})">Transcript</button></td></tr>`
  ).join('') || '<tr><td colspan=7 style="color:#8b94a7">No calls yet.</td></tr>';
}
async function showTx(id){
  const r = await fetch('/calls/' + id);
  const d = await r.json();
  const el = document.getElementById('tx');
  el.style.display = 'block';
  el.textContent = d.length
    ? d.map(t => `[${t.at}] ${t.who}${t.tool?' ('+t.tool+')':''}` +
        `${t.latency_ms?' '+t.latency_ms+'ms':''}: ${t.text}`).join('\n')
    : 'No turns recorded for this call.';
}
load(); loadCalls();
setInterval(loadCalls, 15000);
</script></body></html>"""


@app.get("/admin", response_class=HTMLResponse)
def admin(key: str = ""):
    if key != ADMIN_PASSWORD:
        return HTMLResponse(
            "<body style='font-family:sans-serif;padding:40px'>"
            "<h3>Add ?key=YOUR_ADMIN_PASSWORD to the URL.</h3></body>",
            status_code=401)
    return HTMLResponse(ADMIN_HTML)
