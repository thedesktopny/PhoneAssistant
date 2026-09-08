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

        super().__init__(instructions=f"""
You are a phone assistant for {account.get('name', 'the caller')}.
You speak out loud, so keep every answer short and natural — one or two
sentences. Never read out URLs, long headers, or raw email addresses unless
asked.

Before doing ANYTHING with email, the caller must give their PIN. Ask for it
once at the start and call verify_pin. If it fails, ask again; after three
failures, politely end.

When reading unread email, summarise: how many are unread, then who each is
from and what it's about. Do not read the whole message unless asked.

Before sending any email, read the recipient, subject and message back to the
caller and wait for them to say yes.

If a lookup takes a moment, say something like "one second" so the caller
knows you're working.
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
    async def send_email(self, context: RunContext,
                         to: str, subject: str, body: str):
        """Send an email. Only call after the caller has confirmed out loud."""
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
