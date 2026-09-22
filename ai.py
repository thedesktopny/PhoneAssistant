"""Talking to the models, and paying for it.

Which model does which job, the one place that calls OpenAI, and the
summariser that turns a scraped page into something worth saying out
loud. Every call records its tokens so the Costs tab can separate
the browser's brain from the small helpers.

Change MODEL_BROWSER or MODEL_ADVISOR in Railway, not here.
"""
from core import *                                   # noqa: F401,F403


def _summarise_page(text: str, question: str) -> str:
    """Turn a scraped page into a short spoken answer."""
    if not OPENAI_API_KEY:
        return text[:600]
    try:
        d = _openai_chat(model=MODEL_SUMMARY, messages=[
            {"role": "system",
             "content": ("You turn a scraped web page into a short answer to "
                         "be read aloud on a phone call. Two or three "
                         "sentences. Give names, prices and dates plainly. "
                         "No URLs. If the page does not answer the question, "
                         "reply with exactly NOTHING_RELEVANT and nothing "
                         "else.")},
            {"role": "user",
             "content": f"Question: {question}\n\nPage text:\n{text[:6000]}"},
        ])
        return (d["choices"][0]["message"].get("content") or "")[:900]
    except Exception:
        return text[:600]


def _openai_chat(messages: list, tools=None, model: str = "",
                 account_id=None, call_id=None, cheap: bool = True) -> dict:
    """One chat call. 'cheap' decides which column the tokens are billed to,
    so the Costs tab separates the browser's brain from the helpers."""
    payload = {"model": model or MODEL_TEXT, "messages": messages}
    if tools:
        payload["tools"] = tools
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        # Mark it, so a browser job doesn't report this as a Browserbase
        # fault, and shout - a bad key here silently breaks every job.
        try:
            e._from_openai = True
        except Exception:
            pass
        emit("openai", "chat", f"{payload['model']} call failed: "
                               f"{str(e)[:180]}", "error", account_id)
        raise

    # Until now these tokens were never counted anywhere.
    try:
        u = data.get("usage") or {}
        got_in = int(u.get("prompt_tokens", 0) or 0)
        got_out = int(u.get("completion_tokens", 0) or 0)
        if got_in or got_out:
            fields = ({"mini_in": got_in, "mini_out": got_out} if cheap
                      else {"brain_in": got_in, "brain_out": got_out})
            record_usage(account_id=account_id, call_id=call_id,
                         kind="browser", **fields)
    except Exception:
        pass
    return data


# ------------------------------------------------------------- the advisor
# The voice model is fast and a poor judge: told "my phone isn't with me",
# it answered "I'm handling that" while nothing at all was running. Rules
# were added one call at a time and there is no end to them.
#
# So judgement moves here. The backend reads the real state - what is
# running, what failed and why, what is connected - and a slower model
# decides the next step and the words. The voice model speaks them.
#
# What it may NOT decide is permission: reading an order total back,
# getting a yes before sending or charging, stopping at a human check.
# Those stay in code, where a model cannot talk itself past them.


ASK_SYSTEM = """You are answering a question for someone on a phone call.
They are older, often not technical, and they cannot see a screen.

Answer in two or three short spoken sentences. No lists, no markdown, no
URLs.

Be honest about how sure you are, in plain words:
- Sure: just say it.
- It varies by model, version or place: say what it usually is AND say it
  varies, e.g. "on most of those it's X, but it does differ between
  models".
- You don't know: say so plainly. Never invent a specific button
  combination, part number, price or step. A wrong specific answer is far
  worse than "I'm not certain" - they will go and try it.

If the answer depends on something that changes - today's price, this
week's hours, whether a shop has it in stock - say it needs checking."""
