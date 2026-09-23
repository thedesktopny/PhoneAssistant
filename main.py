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

from admin_page import (ADMIN_HTML)
from rules import (BLOCKED_REPLY, BLOCKED_TERMS, blocked_terms_in, is_blocked)
from ai import (ASK_SYSTEM)
from ai import (_openai_chat, _summarise_page)
from signals import (BLOCK_KINDS, BLOCK_MARKS, BLOCK_REASONS, BLOCK_VENDORS, BOT_CHECK_MARKS, CODE_BAD, CODE_DEST, SIGNED_IN_MARKS, SIGNED_OUT_MARKS, block_reason, classify_block, code_destination, looks_like_bot_check, looks_signed_in, looks_signed_out, record_block)
from payments import (CHARGE_LIMIT_CENTS, StripeError, StripeNeedsRawCardAccess, stripe_card_page_url, stripe_charge, stripe_customer_for, stripe_hold_card, stripe_save_finished)
from payments import (_card_brand, _luhn_ok, _stripe, _stripe_call)
from advisor import (ADVISOR_SYSTEM, PROFILE_SYSTEM, REVIEW_SYSTEM, WORKING_CLAIMS, advise, call_state, learn_about_caller, profile_for, review_call)
from google_tools import (CODE_DIGITS, CODE_MAIL, CONNECT_CODE_HOURS, CONNECT_MAX_FAILS, CONNECT_MAX_FAILS_IP, CONVERTIBLE, DOC, DRIVE_KINDS, DRIVE_MAX_BYTES, MESSAGE_ACTIONS, PERSON_FIELDS, SEND_MAX_BYTES, SHEET, SLIDES, code_from_email, document_text, gmail_client, google_client, list_mailboxes, pick_connection, tool_attachment_text, tool_attachments, tool_cancel_event, tool_contact_add, tool_contacts_search, tool_create_event, tool_doc_add, tool_doc_create, tool_doc_replace, tool_draft_email, tool_drive_editable_copy, tool_drive_read, tool_drive_save_pdf, tool_drive_search, tool_email_drive_file, tool_find_contact, tool_find_free, tool_forward_email, tool_list_events, tool_mark_all_read, tool_mark_read, tool_message_action, tool_read_email, tool_reply_email, tool_search_email, tool_send_email, tool_sheet_add_row, tool_sheet_create, tool_sheet_read, tool_sheet_update, tool_task_add, tool_task_done, tool_tasks_list, tool_unread_summary)
from google_tools import (_as_pdf, _cal, _category, _col_letters, _column_number, _connect_code, _connect_code_ok, _connect_too_many, _drive_kind, _drive_meta, _extract_body, _flow, _google_time, _headers_of, _made, _must_be, _person, _sheet_values, _spoken_date, _tab_range, _upload, _walk_parts)
from browser import (_order_set, BROWSE_SYSTEM, BUY_BUTTONS, CHECKOUT_SYSTEM, DIAL_MAP, DOING_GOAL, MAX_BROWSERS, NAV_NOISE, ORDER_PAGES, PROXY_STATUS, REFUSAL_HINT, SEARCH_PAGES, SITES, STUCK_LIMIT, US_AREA_STATE, claims_action, delete_everything, disconnect_mailbox, do_back, do_click, do_fill, do_goto, forget_site_login, list_site_logins, looks_like_pdf, page_answer, page_eval, page_shot, page_text, page_url, q, q_all, read_pdf, revoke_google, save_site_login, settle, signed_in, use_site_login, _browser_error)
import everyday
import signup
from browser import (site_url, _JOB_STARTED, _LAST_LIMIT_FLAG, _LAST_PROXY_FLAG, _PENDING, _SNAPSHOT_JS, _TASK_CACHE, _action_index, _action_sig, _agent_fallback, _as_placeholder, _bb_connect_url, _bb_session, _body_mark, _decide, _do_site_login, _find_recipe, _first_json, _flag_account_limit, _flag_proxy_fallback, _flag_proxy_unavailable, _forget_context, _get_context, _going_in_circles, _handle, _is_nav_error, _job_set, _match_element, _new_browserbase_context, _ob_set, _open_with_session, _page_snapshot, _queue_lock, _recipe_result, _recipe_value, _record_request, _replay_recipe, _run_browse, _run_checkout, _run_reset, _run_signin, _run_site_login, _run_site_orders, _run_site_search, _save_context, _save_recipe, _shape, _slots, _stuck_note, _task_label, _task_shape, _user_turn, _waiting, _where_for_account, _where_for_phone)
from search import (shopping_prices, tool_web_search)
from search import (_money, _search_serper, _search_tavily, _serper_shopping)
# The foundations: configuration, the database and its tables, the
# scrubber, the live log, the vault, and the clock helpers. Imported
# by name so every reference below reads exactly as it did when this
# was all one file.
from core import *                                    # noqa: F401,F403
from core import (
    _SECRET_PATTERNS, _SPELLED, _akv, _azure_client, _clock,
    _ensure_columns, _kms, _kms_client, _rate, _tz, _when, _JOBS, _make_link_token, _check_link_token, _fmt_address, _CONNECT_FAILS, LINK_LIFE_MIN)










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




RUNNERS = {"site_login": _run_site_login,
           "browse": _run_browse,
           "checkout": _run_checkout,
           "password_reset": _run_reset,
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


# ------------------------------------------------------------- sign-up
# The office makes an invite code; the person rings and signs themselves
# up by voice. See signup.py.

class InviteBody(BaseModel):
    note: str = ""
    days: int = 0


@app.post("/invites")
def invite_make(b: InviteBody, request: Request):
    """The code comes back once, here, and is never shown again."""
    require_auth(request)
    return signup.make_invite(b.note, b.days)


@app.get("/invites")
def invite_list(request: Request):
    require_auth(request)
    return signup.list_invites()


@app.post("/invites/cancel")
def invite_cancel(request: Request, invite_id: int):
    require_auth(request)
    return signup.cancel_invite(invite_id)


class SignupCheck(BaseModel):
    phone: str
    code: str


@app.post("/signup/check")
def signup_check(b: SignupCheck, request: Request):
    require_auth(request)
    return signup.check_invite(b.phone, b.code)


class SignupDone(BaseModel):
    phone: str
    code: str
    first_name: str
    last_name: str
    pin: str
    call_id: int = 0


@app.post("/signup/complete")
def signup_complete(b: SignupDone, request: Request):
    require_auth(request)
    return signup.complete_signup(b.phone, b.code, b.first_name,
                                  b.last_name, b.pin,
                                  call_id=b.call_id or None)


class LinkDone(BaseModel):
    phone: str
    code: str
    pin: str
    call_id: int = 0


@app.post("/signup/link")
def signup_link(b: LinkDone, request: Request):
    """Add the phone they are calling from to their account."""
    require_auth(request)
    return signup.complete_link(b.phone, b.code, b.pin,
                                call_id=b.call_id or None)


@app.post("/accounts/link_code")
def accounts_link_code(request: Request, account_id: int):
    """A one-time code to add another phone - asked for by voice from a
    phone already on the account."""
    require_auth(request)
    return signup.make_link_code(account_id)


class PhoneBody(BaseModel):
    account_id: int
    phone: str


@app.post("/accounts/phone")
def accounts_phone_add(b: PhoneBody, request: Request):
    require_auth(request)
    return signup.add_phone(b.account_id, b.phone)


@app.post("/accounts/phone/remove")
def accounts_phone_remove(b: PhoneBody, request: Request):
    require_auth(request)
    return signup.remove_phone(b.account_id, b.phone)


class NewAccount(BaseModel):
    name: str
    phone: str | None = None
    pin: str = ""


@app.post("/accounts")
def create_account(a: NewAccount, request: Request):
    require_auth(request)
    pin = "".join(ch for ch in (a.pin or "") if ch.isdigit())
    if not 4 <= len(pin) <= 6 or signup.weak_pin(pin):
        raise HTTPException(
            400, "Give them a PIN of 4 to 6 digits that isn't 1234 or one "
                 "digit repeated.")
    db = Session()
    acct = Account(name=a.name, pin=pin)
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


# ------------------------------------------------------------- everyday
# Weather, the Jewish calendar, yahrzeits and "what's my day" - see
# everyday.py. account_id is optional where it only supplies a place:
# the voice side sends it once the caller is verified.

@app.get("/everyday/weather")
def everyday_weather(request: Request, account_id: int = 0, place: str = "",
                     days: int = 1):
    require_auth(request)
    return everyday.weather(account_id or None, place, days)


@app.get("/everyday/jewish")
def everyday_jewish(request: Request, account_id: int = 0, what: str = "",
                    place: str = "", on: str = ""):
    require_auth(request)
    return everyday.jewish_calendar(account_id or None, what, place, on)


@app.get("/everyday/yahrzeit")
def everyday_yahrzeit(request: Request, died_on: str = "",
                      after_sunset: int = 0, hebrew: str = "",
                      years: int = 2):
    require_auth(request)
    try:
        return everyday.yahrzeit(died_on, bool(after_sunset), hebrew, years)
    except Exception as e:
        return {"reason": "unavailable",
                "message": f"The Jewish calendar service did not answer: "
                           f"{str(e)[:100]}"}


class MinhagBody(BaseModel):
    account_id: int
    candle_minutes: int = 0
    havdalah: str = ""
    shema: str = ""
    call_id: int = 0


@app.get("/everyday/minhag")
def everyday_minhag_get(request: Request, account_id: int):
    require_auth(request)
    return everyday.minhag_of(account_id)


@app.post("/everyday/minhag")
def everyday_minhag_set(b: MinhagBody, request: Request):
    """What they keep. Recorded under "What it did" like every other
    change, so the office can see it was the customer who said it."""
    require_auth(request)
    return everyday.set_minhag(b.account_id, b.candle_minutes, b.havdalah,
                               b.shema, call_id=b.call_id or None)


@app.get("/everyday/my_day")
def everyday_my_day(request: Request, account_id: int, which: str = ""):
    require_auth(request)
    return everyday.my_day(account_id, which)


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


@app.post("/privacy/blank_secrets")
def privacy_blank_secrets(request: Request, dry_run: int = 1):
    """Blank passwords and PINs that reached the records before the voice
    side stopped writing them down: the call log, their memory and the
    live log, each read in order with the same rule the voice side now
    uses (secrets_guard.py). dry_run=1 only counts. Running it twice
    changes nothing the second time."""
    require_auth(request)
    from secrets_guard import keep_or_blank, BLANKED
    db = Session()
    counts = {"call_turns": 0, "memory": 0, "live_log": 0}
    calls = {}

    # the call log, one call at a time
    for (cid,) in db.query(CallTurn.call_id).distinct().all():
        state = {"on": False, "turns": 0}
        for t in (db.query(CallTurn).filter_by(call_id=cid)
                  .order_by(CallTurn.id).all()):
            if t.who not in ("caller", "agent"):
                continue
            role = "user" if t.who == "caller" else "assistant"
            new = keep_or_blank(state, role, t.text or "")
            if new != (t.text or ""):
                counts["call_turns"] += 1
                calls[cid] = calls.get(cid, 0) + 1
                if not dry_run:
                    t.text = new

    # their memory, one customer at a time
    for (aid,) in db.query(Memory.account_id).distinct().all():
        state = {"on": False, "turns": 0}
        for m in (db.query(Memory).filter_by(account_id=aid, channel="voice")
                  .order_by(Memory.id).all()):
            role = "user" if m.who == "user" else "assistant"
            new = keep_or_blank(state, role, m.text or "")
            if new != (m.text or ""):
                counts["memory"] += 1
                if not dry_run:
                    m.text = new

    # the live log's copy of each line: "caller: ..." / "agent: ..."
    for (ref,) in (db.query(Event.ref).filter(Event.kind == "call")
                   .distinct().all()):
        state = {"on": False, "turns": 0}
        for e in (db.query(Event).filter_by(kind="call", ref=ref)
                  .order_by(Event.id).all()):
            text = e.text or ""
            for prefix, role in (("caller: ", "user"), ("agent: ", "assistant")):
                if text.startswith(prefix):
                    new = keep_or_blank(state, role, text[len(prefix):])
                    if new != text[len(prefix):]:
                        counts["live_log"] += 1
                        if not dry_run:
                            e.text = prefix + new
                    break

    if not dry_run:
        db.commit()
    db.close()
    if not dry_run and any(counts.values()):
        emit("privacy", "blank", f"blanked {counts['call_turns']} call-log "
             f"lines, {counts['memory']} memory lines and "
             f"{counts['live_log']} live-log lines where a password or PIN "
             f"was being given", "info")
    return {"dry_run": bool(dry_run), "blanked": counts,
            "calls_touched": len(calls),
            "most_in_one_call": max(calls.values()) if calls else 0}


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


@app.post("/jobs/password-reset")
def job_password_reset(request: Request, account_id: int, site: str,
                       email: str = "", call_id: int = 0,
                       check_only: int = 0):
    """Recover a forgotten password on a site.

    Any address or username on the account will do. The site only sends
    its code to the real owner, so it is the site, not this door, that
    stops a stranger: if the address is a mailbox they have connected we
    read the code ourselves, and if not, they read it out. check_only says
    which of the two it would be without starting anything."""
    require_auth(request)
    if is_blocked(site):
        raise HTTPException(400, BLOCKED_REPLY)
    connected = [(b.get("email") or "").lower()
                 for b in list_mailboxes(account_id)]
    want = (email or "").strip()
    if not want:
        saved = next((r for r in list_site_logins(account_id)
                      if r.get("site") == site.lower()), {})
        want = saved.get("username") or (connected[0] if connected else "")
    if not want:
        raise HTTPException(
            400, "We don't know the email address or username on that "
                 "account - ask them for it.")
    mode = ("reads_mailbox" if want.lower() in connected
            else "caller_reads_code")
    if check_only:
        return {"email": want, "mode": mode}
    if not BROWSERBASE_API_KEY:
        raise HTTPException(400, "Browserbase isn't configured.")
    jid = start_job(account_id, "password_reset", site,
                    call_id=call_id or None, payload={"email": want})
    return {"job_id": jid, "state": "queued", "email": want, "mode": mode}


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


PRICE_GOAL = """Find out what {item} costs, and where it is cheapest.

These pages were found for you. Open them one at a time with goto, in
this order:
{shops}

You cannot search the web from here - search engines refuse a browser,
and you do not need to: the pages above are the ones to check.

Work like a careful shopper with a phone call waiting:
1. Read the page you are on.
2. Move to the next page on the list with goto.
3. On EACH page, before you leave it, record with "found" the shop's name,
   the exact price it shows, whether it says in stock, and any delivery
   cost or discount. If a page does not show a price for this exact item,
   note that too and move on.
4. Never open a page you have already noted.

Then reply done with: the cheapest price and which shop it is at, then the
others you found with their shops, and say plainly if a shop had no price.
Prices only from pages you actually read - never from a search summary,
and never a guess. Buy nothing and sign in to nothing."""


@app.post("/jobs/price")
def job_price(request: Request, account_id: int, item: str,
              call_id: int = 0):
    """What does it cost, and where is it cheapest. A plain web search
    answers this with an outlet shop's street address; reading the actual
    shop pages answers it with prices."""
    require_auth(request)
    if is_blocked(item):
        return {"blocked": True, "answer": BLOCKED_REPLY}
    if not BROWSERBASE_API_KEY:
        raise HTTPException(400, "Browserbase isn't configured.")
    # Find the shops with the search API, not by driving Google: Google
    # answers a browser with a captcha, and the job died at the front door
    # having priced nothing.
    shops, seen = [], set()
    def _hosts(results):
        """Best page per shop. A category page is not a price: prefer the
        one that looks like the product itself."""
        best = {}
        for r in results or []:
            link = (r.get("url") or r.get("link") or "").strip()
            low = link.lower()
            if not low.startswith("http") or len(low.split("/")) < 3:
                continue
            host = low.split("/")[2]
            if any(bad in host for bad in
                   ("google.", "youtube.", "facebook.", "reddit.",
                    "pinterest.", "wikipedia.", "instagram.", "tiktok.")):
                continue
            looks_product = any(m in low for m in
                                ("/product", "/dp/", "/p/", "/item",
                                 "/prod", "/shop/"))
            score = (2 if looks_product else 0) + (
                1 if any(w in (r.get("title") or "").lower()
                         for w in item.lower().split()[:3]) else 0)
            if host not in best or score > best[host][0]:
                best[host] = (score, link)
        return best

    hits = []
    for q in (f"{item} price", f"buy {item} online"):
        try:
            hits += (tool_web_search(q) or {}).get("results", [])
        except Exception as e:
            emit("browse", "price", f"search failed: {str(e)[:120]}", "warn",
                 account_id)
    found = _hosts(hits)
    # A maker's own site answers all three searches and crowds out every
    # shop that might be cheaper, which is the whole question. Ask again
    # without it.
    if len(found) < 3 and found:
        top = max(found.items(), key=lambda kv: kv[1][0])[0]
        try:
            more = (tool_web_search(f"{item} price -site:{top}")
                    or {}).get("results", [])
            for host, pair in _hosts(more).items():
                found.setdefault(host, pair)
        except Exception:
            pass
    shops = [link for _score, link in
             sorted(found.values(), key=lambda pair: -pair[0])]
    if not shops:
        raise HTTPException(503, "Couldn't find anywhere selling that.")
    jid = start_job(account_id, "browse", "", call_id=call_id or None,
                    payload={"goal": PRICE_GOAL.format(
                        item=item[:120],
                        shops=chr(10).join(f"  {i + 1}. {u}"
                                           for i, u in enumerate(
                                               shops[:5]))),
                             "url": shops[0], "urls": shops[1:6],
                             "max_steps": 20, "query": item[:120]})
    return {"job_id": jid, "state": "queued", "shops": len(shops)}


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


@app.get("/browser/address")
def browser_address(request: Request, site: str):
    """The address a job would open for this site, without opening it.

    Call 64: "khconnect.kioskhut.com" became
    "https://www.khconnect.kioskhut.com.com". Nothing showed that but the
    job's own log line, after the browser had already gone there, and
    com.com answers everything - so it came back as a certificate error
    and the caller was told his site was insecure."""
    require_auth(request)
    return {"site": site, "url": site_url(site)}


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


@app.get("/price")
def price_now(request: Request, item: str, account_id: int = 0,
              shop: str = ""):
    """Prices across shops in about two seconds, the way the top of a
    Google page shows them. No browser, so nothing can refuse us."""
    require_auth(request)
    out = shopping_prices(item, shop=shop)
    if out.get("offers"):
        emit("price", item[:40],
             f"{len(out['offers'])} shops: {out.get('answer', '')[:120]}",
             "info", account_id or None)
    return out


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



