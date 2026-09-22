"""Everything done inside a customer's Google account.

Their mail, their calendar, their contacts, their Drive and their
to-do list - read, written and changed on their say-so. The rules
that matter are here rather than in a prompt: nothing is deleted for
good, a document is never shared, and a one-time code is read from
their own inbox rather than asked for.

One mailbox is picked per call by pick_connection; everything takes
a "which" so a caller with two addresses is never guessed at.
"""
from core import *                                   # noqa: F401,F403
from core import _re_scrub, _tz, _clock, _when


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
