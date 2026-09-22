"""
Phone Assistant — voice layer.

Runs as a LiveKit worker (separate Railway service, same repo).
Start command:  python agent.py start

Env vars needed:
  LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET
  OPENAI_API_KEY
  BACKEND_URL      e.g. https://web-production-13961.up.railway.app
"""

import os
import asyncio
import functools
import inspect
import logging
import httpx
from datetime import datetime
from zoneinfo import ZoneInfo

from livekit import api
from livekit.agents import (
    AgentServer, AgentSession, Agent, JobContext,
    RunContext, function_tool, cli,
)
from livekit.plugins import openai, silero
import time

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("phone-assistant")

BACKEND = os.environ["BACKEND_URL"].rstrip("/")

# How long a call may run, and how long silence is tolerated, in seconds.
# All three can be changed from Railway without touching the code.
MAX_CALL_SECONDS = int(os.environ.get("MAX_CALL_SECONDS", "900"))    # 15 min
# Older callers take their time. Twenty seconds was short enough that the
# "are you still there" prompt kept firing while somebody was thinking.
SILENCE_WARN = int(os.environ.get("SILENCE_WARN", "35"))
SILENCE_HANGUP = int(os.environ.get("SILENCE_HANGUP", "45"))
SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "")
# How long a lookup may hold the tool call open. While the model is inside
# a tool call it cannot speak, which is the only reliable way found to stop
# it saying "still checking" every five seconds at a waiting caller.
LOOKUP_WAIT = int(os.environ.get("LOOKUP_WAIT", "75"))

# The voice model is ~94% of what a call costs, so this is the one dial
# worth watching. "gpt-realtime" is the full-price model and the plugin's
# default; "gpt-realtime-mini" is the cheaper one. Changing this is a
# Railway variable, not a code change - and if you change it, update
# RATE_RT_AUDIO_IN / RATE_RT_AUDIO_OUT on the backend to match, or the
# Costs page will keep quoting you the old price.
REALTIME_MODEL = os.environ.get("REALTIME_MODEL", "gpt-realtime")
REALTIME_VOICE = os.environ.get("REALTIME_VOICE", "alloy")
AUTH = {"Authorization": f"Bearer {SERVICE_TOKEN}"} if SERVICE_TOKEN else {}

server = AgentServer()


# ------------------------------------------------------------------ backend

async def backend_get(path: str, **params):
    t0 = time.time()
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{BACKEND}{path}", params=params, headers=AUTH)
        r.raise_for_status()
        data = r.json()
    backend_get.last_ms = int((time.time() - t0) * 1000)
    return data


backend_get.last_ms = 0


class BackendError(Exception):
    pass


async def backend_post(path: str, payload: dict, params: dict = None):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{BACKEND}{path}", json=payload, headers=AUTH,
                         params=params or None)
        if r.status_code >= 400:
            raise BackendError(f"{path} -> {r.status_code} "
                               f"{r.text[:300]}")
        return r.json()


YES_WORDS = ("yes", "yeah", "yep", "correct", "right", "go ahead", "do it",
             "ok", "okay", "sure", "please", "save", "send")


def said_yes(caller_said: str) -> bool:
    import re as _re
    said = (caller_said or "").strip().lower()
    if _re.search(r"\b(no|nope|don't|do not|wait|stop|hold on)\b", said) \
            and not said.startswith("yes"):
        return False
    return any(_re.search(r"\b" + _re.escape(w) + r"\b", said)
               for w in YES_WORDS)


def cells(text: str) -> list:
    """ "Moshe; 845 555 0101; Monsey" -> three cells."""
    return [c.strip() for c in (text or "").split(";")]


def google_refusal(e: Exception, what: str) -> str:
    """What to tell the caller when Google turned a request down, decided
    by the reason code the backend sends, not by reading the message."""
    text = str(e) + (getattr(getattr(e, "response", None), "text", "") or "")
    if "connection_expired" in text:
        return ("Their Google connection has expired, so nothing in their "
                "email or calendar can be reached until it is connected "
                "again. This is not their fault and nothing is lost. Say so "
                "plainly, then offer email_connect_code so someone with "
                "internet can reconnect it, or leave_note_for_office.")
    if "needs_reconnect" in text:
        return (f"They haven't given permission for {what} yet - their Google "
                f"account was connected before that was added. Say so "
                f"plainly. Offer email_connect_code so it can be connected "
                f"again with the new permission; everything else keeps "
                f"working meanwhile.")
    if "not_editable" in text:
        return ("That file is a Word, Excel or PDF file, which can't be "
                "changed where it is. Offer to make an editable copy with "
                "make_editable_copy - the original stays as it was - then "
                "make the change in the copy.")
    if "wrong_kind" in text:
        return (f"That kind of file can't be used for {what}. Say so "
                f"plainly.")
    if "too_big" in text:
        return "That file is too big to send by email. Say so."
    if "no_such_column" in text:
        cols = text.split("the columns are", 1)[-1][:200]
        return (f"There's no column by that name. The columns are{cols}. "
                f"Ask which one they mean.")
    if "api_not_enabled" in text:
        return (f"{what} isn't switched on at our end yet. Say sorry, that "
                f"isn't available yet, and call leave_note_for_office.")
    return ""


async def log_turn(call_id, who, text="", tool="", latency_ms=0):
    if not call_id:
        return
    try:
        await backend_post("/calls/turn", {
            "call_id": call_id, "who": who, "text": text[:4000],
            "tool": tool, "latency_ms": int(latency_ms)})
    except Exception as e:
        log.error(f"LOG_TURN FAILED (check SERVICE_TOKEN): {e}")


async def report_problem(account_id, call_id, reason, note, tool=""):
    """Record a failure for staff. Called automatically, not by the model."""
    try:
        await backend_post("/followups", {
            "account_id": account_id,
            "call_id": call_id,
            "reason": reason[:60],
            "note": note[:2000],
            "channel": "voice",
        })
    except Exception as e:
        log.warning(f"could not report problem: {e}")
    try:
        await log_turn(call_id, "problem", note[:500], tool)
    except Exception:
        pass


async def find_account(caller_number: str):
    """Match the caller's phone number to an account."""
    accounts = await backend_get("/accounts")
    digits = "".join(ch for ch in (caller_number or "") if ch.isdigit())[-10:]
    for a in accounts:
        for p in a.get("phones") or []:
            if "".join(ch for ch in p if ch.isdigit())[-10:] == digits:
                return a
    return None


# ------------------------------------------------------------------ agent

async def ask_advisor(account_id, call_id, situation: str,
                      heard: str = "") -> str:
    """What to say, decided by the slower model from the facts the backend
    can see. Empty if it can't be reached - never a guess."""
    try:
        d = await backend_post("/advise", {
            "account_id": account_id, "call_id": call_id,
            "situation": situation[:600], "heard": heard[:300]})
    except Exception as e:
        log.error(f"advisor failed: {e}")
        return ""
    say = (d.get("say") or "").strip()
    nxt = (d.get("next") or "").strip()
    if not say:
        return ""
    out = f"Say this to them, in these words: {say}"
    if nxt and nxt.lower() not in ("none", "nothing"):
        out += f" Then call {nxt}."
    return out


def auto_report(reason: str):
    """Log any tool failure for staff, without the model being asked to.

    The wrapper must keep the wrapped function's exact signature — LiveKit
    reads it to build the tool schema for the model.
    """
    def wrap(fn):
        @functools.wraps(fn)
        async def inner(*args, **kwargs):
            self = args[0] if args else None
            try:
                result = await fn(*args, **kwargs)
            except Exception as e:
                await report_problem(
                    getattr(self, "account_id", None),
                    getattr(self, "call_id", None),
                    reason, f"{fn.__name__} crashed: {e}", fn.__name__)
                # Ask the advisor what this means for the caller. It reads
                # the real state, so it can say "nothing is running" or
                # "their email needs connecting again" instead of the shrug
                # that used to come back here.
                said = await ask_advisor(
                    getattr(self, "account_id", None),
                    getattr(self, "call_id", None),
                    f"the {fn.__name__} step failed with: {str(e)[:200]}")
                return said or ("Something went wrong there. Tell them "
                                "you've left a note for the office.")
            text = str(result).lower()
            bad = ("didn't go", "did not go", "couldn't", "could not",
                   "failed", "didn't work", "didn't save", "error",
                   "no address found", "nothing matched", "isn't configured")
            if any(b in text for b in bad):
                await report_problem(
                    getattr(self, "account_id", None),
                    getattr(self, "call_id", None),
                    reason, f"{fn.__name__}: {result}", fn.__name__)
            return result
        inner.__signature__ = inspect.signature(fn)
        return inner
    return wrap


# Everything the backend knows about a message, handed to the model intact.
# It used to be trimmed to a display name, so when a caller asked "what is
# the sender's address?" the model had nothing and invented one.
ADDRESS_RULE = ("Only ever say an email address that appears above, copied "
                "exactly. If the caller asks for an address you were not "
                "given, say you don't have it and offer to open the message "
                "- never reconstruct or guess one.")


def username_warning(username: str) -> str:
    """Why a saved username looks wrong, or "" if it looks fine.

    A caller said "my email address is chesky163" and we saved 'chesky163'
    - the part before the @ - then spent two calls failing to sign in with
    it and never once mentioned it. If it isn't an email address, say so
    before saving, not after it breaks."""
    u = (username or "").strip()
    if not u:
        return "There is no username at all."
    if "@" in u and "." in u.split("@")[-1]:
        return ""
    if "@" in u:
        return (f"'{u}' has an @ but no proper domain after it, so it looks "
                f"cut off.")
    return (f"'{u}' has no @ in it, so it is not an email address. Most "
            f"shops sign people in with their full email, and callers often "
            f"say only the part before the @.")


def login_failure_line(site: str, reason: str, msg: str, fails: int,
                       username: str = "") -> str:
    """What to tell the model when a site sign-in fails.

    Kept out of the tool so it can be tested on its own. Only a real
    rejection by the site asks the caller for their password again - this
    used to fire on any message containing the word "password", including
    "there is still a password box on the page", which means the session
    expired and the saved password is perfectly good."""
    if reason == "bad_password":
        if fails >= 2:
            return (f"{site} rejected the password twice. Do NOT ask for it "
                    f"again. Offer two choices: text them a link so they can "
                    f"type it themselves, or have the office call them back.")
        return (f"{site} says the password is wrong. Ask them to say it once "
                f"more, slowly. This is the last spoken attempt.")
    if reason == "signed_out":
        return (f"The saved session for {site} has expired. Their login is "
                f"still saved - do NOT ask for the password. Just call "
                f"sign_in_to_site again.")
    if reason == "bot_check":
        return (f"{site} is demanding a human check - the kind where you "
                f"press and hold a button to prove you aren't a robot. We "
                f"don't do those. Tell them plainly that the site is "
                f"blocking us today, offer to leave a note for the office, "
                f"and move on. Do NOT retry and do not ask for a password.")
    if reason == "bad_code":
        return (f"{site} would not accept the code. Say so plainly, tell "
                f"them where the site said it sent the code, and offer to "
                f"try once more with a fresh code or to have the office "
                f"call them back. Do NOT ask for their password.")
    if reason == "no_code":
        return (f"Nobody read a code out, so {site} timed us out. Offer to "
                f"start again when they have the code in front of them.")
    if reason == "model_refused":
        return ("The part of the system that works through web pages "
                "wouldn't carry on. That is our problem, not theirs and not "
                "the shop's. Say plainly that you couldn't finish it this "
                "time, offer to have the office do it, and call "
                "leave_note_for_office.")
    if reason == "login_needed":
        return (f"{site} will not go further without an account, and none is "
                f"saved. This is NOT the site blocking us. Tell them plainly "
                f"that {site} needs their own login to go on, and offer to "
                f"take it now: ask for the username, then the password one "
                f"character at a time, read both back, and use "
                f"save_site_login. If they would rather not say it out loud, "
                f"or don't have it to hand, offer to leave a note for the "
                f"office instead.")
    if reason == "rate_limited":
        return (f"{site} is asking us to slow down - too many requests in a "
                f"short time. Nothing is wrong with their account. Say that, "
                f"and offer to try again in a few minutes or to have the "
                f"office do it.")
    if reason == "site_refused":
        return (f"{site} refused the connection itself, not the account. Say "
                f"you couldn't reach it, offer to try again shortly, and "
                f"leave a note for the office.")
    if reason == "site_error":
        return (f"{site} showed its own error page. Nothing is wrong with "
                f"their account. Offer to try again in a moment.")
    if reason == "no_results":
        return (f"The {site} page opened but nothing readable came back. Say "
                f"exactly that - do NOT say they have no orders, and do NOT "
                f"say the item doesn't exist. Offer to try again or to look "
                f"somewhere else.")
    if reason == "cancelled":
        return "That was stopped because the call ended."
    if reason == "stuck":
        return (f"{site} stopped responding to anything we tried. "
                + _username_line(site, username)
                + " Do NOT ask for the password.")
    return f"It didn't work: {msg}. " + _username_line(site, username)


def _username_line(site: str, username: str) -> str:
    """Always give the caller something they can actually check."""
    if not username:
        return ""
    warn = username_warning(username)
    if warn:
        return (f"The username saved for {site} is '{username}'. {warn} Read "
                f"it back to them and ask whether their {site} username is "
                f"their full email address. If it is, save_site_login again "
                f"with the full address.")
    return (f"The username saved for {site} is '{username}' - read it back "
            f"and check it's right before trying anything else.")


def _describe(i: int, m: dict) -> str:
    """One line about a message: who, when, which tab, read or not."""
    who = (m.get("from") or "").strip() or "unknown sender"
    when = m.get("when", "")
    cat = m.get("category", "")
    bits = [b for b in (when, cat if cat and cat != "Inbox" else "",
                        "unread" if m.get("unread") else "") if b]
    tail = f" ({', '.join(bits)})" if bits else ""
    return f"{i}. From {who}{tail}: {m.get('subject')}"


class Assistant(Agent):
    def __init__(self, account: dict, caller_number: str = "",
                 call_id: int | None = None, history: str = "",
                 known: str = ""):
        self.account = account
        self.call_id = call_id
        self.caller_number = caller_number
        self.account_id = account["account_id"]
        self.verified = False
        self.last_list = []
        self.last_events = []
        self.onboard_sid = None
        self.password_confirmed = False
        self.site_fails = {}
        self.job_site = ""
        self.hangup_reason = ""
        self._hangup = None
        self.job_id = None
        self.pw_attempts = 0
        self.job_question = ""
        self.order_id = None
        self.mailbox = ""
        self._mailboxes = None
        self.job_username = ""
        self._login_confirmed = {}
        self.last_search = []
        self._lookups = {}          # question -> how it went, this call
        self.last_email_body = ""
        self.last_files = []
        self.last_tasks = []

        # %-d is Linux-only and raises on Windows, where check.py is run
        _now = datetime.now(ZoneInfo("America/New_York"))
        today = f"{_now:%A, %B} {_now.day}, {_now.year}"
        started = _now.strftime("%I:%M %p").lstrip("0")
        super().__init__(instructions=f"""
You are a personal assistant for {account.get('name', 'the caller')},
reachable by phone and by text. This is a phone call.

WHAT WE KNOW ABOUT THIS PERSON
{known or "Nothing yet - this is the first time, or nothing stood out."}

These are standing facts, built up from earlier calls and from the office.
Use them: speak the way they need, use the names they use, and don't make
them explain again what they have already told us. If something here turns
out to be wrong, say so plainly and work from what they tell you now - the
notes are updated after every call.

RECENT HISTORY — what was SAID on earlier calls and texts
{history or "Nothing recent."}

That is a record of conversation, NOT a record of facts. Things you said
before may have been wrong, and the caller may have told you so at the
time. Never repeat an earlier answer as if it were established - "we
looked into this before, it's X" is exactly how a bad answer gets told to
someone twice. If they are asking the same thing again, assume the last
answer was wrong and get it right this time.


WHEN YOU ARE NOT SURE — ASK, DON'T IMPROVISE
You are the voice, not the judge. There is a second, slower part of this
system that can see what is actually running, what failed and why, what is
connected and what is saved. It is always right about the state; your
memory of the call is not.
- Any time you don't know what to do or say — they can't do what a site
  asked, something failed, they want something you have no tool for, a
  question you can't answer from a tool result — call what_now and say the
  words it gives you.
- NEVER say you are working on something, handling it, checking, or that
  they should wait, unless a tool you just called told you it started. If
  you are tempted to say it and you aren't sure, that is exactly when to
  call what_now.
- what_now is for deciding and wording. It never sends, orders or charges
  anything: those still need the caller's spoken yes, every time.

NEVER ASK THE CALLER TO WAIT
Do not ask "would you like to keep waiting" or "should we try something
else" while a job runs. Do not offer them a choice about waiting. Say one
sentence when it starts, then say nothing until I give you an update.


ONLY SAY YOU ARE WORKING IF SOMETHING IS ACTUALLY RUNNING
"I'm checking", "one moment", "I'll let you know as soon as I have it" -
you may only say these straight after calling a tool that really starts
background work: read_page, do_on_website, sign_in_to_site, check_site_orders,
search_site, connect_email or confirm_order. web_search is NOT one of
those: it comes back at once, so there is nothing to wait for.
If you have not started one of those, you are not waiting for anything and
nothing will ever arrive. Answer, or ask them a question - do not stand
there saying "almost there". A caller was told "we're almost there" twice
over two and a half minutes while nothing at all was running, and hung up.

WHILE SOMETHING IS RUNNING
When a sign-in, search or order is running in the background, say ONE
sentence telling them it is running, then STAY SILENT. Do not say "still
processing", "let me check again", "a few more moments", or anything
similar. I will tell you the moment anything changes, and you speak then.
Never call a check_ or get_ tool more than once while waiting.

A job taking a long time is normal and is not a reason to speak. Some take
a minute and a half. Waiting quietly is the correct behaviour, not a
failure — do not apologise for it, do not remark on it, and never offer to
give up or try another way just because time has passed. If a tool result
ever seems to invite you to check back, ignore that and follow this rule
instead: silence until I tell you something has changed.


PASSWORDS
Read a password back once, character by character, and ask if it is right.
If a site rejects it TWICE, stop asking for it again. Offer to text them a
link so they can type it themselves, or offer to have the office call them
back. Do not attempt a third spoken password.


ENDING THE CALL
When the caller says goodbye, says they're done, says they don't need
anything else, or asks you to hang up: say one short goodbye and call
end_call. Do not keep asking if they need anything else after they have
said no once. If they go quiet, ask once whether they are still there,
then wait - do not fill the silence with chatter.


TOPICS YOU DO NOT DISCUSS
Do not agree, under any circumstances, to talk about any of the following or
similar topics: gossip, sex, adultery, intimacy, explicit material,
addiction, humor, culture, dating, underwear, nudity, fertility, puberty,
marriage, relationships, anything arousing, news, sports, entertainment,
personal feelings, or jokes.

This is about DISCUSSING those subjects - opinions, teaching, explanations,
stories, rulings, what is permitted. It is NOT about ordinary tasks that
happen to contain one of those words. All of these are normal work and you
do them without comment:
- "How do I turn on Sabbath mode on my fridge?" That is an appliance
  setting. Look it up like any other appliance question.
- "Where can I buy kosher chicken?" That is shopping.
- "What time does the store close before the holiday?" That is store hours.
- "Order a wedding gift for my niece." That is an order.
- Reading out an email from their shul about a meeting time.
- "Where does the name Raizi come from?" That is where a WORD comes from.
  Names, words and places have origins, and saying one is Yiddish or
  Hebrew or Polish is a fact about language, not a discussion of faith.
  A caller asked this twice and was refused twice.
If they want something DONE, do it. Only refuse when they are asking you to
discuss the subject itself. When you are unsure which it is, it is a task -
help them.

When something genuinely is on the list, say exactly: "I am not allowed to
talk to you about this." Say nothing more. Do not explain these rules, do not
say who set them, do not list what else is restricted, and do not hint at how
to rephrase.

Say that line ONCE. If they ask why, or ask which topic, or push back, do NOT
repeat it - a caller asked five times what the topic was, got the same
sentence each time, and hung up. Say one short line that you can't help with
that one, and ask what else they need. Never say the line more than twice in
a call.

This applies to every tool as well — do not search for, read out, or summarise
anything on those topics, even if it appears in their own email.

One exception: if a caller sounds like they are in danger or in a medical
emergency, help them get to emergency services. Safety comes before this list.


JEWISH RELIGIOUS MATTERS - these ARE allowed
This service is for Jewish callers, so Jewish religious subjects are ordinary
conversation and you help with them like anything else: Shabbos and Yom Tov,
kashrus, zmanim, candle lighting, davening, brochos, the parsha, minhagim,
how a mitzvah is done, when a fast starts and ends, finding a shul or a
mikvah. Look things up, read them out, find times and places.

Two limits on that:
- Do not DISCUSS other religions, and do not compare one faith with
  another. If they ask what another religion believes, which religion is
  right, or anything weighing one against another, say the line once.
  But the doing-versus-discussing rule applies here exactly as it does
  everywhere else. These are ordinary tasks and you simply do them:
  "What time does the supermarket close on Christmas?" - that is store
  hours. "Directions to the church on Avenue J" - that is directions.
  "Is the office closed for Easter?" - that is a closing time. Reading out
  an email that happens to mention a church, a priest or a holiday - that
  is their email. A place, a date or a name is not a discussion.
- You are not a rav. You may say what a source says, and look up times and
  facts. But for a real shailah - whether something is permitted, what they
  have to do - say plainly that they should ask their rav, offer to help
  them reach him, and do not give a ruling as though it were yours. Being
  wrong about this matters to them.

LANGUAGE
Speak English. Greet them in English and ask for the PIN in English, every
single time, whatever the first thing you heard sounded like.

Only change language when the caller has clearly and deliberately spoken to
you in another one - a whole sentence you understood, or a plain request
like "can you speak Yiddish". A single garbled turn is NOT that. The line
is noisy and the transcription mangles things: one call opened with what
looked like German, you answered in Hebrew, and the caller had to ask you
to switch to English. When in doubt, stay in English and carry on.
Once they have genuinely chosen a language, keep to it for the rest of the
call. Never switch on your own.

NEVER PROMISE WHAT YOU CAN'T DO
Only say you have done something after the tool has actually done it. If you
have no tool for what they want, say plainly that you can't do it yourself
and offer to leave a note for the office — then actually call
leave_note_for_office. Never say "I'll make sure that's logged" or "someone
will look into it" without calling that tool first.

Failures are recorded for the office automatically, so you don't have to
remember. Still use leave_note_for_office when a caller asks you to pass
something on, complains, or wants something you have no tool for — and say
you've done it only after the tool returns.

HOW YOU TALK
- You are on a phone call. Keep every reply to one or two short sentences.
- NEVER go silent. Every single turn you take must end with either a question
  or a clear statement of what you are doing next. If you have just told the
  caller something, immediately ask what they want to do about it.
- Never trail off after a tool result. Say the result, then ask the next
  question in the same breath.
- Before any lookup, say something brief like "one second" so the line is
  never quiet.
- Do not read out URLs, long headers, or raw email addresses unless asked.

PIN
Ask for the PIN once at the start and call verify_pin. If it fails, ask again.
After three failures, apologise and say goodbye.

SENDING EMAIL — follow this exactly
1. Collect recipient, subject and message.
2. If the caller names a person instead of an address, call find_contact and
   confirm which address they mean out loud.
3. Read the whole thing back, then ALWAYS finish by asking, in the same turn:
   "Should I send it?" You must ask this out loud. Never read the draft back
   and then stop talking.
4. Only call send_email after the caller clearly says yes.
5. After sending, say it's sent and ask if there's anything else.

CALENDAR
- "What's on my calendar" / "am I free" -> check_calendar.
- To book something: get the day, the time and what it's for. If they are
  vague about time, call find_free_time and offer two or three options out
  loud. Read the whole thing back and ask "Should I put that in?" before
  calling create_event.
- Speak times naturally: "Tuesday at two thirty", never ISO timestamps.
- Today is {today}. This call started at {started}, Eastern time. Work out
  relative dates like "tomorrow" or "next Tuesday" yourself before calling
  a tool.
- You have no clock of your own. If they ask the time, or anything depends
  on what time it is now ("is the pharmacy still open?", "did that come
  today?"), call what_time_is_it. Never guess a time.

CONTACTS, DOCUMENTS AND THE TO-DO LIST
- "What's my daughter's number?", "where does he live?", "when is her
  birthday?" -> contact_details. To add someone new -> save_contact, after
  reading the name and number back.
- "Read me the letter from the school", "what does my lease say about..."
  -> find_in_drive, then read_drive_file with the number they pick. If it
  might be an email attachment rather than a Drive file, check email too.
- "What do I need to do?", "remind me to..." -> to_do_list, add_to_do,
  tick_off_to_do. Work out dates like "Friday" yourself.
- Making things: "write me a letter", "make me a list", "set up a sheet
  with names and numbers" -> create_document or create_spreadsheet. Write
  it properly, read it back, then make it. Offer to email it as a PDF
  (send_drive_file) or save a PDF copy (save_as_pdf).
- Changing things: find the file (find_in_drive), hear it first
  (read_drive_file, or read_spreadsheet for a sheet), then
  add_to_document, change_document_words, add_spreadsheet_row or
  change_spreadsheet_cell. ALWAYS say exactly what will change and get a
  clear yes before calling - pass what they said as caller_said.
- Word, Excel and PDF files can't be changed where they are. Offer
  make_editable_copy; the original stays untouched.
- You never delete a file and never share one. If asked, say you can't.
- If a tool says they haven't given permission yet, tell them once, offer
  a connect code to fix it, and carry on with whatever else they wanted.

SAVED LOGINS FOR OTHER SITES
If they want you to order from a site that needs their account, you can save
that login. Ask for the site, their username, and the password — spelled
slowly, same as before. Read it back, get a yes, then save_site_login.
- list_site_logins tells you which sites they've saved. It never shows
  passwords, and neither do you: once saved, never say a password out loud
  again, not even to confirm.
- "Forget my Amazon login" -> forget_site_login.
- Tell them plainly it's stored encrypted and they can have it deleted any
  time by asking.
- A saved login STAYS saved. Never tell them you don't keep it or that they
  have to give it again - you can always sign in again with what is stored.
  Only ask for a password again if the site itself rejected it.
- Right after saving, offer to check it works: sign_in_to_site. It takes a
  minute. Call check_site_login once. If it says needs_code, the site texted or
  emailed them a code — ask for it and call submit_site_code.
- Once a site is signed in, we stay signed in, so they won't be asked again
  every time.

WHEN A SITE ASKS FOR A ONE-TIME CODE
- Say where the site sent it, in the words you were given ("to the phone
  ending 96"). Never say just "they sent a code".
- If the code was EMAILED and we can read that mailbox, it is read and
  typed in automatically. Say nothing; wait to be told.
- submit_site_code takes DIGITS ONLY, read out by the caller. Never send
  words, never send "resend", never make a code up.
- If they cannot get the code - the phone is in another room, no message
  arrived, they are out - do NOT say "I'm handling it" and do NOT sit
  waiting. Call stop_waiting_for_code, say plainly that you have stopped,
  and offer to try again later or to have the office ring them.

CHECKING A BASKET BEFORE BUYING
- "What's in my cart?", "how much is it altogether?", "is it going to the
  right address?" -> review_checkout. It reads the checkout page back:
  every item, the delivery address, the card, and the total. It cannot buy
  anything, so it is always safe to look.
- If they want it somewhere else or on a different card, pass a few words
  in deliver_to or pay_with ("Monsey", "the Visa ending 6158"). If the shop
  won't let it be changed, say so and offer to have the office do it.
- Read the WHOLE thing back before asking about buying: each item with its
  price, the address, the card ending, and the total. If the total is more
  than they expected, say so plainly before anything else.
- The cart may hold things they put there themselves weeks ago. If there
  is more in it than they asked for, tell them item by item.

PLACING AN ORDER — do it exactly like a careful person would
1. Find out what they want: the item, how many, and which site. If they're
   vague, use search_site or do_on_website to find it and read them the
   name and price. Get a yes on the exact item before going further.
2. Address: call list_addresses. If they have one, read it back and ask
   "ship it there?" If none, take it down — street, city, state, zip —
   read it back, and save_address.
3. Payment: call list_cards. If they have one, say "the Visa ending 1234?"
   and get a yes. If none, ask if the site has a card saved already. If
   not, the best way is card_setup_code: someone with internet adds the
   card on our secure page, so the number is never said aloud. Only if
   nobody can help, take the card: number in groups of four, expiry,
   security code, name. Read back ONLY the last four digits and expiry,
   never the full number, then save_card. If the number is rejected, ask
   them to read it again.
4. Call draft_order with everything. It tells you what it has.
5. Read the whole thing back in one go: item, quantity, price, address,
   card ending. Then ask exactly: "Should I place this order?" Wait.
6. Only on a clear yes, call confirm_order. Tell them it takes a minute or
   two and stay with them. Call check_order once, then wait for my update.
   - needs_input: it's asking something only they can answer — a code, or
     the total came out higher than expected. Ask them, then
     answer_website_question.
   - placed: read them the confirmation number and total, and say they'll
     get the site's own email too.
   - failed: say what happened and that nothing was charged unless it says
     otherwise. Leave a note for the office.
7. "Cancel that" at any point before it's placed -> cancel_order.
Never read a full card number aloud. Never place anything without the
explicit "yes" to "Should I place this order?"

ANY WEBSITE AT ALL
do_on_website works on sites we've never set up. Give it a plain-English
goal and, if you know it, the site.
- "Check my Verizon bill" -> do_on_website(goal="find the current balance
  and due date", site="verizon")
- "Is my prescription ready at CVS?" -> goal="check if the prescription is
  ready for pickup", site="cvs"
- "How much is a snow blower at Home Depot?" -> goal="find snow blowers and
  their prices", site="homedepot"
Then call get_site_result once. It takes 30 to 90 seconds — say what you are
doing, then wait for my update. If it says needs_input, it's asking a question only they
can answer, usually a code or a choice: ask them, then call
answer_website_question.
It never buys or pays anything. If a goal needs that, it stops and asks.

USING A SIGNED-IN SITE
- "What did I order from Walmart?" / "where's my order?" ->
  check_site_orders, then call get_site_result once I tell you it is done.
  It takes 20 to 40 seconds — say you're looking it up and stay with them.
- "Does Walmart have paper towels?" / "how much is X?" ->
  search_site with the site and what they want, then get_site_result once
  I tell you it is done.
- Read prices and dates plainly. Never read a URL out loud.
- If it says they're signed out, offer sign_in_to_site again.

DISCONNECTING AND DELETING
The caller can undo anything they've set up.
- "Disconnect my work email" -> confirm which one out loud, then
  disconnect_email. Tell them access has been removed at Google's end too.
- "Delete everything" / "remove me from the system" -> take it seriously.
  Say plainly what goes: every connected mailbox, all their call history,
  and their account, and that it cannot be undone. Ask them to say the word
  DELETE to go ahead. Only then call delete_my_account. If they hesitate or
  give any answer other than DELETE, do not do it.
- Never talk them out of it and never ask why. If they want it gone, remove
  it.

MORE THAN ONE MAILBOX
Some callers have several email addresses. list_mailboxes tells you which
they have and which one they use most.
- If they have one, just use it. Don't mention there's only one.
- If they have several and it's obvious which they mean ("my work email"),
  pass that name as the mailbox.
- If it's not obvious, ask once: "Which one — work or personal?" Then use it
  for the rest of the call unless they say otherwise.
- When reading email from a specific mailbox, say which one you're reading.
- Right after they connect a second mailbox, offer once: "Do you want to
  give these short names, like work and personal, so you can just say which
  one you want?" If they say yes, ask for a name for each and use
  name_mailbox. If they say no, drop it and don't ask again.
- If they ever say something like "call this one my work email" or "make
  this my main one", use name_mailbox straight away.

CONNECTING THEIR EMAIL (only if they aren't connected yet)
If check_email says their account has no email linked, offer to connect it.
FIRST ask whether someone who uses the internet can help them - a son,
daughter, neighbour, or anyone with a smartphone or computer. If yes, call
email_connect_code and read out what it gives you. That is the preferred
way: they never have to say a password out loud. Only if there is nobody
who can help, do it on this call:
1. Ask for their email address. Have them spell the part before the @.
   Read it back and get a yes.
2. Ask for their password. This is the part that goes wrong most, so be
   careful:
   - Tell them to say it one character at a time, saying "capital" before a
     capital letter, and naming symbols out loud ("exclamation mark", "at
     sign", "hyphen").
   - Read the whole thing back character by character, saying "capital"
     where it applies, and get a clear yes before you use it.
   - If any character is unclear, ask about that one character again rather
     than the whole password.
   - If they'd rather not say it out loud, offer to text them a link where
     they can type it: send_password_link. That's often easier and always
     more accurate.
3. Call connect_email. It takes up to a minute — tell them you're working
   on it and stay on the line.
4. Call check_connect once. Google will ask them to prove
   it's them. It picks the method, and there are several — just do what
   check_connect tells you:
   - needs_tap: a notification went to their phone. Tell them to unlock it,
     tap Yes, and choose the number you give them.
   - needs_code: read out whatever check_connect says — it will tell you
     whether the code came by text, by phone call, or is in their
     authenticator app — then ask for the code and call submit_code.
   Whenever they can't do the method Google chose — no smartphone, phone in
   another room, no authenticator app, didn't get the text — call
   try_another_way. Google will offer a different method and check_connect
   will tell you the new one. You can do this more than once.
   Google sometimes asks twice. That's normal; keep going.
   If a code doesn't work, ask them to read it again rather than assuming
   you misheard.
5. When it says done, tell them their email is connected and offer to read
   their new messages.
6. If Google says the password is wrong, you may try TWICE more, no more:
   ask them to say it again slowly, read it back, and call connect_email
   again. Say plainly that you may have misheard rather than blaming them.
   After a third wrong attempt, stop — Google can lock the account. Say
   you'll have someone call them back, and move on.
7. Any other failure: apologise once, say you've left a note for the office,
   and move on. Do not ask for the password again.
Never repeat their password back to anyone else, never say it after the
sign-in is finished, and never put it in a text message.

TEXTING
- You can text the caller. Use send_text for an address, a phone number, a
  link, or anything long or fiddly. Say you are texting it rather than
  reading out a long string.
- If the caller's email isn't connected yet, use text_setup_link — it sends
  them a link they tap on their phone to connect their email. Tell them to
  tap it and call back when done.
- Texts go to the number they are calling from unless they give another one.
- The topic rules above apply to texts exactly as they do to speech.

LOOKING THINGS UP
- Use web_search for anything outside their email and calendar: a business's
  address, phone number or hours, how far somewhere is, a fact, a price,
  what's open nearby.
- ANSWER FROM WHAT YOU KNOW FIRST, and do not reach for a tool you do not
  need. You know a great deal already. If they ask something general - how
  a kind of appliance usually works, what a word means, how something is
  normally done, a bit of history, how to do an everyday thing - just say
  it. No search, no waiting, no "one moment". A caller waited a minute and
  a half for something you could have answered at once.
  Looking it up is SLOWER and worse when you already know the answer. Only
  reach for web_search when the thing genuinely changes, when being wrong
  would matter, or when they ask you to check.

- BE STRAIGHT ABOUT HOW SURE YOU ARE. There are three kinds of question
  and they are handled differently:
  1. General knowledge - answer it. Say plainly how confident you are:
     "on most Frigidaire models you hold Control Lock and Power for five
     seconds, though it varies by model."
  2. Anything about THIS caller - who an email is from, what they ordered,
     what is in their calendar - never from memory, always from a tool.
     Guessing about their own things is how you tell them a wrong email
     address.
  3. Anything exact or that changes - a price, opening hours, a specific
     model's exact steps, whether something is in stock - you may say what
     you believe, but say it is worth checking, and offer to check.

- NEVER dress a guess up as a source. Do not say "the page says" unless a
  page actually came back and said it. Do not give the same question three
  different confident answers - that is how they know you are guessing.

- WHAT THEY SAY IS WHAT THEY WANT, NOT HOW TO GET IT. "Search that",
  "look it up", "check online", "Google it" - they are not choosing a tool
  for you. They do not know you have tools. They are telling you the last
  answer was not good enough and they want a better one. Deciding HOW is
  your job: answer from what you know, ask_ai, or look_it_up - whichever
  actually gets them the right answer soonest.
  In particular, if a lookup just failed, "search again" does NOT mean run
  that same search again. It means find another way, or tell them honestly
  that you cannot get it. Never repeat something that has already failed
  simply because they used the word "search".

- WHEN YOU ASK OR LOOK SOMETHING UP, WRITE THE WHOLE QUESTION. Those
  tools cannot see this call. Put the make, the model number and what they
  actually want into the question every single time, even if they told you
  ten seconds ago. A caller asked about his ice maker and the question
  that went out was "how to turn on the icemaker for the" - so he got a
  generic answer that missed the first step.

- NOT SURE? ASK first, don't browse. ask_ai puts the question to a bigger
  model and comes back in a couple of seconds. Use it the moment you are
  less than certain about anything general - an appliance, a word, how
  something is usually done. It is nearly instant, so just call it and
  answer; no "one moment", no waiting.
  Three callers were told wrong fridge instructions because you answered
  from your own guess instead of asking.

- WHEN IT HAS TO BE RIGHT, USE look_it_up - one tool, and it searches and
  reads the real pages itself. A specific model's steps, today's price,
  this week's hours: look_it_up, then one short sentence and silence until
  I give you the answer.
  Do NOT use web_search for those. A search alone gives you headlines, and
  three separate callers have been told wrong fridge instructions built
  out of headlines. web_search is for a phone number, an address, a quick
  fact - things a headline actually contains.
- Do not ask them to go and look at their own appliance and report back.
  They rang you to be told.
- The caller is in the New York area. For anything local, pass their area in
  the "near" field.
- Give the answer in one or two spoken sentences. Read a phone number in
  groups, slowly. Never read out a URL.
- If they ask for directions, tell them roughly how long it takes and from
  which direction, then offer to text them the address rather than reading
  turn-by-turn steps.

FINDING EMAIL — pick the right tool
- "My last few emails", "what came in today", "what's new", "read or
  unread, doesn't matter" -> recent_email. This is the common one.
- "Anything new?", "any unread?" -> check_email. Unread only.
- A person, a topic, an old thread, an attachment -> search_email, using
  real Gmail syntax: "from:chaim", "invoice", "after:2026/08/01". Never
  invent an operator - if you aren't sure of the syntax, use recent_email
  and read from that instead.
- If a search returns nothing, do not just say you found nothing. Try a
  different, broader wording once, and tell the caller what you tried.
- To get somebody's address, use find_contact with their name.

DOING SOMETHING WITH AN EMAIL
Once you have read a list out, they can act on any of them by number.
- "Reply and tell him yes" -> write the reply, read the WHOLE thing back,
  ask "should I send it?", and only then call reply_to_email.
- "Send that to my son" -> forward_email. Say who it is going to and get a
  yes first: forwarding sends the whole original to another person.
- "Get that out of my inbox" -> tidy_email with archive. "Flag that" ->
  star. "That's junk" -> spam. "Bin it" -> trash.
  Nothing there is permanent - trash is recoverable for 30 days and every
  label can be put back. Still say what you are about to do for trash and
  spam, and tell them afterwards that it can be undone.
- "What does the invoice say?" -> read_attachment for a file attached to
  the message, or read_document if it is a link inside the message.
- "Write it but don't send it" -> save_draft.
Always say which message you are acting on - "the one from the pharmacy" -
so they can stop you if you have the wrong one.

WHAT YOU KNOW ABOUT A MESSAGE
Every message you are given comes with who it is from, when it arrived,
which tab it landed in, and whether it is read. Say the date whenever you
describe a message - "from Coinbase, last Tuesday" - so they can tell
straight away if you are reading something old.
Never say an email address, a date, or a subject you were not given. If
they ask for something you do not have, say plainly that you do not have
it and offer to open the message with read_email. Guessing an address is
worse than useless: they use it to decide whether an email is genuine.

WHICH MAILBOX
If they have more than one, ask which before you read anything. The tools
will tell you when this applies - when they do, ask the question and wait.
Never pick one for them silently.
""".strip())

    async def _which_mailbox(self, mailbox: str = "") -> str:
        """Returns an instruction to ask the caller which mailbox, or "" to
        carry on. Asking is enforced here rather than left to the prompt,
        because silently guessing looks like reading the wrong person's
        mail."""
        if mailbox or self.mailbox:
            return ""
        if self._mailboxes is None:
            try:
                rows = await backend_get("/mailboxes",
                                         account_id=self.account_id)
            except Exception:
                self._mailboxes = []
                return ""
            self._mailboxes = [(r.get("label") or r.get("email") or "")
                               for r in rows]
            if len(rows) == 1:
                self.mailbox = rows[0].get("email", "")
        if len(self._mailboxes) > 1:
            return ("They have more than one mailbox: "
                    + ", ".join(self._mailboxes)
                    + ". Ask which one they want, in one short question, "
                      "then call this again passing that name as mailbox. "
                      "Do not guess and do not read anything yet.")
        return ""

    async def _watch(self, kind, fetch, describe):
        """Poll a background job and make the agent speak when it changes."""
        last = None
        for _ in range(100):                     # about five minutes
            await asyncio.sleep(3)
            try:
                d = await fetch()
            except Exception:
                continue
            state = d.get("state", "")
            key = (state, (d.get("message") or "")[:80])
            if key == last:
                continue
            last = key
            line = describe(d)
            if line:
                try:
                    sess = getattr(self, "session", None)
                    if sess:
                        await sess.generate_reply(
                            instructions=(f"Update the caller now, in one "
                                          f"short sentence, in English: "
                                          f"{line}"))
                except Exception as e:
                    log.warning(f"watch speak failed: {e}")
            if state in ("done", "failed", "placed", "cancelled"):
                break

    def _start_watch(self, kind, fetch, describe):
        try:
            # Stop whatever was being watched before. Each watcher can make
            # the agent speak, and they were never cancelled - after three
            # lookups there were three of them running at once, any of
            # which could talk over the others.
            for old in getattr(self, "_watchers", []):
                if not old.done():
                    old.cancel()
            t = asyncio.create_task(self._watch(kind, fetch, describe))
            self._watchers = [t]
        except Exception as e:
            log.warning(f"could not start watcher: {e}")

    def _watch_job(self, label: str):
        # (site jobs)
        jid = getattr(self, "job_id", None)
        if not jid:
            return

        async def fetch():
            return await backend_get("/jobs/status", job_id=jid)

        def describe(d):
            st, msg = d.get("state", ""), d.get("message", "")
            kind = d.get("kind", "")
            if st == "needs_code":
                return f"{msg} Ask them for it."
            if st == "needs_input":
                return f"It needs to know: {msg}. Ask them."
            if st == "waiting":
                return "Say it's queued and will start in a moment."
            if st == "done":
                if kind == "site_login":
                    return ("Say they are signed in now, then start what "
                            "they originally asked for again. Do not ask "
                            "get_site_result about the sign-in itself.")
                if kind == "browse":
                    # the message IS the answer - don't make them wait
                    # through another round trip to hear it
                    return (f"Tell them this now, in your own words, and "
                            f"say where it came from if it names a model: "
                            f"{msg}")
                return ("Say it's done, then get the details with "
                        "get_site_result.")
            if st == "failed":
                if kind == "browse":
                    if d.get("reason") == "bot_check":
                        return ("Say that page is blocking us and you'll "
                                "try a different source. You have NOTHING "
                                "from it - do not describe what it said.")
                    return (f"Say you couldn't open that page: {msg}. You "
                            f"have NOTHING from it - do not describe what "
                            f"it said. Offer to try another source.")
                if d.get("reason") == "signed_out":
                    return ("Tell them the saved session has expired and "
                            "that you'll sign in again with the login they "
                            "already gave you - do not ask for a password. "
                            "Then call sign_in_to_site.")
                if d.get("reason") == "bad_password":
                    return (f"Say the site didn't accept the password: "
                            f"{msg}")
                if d.get("reason") == "login_needed":
                    return (f"Say plainly that this site needs their own "
                            f"login before you can go on - it is not "
                            f"blocking us - and offer to take it now. If "
                            f"they agree, ask for the username first. "
                            f"Details: {msg}")
                if d.get("reason") in ("rate_limited", "site_refused",
                                       "site_error"):
                    return (f"Say this plainly, in your own words, and make "
                            f"clear it is not their fault: {msg}")
                if d.get("reason") in ("bad_code", "no_code"):
                    return (f"Say this plainly, in your own words: {msg} "
                            f"Do not ask for their password.")
                return f"Say it didn't work: {msg}."
            return None

        self._start_watch("job", fetch, describe)

    @function_tool
    async def verify_pin(self, context: RunContext, pin: str):
        """Check the caller's PIN. Must be called before any email action."""
        # Compared on the backend, never here. /accounts does not carry a
        # "pin" key, so this used to fall back to "1234" for every caller.
        try:
            r = await backend_post("/accounts/verify_pin", {
                "account_id": self.account_id, "pin": pin})
        except Exception as e:
            log.error(f"pin check failed: {e}")
            return "Could not verify right now."
        if r.get("ok"):
            self.verified = True
            return "PIN correct. The caller is verified."
        return "PIN incorrect."

    @function_tool
    @auto_report("email")
    async def check_email(self, context: RunContext, how_many: int = 5,
                          mailbox: str = "", primary_only: bool = False):
        """Get the caller's unread emails. Set mailbox to their name for it
        ("work", "personal") if they have more than one. Set primary_only
        to true if they only want real inbox mail, not Promotions."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        ask = await self._which_mailbox(mailbox)
        if ask:
            return ask
        try:
            data = await backend_get(
                "/test/unread", account_id=self.account_id, limit=how_many,
                which=mailbox or self.mailbox, primary_only=primary_only)
        except Exception as e:
            log.error(f"unread failed: {e}")
            return (google_refusal(e, "their email")
                    or "I couldn't reach the mailbox just now.")

        self.last_list = data.get("messages", [])
        await log_turn(self.call_id, "tool", "unread check",
                       "check_email", backend_get.last_ms)
        lines = [f"{data.get('unread_count', 0)} unread."]
        for i, m in enumerate(self.last_list, 1):
            lines.append(_describe(i, m))
        lines.append("Say when each one arrived. If any are marked "
                     "Promotions or Updates, mention that they came from "
                     "that tab, not the main inbox. " + ADDRESS_RULE)
        return "\n".join(lines)

    @function_tool
    @auto_report("email")
    async def read_email(self, context: RunContext, which: int):
        """Read the full text of one email, by its number in the last list."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        if not self.last_list:
            return "No list loaded. Call check_email first."
        if which < 1 or which > len(self.last_list):
            return f"Pick a number between 1 and {len(self.last_list)}."
        msg_id = self.last_list[which - 1]["id"]
        try:
            data = await backend_get(
                "/test/read", account_id=self.account_id, msg_id=msg_id,
                which=self.mailbox)
        except Exception as e:
            log.error(f"read failed: {e}")
            return (google_refusal(e, "their email")
                    or "I couldn't open that message.")
        body = " ".join((data.get("body") or "").split())[:1500]
        self.last_email_body = " ".join((data.get("body") or "").split())
        return (f"From {data.get('from')}. Subject {data.get('subject')}. "
                f"{body}"
                f" -- If there is a link to an invoice, receipt, statement "
                f"or other document in here and they want to know what it "
                f"says, call read_document with that exact link.")

    @function_tool
    @auto_report("email")
    async def search_email(self, context: RunContext, query: str,
                           how_many: int = 5, mailbox: str = ""):
        """Search the whole mailbox using Gmail search syntax. Use this for
        anything that isn't the unread list — a person, a topic, an old
        thread. Examples: 'from:chaim', 'invoice', 'from:amazon'."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        ask = await self._which_mailbox(mailbox)
        if ask:
            return ask
        try:
            data = await backend_get("/test/search",
                                     account_id=self.account_id,
                                     q=query, limit=how_many,
                                     which=mailbox or self.mailbox)
        except Exception as e:
            log.error(f"search failed: {e}")
            return (google_refusal(e, "their email")
                    or "The search didn't go through.")

        msgs = data.get("messages", [])
        if not msgs:
            return (f"Nothing matched '{query}'. Tell the caller what you "
                    f"searched and try a broader wording once.")
        self.last_list = msgs
        await log_turn(self.call_id, "tool", f"search: {query}",
                       "search_email", backend_get.last_ms)
        lines = [f"{len(msgs)} found."]
        for i, m in enumerate(msgs, 1):
            lines.append(_describe(i, m))
        lines.append(ADDRESS_RULE)
        return "\n".join(lines)

    @function_tool
    @auto_report("email")
    async def recent_email(self, context: RunContext, how_many: int = 5,
                           mailbox: str = ""):
        """The caller's most recent emails, read AND unread, newest first.
        Use this whenever they ask for their last few emails, what came in
        today, or what's new - check_email only shows unread ones."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        ask = await self._which_mailbox(mailbox)
        if ask:
            return ask
        try:
            data = await backend_get("/test/search",
                                     account_id=self.account_id,
                                     q="in:inbox", limit=how_many,
                                     which=mailbox or self.mailbox,
                                     newest_first=True)
        except Exception as e:
            log.error(f"recent failed: {e}")
            return (google_refusal(e, "their email")
                    or "I couldn't reach the mailbox just now.")
        msgs = data.get("messages", [])
        if not msgs:
            return "Nothing in the inbox at all."
        self.last_list = msgs
        await log_turn(self.call_id, "tool", f"recent {len(msgs)}",
                       "recent_email", backend_get.last_ms)
        lines = [f"The {len(msgs)} most recent, newest first:"]
        for i, m in enumerate(msgs, 1):
            lines.append(_describe(i, m))
        lines.append("Read them out newest first and say when each arrived. "
                     + ADDRESS_RULE)
        return "\n".join(lines)

    def _pick(self, which: int):
        """Turn "number three" into a message, or say what went wrong."""
        if not self.last_list:
            return None, ("No list loaded. Call recent_email, check_email "
                          "or search_email first.")
        if which < 1 or which > len(self.last_list):
            return None, (f"Pick a number between 1 and "
                          f"{len(self.last_list)} from the list you read "
                          f"out.")
        return self.last_list[which - 1], ""

    @function_tool
    @auto_report("email")
    async def reply_to_email(self, context: RunContext, which: int,
                             body: str, caller_said: str = ""):
        """Reply to one of the emails in the list you just read out.

        Read the whole reply back to them first and ask "should I send
        it?". Only call this with caller_said set to what they actually
        answered, once they have clearly said yes."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        msg, problem = self._pick(which)
        if problem:
            return problem
        said = (caller_said or "").strip().lower()
        if not any(w in said for w in ("yes", "yeah", "send it", "go ahead",
                                       "ok", "okay", "correct", "sure")):
            return (f"Do not send yet. Read this back to them word for "
                    f"word and ask 'should I send it?': {body}")
        try:
            d = await backend_post("/email/reply", {
                "account_id": self.account_id, "msg_id": msg["id"],
                "body": body, "which": self.mailbox})
        except Exception as e:
            log.error(f"reply failed: {e}")
            return "The reply didn't go through."
        await log_turn(self.call_id, "tool",
                       f"replied to {d.get('to', '')}", "reply_to_email")
        return f"Sent to {d.get('to')}. Tell them it has gone."

    @function_tool
    @auto_report("email")
    async def forward_email(self, context: RunContext, which: int, to: str,
                            note: str = "", caller_said: str = ""):
        """Pass one of the emails on to somebody else.

        Forwarding sends the whole original message to another person, so
        read back WHO it is going to and get a clear yes first."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        msg, problem = self._pick(which)
        if problem:
            return problem
        said = (caller_said or "").strip().lower()
        if not any(w in said for w in ("yes", "yeah", "send it", "go ahead",
                                       "ok", "okay", "correct", "sure")):
            return (f"Do not forward yet. Say back to them that this sends "
                    f"the whole message from {msg.get('from', '')} to {to}, "
                    f"ask if that is right, and only call this again once "
                    f"they say yes.")
        try:
            d = await backend_post("/email/forward", {
                "account_id": self.account_id, "msg_id": msg["id"],
                "to": to, "note": note, "which": self.mailbox})
        except Exception as e:
            log.error(f"forward failed: {e}")
            return "The forward didn't go through."
        await log_turn(self.call_id, "tool", f"forwarded to {to}",
                       "forward_email")
        return f"Forwarded to {d.get('to')}. Tell them it has gone."

    @function_tool
    @auto_report("email")
    async def tidy_email(self, context: RunContext, which_ones: str,
                         action: str):
        """Do something with messages from the list you read out.

        which_ones is the numbers, like "1,3". action is one of:
          archive     take it out of the inbox but keep it
          star        flag it to come back to
          important   mark it important
          spam        move it to spam
          trash       put it in the bin, recoverable for 30 days
          unarchive, unstar, not_spam, untrash - undo any of those

        Nothing here deletes anything for good. For trash and spam, say
        what you are about to do and get a yes first."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        ids = []
        for part in (which_ones or "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                msg, problem = self._pick(int(part))
            except ValueError:
                continue
            if problem:
                return problem
            ids.append(msg["id"])
        if not ids:
            return ("Ask which messages they mean, by number from the list "
                    "you read out.")
        try:
            d = await backend_post("/email/action", {}, params={
                "account_id": self.account_id, "msg_ids": ",".join(ids),
                "action": action, "which": self.mailbox})
        except Exception as e:
            log.error(f"tidy failed: {e}")
            return f"That didn't work: {str(e)[:150]}"
        await log_turn(self.call_id, "tool",
                       f"{action} on {len(ids)} message(s)", "tidy_email")
        undo = d.get("undo", "")
        return (f"Done - {action} on {d.get('changed', 0)} message(s)"
                + (f" ({undo})" if undo else "")
                + ". Tell them plainly what happened, and that it can be "
                  "undone if they change their mind.")

    @function_tool
    @auto_report("email")
    async def read_attachment(self, context: RunContext, which: int,
                              looking_for: str = "what this says"):
        """Read a document attached to one of the emails - an invoice, a
        statement, a bill. Works on PDFs and plain text."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        msg, problem = self._pick(which)
        if problem:
            return problem
        try:
            listing = await backend_get("/email/attachments",
                                        account_id=self.account_id,
                                        msg_id=msg["id"],
                                        which=self.mailbox)
        except Exception as e:
            log.error(f"attachments failed: {e}")
            return "Couldn't check what was attached."
        atts = listing.get("attachments", [])
        if not atts:
            return ("Nothing is attached to that one. If the email has a "
                    "LINK to a document, read the message and use "
                    "read_document with that link instead.")
        best = atts[0]
        for a in atts:
            if (a.get("mime") or "").endswith("pdf"):
                best = a
                break
        try:
            got = await backend_get("/email/attachment",
                                    account_id=self.account_id,
                                    msg_id=msg["id"],
                                    attachment_id=best["id"],
                                    which=self.mailbox)
        except Exception as e:
            log.error(f"attachment read failed: {e}")
            return "Couldn't open that attachment."
        text = (got.get("text") or "").strip()
        if not text:
            return (f"Couldn't read {best.get('filename', 'that file')}: "
                    f"{got.get('error', 'unreadable')}. Say so plainly.")
        await log_turn(self.call_id, "tool",
                       f"read attachment {best.get('filename', '')}",
                       "read_attachment")
        return (f"{best.get('filename', 'document')} says: {text[:3000]}"
                f" -- Answer their question from THIS text only, in plain "
                f"spoken words. They asked about: {looking_for}. If it "
                f"isn't in here, say so.")

    @function_tool
    @auto_report("email")
    async def save_draft(self, context: RunContext, to: str, subject: str,
                         body: str):
        """Save an email as a draft instead of sending it, so they or the
        office can finish it later."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            d = await backend_post("/email/draft", {
                "account_id": self.account_id, "to": to,
                "subject": subject, "body": body, "which": self.mailbox})
        except Exception as e:
            log.error(f"draft failed: {e}")
            return "That didn't save."
        await log_turn(self.call_id, "tool", f"drafted to {to}",
                       "save_draft")
        return (f"Saved as a draft to {d.get('to')}. Tell them it is "
                f"waiting in their drafts, not sent.")

    @function_tool
    @auto_report("contacts")
    async def contact_details(self, context: RunContext, name: str):
        """Look someone up in the caller's Google Contacts: phone numbers,
        email, address and birthday. Use for "what's my son's number",
        "where does Mrs Klein live", "when is Moshe's birthday"."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            d = await backend_get("/contacts/search",
                                  account_id=self.account_id, name=name,
                                  which=self.mailbox)
        except Exception as e:
            log.error(f"contacts failed: {e}")
            return (google_refusal(e, "their contacts")
                    or "The contacts lookup didn't go through.")
        people = d.get("contacts", [])
        if not people:
            return (f"Nobody called {name} in their contacts. Ask them to "
                    f"say the name another way, or spell it.")
        lines = []
        for i, c in enumerate(people, 1):
            bits = [c.get("name") or "(no name)"]
            for ph in c.get("phones", []):
                kind = f" ({ph['type']})" if ph.get("type") else ""
                bits.append(f"phone {ph['number']}{kind}")
            if c.get("emails"):
                bits.append("email " + ", ".join(c["emails"]))
            if c.get("address"):
                bits.append("address " + c["address"])
            if c.get("birthday"):
                bits.append("birthday " + c["birthday"])
            lines.append(f"{i}. " + "; ".join(bits))
        return ("\n".join(lines) + "\nIf more than one could be who they "
                "mean, ask which. Say phone numbers in groups of digits, "
                "slowly.")

    @function_tool
    @auto_report("contacts")
    async def save_contact(self, context: RunContext, name: str,
                           phone: str = "", email: str = "",
                           caller_said: str = ""):
        """Add a new person to the caller's Google Contacts. Read the name
        and number back first, and only call with caller_said set to what
        they answered once they clearly say yes."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        if not (phone.strip() or email.strip()):
            return "Ask for a phone number or an email address to save."
        said = (caller_said or "").strip().lower()
        if not any(w in said for w in ("yes", "yeah", "correct", "right",
                                       "go ahead", "save", "ok", "sure")):
            digits = " ".join(ch for ch in phone if ch.isdigit())
            return (f"Do not save yet. Read back: {name}"
                    + (f", phone {digits}" if digits else "")
                    + (f", email {email}" if email else "")
                    + ". Ask if that's right.")
        try:
            await backend_post("/contacts/add", {
                "account_id": self.account_id, "name": name, "phone": phone,
                "email": email, "which": self.mailbox})
        except Exception as e:
            log.error(f"save contact failed: {e}")
            return (google_refusal(e, "their contacts")
                    or "That didn't save.")
        await log_turn(self.call_id, "tool", f"saved contact {name}",
                       "save_contact")
        return f"Saved {name} to their contacts. Tell them."

    @function_tool
    @auto_report("drive")
    async def find_in_drive(self, context: RunContext, words: str = ""):
        """Find a document in the caller's Google Drive, including ones
        other people shared with them. words is what it's called or what
        it's about, like "lease" or "school letter". Leave words empty for
        the most recent files."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            d = await backend_get("/drive/search",
                                  account_id=self.account_id, words=words,
                                  which=self.mailbox)
        except Exception as e:
            log.error(f"drive search failed: {e}")
            return (google_refusal(e, "their Google Drive")
                    or "The Drive search didn't go through.")
        self.last_files = d.get("files", [])
        if not self.last_files:
            return (f"Nothing in their Drive matches '{words}'. Ask what else "
                    f"it might be called.")
        lines = []
        for i, f in enumerate(self.last_files, 1):
            src = f" from {f['from']}" if f.get("from") else ""
            lines.append(f"{i}. {f['name']} - a {f['kind']}{src}, changed "
                         f"{f.get('changed', '')}"
                         + ("" if f.get("readable") else
                            " (can't be read aloud)"))
        return ("\n".join(lines) + "\nTell them what you found by name and "
                "ask which one to read. Use read_drive_file with its number.")

    @function_tool
    @auto_report("drive")
    async def read_drive_file(self, context: RunContext, which: int,
                              looking_for: str = "what it says"):
        """Read one of the files find_in_drive just listed, by its number."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        if not self.last_files:
            return "Call find_in_drive first."
        if which < 1 or which > len(self.last_files):
            return f"Pick a number from 1 to {len(self.last_files)}."
        f = self.last_files[which - 1]
        try:
            d = await backend_get("/drive/read", account_id=self.account_id,
                                  file_id=f["id"], which=self.mailbox)
        except Exception as e:
            log.error(f"drive read failed: {e}")
            return (google_refusal(e, "their Google Drive")
                    or "Couldn't open that file.")
        text = (d.get("text") or "").strip()
        if not text:
            return (f"Couldn't read {f['name']}: "
                    f"{d.get('error', 'no words in it')}. Say so plainly.")
        await log_turn(self.call_id, "tool", f"read drive file {f['name']}",
                       "read_drive_file")
        return (f"{f['name']} says: {text[:3000]} -- Answer from THIS text "
                f"only, in plain spoken words. They asked about: "
                f"{looking_for}. If it isn't in here, say so. Don't read "
                f"out long tables; sum them up and offer detail.")

    def _drive_file(self, which: int):
        if not self.last_files:
            return None, "Call find_in_drive first, so you know which file."
        if which < 1 or which > len(self.last_files):
            return None, f"Pick a number from 1 to {len(self.last_files)}."
        return self.last_files[which - 1], ""

    def _remember_file(self, f: dict) -> int:
        """A file just made goes to the top of the list, as number 1."""
        self.last_files = [f] + [x for x in self.last_files
                                 if x.get("id") != f.get("id")]
        return 1

    @function_tool
    @auto_report("drive")
    async def create_document(self, context: RunContext, title: str,
                              text: str):
        """Make a new Google Doc in the caller's Drive - a letter, a list,
        notes. Write the text out properly first, read it back, and make
        it once they're happy."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            d = await backend_post("/drive/doc/create", {
                "account_id": self.account_id, "title": title, "text": text,
                "which": self.mailbox})
        except Exception as e:
            log.error(f"doc create failed: {e}")
            return (google_refusal(e, "making documents")
                    or "The document didn't get made.")
        n = self._remember_file(d["file"])
        await log_turn(self.call_id, "tool", f"made doc {title}",
                       "create_document")
        return (f"Made '{d['file']['name']}' in their Google Drive (file "
                f"number {n}). Tell them. Offer to email it as a PDF with "
                f"send_drive_file if that helps.")

    @function_tool
    @auto_report("drive")
    async def create_spreadsheet(self, context: RunContext, title: str,
                                 columns: str, rows: str = ""):
        """Make a new Google Sheet. columns: the headings separated by
        semicolons, like "Name; Phone; Address". rows: one row per line,
        cells separated by semicolons, in the same order. rows can be empty
        for a blank sheet with headings."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        grid = [cells(line) for line in (rows or "").splitlines()
                if line.strip()]
        try:
            d = await backend_post("/drive/sheet/create", {
                "account_id": self.account_id, "title": title,
                "columns": cells(columns), "rows": grid,
                "which": self.mailbox})
        except Exception as e:
            log.error(f"sheet create failed: {e}")
            return (google_refusal(e, "making spreadsheets")
                    or "The spreadsheet didn't get made.")
        n = self._remember_file(d["file"])
        await log_turn(self.call_id, "tool", f"made sheet {title}",
                       "create_spreadsheet")
        return (f"Made the spreadsheet '{d['file']['name']}' with "
                f"{len(grid)} rows (file number {n}). Tell them.")

    @function_tool
    @auto_report("drive")
    async def add_to_document(self, context: RunContext, which: int,
                              text: str, caller_said: str = ""):
        """Add text to the end of a Google Doc from the list. Read back
        exactly what will be added, and only call with caller_said set to
        their answer once they clearly say yes."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        f, problem = self._drive_file(which)
        if problem:
            return problem
        if not said_yes(caller_said):
            return (f"Do not change it yet. Say: 'I'll add this to the end "
                    f"of {f['name']}: {text}. Shall I?' and wait for yes.")
        try:
            await backend_post("/drive/doc/add", {
                "account_id": self.account_id, "file_id": f["id"],
                "text": text, "which": self.mailbox})
        except Exception as e:
            log.error(f"doc add failed: {e}")
            return (google_refusal(e, "changing documents")
                    or "That change didn't go through.")
        await log_turn(self.call_id, "tool", f"added to {f['name']}",
                       "add_to_document")
        return f"Added to {f['name']}. Tell them it's done."

    @function_tool
    @auto_report("drive")
    async def change_document_words(self, context: RunContext, which: int,
                                    find: str, replace_with: str,
                                    all_of_them: bool = False,
                                    caller_said: str = ""):
        """Change words in a Google Doc from the list: find is the exact
        words there now, replace_with what they should become. Read the
        change back and get a clear yes first."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        f, problem = self._drive_file(which)
        if problem:
            return problem
        if not said_yes(caller_said):
            return (f"Do not change it yet. Say: 'In {f['name']} I'll change "
                    f"\"{find}\" to \"{replace_with}\". Shall I?' and wait "
                    f"for yes.")
        try:
            d = await backend_post("/drive/doc/replace", {
                "account_id": self.account_id, "file_id": f["id"],
                "find": find, "replace_with": replace_with,
                "all_of_them": all_of_them, "which": self.mailbox})
        except Exception as e:
            log.error(f"doc replace failed: {e}")
            return (google_refusal(e, "changing documents")
                    or "That change didn't go through.")
        if not d.get("found"):
            return (f"The words \"{find}\" aren't in {f['name']}. Use "
                    f"read_drive_file to hear what it actually says, then "
                    f"try again with the exact words.")
        if not d.get("changed"):
            return (f"Those words appear {d['found']} times. Nothing is "
                    f"changed yet. Ask whether to change every one; if yes, "
                    f"call again with all_of_them true.")
        await log_turn(self.call_id, "tool", f"changed words in {f['name']}",
                       "change_document_words")
        return (f"Changed it in {f['name']}"
                + (f" ({d['times']} places)" if d.get("times", 1) > 1 else "")
                + ". Tell them it's done.")

    @function_tool
    @auto_report("drive")
    async def read_spreadsheet(self, context: RunContext, which: int):
        """Hear a Google Sheet from the list as rows, with its row numbers,
        before changing anything in it."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        f, problem = self._drive_file(which)
        if problem:
            return problem
        try:
            d = await backend_get("/drive/sheet", account_id=self.account_id,
                                  file_id=f["id"], which=self.mailbox)
        except Exception as e:
            log.error(f"sheet read failed: {e}")
            return (google_refusal(e, "reading spreadsheets")
                    or "Couldn't open that spreadsheet.")
        cols = d.get("columns", [])
        lines = [f"Columns: {'; '.join(map(str, cols)) or '(none)'}"]
        for r in d.get("rows", []):
            lines.append(f"row {r['row']}: " + "; ".join(map(str, r["cells"])))
        more = d.get("total_rows", 0) - len(d.get("rows", []))
        if more > 0:
            lines.append(f"...and {more} more rows")
        return ("\n".join(lines) + "\nThe row numbers are for YOU, to "
                "use with change_spreadsheet_cell - talk to the caller about "
                "the contents, not row numbers. Summarise; don't read a big "
                "table out cell by cell.")

    @function_tool
    @auto_report("drive")
    async def add_spreadsheet_row(self, context: RunContext, which: int,
                                  values: str, caller_said: str = ""):
        """Add a row at the bottom of a Google Sheet. values: the cells in
        column order, separated by semicolons. Call read_spreadsheet first
        so you know the columns, read the row back, and get a yes."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        f, problem = self._drive_file(which)
        if problem:
            return problem
        if not said_yes(caller_said):
            return (f"Do not add it yet. Read back the new row for "
                    f"{f['name']}: {values}. Ask 'Shall I add that?'")
        try:
            await backend_post("/drive/sheet/add_row", {
                "account_id": self.account_id, "file_id": f["id"],
                "values": cells(values), "which": self.mailbox})
        except Exception as e:
            log.error(f"sheet add failed: {e}")
            return (google_refusal(e, "changing spreadsheets")
                    or "That row didn't get added.")
        await log_turn(self.call_id, "tool", f"row added to {f['name']}",
                       "add_spreadsheet_row")
        return f"Added the row to {f['name']}. Tell them."

    @function_tool
    @auto_report("drive")
    async def change_spreadsheet_cell(self, context: RunContext, which: int,
                                      row: int, column: str, value: str,
                                      caller_said: str = ""):
        """Change one cell in a Google Sheet. row is the row number from
        read_spreadsheet; column is the heading, like "Phone". Say back
        what it is now and what it will become, and get a yes first."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        f, problem = self._drive_file(which)
        if problem:
            return problem
        if not said_yes(caller_said):
            return (f"Do not change it yet. Say what the {column} is now in "
                    f"that row and that it will become {value}, and ask "
                    f"'Shall I change it?'")
        try:
            d = await backend_post("/drive/sheet/update", {
                "account_id": self.account_id, "file_id": f["id"],
                "row": row, "column": column, "value": value,
                "which": self.mailbox})
        except Exception as e:
            log.error(f"sheet update failed: {e}")
            return (google_refusal(e, "changing spreadsheets")
                    or "That change didn't go through.")
        await log_turn(self.call_id, "tool",
                       f"changed {f['name']} row {row} {column}",
                       "change_spreadsheet_cell")
        was = f" (it was {d['was']})" if d.get("was") else ""
        return f"Changed {d.get('column') or column} to {value}{was}. Tell them."

    @function_tool
    @auto_report("drive")
    async def make_editable_copy(self, context: RunContext, which: int):
        """Turn a Word, Excel or PDF file from the list into a Google Doc or
        Sheet that can be changed. The original is left exactly as it was."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        f, problem = self._drive_file(which)
        if problem:
            return problem
        try:
            d = await backend_post("/drive/copy_editable", {
                "account_id": self.account_id, "file_id": f["id"],
                "which": self.mailbox})
        except Exception as e:
            log.error(f"copy failed: {e}")
            return (google_refusal(e, "making a copy")
                    or "The copy didn't get made.")
        n = self._remember_file(d["file"])
        await log_turn(self.call_id, "tool", f"editable copy of {f['name']}",
                       "make_editable_copy")
        return (f"Made an editable copy called '{d['file']['name']}' (now "
                f"file number {n}). The original is untouched. Make the "
                f"change in number {n}.")

    @function_tool
    @auto_report("drive")
    async def save_as_pdf(self, context: RunContext, which: int):
        """Save a PDF copy of a Google Doc or Sheet into their Drive."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        f, problem = self._drive_file(which)
        if problem:
            return problem
        try:
            d = await backend_post("/drive/save_pdf", {
                "account_id": self.account_id, "file_id": f["id"],
                "which": self.mailbox})
        except Exception as e:
            log.error(f"pdf failed: {e}")
            return (google_refusal(e, "making a PDF")
                    or "The PDF didn't get made.")
        self._remember_file(d["file"])
        await log_turn(self.call_id, "tool", f"pdf of {f['name']}",
                       "save_as_pdf")
        return f"Saved '{d['file']['name']}' in their Drive. Tell them."

    @function_tool
    @auto_report("drive")
    async def send_drive_file(self, context: RunContext, which: int, to: str,
                              note: str = "", caller_said: str = ""):
        """Email a file from the list to someone, as an attachment. Google
        Docs and Sheets are sent as a PDF. Say back which file and who it
        goes to, and get a clear yes first."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        f, problem = self._drive_file(which)
        if problem:
            return problem
        if not said_yes(caller_said):
            return (f"Do not send yet. Say: 'I'll email {f['name']} to {to}. "
                    f"Shall I send it?' and wait for yes.")
        try:
            d = await backend_post("/drive/email", {
                "account_id": self.account_id, "file_id": f["id"], "to": to,
                "note": note, "which": self.mailbox})
        except Exception as e:
            log.error(f"drive email failed: {e}")
            return (google_refusal(e, "sending files")
                    or "The email didn't go through.")
        await log_turn(self.call_id, "tool", f"emailed {f['name']} to {to}",
                       "send_drive_file")
        return f"Sent {d.get('file')} to {to}. Tell them it's gone."

    @function_tool
    @auto_report("tasks")
    async def to_do_list(self, context: RunContext):
        """What's on the caller's to-do list (Google Tasks) that isn't done."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            d = await backend_get("/todo", account_id=self.account_id,
                                  which=self.mailbox)
        except Exception as e:
            log.error(f"tasks failed: {e}")
            return (google_refusal(e, "their to-do list")
                    or "Couldn't get the to-do list.")
        self.last_tasks = d.get("tasks", [])
        if not self.last_tasks:
            return "Their to-do list is empty. Tell them."
        lines = []
        for i, t in enumerate(self.last_tasks, 1):
            due = f" - by {t['due_spoken']}" if t.get("due_spoken") else ""
            lines.append(f"{i}. {t['title']}{due}")
        return ("\n".join(lines) + "\nRead these out plainly. Today is "
                + datetime.now(ZoneInfo("America/New_York")).strftime("%A")
                + ". Mention anything overdue first.")

    @function_tool
    @auto_report("tasks")
    async def add_to_do(self, context: RunContext, title: str,
                        due_date: str = "", notes: str = ""):
        """Put something on the caller's to-do list. due_date is YYYY-MM-DD
        if they gave a day - work out "Friday" yourself. Say back what you
        added."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            d = await backend_post("/todo/add", {
                "account_id": self.account_id, "title": title,
                "due_date": due_date, "notes": notes, "which": self.mailbox})
        except Exception as e:
            log.error(f"add task failed: {e}")
            return (google_refusal(e, "their to-do list")
                    or "That didn't get added.")
        await log_turn(self.call_id, "tool", f"to-do added: {title}",
                       "add_to_do")
        due = f" for {d['due_spoken']}" if d.get("due_spoken") else ""
        return f"Added '{title}'{due}. Tell them it's on the list."

    @function_tool
    @auto_report("tasks")
    async def tick_off_to_do(self, context: RunContext, which: int):
        """Mark one item from the to-do list you just read out as done."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        if not self.last_tasks:
            return "Call to_do_list first."
        if which < 1 or which > len(self.last_tasks):
            return f"Pick a number from 1 to {len(self.last_tasks)}."
        t = self.last_tasks[which - 1]
        try:
            await backend_post("/todo/done", {}, params={
                "account_id": self.account_id, "task_id": t["id"],
                "which": self.mailbox})
        except Exception as e:
            log.error(f"task done failed: {e}")
            return (google_refusal(e, "their to-do list")
                    or "That didn't get ticked off.")
        await log_turn(self.call_id, "tool", f"to-do done: {t['title']}",
                       "tick_off_to_do")
        return f"Ticked off '{t['title']}'. Tell them."

    @function_tool
    @auto_report("email")
    async def find_contact(self, context: RunContext, name: str):
        """Find someone's EMAIL ADDRESS to write to: their contacts first,
        then people they've emailed. For phone numbers, addresses or
        birthdays use contact_details instead."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            data = await backend_get("/test/contact",
                                     account_id=self.account_id, name=name)
        except Exception as e:
            log.error(f"contact failed: {e}")
            return "The lookup didn't go through."
        matches = data.get("matches", [])
        if not matches:
            return f"No address found for {name}. Ask the caller to spell it."
        return "; ".join(f"{m['name'] or m['email']} at {m['email']}"
                         for m in matches)

    @function_tool
    async def leave_note_for_office(self, context: RunContext, note: str,
                                    reason: str = "general"):
        """Record something for staff to follow up on: a failure, a request
        you can't handle, or anything the caller asks to be passed on.
        Reason is a short label like signin_failed, complaint, request."""
        try:
            await backend_post("/followups", {
                "account_id": self.account_id,
                "call_id": self.call_id,
                "reason": reason[:60],
                "note": note,
                "channel": "voice",
            })
        except Exception as e:
            log.error(f"followup failed: {e}")
            return ("It didn't save. Tell them honestly that you couldn't "
                    "log it and to call the office directly.")
        await log_turn(self.call_id, "tool", f"note: {reason}",
                       "leave_note_for_office")
        return "Noted for the office. Tell them it's been passed on."

    # ------------------------------------------------------ ordering
    @function_tool
    @auto_report("orders")
    async def list_addresses(self, context: RunContext):
        """The caller's saved shipping addresses."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        rows = await backend_get("/addresses", account_id=self.account_id)
        if not rows:
            return "No address saved. Take one down and save_address."
        return "; ".join(f"[{r['id']}] {r['label']}: {r['address']}"
                         + (" (main)" if r.get("default") else "")
                         for r in rows)

    @function_tool
    @auto_report("orders")
    async def save_address(self, context: RunContext, line1: str, city: str,
                           state: str, zip: str, line2: str = "",
                           label: str = "home"):
        """Save a shipping address after reading it back and getting a yes."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        d = await backend_post("/addresses", {
            "account_id": self.account_id, "label": label, "line1": line1,
            "line2": line2, "city": city, "state": state, "zip": zip})
        return f"Saved: {d.get('address')} (id {d.get('id')})."

    @function_tool
    @auto_report("orders")
    async def list_cards(self, context: RunContext):
        """The caller's saved cards — brand and last four only."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        rows = await backend_get("/cards", account_id=self.account_id)
        if not rows:
            return ("No card saved. Ask whether the site already has one, "
                    "or take one down and save_card.")
        return "; ".join(f"[{r['id']}] {r['brand']} ending {r['last4']}, "
                         f"expires {r['exp']}"
                         + (" (main)" if r.get("default") else "")
                         for r in rows)

    @function_tool
    @auto_report("orders")
    async def save_card(self, context: RunContext, number: str, exp: str,
                        cvv: str = "", name_on_card: str = ""):
        """Save a payment card. exp is MM/YY. Read back only the last four
        digits and expiry — never the full number."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            d = await backend_post("/cards", {
                "account_id": self.account_id, "number": number,
                "exp": exp, "cvv": cvv, "name_on_card": name_on_card})
        except Exception as e:
            if "400" in str(e):
                return ("That card number wasn't accepted. Ask them to read "
                        "it again in groups of four.")
            raise
        return (f"Saved a {d.get('brand')} ending {d.get('last4')}. "
                f"Never say the full number again.")

    @function_tool
    @auto_report("orders")
    async def draft_order(self, context: RunContext, site: str, item: str,
                          quantity: int = 1, expected_price: str = "",
                          address_id: int = 0, card_id: int = 0):
        """Put the order together. Nothing is placed yet."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        d = await backend_post("/orders/draft", {
            "account_id": self.account_id, "site": site, "item": item,
            "quantity": quantity, "expected_price": expected_price,
            "address_id": address_id or None, "card_id": card_id or None,
            "call_id": self.call_id})
        self.order_id = d.get("order_id")
        return (f"Order {d.get('order_id')} drafted: {d.get('quantity')} x "
                f"{d.get('item')} from {d.get('site')}"
                f"{', about $' + d['expected_price'] if d.get('expected_price') else ''}"
                f". Ship to {d.get('address') or 'the site\'s saved address'}. "
                f"Pay with {d.get('card') or 'the site\'s saved card'}. "
                f"Read all of that back and ask: Should I place this order?")

    @function_tool
    @auto_report("orders")
    async def review_checkout(self, context: RunContext, site: str,
                              deliver_to: str = "", pay_with: str = ""):
        """Take what is in the caller's basket on a shop as far as the
        checkout page and read everything back: items, address, card and
        total. It CANNOT buy anything - the buttons that would are blocked.
        deliver_to and pay_with are optional: a few words to pick between
        saved ones, like "Monsey" or "the Visa ending 6158"."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            async with httpx.AsyncClient(timeout=25) as c:
                r = await c.post(f"{BACKEND}/jobs/checkout", headers=AUTH,
                                 params={"account_id": self.account_id,
                                         "site": site,
                                         "deliver_to": deliver_to,
                                         "pay_with": pay_with,
                                         "call_id": self.call_id or 0})
                d = r.json()
        except Exception as e:
            log.error(f"checkout failed: {e}")
            return (google_refusal(e, "the checkout")
                    or "Couldn't open the checkout.")
        self.job_id = d.get("job_id")
        self.job_site = site
        self._watch_job(f"opening the {site} checkout")
        return (f"Opening the {site} checkout. Tell them it takes a minute "
                f"and that NOTHING is being bought yet. Wait to be told what "
                f"it says, then read it all back - each item, the address, "
                f"the card and the total - and ask whether to go ahead.")

    @function_tool
    @auto_report("orders")
    async def confirm_order(self, context: RunContext, caller_said: str):
        """Place the order. ONLY after the caller clearly said yes to
        'Should I place this order?' Pass what they said."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        if not getattr(self, "order_id", None):
            return "No order drafted."
        said = caller_said.strip().lower()
        if not any(w in said for w in ("yes", "yeah", "place it", "go ahead",
                                       "confirm", "do it", "sure")):
            return "That wasn't a clear yes. Do not place it. Ask again."
        try:
            async with httpx.AsyncClient(timeout=25) as c:
                r = await c.post(f"{BACKEND}/orders/confirm", headers=AUTH,
                                 params={"order_id": self.order_id,
                                         "confirmed": "yes"})
                d = r.json()
        except Exception as e:
            log.error(f"confirm failed: {e}")
            return "Couldn't start the checkout."
        self.job_id = d.get("job_id")
        await log_turn(self.call_id, "tool", f"order {self.order_id} placing",
                       "confirm_order")
        oid = self.order_id

        async def fetch():
            return await backend_get("/orders/status", order_id=oid)

        def describe(d):
            st, msg = d.get("state", ""), d.get("message", "")
            if st == "placed":
                return (f"Say the order is placed, confirmation "
                        f"{d.get('confirmation') or 'not shown'}, total "
                        f"{d.get('final_total') or 'not shown'}.")
            if st == "failed":
                return f"Say it didn't go through: {msg}. Nothing charged."
            if "Needs the customer" in msg:
                return f"{msg} Ask them and pass the answer on."
            if st == "placing" and msg.startswith("Step"):
                return None            # don't narrate every click
            return None

        self._start_watch("order", fetch, describe)
        return ("Placing it now. Tell them it takes a minute or two, stay "
                "with them, and call check_order.")

    @function_tool
    @auto_report("orders")
    async def check_order(self, context: RunContext):
        """How the order is going. Call this at most ONCE. You will be told
        automatically when it changes."""
        if not getattr(self, "order_id", None):
            return "No order in progress."
        d = await backend_get("/orders/status", order_id=self.order_id)
        st, msg = d.get("state", ""), d.get("message", "")
        if st == "placed":
            conf = d.get("confirmation") or "not shown"
            total = d.get("final_total") or "not shown"
            return f"PLACED. Confirmation {conf}, total {total}. Tell them."
        if st == "failed":
            return f"It didn't go through: {msg}. Nothing should be charged."
        if st == "cancelled":
            return "That order was cancelled."
        if "Needs the customer" in msg:
            return (msg + " Ask them, then call answer_website_question.")
        return (f"Still working: {msg}. Say nothing more about it - "
                f"I will tell you the moment it changes.")

    @function_tool
    @auto_report("orders")
    async def cancel_order(self, context: RunContext):
        """Cancel the current order before it's placed."""
        if not getattr(self, "order_id", None):
            return "No order to cancel."
        async with httpx.AsyncClient(timeout=20) as c:
            await c.post(f"{BACKEND}/orders/cancel", headers=AUTH,
                         params={"order_id": self.order_id})
        oid = self.order_id
        self.order_id = None
        return f"Order {oid} cancelled. Nothing was placed."

    @function_tool
    @auto_report("browse")
    async def do_on_website(self, context: RunContext, goal: str,
                            site: str = "", url: str = ""):
        """Do something on any website, described in plain English. Works on
        sites we've never configured. It reads pages and decides its own
        steps. It will never buy or pay for anything."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            async with httpx.AsyncClient(timeout=25) as c:
                r = await c.post(f"{BACKEND}/jobs/browse", headers=AUTH,
                                 params={"account_id": self.account_id,
                                         "goal": goal, "site": site,
                                         "url": url,
                                         "call_id": self.call_id or 0})
                d = r.json()
        except Exception as e:
            log.error(f"browse failed: {e}")
            return "Couldn't start that."
        if d.get("blocked"):
            return d.get("answer") or "BLOCKED. Say exactly: I am not " \
                                      "allowed to talk to you about this."
        self.job_id = d.get("job_id")
        self.job_question = goal
        self._watch_job(goal)
        return ("On it. Tell them it takes up to a minute and stay with "
                "them, then call get_site_result.")

    @function_tool
    @auto_report("browse")
    async def answer_website_question(self, context: RunContext, answer: str):
        """Pass the caller's answer back to a browsing job that asked for
        something — a code, a choice, a size."""
        if not getattr(self, "job_id", None):
            return "Nothing is waiting on an answer."
        try:
            await backend_post("/jobs/code",
                               {"job_id": self.job_id, "code": answer})
        except Exception as e:
            log.error(f"answer failed: {e}")
            return "That didn't go through."
        return ("Passed it on. Say nothing more about it - I will tell you "
                "when it changes.")

    @function_tool
    @auto_report("site_read")
    async def check_site_orders(self, context: RunContext, site: str):
        """Look up the caller's recent orders on a site they're signed into."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            async with httpx.AsyncClient(timeout=25) as c:
                r = await c.post(f"{BACKEND}/jobs/site-orders", headers=AUTH,
                                 params={"account_id": self.account_id,
                                         "site": site,
                                         "call_id": self.call_id or 0})
                d = r.json()
        except Exception as e:
            log.error(f"site orders failed: {e}")
            return "Couldn't start that."
        self.job_id = d.get("job_id")
        self.job_question = f"their recent orders on {site}"
        self._watch_job(f"recent orders on {site}")
        return ("Looking that up. Tell them it takes about half a minute, "
                "then say nothing until I tell you it's done.")

    @function_tool
    @auto_report("site_read")
    async def search_site(self, context: RunContext, site: str, query: str):
        """Search a site for a product on the caller's behalf."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            async with httpx.AsyncClient(timeout=25) as c:
                r = await c.post(f"{BACKEND}/jobs/site-search", headers=AUTH,
                                 params={"account_id": self.account_id,
                                         "site": site, "query": query,
                                         "call_id": self.call_id or 0})
                d = r.json()
        except Exception as e:
            log.error(f"site search failed: {e}")
            return "Couldn't start that."
        if d.get("blocked"):
            return d.get("answer") or "BLOCKED. Say exactly: I am not " \
                                      "allowed to talk to you about this."
        self.job_id = d.get("job_id")
        self.job_question = f"{query} on {site}"
        self._watch_job(f"searching {site} for {query}")
        return ("Searching. Tell them it takes about half a minute, then "
                "say nothing until I tell you it's done.")

    @function_tool
    @auto_report("site_read")
    async def get_site_result(self, context: RunContext):
        """What the site lookup found. Call this at most ONCE. You will be
        told automatically when it changes."""
        if not getattr(self, "job_id", None):
            return "Nothing running."
        try:
            d = await backend_get("/jobs/answer", job_id=self.job_id,
                                  question=getattr(self, "job_question", ""))
        except Exception:
            return "Couldn't check just now."
        if d.get("state") == "done":
            return d.get("answer") or "Nothing came back."
        if d.get("state") == "failed":
            return (f"That did NOT work: {d.get('message', '')}. You have "
                    f"nothing from that page - not a summary, not a hint. "
                    f"Do NOT say what it said, do not describe its contents "
                    f"and do not produce an answer from it. Tell them you "
                    f"couldn't open it and offer to try a different source.")
        if d.get("state") == "needs_input":
            return (d.get("message", "") +
                    " Ask them, then call answer_website_question.")
        if d.get("state") == "working":
            return (f"Still going: {d.get('message', '')}. Say nothing more "
                    f"about it - I will tell you when it changes.")
        if d.get("state") == "waiting":
            return "Queued behind another job. A moment longer."
        return ("Still loading the page. Say nothing more about it - I will "
                "tell you when it changes.")

    @function_tool
    @auto_report("site_login")
    async def sign_in_to_site(self, context: RunContext, site: str):
        """Sign the caller into any site they've saved a login for, and keep
        the session for next time. Works on sites we've never set up."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            async with httpx.AsyncClient(timeout=25) as c:
                r = await c.post(f"{BACKEND}/jobs/site-login", headers=AUTH,
                                 params={"account_id": self.account_id,
                                         "site": site,
                                         "call_id": self.call_id or 0})
                d = r.json()
        except Exception as e:
            log.error(f"site login failed: {e}")
            return "Couldn't start that."
        self.job_id = d.get("job_id")
        self.job_site = site
        try:
            rows = await backend_get("/logins", account_id=self.account_id)
            self.job_username = next(
                (r.get("username", "") for r in rows
                 if (r.get("site") or "").lower() == site.lower()), "")
        except Exception:
            self.job_username = ""
        self._watch_job(f"signing in to {site}")
        return (f"Signing in to {site}. Tell them it takes about a minute, "
                f"then call check_site_login.")

    @function_tool
    @auto_report("site_login")
    async def check_site_login(self, context: RunContext):
        """How the site sign-in is going. Call this at most ONCE. You will
        be told automatically when it changes."""
        if not getattr(self, "job_id", None):
            return "No sign-in running."
        try:
            d = await backend_get("/jobs/status", job_id=self.job_id)
        except Exception:
            return "Couldn't check just now."
        state, msg = d.get("state", ""), d.get("message", "")
        reason = d.get("reason", "")
        if state == "needs_code":
            return msg + " Ask for it, then call submit_site_code."
        if state == "done":
            return f"Done. {msg}"
        if state == "failed":
            site = getattr(self, "job_site", "") or "the site"
            if reason == "bad_password":
                self.site_fails[site] = self.site_fails.get(site, 0) + 1
            return login_failure_line(site, reason, msg,
                                      self.site_fails.get(site, 0),
                                      getattr(self, "job_username", ""))
        if state == "waiting":
            return (msg + " Tell them it's queued and will start in a moment.")
        return ("Still running. Say nothing more about it - I will tell you "
                "when it changes.")

    @function_tool
    @auto_report("site_login")
    async def submit_site_code(self, context: RunContext, code: str):
        """Give the site the one-time code the caller read out."""
        if not getattr(self, "job_id", None):
            return "No sign-in running."
        digits = "".join(ch for ch in (code or "") if ch.isdigit())
        if len(digits) < 3:
            return ("That is not a code. Ask them to read out the digits "
                    "from the message, slowly, and call this again with "
                    "just those digits. Never send anything else here.")
        try:
            await backend_post("/jobs/code",
                               {"job_id": self.job_id, "code": code})
        except Exception as e:
            log.error(f"job code failed: {e}")
            return "That code didn't go through."
        return ("Code sent. Say nothing more about it - I will tell you when "
                "it changes.")

    @function_tool
    @auto_report("logins")
    async def stop_waiting_for_code(self, context: RunContext,
                                    why: str = "they can't get the code"):
        """Stop a sign-in that is waiting for a one-time code the caller
        cannot get - their phone is elsewhere, the message never came, they
        can't reach their email. Use this INSTEAD of saying you'll handle
        it. Nothing carries on afterwards."""
        jid = getattr(self, "job_id", None)
        if not jid:
            return ("Nothing is waiting. Do not say you are working on "
                    "anything.")
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                await c.post(f"{BACKEND}/jobs/cancel", headers=AUTH,
                             params={"job_id": jid, "why": why[:120]})
        except Exception as e:
            log.error(f"cancel job failed: {e}")
        site = getattr(self, "job_site", "") or "the site"
        self.job_id = None
        await log_turn(self.call_id, "tool", f"stopped sign-in: {why}",
                       "stop_waiting_for_code")
        return (f"Stopped. Tell them plainly that you have stopped trying to "
                f"sign in to {site}, because the code can't be reached right "
                f"now. Their login is still saved. Offer: try again later "
                f"when they have the code in front of them, or have the "
                f"office call them back. Then ask what else they need.")

    @function_tool
    @auto_report("logins")
    async def save_site_login(self, context: RunContext, site: str,
                              username: str, password: str = ""):
        """Save a login for a site with no API, e.g. Amazon. Only after
        reading the details back and getting a yes.

        To change ONLY the username, pass the new username and leave
        password empty - the stored password is kept. Never invent or
        re-send a password you were not just given."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."

        # Confirm before storing, in code rather than in the instructions.
        # A caller said "my email address is chesky163" and we saved the
        # part before the @ without ever reading it back, then failed to
        # sign in with it across two calls.
        key = site.strip().lower()
        if password and not self._login_confirmed.get(key):
            self._login_confirmed[key] = True
            warn = username_warning(username)
            if warn:
                return (f"Do not save yet. The username they gave for {site} "
                        f"is '{username}'. {warn} Say that back to them and "
                        f"ask plainly whether their {site} username is their "
                        f"full email address. Then call save_site_login "
                        f"again with whatever they confirm - the full "
                        f"address if that's what it is, or '{username}' "
                        f"unchanged if they're sure.")
            return (f"Read this back before it is saved: the username is "
                    f"'{username}'. Ask if that is right. If yes, call "
                    f"save_site_login again unchanged; if not, call it "
                    f"again with the correction.")
        try:
            saved = await backend_post("/logins", {
                "account_id": self.account_id, "site": site,
                "username": username, "password": password})
        except Exception as e:
            log.error(f"save login failed: {e}")
            if "400" in str(e):
                return (f"There is nothing saved for {site} yet, so a "
                        f"password is needed too. Ask them for it, one "
                        f"character at a time.")
            return "That didn't save."
        kept = bool(saved.get("password_unchanged"))
        await log_turn(self.call_id, "tool", f"saved login for {site}",
                       "save_site_login")
        if kept:
            return (f"Updated their {site} username to '{username}'. The "
                    f"password they already had is unchanged - say that, so "
                    f"they know they don't need to give it again.")
        return (f"Saved their {site} login. Do not say the password again. "
                f"Tell them it's stored encrypted and they can have it "
                f"deleted whenever they want.")

    @function_tool
    @auto_report("logins")
    async def list_site_logins(self, context: RunContext):
        """Which sites they've saved a login for. Never shows passwords."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            rows = await backend_get("/logins", account_id=self.account_id)
        except Exception:
            return "Couldn't check."
        if not rows:
            return "No site logins saved."
        return "; ".join(f"{r['site']} as {r['username']}" for r in rows)

    @function_tool
    @auto_report("logins")
    async def forget_site_login(self, context: RunContext, site: str):
        """Delete a saved site login."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.request("DELETE", f"{BACKEND}/logins",
                                    headers=AUTH,
                                    params={"account_id": self.account_id,
                                            "site": site})
                d = r.json()
        except Exception as e:
            log.error(f"forget login failed: {e}")
            return "That didn't go through."
        return (f"Deleted their {site} login." if d.get("removed")
                else f"Nothing saved for {site}.")

    @function_tool
    @auto_report("account")
    async def disconnect_email(self, context: RunContext, mailbox: str = ""):
        """Remove one connected mailbox and revoke access at Google."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            async with httpx.AsyncClient(timeout=25) as c:
                r = await c.post(f"{BACKEND}/mailboxes/disconnect",
                                 headers=AUTH,
                                 params={"account_id": self.account_id,
                                         "which": mailbox})
                d = r.json()
        except Exception as e:
            log.error(f"disconnect failed: {e}")
            return "That didn't go through."
        if d.get("removed"):
            await log_turn(self.call_id, "tool",
                           f"disconnected {d.get('email')}",
                           "disconnect_email")
            self.mailbox = ""
            return (f"{d.get('email')} is disconnected and access revoked "
                    f"at Google. They have {d.get('remaining', 0)} left.")
        if d.get("mailboxes"):
            return ("Ask which one: " + ", ".join(d["mailboxes"]))
        return f"Nothing removed: {d.get('reason', 'unknown')}."

    @function_tool
    async def delete_my_account(self, context: RunContext,
                                confirmation: str):
        """Erase the caller entirely. Only call when they have said the word
        DELETE out loud after you explained what is removed."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        if confirmation.strip().upper() != "DELETE":
            return ("They did not say DELETE. Do not delete anything. "
                    "Ask again or drop it.")
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.request("DELETE", f"{BACKEND}/account",
                                    headers=AUTH,
                                    params={"account_id": self.account_id,
                                            "confirm": "DELETE"})
                d = r.json()
        except Exception as e:
            log.error(f"account delete failed: {e}")
            return "That didn't go through."
        if d.get("deleted"):
            return ("Everything is deleted. Tell them it's done, that their "
                    "email access has been revoked, and say goodbye warmly.")
        return "Nothing was deleted."

    @function_tool
    @auto_report("account")
    async def list_mailboxes(self, context: RunContext):
        """Which email addresses this caller has connected, most-used first."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            rows = await backend_get("/mailboxes",
                                     account_id=self.account_id)
        except Exception:
            return "Couldn't check their mailboxes."
        if not rows:
            return "No email connected yet. Offer to connect one."
        if len(rows) == 1:
            self.mailbox = rows[0]["email"]
            return f"One mailbox: {rows[0]['email']}. Just use it."
        parts = []
        for r in rows:
            name = r.get("label") or r.get("email")
            tags = []
            if r.get("default"):
                tags.append("main")
            tags.append(f"{r.get('used', 0)} uses")
            parts.append(f"{name} ({', '.join(tags)})")
        return ("They have several: " + "; ".join(parts) +
                ". Ask which one if it isn't obvious.")

    @function_tool
    @auto_report("account")
    async def name_mailbox(self, context: RunContext, mailbox: str,
                           name: str = "", make_main: bool = False):
        """Give a mailbox a short name the caller can say, and/or make it
        their main one. 'mailbox' is its address or current name."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            rows = await backend_get("/mailboxes",
                                     account_id=self.account_id)
        except Exception:
            return "Couldn't reach their mailboxes."
        w = mailbox.strip().lower()
        match = next((r for r in rows
                      if w == (r.get("email") or "").lower()
                      or w == (r.get("label") or "").lower()), None)
        if not match:
            match = next((r for r in rows
                          if w and (w in (r.get("email") or "").lower()
                                    or w in (r.get("label") or "").lower())),
                         None)
        if not match:
            return f"Couldn't find a mailbox matching '{mailbox}'. Ask again."
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                await c.post(f"{BACKEND}/mailboxes/label", headers=AUTH,
                             params={"connection_id": match["id"],
                                     "label": name.strip(),
                                     "make_default": 1 if make_main else 0})
        except Exception as e:
            log.error(f"name mailbox failed: {e}")
            return "That didn't save."
        bits = []
        if name.strip():
            bits.append(f"now called {name.strip()}")
        if make_main:
            bits.append("set as their main one")
        return f"{match['email']} is " + " and ".join(bits or ["unchanged"]) + "."

    @function_tool
    async def use_mailbox(self, context: RunContext, mailbox: str):
        """Set which mailbox to use for the rest of the call."""
        self.mailbox = mailbox.strip()
        return f"Using {self.mailbox} from now on."

    @function_tool
    @auto_report("signin")
    async def send_password_link(self, context: RunContext):
        """Text the caller a link where they can type their Google password
        instead of saying it out loud. Use when they'd rather not speak it,
        or after a mis-heard attempt."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(f"{BACKEND}/sms/link", headers=AUTH,
                                 params={"account_id": self.account_id,
                                         "to": self.caller_number or ""})
                d = r.json()
        except Exception as e:
            log.error(f"password link failed: {e}")
            return "Couldn't send that."
        if d.get("sent"):
            return ("Sent them a link. Tell them to tap it, sign in there, "
                    "and call back when done.")
        return "The text didn't go out. Carry on by voice instead."

    @function_tool
    @auto_report("email")
    async def mark_read(self, context: RunContext, which_ones: str = "",
                        mailbox: str = ""):
        """Mark messages as read. Pass which_ones as the numbers from the
        list you just read out, like "1,3", or leave it empty and set
        everything=true via mark_all_read instead."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        if not self.last_list:
            return "No list loaded. Call check_email or search_email first."
        picks = [p.strip() for p in which_ones.split(",") if p.strip()]
        ids = []
        for p in picks:
            try:
                ids.append(self.last_list[int(p) - 1]["id"])
            except Exception:
                pass
        if not ids:
            return ("Ask which of the messages they mean, by number from the "
                    "list you read out.")
        try:
            d = await backend_post("/email/mark_read", {}, params={
                "account_id": self.account_id, "msg_ids": ",".join(ids),
                "read": True, "which": mailbox})
        except Exception as e:
            return f"Couldn't do that: {str(e)[:200]}"
        return f"Marked {d.get('changed', 0)} message(s) as read. Say so."

    @function_tool
    @auto_report("email")
    async def mark_all_read(self, context: RunContext,
                            primary_only: bool = False, mailbox: str = ""):
        """Mark every unread message in the inbox as read. Only call this
        after the caller has clearly confirmed they want all of them."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            d = await backend_post("/email/mark_read", {}, params={
                "account_id": self.account_id, "all_unread": True,
                "primary_only": primary_only, "which": mailbox})
        except Exception as e:
            return f"Couldn't do that: {str(e)[:200]}"
        return (f"Marked {d.get('marked_read', 0)} messages as read in the "
                f"{d.get('scope', 'inbox')}. Tell them the number.")

    @function_tool
    @auto_report("advice")
    async def what_now(self, context: RunContext, situation: str,
                       they_said: str = ""):
        """Ask for a decision when you are not sure what to do or say: the
        caller can't do what a site wants, something failed, they asked for
        something you have no tool for, or you are about to say you are
        working on something. Give the situation plainly and what they just
        said. You get back the exact words to speak."""
        said = await ask_advisor(self.account_id, self.call_id,
                                 situation, they_said)
        if not said:
            return ("No advice came back. Say plainly what you do and don't "
                    "know, and offer to have the office call them. Do NOT "
                    "say you are working on anything.")
        await log_turn(self.call_id, "tool", situation[:200], "what_now")
        return said

    @function_tool
    async def what_time_is_it(self, context: RunContext):
        """The real time and date right now, in the caller's time zone."""
        now = datetime.now(ZoneInfo("America/New_York"))
        return (f"It is {now.strftime('%I:%M %p').lstrip('0')} on "
                f"{now:%A, %B} {now.day}, Eastern time. Say it naturally, "
                f"like 'just after midnight' or 'ten past three'.")

    @function_tool
    async def end_call(self, context: RunContext, reason: str = "finished"):
        """Hang up. Call this only after saying goodbye, when the caller has
        said they're done, said goodbye, or asked you to hang up."""
        await log_turn(self.call_id, "tool", f"hanging up: {reason}",
                       "end_call")
        self.hangup_reason = reason
        if self._hangup:
            self._hangup.set()
        return "Say a short goodbye now. Nothing else."

    @function_tool
    @auto_report("cards")
    async def card_setup_code(self, context: RunContext):
        """Get a six-digit code so a card can be added on our website, on
        the payment provider's secure page. The PREFERRED way to add a card:
        the number is never said out loud."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            d = await backend_get("/card/code", account_id=self.account_id)
        except Exception as e:
            log.error(f"card code failed: {e}")
            return "Couldn't make a card code just now. Say sorry."
        digits = " ".join(d.get("code", ""))
        page = d.get("page", "").replace("https://", "")
        host, _, path = page.partition("/")
        spoken = " dot ".join(
            part if part == "com" or len(part) > 3 else " ".join(part)
            for part in host.split(".")) + (f" slash {path}" if path else "")
        odd = max(host.split("."), key=len)
        await log_turn(self.call_id, "tool", "gave a card code",
                       "card_setup_code")
        return (f"Tell them, slowly: go to {spoken}. Spell '{odd}' letter by "
                f"letter - {' '.join(odd)}. On that page they type the phone "
                f"number they're calling from and this code: {digits}. Say "
                f"the code twice and ask them to read it back. It works for "
                f"about an hour. They'll type the card on Stripe's secure "
                f"page, and nothing is charged. Once it's done, list_cards "
                f"will show it.")

    @function_tool
    @auto_report("signin")
    async def email_connect_code(self, context: RunContext):
        """Get a six-digit code so someone with internet - a relative or
        neighbour - can connect the caller's Gmail on our website. Use this
        whenever somebody is able to help them with it."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            d = await backend_get("/link/code", account_id=self.account_id)
        except Exception as e:
            log.error(f"connect code failed: {e}")
            return ("Couldn't make a code just now. Say sorry, and offer to "
                    "connect it on this call instead.")
        digits = " ".join(d.get("code", ""))
        page = d.get("page", "").replace("https://", "")
        host, _, path = page.partition("/")
        spoken = " dot ".join(
            part if part == "com" or len(part) > 3 else " ".join(part)
            for part in host.split(".")) + (f" slash {path}" if path else "")
        odd = max(host.split("."), key=len)
        await log_turn(self.call_id, "tool", "gave a connect code",
                       "email_connect_code")
        return (f"Tell them, slowly: whoever is helping goes to {spoken}. "
                f"Spell '{odd}' letter by letter - {' '.join(odd)} - because "
                f"it is not spelled the usual way. On that page they type "
                f"the phone number the caller is ringing from, and this "
                f"code: {digits}. Say the code twice, then ask them to read "
                f"it back. The code works for about an hour. The person "
                f"whose email it is must be there, because Google will ask "
                f"THEM to agree. Once it's done, next time they call their "
                f"email will be ready. Do not ask for their password.")

    @function_tool
    @auto_report("signin")
    async def connect_email(self, context: RunContext, email: str,
                            password: str):
        """Connect the caller's Gmail using the address and password they
        just gave you. Only call after spelling both back character by
        character and getting a clear yes."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        low = (email or "").lower()
        if not any(low.endswith(d) for d in
                   ("@gmail.com", "@googlemail.com")) and "@" in low:
            return ("This tool only connects Gmail. If they are trying to "
                    "sign in to a shop like Amazon or Walmart, use "
                    "save_site_login and sign_in_to_site instead. Do not call "
                    "connect_email again for this.")
        if not self.password_confirmed:
            self.password_confirmed = True
            spelled = " ".join(
                ("capital " + ch) if ch.isupper() else
                ("the digit " + ch) if ch.isdigit() else
                ch for ch in password)
            return (f"Before signing in, spell the password back to them "
                    f"exactly like this, slowly: {spelled}. That is "
                    f"{len(password)} characters. Ask if every character is "
                    f"right. If they say yes, call connect_email again with "
                    f"the same values. If they correct anything, call it "
                    f"again with the corrected password.")
        try:
            data = await backend_post("/onboard/start", {
                "account_id": self.account_id,
                "email": email.strip(),
                "password": password,
            })
        except Exception as e:
            log.error(f"onboard start failed: {e}")
            return (f"Couldn't start the sign-in: {str(e)[:300]}. Tell them "
                    f"you'll have someone call back.")
        self.onboard_sid = data.get("session_id")
        await log_turn(self.call_id, "tool", f"signin started for {email}",
                       "connect_email")

        sid = self.onboard_sid

        async def fetch():
            return await backend_get("/onboard/status", session_id=sid)

        def describe(d):
            st, msg = d.get("state", ""), d.get("message", "")
            if st == "needs_tap":
                return (f"Say this once, then stop talking: {msg} Do not ask "
                        f"them to confirm and do not repeat it. Stay silent "
                        f"until I give you the next update.")
            if st == "consenting":
                return None
            if st == "verifying":
                return msg
            if st == "needs_code":
                return f"{msg} Ask them for the code."
            if st == "done":
                return (f"Say their email is now connected ({msg}) and "
                        f"offer to read new messages.")
            if st == "failed":
                self.password_confirmed = False
                if d.get("reason") == "bad_password":
                    return ("Tell them Google didn't accept the password, "
                            "and offer to try once more, spelling it out "
                            "one character at a time.")
                return (f"Say it didn't work — {msg} — and that you've "
                        f"left a note for the office.")
            return None

        self._start_watch("signin", fetch, describe)
        return ("Sign-in started. Tell them it takes about a minute, then "
                "call check_connect.")

    @function_tool
    @auto_report("signin")
    async def check_connect(self, context: RunContext):
        """How the email sign-in is going. Call this at most ONCE. You will
        be told automatically when it changes."""
        if not getattr(self, "onboard_sid", None):
            return "No sign-in running."
        try:
            d = await backend_get("/onboard/status",
                                  session_id=self.onboard_sid)
        except Exception:
            return "Couldn't check just now. Try again shortly."
        state = d.get("state", "")
        msg = d.get("message", "")
        if state == "needs_tap":
            return msg + (" Keep checking. If they can't do it, "
                          "call try_another_way.")
        if state == "needs_code":
            return ("Google sent them a verification code. Ask them to read "
                    "it out, then call submit_code.")
        if state == "done":
            return f"Connected. {msg}"
        if state == "failed":
            try:
                await backend_post("/followups", {
                    "account_id": self.account_id,
                    "call_id": self.call_id,
                    "reason": "signin_failed",
                    "note": f"Email sign-in failed for "
                            f"{d.get('email', '')}: {msg}",
                    "channel": "voice",
                })
            except Exception:
                pass
            if d.get("reason") == "bad_password":
                self.pw_attempts = getattr(self, "pw_attempts", 0) + 1
                if self.pw_attempts < 3:
                    return (f"Google didn't accept that password. Say you "
                            f"may have misheard, ask them to say it again "
                            f"one character at a time, read it back, then "
                            f"call connect_email again. "
                            f"(Attempt {self.pw_attempts} of 3.)")
                return ("Three wrong passwords. Stop trying — Google can "
                        "lock the account. Tell them someone will call back.")
            return (f"It didn't work: {msg}. Apologise, tell them you've "
                    f"left a note for the office and someone will call "
                    f"them back, then move on.")
        return (f"Still working ({state}). Stay with them, but say nothing "
                f"more about it - I will tell you when it changes.")

    @function_tool
    @auto_report("signin")
    async def try_another_way(self, context: RunContext):
        """If the caller can't tap the notification on their phone, ask
        Google to text them a code instead."""
        if not getattr(self, "onboard_sid", None):
            if getattr(self, "job_id", None):
                return ("This is a shop sign-in, not Google, and the shop "
                        "chooses how it sends the code - there is no other "
                        "way to ask for. Tell them where the site said it "
                        "sent the code, ask them to look there, and offer "
                        "to have the office call them back if it never "
                        "arrives. Do NOT call submit_site_code with "
                        "anything but digits they read out.")
            return "No sign-in running."
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                await c.post(f"{BACKEND}/onboard/another-way", headers=AUTH,
                             params={"session_id": self.onboard_sid})
        except Exception as e:
            log.error(f"another way failed: {e}")
            return "Couldn't switch methods."
        return ("Asked Google to text a code instead. Wait about ten seconds, "
                "then check_connect again.")

    @function_tool
    @auto_report("signin")
    async def submit_code(self, context: RunContext, code: str):
        """Give Google the verification code the caller just read out."""
        if not getattr(self, "onboard_sid", None):
            return "No sign-in running."
        try:
            await backend_post("/onboard/code", {
                "session_id": self.onboard_sid,
                "code": "".join(ch for ch in code if ch.isdigit()),
            })
        except Exception as e:
            log.error(f"code submit failed: {e}")
            return "That code didn't go through. Ask them to read it again."
        return "Code sent. Wait a few seconds and call check_connect."

    @function_tool
    @auto_report("sms")
    async def send_text(self, context: RunContext, message: str,
                        to: str = ""):
        """Text the caller something — an address, a number, a link. Leave
        'to' blank to text the number they are calling from."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        target = to or self.caller_number
        if not target:
            return "No number to text. Ask the caller for one."
        try:
            data = await backend_post("/sms/send",
                                      {"to": target, "message": message})
        except Exception as e:
            log.error(f"sms failed: {e}")
            return "The text didn't go out."
        return "Text sent." if data.get("sent") else "The text didn't go out."

    @function_tool
    @auto_report("sms")
    async def text_setup_link(self, context: RunContext):
        """Text the caller the link to connect their email account."""
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(
                    f"{BACKEND}/sms/link", headers=AUTH,
                    params={"account_id": self.account_id,
                            "to": self.caller_number or ""})
                data = r.json()
        except Exception as e:
            log.error(f"link sms failed: {e}")
            return "The link didn't go out."
        if data.get("sent"):
            return ("Link sent. Tell them to tap it, sign in, then call back.")
        return "The link didn't go out."

    @function_tool
    @auto_report("search")
    async def web_search(self, context: RunContext, query: str,
                         near: str = ""):
        """Look something up ONLY when you don't already know it, or when
        it changes: today's prices, opening hours now, a phone number, an
        address, whether something is in stock, how far somewhere is, a
        specific model's exact steps.

        Do NOT use this for ordinary knowledge you already have - it costs
        the caller a wait for nothing. Answer those yourself, straight away.
        Set near to a place name for local questions."""
        try:
            data = await backend_get("/web/search", q=query, near=near)
        except Exception as e:
            log.error(f"web search failed: {e}")
            return "The search didn't go through."
        if data.get("blocked"):
            return ("BLOCKED. Say exactly: I am not allowed to talk to you "
                    "about this. Nothing else.")
        ans = data.get("answer") or ""
        results = data.get("results", [])
        if not ans and not results:
            return f"Nothing useful came back for '{query}'."

        self.last_search = [r.get("url", "") for r in results]
        await log_turn(self.call_id, "tool", f"searched: {query}",
                       "web_search", backend_get.last_ms)
        lines = []
        if ans:
            lines.append(f"Summary: {ans}")
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r.get('title', '')} — {r.get('snippet', '')}")
        lines.append(
            "These are short summaries, not the pages themselves. Anything "
            "you say came from HERE must actually be here - never say 'the "
            "page says' about something you are filling in yourself. If "
            "what they need isn't in these summaries, either say what you "
            "know from your own knowledge and make clear that is what it "
            "is, or call read_page with a result number to read the real "
            "page. Do not give the same question two different confident "
            "answers. "
            "This search is FINISHED - there is nothing still running and "
            "nothing more will arrive. If you told them you were going to "
            "check, searching was not checking: call read_page NOW or give "
            "them your answer. Do not say 'almost there'.")
        return "\n".join(lines)[:2000]

    @function_tool
    @auto_report("email")
    async def read_document(self, context: RunContext, url: str,
                            looking_for: str = "what this document says"):
        """Open and read a document linked in an email - an invoice, a
        receipt, a statement, a bill. Works on PDFs.

        The link must be one that actually appeared in the email you just
        read. Never type a link they did not send you."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        link = (url or "").strip().strip(".,)>\"'")
        if not link.lower().startswith("http"):
            return "That isn't a link. Read the email again and use the "                   "exact address from it."
        seen = (self.last_email_body or "") + " " + " ".join(
            self.last_search or [])
        if link not in seen:
            return ("That link wasn't in the message you read. Do not make "
                    "up a web address - read the email again and use "
                    "exactly what is written there.")
        try:
            async with httpx.AsyncClient(timeout=25) as c:
                r = await c.post(f"{BACKEND}/jobs/browse", headers=AUTH,
                                 params={"account_id": self.account_id,
                                         "goal": looking_for,
                                         "url": link, "max_steps": 6,
                                         "call_id": self.call_id or 0})
                d = r.json()
        except Exception as e:
            log.error(f"read document failed: {e}")
            return "Couldn't open that document."
        if d.get("blocked"):
            return d.get("answer") or "BLOCKED."
        self.job_id = d.get("job_id")
        self.job_question = looking_for
        await log_turn(self.call_id, "tool", f"reading document: {link[:90]}",
                       "read_document")
        sess = getattr(self, "session", None)
        if sess:
            try:
                handle = sess.say("Let me open that and read it.",
                                  allow_interruptions=True)
                if inspect.isawaitable(handle):
                    await handle
            except Exception as e:
                log.warning(f"could not announce the document: {e}")
        waited = 0
        while waited < LOOKUP_WAIT:
            await asyncio.sleep(3)
            waited += 3
            try:
                st = await backend_get("/jobs/status", job_id=self.job_id)
            except Exception:
                continue
            if st.get("state") == "done":
                return (f"{st.get('message', '')} -- Tell them what it says, "
                        f"in plain spoken words.")
            if st.get("state") == "failed":
                return (f"Couldn't read it: {st.get('message', '')}. You "
                        f"have nothing from it - do not describe what was "
                        f"in it. Say so plainly.")
        self._watch_job("reading the document")
        return ("Still reading. Say NOTHING more - I will tell you what it "
                "says.")

    @function_tool
    @auto_report("search")
    async def ask_ai(self, context: RunContext, question: str):
        """Ask a bigger, better-informed model a general-knowledge question
        and get an answer back almost at once.

        Use this the moment you are not certain of something general - how
        a particular appliance works, what something means, how a thing is
        normally done. It knows far more than you do and it will say when
        it isn't sure instead of inventing.

        IT CANNOT SEE THIS CONVERSATION. Write the whole question out,
        every time: the make and model, the brand, what they actually want.
        "How do I turn on the ice maker" gets a useless generic answer;
        "how do I turn on the ice maker on a Frigidaire PRDF1922AF" gets a
        real one. Never send a question with "the" or "it" standing in for
        something they told you earlier.

        This is FAST. Do not announce it, do not say "one moment", just
        call it and answer. Only use look_it_up if this says it needs
        checking, or if they ask you to check properly."""
        try:
            data = await backend_get("/ask", q=question)
        except Exception as e:
            log.error(f"ask failed: {e}")
            return "Couldn't check that just now."
        if data.get("blocked"):
            return ("BLOCKED. Say exactly: I am not allowed to talk to you "
                    "about this. Nothing else.")
        said = (data.get("answer") or "").strip()
        if not said:
            return ("No answer came back. Say plainly that you don't know "
                    "and offer to look it up properly with look_it_up.")
        await log_turn(self.call_id, "tool", f"asked: {question[:80]}",
                       "ask_ai", backend_get.last_ms)
        return (f"{said} -- Say that to them in your own words, keeping "
                f"any 'it varies' or 'I'm not certain' part - do not turn a "
                f"hedge into a definite answer. If it says something needs "
                f"checking, offer look_it_up.")

    @function_tool
    @auto_report("search")
    async def look_it_up(self, context: RunContext, question: str):
        """Find something out PROPERLY: this searches and then reads the
        real pages itself, and tells you the answer when it has one.

        Use this whenever they need exact detail you do not already know -
        a particular model's steps, today's price, this week's opening
        hours. It takes about a minute and runs in the background, so say
        one short sentence and then stay quiet until I tell you the answer.

        IT CANNOT SEE THIS CONVERSATION. Write the whole question out,
        every time - the make, the model number, exactly what they want.
        A question like "how do I turn on the ice maker for the" searches
        for nothing and comes back with nothing.

        Do NOT use web_search for these. A search on its own only gives you
        headlines, and answering from those is how people get told wrong
        instructions."""
        key = " ".join(question.lower().split())[:120]
        if self._lookups.get(key) == "failed":
            # Running the same failed search again gets the same nothing,
            # and the caller is listening to it. One call spent four and a
            # half minutes on three identical lookups.
            return ("That exact lookup already failed once on this call. Do "
                    "NOT run it again unchanged - it will fail the same way "
                    "and they are waiting. Tell them straight that you "
                    "can't get the manufacturer's own instructions. Then "
                    "either say what you know with the caveat that it "
                    "varies by model, or try look_it_up ONCE more with a "
                    "genuinely different question - a different wording, "
                    "the manual, a part number - never the same one.")
        try:
            async with httpx.AsyncClient(timeout=25) as c:
                r = await c.post(f"{BACKEND}/jobs/browse", headers=AUTH,
                                 params={"account_id": self.account_id,
                                         "goal": question,
                                         "max_steps": 10,
                                         "call_id": self.call_id or 0})
                d = r.json()
        except Exception as e:
            log.error(f"look up failed: {e}")
            return "Couldn't start that."
        if d.get("blocked"):
            return d.get("answer") or "BLOCKED."
        self.job_id = d.get("job_id")
        self.job_question = question
        await log_turn(self.call_id, "tool", f"looking up: {question[:120]}",
                       "look_it_up")

        # Say it ONCE, in fixed words the model cannot embroider, and then
        # hold this call open until there's an answer. Three different
        # wordings of "say it once then be quiet" all failed - it told one
        # caller it was checking three times in ten seconds. It cannot talk
        # while it is waiting inside a tool.
        sess = getattr(self, "session", None)
        if sess:
            try:
                handle = sess.say("Let me look that up for you. "
                                  "It takes about a minute.",
                                  allow_interruptions=True)
                if inspect.isawaitable(handle):
                    await handle
            except Exception as e:
                log.warning(f"could not announce the lookup: {e}")

        waited = 0
        while waited < LOOKUP_WAIT:
            await asyncio.sleep(3)
            waited += 3
            try:
                st = await backend_get("/jobs/status", job_id=self.job_id)
            except Exception:
                continue
            state = st.get("state", "")
            if state == "done":
                self._lookups[key] = "done"
                return (f"{st.get('message', '')} -- Tell them that now, in "
                        f"your own words. Do not say you are still looking, "
                        f"you have the answer.")
            if state == "failed":
                self._lookups[key] = "failed"
                return (f"It didn't work: {st.get('message', '')}. You have "
                        f"nothing from it - do not describe what it said. "
                        f"Tell them plainly you couldn't get it, then say "
                        f"what you know with the caveat that it varies by "
                        f"model. Do not run this same lookup again.")
            if state == "needs_input":
                return (st.get("message", "") + " Ask them, then call "
                        "answer_website_question.")

        # slower than expected - hand back to the watcher so the call
        # doesn't sit inside a tool for ever
        self._watch_job(f"looking up {question}")
        return ("Still going. Say NOTHING further about it - I will tell "
                "you the moment it finishes.")

    @function_tool
    @auto_report("search")
    async def read_page(self, context: RunContext, which: int,
                        looking_for: str):
        """Open one of the search results and read the real page, when the
        search summary doesn't have the detail the caller needs. 'which' is
        the number from the search list. Use this instead of guessing at
        steps, buttons, prices or instructions."""
        urls = getattr(self, "last_search", [])
        if not urls:
            return "No search results to open. Do a web_search first."
        if which < 1 or which > len(urls) or not urls[which - 1]:
            return (f"Pick a number between 1 and {len(urls)} from the "
                    f"search results.")
        try:
            async with httpx.AsyncClient(timeout=25) as c:
                # hand over the other results too, so a page that blocks
                # robots falls through to the next one without coming back
                # to ask - the manufacturer's own page is usually first and
                # usually the one that refuses
                spares = " ".join(u for u in urls[which:] if u)
                r = await c.post(f"{BACKEND}/jobs/browse", headers=AUTH,
                                 params={"account_id": self.account_id,
                                         "goal": looking_for,
                                         "url": urls[which - 1],
                                         "urls": spares,
                                         "max_steps": 8,
                                         "call_id": self.call_id or 0})
                d = r.json()
        except Exception as e:
            log.error(f"read page failed: {e}")
            return "Couldn't open that page."
        if d.get("blocked"):
            return d.get("answer") or "BLOCKED."
        self.job_id = d.get("job_id")
        self.job_question = looking_for
        self._watch_job(f"reading a page about {looking_for}")
        return ("Reading the page now. Tell them that in one sentence, then "
                "say nothing until I tell you what it says.")

    @function_tool
    @auto_report("calendar")
    async def check_calendar(self, context: RunContext, days: int = 1):
        """What's on the caller's calendar. days=1 is today, 7 is the week."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            data = await backend_get("/cal/events",
                                     account_id=self.account_id, days=days)
        except Exception as e:
            log.error(f"calendar failed: {e}")
            return (google_refusal(e, "their email")
                    or "I couldn't reach the calendar.")
        evs = data.get("events", [])
        if not evs:
            return "Nothing scheduled in that window."
        self.last_events = evs
        await log_turn(self.call_id, "tool", f"calendar {days}d",
                       "check_calendar", backend_get.last_ms)
        lines = []
        for i, e in enumerate(evs, 1):
            when = e.get("start", "")
            if "T" in when:
                try:
                    when = datetime.fromisoformat(when).strftime(
                        "%A %-I:%M %p")
                except Exception:
                    pass
            lines.append(f"{i}. {e.get('title')} — {when}")
        return "\n".join(lines)

    @function_tool
    @auto_report("calendar")
    async def find_free_time(self, context: RunContext, date: str,
                             minutes: int = 60):
        """Open slots on a date. date must be YYYY-MM-DD."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            data = await backend_get("/cal/free",
                                     account_id=self.account_id,
                                     date=date, minutes=minutes)
        except Exception as e:
            log.error(f"free failed: {e}")
            return "I couldn't check availability."
        free = data.get("free", [])
        if not free:
            return f"{data.get('date')} looks full."
        return f"{data.get('date')} is open at: " + ", ".join(free)

    @function_tool
    @auto_report("calendar")
    async def create_event(self, context: RunContext, title: str,
                           start_iso: str, minutes: int = 60,
                           location: str = ""):
        """Book something. start_iso is YYYY-MM-DDTHH:MM:SS, 24-hour clock.
        Only call after reading it back and the caller saying yes."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            await backend_post("/cal/create", {
                "account_id": self.account_id, "title": title,
                "start_iso": start_iso, "minutes": minutes,
                "location": location,
            })
        except Exception as e:
            log.error(f"create event failed: {e}")
            return "That didn't get added."
        await log_turn(self.call_id, "tool", f"booked: {title} {start_iso}",
                       "create_event")
        return "Added to the calendar."

    @function_tool
    @auto_report("email")
    async def send_email(self, context: RunContext,
                         to: str, subject: str, body: str):
        """Send an email. Only call AFTER you have read the draft back out loud
        and the caller has clearly said yes."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            await backend_post("/test/send", {
                "account_id": self.account_id,
                "to": to, "subject": subject, "body": body,
                "which": self.mailbox,
            })
        except Exception as e:
            log.error(f"send failed: {e}")
            return "The message did not go through."
        await log_turn(self.call_id, "tool", f"emailed {to}: {subject}",
                       "send_email")
        return "Sent."


# ------------------------------------------------------------------ session

@server.rtc_session(agent_name="phone-assistant")
async def entrypoint(ctx: JobContext):
    await ctx.connect()
    participant = await ctx.wait_for_participant()

    caller = (participant.attributes or {}).get("sip.phoneNumber", "")
    log.info(f"call from {caller}")

    account = await find_account(caller)

    call_id = None
    try:
        started = await backend_post("/calls/start", {
            "account_id": account["account_id"] if account else None,
            "from_number": caller,
            "room": ctx.room.name,
        })
        call_id = started.get("call_id")
    except Exception as e:
        log.warning(f"could not open call record: {e}")

    log.info(f"voice model: {REALTIME_MODEL} ({REALTIME_VOICE})")
    session = AgentSession(
        llm=openai.realtime.RealtimeModel(model=REALTIME_MODEL,
                                          voice=REALTIME_VOICE),
        vad=silero.VAD.load(),
    )

    if not account:
        await session.start(room=ctx.room, agent=Agent(
            instructions=("Speak English. Say this number isn't set up yet, "
                          "then say goodbye.")))
        await session.generate_reply(
            instructions=("In English: tell them this number isn't registered "
                          "and to call the office. One sentence."))
        return

    known = ""
    try:
        p = await backend_get("/profile",
                              account_id=account["account_id"])
        known = p.get("for_the_assistant", "") or ""
    except Exception as e:
        log.warning(f"profile load failed: {e}")

    history = ""
    try:
        rows = await backend_get("/memory",
                                 account_id=account["account_id"], limit=12)
        history = "\n".join(
            f"- ({r['channel']}) {r['who']}: {r['text'][:160]}" for r in rows)
    except Exception as e:
        log.warning(f"history load failed: {e}")

    agent_obj = Assistant(account, caller, call_id, history, known)

    @session.on("conversation_item_added")
    def _on_item(ev):
        try:
            item = getattr(ev, "item", None)
            role = getattr(item, "role", "")
            text = getattr(item, "text_content", None) or ""
            if text:
                if role != "user":
                    last_heard["agent_done"] = time.monotonic()
                who = "caller" if role == "user" else "agent"
                asyncio.create_task(log_turn(call_id, who, text))
                if account:
                    asyncio.create_task(backend_post("/memory", {
                        "account_id": account["account_id"],
                        "channel": "voice",
                        "who": "user" if role == "user" else "assistant",
                        "text": text}))
        except Exception:
            pass

    async def _close():
        if call_id:
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    await c.post(f"{BACKEND}/calls/end", headers=AUTH,
                                 params={"call_id": call_id,
                                         "verified": int(agent_obj.verified)})
                    # nobody is listening any more - don't keep paying for
                    # a browser to finish an answer no one will hear
                    await c.post(f"{BACKEND}/jobs/cancel_for_call",
                                 headers=AUTH, params={"call_id": call_id})
                    if getattr(agent_obj, "onboard_sid", None):
                        await c.post(f"{BACKEND}/onboard/cancel",
                                     headers=AUTH,
                                     params={"session_id":
                                             agent_obj.onboard_sid})
            except Exception:
                pass


    # ---------------------------------------------------------- hang up
    # Calls must not stay open. LiveKit bills by the minute and a forgotten
    # line ties up a browser session too.
    hangup = asyncio.Event()
    agent_obj._hangup = hangup
    # "at" = the caller last said something. "agent_done" = the agent last
    # finished saying something. The caller's turn starts at the LATER of
    # the two - listening to a long answer is not the same as being absent,
    # and a caller was hung up on for "no answer" while the agent was still
    # talking.
    last_heard = {"at": time.monotonic(), "agent_done": time.monotonic()}
    started_at = time.monotonic()

    try:
        @session.on("user_input_transcribed")
        def _heard(ev):
            last_heard["at"] = time.monotonic()
    except Exception as e:
        log.warning(f"could not watch for silence: {e}")

    async def watchdog():
        warned_at = 0.0
        while not hangup.is_set():
            await asyncio.sleep(5)
            now = time.monotonic()
            # measured from whenever it last became the caller's turn
            quiet = now - max(last_heard["at"], last_heard["agent_done"])
            total = now - started_at
            busy = bool(getattr(agent_obj, "onboard_sid", None)
                        or getattr(agent_obj, "job_id", None)
                        or getattr(agent_obj, "order_id", None))

            if total > MAX_CALL_SECONDS:
                agent_obj.hangup_reason = "reached the maximum call length"
                try:
                    await session.generate_reply(
                        instructions=("In English: say the call has reached "
                                      "its time limit, they can call back "
                                      "any time, then say goodbye. Two "
                                      "sentences."))
                    await asyncio.sleep(5)
                except Exception:
                    pass
                hangup.set()
                return

            # while a sign-in or order is running, silence is expected
            limit = SILENCE_HANGUP if not busy else SILENCE_HANGUP * 3
            warn_at = SILENCE_WARN if not busy else SILENCE_WARN * 3

            if quiet > limit:
                agent_obj.hangup_reason = "no answer from the caller"
                hangup.set()
                return

            # warn once per silence, and not again until they speak
            if quiet > warn_at and warned_at < last_heard["at"]:
                warned_at = now
                try:
                    # say() speaks these exact words. Asking the model to
                    # "check if they are still there" made it repeat its
                    # previous answer in full instead - a caller heard the
                    # same paragraph about building a sukkah twice.
                    handle = session.say("Are you still there?",
                                         allow_interruptions=True)
                    if inspect.isawaitable(handle):
                        await handle
                except Exception as e:
                    log.warning(f"could not ask if they're there: {e}")

    async def hangup_when_asked():
        await hangup.wait()
        await asyncio.sleep(3)          # let the goodbye finish playing
        log.info(f"hanging up: {agent_obj.hangup_reason}")
        await log_turn(call_id, "tool",
                       f"call ended: {agent_obj.hangup_reason}", "end_call")
        try:
            await ctx.api.room.delete_room(
                api.DeleteRoomRequest(room=ctx.room.name))
        except Exception as e:
            log.warning(f"delete_room failed, disconnecting: {e}")
            try:
                await ctx.room.disconnect()
            except Exception:
                pass
        ctx.shutdown(reason=agent_obj.hangup_reason or "done")

    try:
        @ctx.room.on("participant_disconnected")
        def _gone(p):
            agent_obj.hangup_reason = "caller hung up"
            hangup.set()
    except Exception as e:
        log.warning(f"could not watch for disconnect: {e}")

    # ------------------------------------------------------------ costs
    usage = {"audio_in": 0, "audio_out": 0, "text_in": 0, "text_out": 0,
             "cached_in": 0}

    try:
        @session.on("metrics_collected")
        def _metrics(ev):
            try:
                m = getattr(ev, "metrics", None)
                d = getattr(m, "__dict__", {}) or {}
                usage["text_in"] += int(d.get("prompt_tokens", 0) or 0)
                usage["text_out"] += int(d.get("completion_tokens", 0) or 0)
                det = d.get("input_token_details") or {}
                get = (det.get if isinstance(det, dict)
                       else lambda k, v=0: getattr(det, k, v))
                usage["audio_in"] += int(get("audio_tokens", 0) or 0)
                usage["cached_in"] += int(get("cached_tokens", 0) or 0)
                odet = d.get("output_token_details") or {}
                oget = (odet.get if isinstance(odet, dict)
                        else lambda k, v=0: getattr(odet, k, v))
                usage["audio_out"] += int(oget("audio_tokens", 0) or 0)
            except Exception as e:
                log.debug(f"metrics parse: {e}")
    except Exception as e:
        log.warning(f"metrics not available: {e}")

    async def _report_usage():
        secs = int(time.monotonic() - started_at)
        # audio tokens the model reported are for text-priced turns too;
        # subtract them so nothing is counted twice
        body = {
            "call_id": call_id,
            "account_id": account["account_id"],
            "kind": "voice",
            "audio_in": usage["audio_in"],
            "audio_out": usage["audio_out"],
            "text_in": max(0, usage["text_in"] - usage["audio_in"]
                           - usage["cached_in"]),
            "text_out": max(0, usage["text_out"] - usage["audio_out"]),
            "cached_in": usage["cached_in"],
            "call_seconds": secs,
        }
        try:
            await backend_post("/usage", body)
        except Exception as e:
            log.warning(f"usage report failed: {e}")

    # usage first: shutdown has a time budget, and losing
    # the cost of a call is worse than losing its tidy-up
    ctx.add_shutdown_callback(_report_usage)
    ctx.add_shutdown_callback(_close)

    # Start the agent FIRST. Nothing above this line may prevent it.
    await session.start(room=ctx.room, agent=agent_obj)

    try:
        asyncio.create_task(watchdog())
        asyncio.create_task(hangup_when_asked())
    except Exception as e:
        log.warning(f"hangup watchdog not started: {e}")
    await session.generate_reply(
        instructions=(f"In English: greet {account.get('name')} by name in "
                      f"one short sentence and ask for their PIN. "
                      f"Speak English."))


if __name__ == "__main__":
    cli.run_app(server)
