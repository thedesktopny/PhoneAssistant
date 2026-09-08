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
        onclick="copyLink(${a.account_id})">Copy link</button></td></tr>`
  ).join('') || '<tr><td colspan=5 style="color:#8b94a7">None yet.</td></tr>';
}
function copyLink(id){
  const url = location.origin + '/link/start?account_id=' + id;
  navigator.clipboard.writeText(url);
  document.getElementById('msg').textContent =
    'Link copied — text it to the customer: ' + url;
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
load();
</script></body></html>"""


@app.get("/admin", response_class=HTMLResponse)
def admin(key: str = ""):
    if key != ADMIN_PASSWORD:
        return HTMLResponse(
            "<body style='font-family:sans-serif;padding:40px'>"
            "<h3>Add ?key=YOUR_ADMIN_PASSWORD to the URL.</h3></body>",
            status_code=401)
    return HTMLResponse(ADMIN_HTML)
