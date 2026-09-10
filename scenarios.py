"""
Replay the things that went wrong on real calls, without making a call.

    python scenarios.py                 run everything
    python scenarios.py email           run the ones matching "email"

Every scenario here is a problem a customer actually hit on the phone. The
rule is: when a call goes wrong, the fix isn't finished until there is a
scenario here that fails before it and passes after it. Then nobody has to
ring in and reproduce it by hand.

This talks to the REAL deployed backend, so it proves what is live, not
what is on your laptop. check.py proves the code holds together;
scenarios.py proves the running system behaves.

Needs, from the environment:
    BACKEND_URL      https://web-production-13961.up.railway.app
    SERVICE_TOKEN    the same token the voice agent uses
    TEST_ACCOUNT_ID  which account to read (default 1)

Nothing here sends an email, places an order or spends money.
"""
import os
import sys
import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

BACKEND = os.environ.get(
    "BACKEND_URL", "https://web-production-13961.up.railway.app").rstrip("/")
TOKEN = os.environ.get("SERVICE_TOKEN", "")
ACCOUNT = int(os.environ.get("TEST_ACCOUNT_ID", "1"))
LOCAL_TZ = os.environ.get("LOCAL_TZ", "America/New_York")

RESULTS = []


def _tz():
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(LOCAL_TZ)
    except Exception:
        return timezone.utc


def call(path, method="GET", body=None, **params):
    """One request to the live backend."""
    url = BACKEND + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw else {}


def scenario(name, because):
    """because = the real call this came from, so nobody deletes it later."""
    def wrap(fn):
        if len(sys.argv) > 1 and not any(a.lower() in name.lower()
                                         for a in sys.argv[1:]):
            return fn
        try:
            fn()
            print(f"  ok   {name}")
            RESULTS.append(True)
        except Exception as e:
            print(f"  FAIL {name}")
            print(f"       {e}")
            print(f"       (from: {because})")
            RESULTS.append(False)
        return fn
    return wrap


import urllib.parse            # noqa: E402  (used by call())

print(f"replaying real problems against {BACKEND}\n")

if not TOKEN:
    print("SERVICE_TOKEN is not set - nothing can be checked.")
    raise SystemExit(2)


# ----------------------------------------------------------------- email

@scenario("email: every message comes with a date",
          "call 37 - asked when an email arrived, it had no idea")
def _():
    d = call("/test/search", account_id=ACCOUNT, q="in:inbox", limit=5,
             newest_first=True)
    msgs = d.get("messages", [])
    assert msgs, "no messages came back at all"
    for m in msgs:
        assert m.get("when"), f"no date on: {m.get('subject')}"


@scenario("email: the sender's real address is included",
          "call 37 - asked to verify a sender, it invented an address")
def _():
    d = call("/test/search", account_id=ACCOUNT, q="in:inbox", limit=5,
             newest_first=True)
    for m in d.get("messages", []):
        assert "@" in (m.get("from") or ""), \
            f"no address for {m.get('subject')!r}, only a display name"


@scenario("email: times are the caller's clock, not UTC",
          "call 38 - said 7:17 PM for an email that arrived at 3:17 PM")
def _():
    d = call("/test/search", account_id=ACCOUNT, q="in:inbox", limit=5,
             newest_first=True)
    now = datetime.now(_tz())
    said = [m["when"] for m in d.get("messages", []) if m.get("when")]
    todays = [s for s in said if s.startswith("today at")]
    assert todays or said, "nothing to check"
    for s in todays:
        hhmm = s.replace("today at", "").strip()
        t = datetime.strptime(hhmm, "%I:%M %p")
        stamp = now.replace(hour=t.hour, minute=t.minute,
                            second=0, microsecond=0)
        assert stamp <= now + timedelta(minutes=5), (
            f"an email is dated {hhmm}, which is still in the future for "
            f"the caller ({now:%I:%M %p}) - that's the UTC bug")


@scenario("email: most recent really is most recent",
          "call 38 - read out a 4:07 PM email before a 7:17 PM one")
def _():
    d = call("/test/search", account_id=ACCOUNT, q="in:inbox", limit=6,
             newest_first=True)
    stamps = [m.get("at_ms", 0) for m in d.get("messages", [])]
    assert stamps == sorted(stamps, reverse=True), \
        f"not newest-first: {[m.get('when') for m in d.get('messages', [])]}"


# ---------------------------------------------------------- house rules

@scenario("rules: a blocked topic is refused on the browser too",
          "the filter only ran on search, so browsing to news got through")
def _():
    for path, params in (("/jobs/browse", {"account_id": ACCOUNT,
                                           "goal": "read me today's news"}),
                         ("/jobs/site-search", {"account_id": ACCOUNT,
                                                "site": "x",
                                                "query": "sports scores"})):
        d = call(path, "POST", **params)
        assert d.get("blocked"), f"{path} did not refuse: {str(d)[:120]}"


@scenario("rules: a one-time code is cleaned before it is typed in",
          "call 38 - speech-to-text produced Chinese numerals for a code")
def _():
    try:
        call("/jobs/code", "POST", body={"job_id": -1, "code": "123456"})
    except urllib.error.HTTPError as e:
        assert e.code == 400, f"expected a plain refusal, got {e.code}"
    else:
        raise AssertionError("a made-up job id was accepted")


# --------------------------------------------------------------- money

@scenario("costs: a call's usage is actually recorded",
          "calls 40 and 41 recorded nothing, so cost was invisible")
def _():
    probe = 990001
    d = call("/usage", "POST", body={"call_id": probe, "account_id": ACCOUNT,
                                     "audio_in": 10000, "audio_out": 2000,
                                     "call_seconds": 120})
    assert d.get("cost_usd", 0) > 0, f"priced at zero: {d}"
    back = call("/usage/call", call_id=probe)
    assert back.get("cost_usd", 0) > 0, f"nothing stored: {back}"
    assert "voice model in" in back.get("breakdown_usd", {}), back


@scenario("costs: the browser's model is counted separately",
          "browser tokens were never counted, so an upgrade was invisible")
def _():
    probe = 990002
    call("/usage", "POST", body={"call_id": probe, "account_id": ACCOUNT,
                                 "brain_in": 100000, "brain_out": 5000})
    back = call("/usage/call", call_id=probe)
    assert "browser brain" in back.get("breakdown_usd", {}), \
        f"no browser-brain line: {back.get('breakdown_usd')}"


# ------------------------------------------------------------- plumbing

@scenario("setup: the backend can reach the model it is configured to use",
          "the browser silently failed for months with no OpenAI key")
def _():
    d = call("/models")
    assert d.get("openai_key_set_on_backend"), \
        "the backend has no OPENAI_API_KEY - every browser job will fail"
    assert not d.get("error"), d.get("error")
    assert d.get("browser_model_exists"), \
        f"{d['in_use']['browser']} is not available on this account"


@scenario("setup: the live log shows the caller's clock",
          "the admin panel showed everything in UTC")
def _():
    rows = call("/events", limit=5)
    assert rows, "no events at all"
    now = datetime.now(_tz())
    newest = rows[-1]["at"]
    t = datetime.strptime(newest, "%I:%M:%S %p")
    stamp = now.replace(hour=t.hour, minute=t.minute, second=0,
                        microsecond=0)
    drift = abs((now - stamp).total_seconds())
    assert drift < 3 * 3600 or drift > 21 * 3600, (
        f"newest event says {newest}, local time is {now:%I:%M:%S %p} - "
        f"that looks like a timezone problem")


# --------------------------------------------------------------- result

print()
if not RESULTS:
    print("nothing matched - check the name you passed")
    raise SystemExit(2)
bad = RESULTS.count(False)
if bad:
    print(f"{bad} of {len(RESULTS)} scenarios FAILED - the live system is "
          f"still doing the thing a customer complained about")
    raise SystemExit(1)
print(f"all {len(RESULTS)} scenarios pass against the live system")
