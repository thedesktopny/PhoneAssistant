"""New customers signing themselves up by phone, with an invite code.

The office makes a code in the admin panel and gives it to the person.
They ring from their own phone, say the code, say their name, choose a
PIN - and they are a customer, on that call, from that number.

Why a code and not open sign-up: every minute costs money, and a number
anyone can ring should not be a number anyone can use. The code is the
office saying yes to one person, once.

The code is a secret like a PIN: only a keyed hash of it is stored, it
is shown to the office exactly once, it works once, and it expires.
Guessing is stopped by counting wrong tries per phone and overall.
"""
from core import *                                   # noqa: F401,F403
from core import _re_scrub
import hashlib
import hmac
import secrets
from google_tools import _connect_too_many

INVITE_DAYS = int(os.environ.get("INVITE_DAYS", "14"))
TRIES_PER_PHONE = 5          # wrong codes from one number in an hour
TRIES_OVERALL = 40           # wrong codes from everyone in an hour


def _code_hash(code: str) -> str:
    key = (ENCRYPTION_KEY or "invite").encode()
    return hmac.new(key, code.encode(), hashlib.sha256).hexdigest()


def _digits(text: str) -> str:
    return "".join(ch for ch in (text or "") if "0" <= ch <= "9")


def _last10(phone: str) -> str:
    return _digits(phone)[-10:]


def make_invite(note: str = "", days: int = 0) -> dict:
    """A new six-digit code. The only time it is ever shown."""
    days = int(days or INVITE_DAYS)
    db = Session()
    live = {r.code_hash for r in db.query(Invite)
            .filter(Invite.used_at.is_(None)).all()}
    code = ""
    for _ in range(50):
        c = f"{secrets.randbelow(1000000):06d}"
        if c[0] != "0" and _code_hash(c) not in live:
            code = c
            break
    row = Invite(code_hash=_code_hash(code), note=(note or "")[:120],
                 expires=datetime.utcnow() + timedelta(days=days))
    db.add(row)
    db.commit()
    db.refresh(row)
    out = {"id": row.id, "code": code, "note": row.note,
           "expires": local_str(row.expires, "day")}
    db.close()
    emit("signup", "invite", f"invite {out['id']} made"
         + (f" for {note[:60]}" if note else ""), "info")
    return out


def list_invites() -> list:
    """What the office can see: who each was for and what became of it.
    Never the code."""
    db = Session()
    rows = db.query(Invite).order_by(Invite.id.desc()).limit(100).all()
    now = datetime.utcnow()
    out = []
    for r in rows:
        if r.used_at:
            state = "used"
        elif r.cancelled:
            state = "cancelled"
        elif r.expires and r.expires < now:
            state = "expired"
        else:
            state = "waiting"
        out.append({"id": r.id, "note": r.note or "", "state": state,
                    "made": local_str(r.made, "day"),
                    "expires": local_str(r.expires, "day"),
                    "used": local_str(r.used_at, "stamp") if r.used_at
                    else "",
                    "account_id": r.account_id, "name": r.used_name or "",
                    "phone": r.used_phone or ""})
    db.close()
    return out


def cancel_invite(invite_id: int) -> dict:
    db = Session()
    row = db.query(Invite).filter_by(id=invite_id).first()
    if row and not row.used_at:
        row.cancelled = 1
        db.commit()
    ok = bool(row and row.cancelled)
    db.close()
    return {"cancelled": ok}


def _live_invite(db, code: str):
    want = _code_hash(_digits(code))
    now = datetime.utcnow()
    for r in db.query(Invite).filter(Invite.used_at.is_(None)).all():
        if r.cancelled or (r.expires and r.expires < now):
            continue
        if hmac.compare_digest(r.code_hash, want):
            return r
    return None


def _registered(db, phone: str):
    want = _last10(phone)
    for p in db.query(PhoneNumber).all():
        if _last10(p.number) == want:
            return p
    return None


def check_invite(phone: str, code: str) -> dict:
    """Is this a code the office gave out? Answered with a reason code:
    ok, no_number, already_customer, too_many, bad_code."""
    if len(_last10(phone)) < 10:
        return {"ok": False, "reason": "no_number"}
    key = "invite:" + _last10(phone)
    if _connect_too_many(key, TRIES_PER_PHONE) or \
            _connect_too_many("invite:all", TRIES_OVERALL):
        return {"ok": False, "reason": "too_many"}
    db = Session()
    if _registered(db, phone):
        db.close()
        return {"ok": False, "reason": "already_customer"}
    row = _live_invite(db, code) if len(_digits(code)) == 6 else None
    db.close()
    if not row:
        _connect_too_many(key, TRIES_PER_PHONE, add=True)
        _connect_too_many("invite:all", TRIES_OVERALL, add=True)
        emit("signup", _last10(phone)[-4:], "a wrong invite code was tried",
             "warn")
        return {"ok": False, "reason": "bad_code"}
    return {"ok": True}


# PINs nobody should have: the same digit over and over, or a straight run.
def weak_pin(pin: str) -> bool:
    p = _digits(pin)
    if len(set(p)) == 1:
        return True
    runs = "01234567890", "09876543210"
    return any(p in r for r in runs)


def _tidy_name(part: str) -> str:
    """"moshe" -> "Moshe", "o'brien" -> "O'Brien", "ben-david" ->
    "Ben-David". Only letters, spaces, apostrophes and hyphens stay."""
    part = _re_scrub.sub(r"[^A-Za-zÀ-ɏ' \-]", "", part or "")
    part = " ".join(part.split())
    return "-".join("'".join(w[:1].upper() + w[1:] for w in bit.split("'"))
                    for bit in part.split("-")) if part else ""


def complete_signup(phone: str, code: str, first_name: str, last_name: str,
                    pin: str, call_id=None) -> dict:
    """Make the customer. Everything is checked again here - the voice
    side's word is not taken for any of it."""
    first, last = _tidy_name(first_name), _tidy_name(last_name)
    if not first or not last:
        return {"ok": False, "reason": "name_needed"}
    p = _digits(pin)
    if not 4 <= len(p) <= 6:
        return {"ok": False, "reason": "pin_length"}
    if weak_pin(p):
        return {"ok": False, "reason": "weak_pin"}
    checked = check_invite(phone, code)
    if not checked["ok"]:
        return checked
    db = Session()
    row = _live_invite(db, code)
    if not row or _registered(db, phone):
        db.close()
        return {"ok": False, "reason": "bad_code" if not row
                else "already_customer"}
    name = f"{first} {last}"
    acct = Account(name=name, pin=p)
    db.add(acct)
    db.commit()
    db.refresh(acct)
    db.add(PhoneNumber(number="+1" + _last10(phone)
                       if len(_digits(phone)) <= 11 else phone,
                       account_id=acct.id))
    if call_id:
        call = db.query(Call).filter_by(id=call_id).first()
        if call and not call.account_id:
            call.account_id = acct.id
    row.used_at = datetime.utcnow()
    row.account_id = acct.id
    row.used_name = name
    row.used_phone = _last10(phone)
    db.commit()
    out = {"ok": True, "account_id": acct.id, "name": name,
           "note": row.note or ""}
    db.close()
    record_change(acct.id, "account", "signed up",
                  f"{name} signed up by phone from the number ending "
                  f"{_last10(phone)[-4:]} with invite {out['note'] or '#'}",
                  call_id=call_id)
    emit("signup", name[:40], f"new customer {name} (account {acct.id}) "
                              f"signed up by phone", "info", acct.id)
    return out
