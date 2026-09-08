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
import logging
import httpx
from datetime import datetime
from zoneinfo import ZoneInfo

from livekit.agents import (
    AgentServer, AgentSession, Agent, JobContext,
    RunContext, function_tool, cli,
)
from livekit.plugins import openai, silero

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("phone-assistant")

BACKEND = os.environ["BACKEND_URL"].rstrip("/")

server = AgentServer()


# ------------------------------------------------------------------ backend

async def backend_get(path: str, **params):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{BACKEND}{path}", params=params)
        r.raise_for_status()
        return r.json()


async def backend_post(path: str, payload: dict):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{BACKEND}{path}", json=payload)
        r.raise_for_status()
        return r.json()


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
    def __init__(self, account: dict):
        self.account = account
        self.account_id = account["account_id"]
        self.verified = False
        self.last_list = []
        self.last_events = []

        today = datetime.now(ZoneInfo("America/New_York")).strftime(
            "%A, %B %-d, %Y")
        super().__init__(instructions=f"""
You are a phone assistant for {account.get('name', 'the caller')}.

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
    async def check_email(self, context: RunContext, how_many: int = 5):
        """Get the caller's unread emails — count, senders and subjects."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            data = await backend_get(
                "/test/unread", account_id=self.account_id, limit=how_many)
        except Exception as e:
            log.error(f"unread failed: {e}")
            return "I couldn't reach the mailbox just now."

        self.last_list = data.get("messages", [])
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
                "/test/read", account_id=self.account_id, msg_id=msg_id)
        except Exception as e:
            log.error(f"read failed: {e}")
            return "I couldn't open that message."
        body = " ".join((data.get("body") or "").split())[:1500]
        return f"From {data.get('from')}. Subject {data.get('subject')}. {body}"

    @function_tool
    async def search_email(self, context: RunContext, query: str,
                           how_many: int = 5):
        """Search the whole mailbox using Gmail search syntax. Use this for
        anything that isn't the unread list — a person, a topic, an old
        thread. Examples: 'from:chaim', 'invoice', 'from:amazon'."""
        if not self.verified:
            return "Not verified yet. Ask for the PIN first."
        try:
            data = await backend_get("/test/search",
                                     account_id=self.account_id,
                                     q=query, limit=how_many)
        except Exception as e:
            log.error(f"search failed: {e}")
            return "The search didn't go through."

        msgs = data.get("messages", [])
        if not msgs:
            return (f"Nothing matched '{query}'. Tell the caller what you "
                    f"searched and try a broader wording once.")
        self.last_list = msgs
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
            })
        except Exception as e:
            log.error(f"send failed: {e}")
            return "The message did not go through."
        return "Sent."


# ------------------------------------------------------------------ session

@server.rtc_session(agent_name="phone-assistant")
async def entrypoint(ctx: JobContext):
    await ctx.connect()
    participant = await ctx.wait_for_participant()

    caller = (participant.attributes or {}).get("sip.phoneNumber", "")
    log.info(f"call from {caller}")

    account = await find_account(caller)

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

    await session.start(room=ctx.room, agent=Assistant(account))
    await session.generate_reply(
        instructions=f"Greet {account.get('name')} by name in one short "
                     f"sentence and ask for their PIN.")


if __name__ == "__main__":
    cli.run_app(server)
