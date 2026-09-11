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
SILENCE_WARN = int(os.environ.get("SILENCE_WARN", "20"))
SILENCE_HANGUP = int(os.environ.get("SILENCE_HANGUP", "45"))
SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "")

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
                return ("Something went wrong there. Tell them you've left "
                        "a note for the office.")
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
                 call_id: int | None = None, history: str = ""):
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

        # %-d is Linux-only and raises on Windows, where check.py is run
        _now = datetime.now(ZoneInfo("America/New_York"))
        today = f"{_now:%A, %B} {_now.day}, {_now.year}"
        super().__init__(instructions=f"""
You are a personal assistant for {account.get('name', 'the caller')},
reachable by phone and by text. This is a phone call.

RECENT HISTORY (shared with their text messages — you already know this)
{history or "Nothing recent."}


NEVER ASK THE CALLER TO WAIT
Do not ask "would you like to keep waiting" or "should we try something
else" while a job runs. Do not offer them a choice about waiting. Say one
sentence when it starts, then say nothing until I give you an update.


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
Speak English. Start every call in English and stay in English unless the
caller clearly speaks to you in another language first — then answer in
theirs and keep to it for the rest of the call. Never switch languages on
your own, and never switch back mid-call unless they do.

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
- Today is {today}. Work out relative dates like "tomorrow" or "next Tuesday"
  yourself before calling a tool.

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

PLACING AN ORDER — do it exactly like a careful person would
1. Find out what they want: the item, how many, and which site. If they're
   vague, use search_site or do_on_website to find it and read them the
   name and price. Get a yes on the exact item before going further.
2. Address: call list_addresses. If they have one, read it back and ask
   "ship it there?" If none, take it down — street, city, state, zip —
   read it back, and save_address.
3. Payment: call list_cards. If they have one, say "the Visa ending 1234?"
   and get a yes. If none, ask if the site has a card saved already; if not,
   take the card: number in groups of four, expiry, security code, name.
   Read back ONLY the last four digits and expiry, never the full number,
   then save_card. If the number is rejected, ask them to read it again.
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
If check_email says their account has no email linked, offer to connect it
on this call. Then:
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
            t = asyncio.create_task(self._watch(kind, fetch, describe))
            self._watchers = getattr(self, "_watchers", [])
            self._watchers.append(t)
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
                return ("Say it's done, then get the details with "
                        "get_site_result.")
            if st == "failed":
                if d.get("reason") == "signed_out":
                    return ("Tell them the saved session has expired and "
                            "that you'll sign in again with the login they "
                            "already gave you - do not ask for a password. "
                            "Then call sign_in_to_site.")
                if d.get("reason") == "bad_password":
                    return (f"Say the site didn't accept the password: "
                            f"{msg}")
                return f"Say it didn't work: {msg}."
            return None

        self._start_watch("job", fetch, describe)

    @function_tool
    async def verify_pin(self, context: RunContext, pin: str):
        """Check the caller's PIN. Must be called before any email action."""
        try:
            data = await backend_get("/accounts")
        except Exception as e:
            log.error(f"pin lookup failed: {e}")
            return "Could not verify right now."
        digits = "".join(ch for ch in pin if ch.isdigit())
        expected = str(self.account.get("pin", "1234"))
        if digits == expected:
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
            return "I couldn't reach the mailbox just now."

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
            return "I couldn't open that message."
        body = " ".join((data.get("body") or "").split())[:1500]
        return f"From {data.get('from')}. Subject {data.get('subject')}. {body}"

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
            return "The search didn't go through."

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
            return "I couldn't reach the mailbox just now."
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

    @function_tool
    @auto_report("email")
    async def find_contact(self, context: RunContext, name: str):
        """Look up someone's email address from past correspondence."""
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
            return f"It didn't work: {d.get('message', '')}"
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
        """Search the web for anything not in their email or calendar —
        addresses, phone numbers, business hours, travel time, facts, prices.
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
        extra = " ".join(r.get("snippet", "") for r in data.get("results", []))
        if not ans and not extra:
            return f"Nothing useful came back for '{query}'."
        return (ans + " " + extra)[:1200]

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
            return "I couldn't reach the calendar."
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

    history = ""
    try:
        rows = await backend_get("/memory",
                                 account_id=account["account_id"], limit=12)
        history = "\n".join(
            f"- ({r['channel']}) {r['who']}: {r['text'][:160]}" for r in rows)
    except Exception as e:
        log.warning(f"history load failed: {e}")

    agent_obj = Assistant(account, caller, call_id, history)

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
                    await session.generate_reply(
                        instructions=("In English: ask once, gently, if "
                                      "they're still there. One short "
                                      "sentence."))
                except Exception:
                    pass

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
