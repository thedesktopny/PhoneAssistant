"""The part that thinks, and the part that marks its own homework.

The voice model is fast and a poor judge: told "my phone is not with
me", it once answered "I am handling that" while nothing at all was
running. So judgement happens here instead. call_state reads what is
actually true - what is running, what failed and why, what is
connected - and advise() gives a slower model those facts and gets
back the words to say.

It decides HOW to help. It never decides what is allowed: reading a
total back, a spoken yes before sending or charging, and stopping at
a human check all stay in code, where a model cannot talk its way
past them.

review_call reads each finished call back against what actually ran
and flags anything the record does not support; learn_about_caller
keeps the standing notes, minus anything secret.
"""
from core import *                                   # noqa: F401,F403
from core import _re_scrub, _clock, _tz, _JOBS
from ai import _openai_chat
from rules import is_blocked, BLOCKED_REPLY


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
