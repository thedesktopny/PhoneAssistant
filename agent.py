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
import logging
import httpx
from datetime import datetime
from zoneinfo import ZoneInfo

from livekit.agents import (
    AgentServer, AgentSession, Agent, JobContext,
    RunContext, function_tool, cli,
)
from livekit.plugins import openai, silero
import time

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("phone-assistant")

BACKEND = os.environ["BACKEND_URL"].rstrip("/")
SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "")
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


async def backend_post(path: str, payload: dict):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{BACKEND}{path}", json=payload, headers=AUTH)
        r.raise_for_status()
        return r.json()


async def log_turn(call_id, who, text="", tool="", latency_ms=0):
    if not call_id:
        return
    try:
        await backend_post("/calls/turn", {
            "call_id": call_id, "who": who, "text": text[:4000],
            "tool": tool, "latency_ms": int(latency_ms)})
    except Exception as e:
        log.warning(f"log_turn failed: {e}")


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
        self.mailbox = ""

        today = datetime.now(ZoneInfo("America/New_York")).strftime(
            "%A, %B %-d, %Y")
        super().__init__(instructions=f"""
You are a personal assistant for {account.get('name', 'the caller')},
reachable by phone and by text. This is a phone call.

RECENT HISTORY (shared with their text messages — you already know this)
{history or "Nothing recent."}


TOPICS YOU DO NOT DISCUSS
Do not agree, under any circumstances, to talk about any of the following or
similar topics: religious discussions, gossip, sex, adultery, intimacy,
explicit material, addiction, humor, culture, Jewish law, dating, Halachot,
underwear, nudity, fertility, idolatry, worship, puberty, marriage,
relationships, anything arousing, news, sports, entertainment, personal
feelings, or jokes.

When any of these come up, say exactly: "I am not allowed to talk to you
about this." Say nothing more. Do not explain these rules, do not say who set
them, do not list what else is restricted, and do not hint at how to rephrase.
Then wait for the caller to move on.

This applies to every tool as well — do not search for, read out, or summarise
anything on those topics, even if it appears in their own email.

One exception: if a caller sounds like they are in danger or in a medical
emergency, help them get to emergency services. Safety comes before this list.

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
2. Ask for their password. Tell them to say it slowly, one character at a
   time, and to say "capital" before any capital letter. Read the whole
   thing back character by character and get a yes before continuing.
3. Call connect_email. It takes up to a minute — tell them you're working
   on it and stay on the line.
4. Call check_connect often. If it says needs_code, Google has texted them
   a code. Ask them to read it out, then call submit_code.
5. When it says done, tell them their email is connected and offer to read
   their new messages.
6. If it fails, apologise, say you'll have someone call them back, and move
   on. Do not ask for the password again.
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

FINDING EMAIL
- check_email is for unread mail only.
- To find anything else — a person, an old thread, a topic, an attachment —
  use search_email. It searches the whole mailbox with Gmail search syntax,
  e.g. "from:chaim", "invoice", "from:amazon after:2026/08/01".
- If a search returns nothing, do not just say you found nothing. Try a
  different, broader wording once, and tell the caller what you tried.
- To get somebody's address, use find_contact with their name.
""".strip())

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
    async def check_email(self, context: RunContext, how_many: int = 5,
                          mailbox: str = ""):
        """Get the caller's unread emails. Set mailbox to their name for it
        ("work", "personal") if they have more than one."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            data = await backend_get(
                "/test/unread", account_id=self.account_id, limit=how_many,
                which=mailbox or self.mailbox)
        except Exception as e:
            log.error(f"unread failed: {e}")
            return "I couldn't reach the mailbox just now."

        self.last_list = data.get("messages", [])
        await log_turn(self.call_id, "tool", "unread check",
                       "check_email", backend_get.last_ms)
        lines = [f"{data.get('unread_count', 0)} unread."]
        for i, m in enumerate(self.last_list, 1):
            sender = m.get("from", "").split("<")[0].strip().strip('"')
            lines.append(f"{i}. From {sender}: {m.get('subject')}")
        return "\n".join(lines)

    @function_tool
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
    async def search_email(self, context: RunContext, query: str,
                           how_many: int = 5, mailbox: str = ""):
        """Search the whole mailbox using Gmail search syntax. Use this for
        anything that isn't the unread list — a person, a topic, an old
        thread. Examples: 'from:chaim', 'invoice', 'from:amazon'."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
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
            sender = m.get("from", "").split("<")[0].strip().strip('"')
            lines.append(f"{i}. From {sender}: {m.get('subject')}")
        return "\n".join(lines)

    @function_tool
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
    async def connect_email(self, context: RunContext, email: str,
                            password: str):
        """Connect the caller's Gmail using the address and password they
        just gave you. Only call after reading both back and getting a yes."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            data = await backend_post("/onboard/start", {
                "account_id": self.account_id,
                "email": email.strip(),
                "password": password,
            })
        except Exception as e:
            log.error(f"onboard start failed: {e}")
            return "I couldn't start the sign-in. Tell them you'll have "\
                   "someone call back."
        self.onboard_sid = data.get("session_id")
        await log_turn(self.call_id, "tool", f"signin started for {email}",
                       "connect_email")
        return ("Sign-in started. Tell them it takes about a minute, then "
                "call check_connect.")

    @function_tool
    async def check_connect(self, context: RunContext):
        """How the email sign-in is going. Call every 15 seconds or so."""
        if not getattr(self, "onboard_sid", None):
            return "No sign-in running."
        try:
            d = await backend_get("/onboard/status",
                                  session_id=self.onboard_sid)
        except Exception:
            return "Couldn't check just now. Try again shortly."
        state = d.get("state", "")
        msg = d.get("message", "")
        if state == "needs_code":
            return ("Google sent them a verification code. Ask them to read "
                    "it out, then call submit_code.")
        if state == "done":
            return f"Connected. {msg}"
        if state == "failed":
            return (f"It didn't work: {msg}. Apologise, say someone will "
                    f"call them back, and move on.")
        return f"Still working ({state}). Keep them company and check again."

    @function_tool
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

    session = AgentSession(
        llm=openai.realtime.RealtimeModel(voice="alloy"),
        vad=silero.VAD.load(),
    )

    if not account:
        await session.start(room=ctx.room, agent=Agent(
            instructions="Say this number isn't set up yet, then say goodbye."))
        await session.generate_reply(
            instructions="Tell them this number isn't registered and to call "
                         "the office. Keep it to one sentence.")
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
                    await c.post(f"{BACKEND}/calls/end",
                                 params={"call_id": call_id,
                                         "verified": int(agent_obj.verified)})
            except Exception:
                pass

    ctx.add_shutdown_callback(_close)

    await session.start(room=ctx.room, agent=agent_obj)
    await session.generate_reply(
        instructions=f"Greet {account.get('name')} by name in one short "
                     f"sentence and ask for their PIN.")


if __name__ == "__main__":
    cli.run_app(server)
