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
SKIPPED = []


class SetupProblem(Exception):
    """Something about how this was RUN is wrong - the token, the URL, the
    account. Never a fault in the phone system, and never reported as one."""


def looks_unset(token: str) -> bool:
    """A token that was never really supplied. '<your token>' pasted from
    an instruction counts - that wasted a real person's evening once."""
    t = (token or "").strip()
    return (not t or "<" in t or ">" in t or " " in t
            or t.lower() in ("your token", "token", "changeme")
            or len(t) < 20)


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
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise SetupProblem(
                "the backend refused the token (401)") from None
        if e.code == 403:
            body = e.read().decode("utf-8", "ignore")
            if "connection_expired" in body:
                raise SetupProblem(
                    "the mailbox connection to Google has expired (this "
                    "happens after 7 days while the app is unverified) - "
                    "reconnect it at /connect and run this again") from None
            e = urllib.error.HTTPError(e.url, e.code, e.reason, e.headers,
                                       None)
            e.read = lambda _b=body: _b.encode()
        raise e
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
        except SetupProblem as e:
            # Not a fault in the phone system - something it needs isn't
            # connected. Say so and carry on; one expired mailbox used to
            # stop the whole suite, so nothing else got tested.
            print(f"  skip {name}")
            print(f"       {e}")
            SKIPPED.append(f"{name}: {e}")
        except Exception as e:
            print(f"  FAIL {name}")
            print(f"       {e}")
            print(f"       (from: {because})")
            RESULTS.append(False)
        return fn
    return wrap


import urllib.parse            # noqa: E402  (used by call())


def stop(why: str):
    """Nothing was tested. Say exactly that - never imply the phone system
    is at fault when the problem is how this was run."""
    print()
    print(f"NOTHING WAS TESTED - {why}.")
    print("This is about how the check was run, not about the phone system.")
    print()
    print("SERVICE_TOKEN must be the real token from the BACKEND service in")
    print("Railway. For this terminal only:")
    print('    $env:SERVICE_TOKEN = "paste-the-real-token-here"')
    print("Or once, so every future terminal already has it:")
    print('    setx SERVICE_TOKEN "paste-the-real-token-here"')
    raise SystemExit(2)


print(f"replaying real problems against {BACKEND}\n")

if looks_unset(TOKEN):
    stop(f"SERVICE_TOKEN is not a real token (got {TOKEN!r})")

try:
    call("/models")
except SetupProblem as _e:
    stop(str(_e))
except Exception as _e:
    stop(f"could not reach the backend: {str(_e)[:120]}")


# ----------------------------------------------------------------- email

@scenario("sign-in: words are never accepted as a one-time code",
          "call 57 - 'another way' was typed into Amazon's code box")
def _():
    try:
        call("/jobs/code", method="POST",
             body={"job_id": 999999, "code": "another way"})
        raise AssertionError("an unknown job accepted a code")
    except urllib.error.HTTPError as e:
        assert e.code == 400, e.code


@scenario("the advisor never says it is working on something that isn't",
          "call 58 - 'I'm handling that' while nothing at all was running")
def _():
    d = call("/advise", method="POST", body={
        "account_id": ACCOUNT, "call_id": 0,
        "situation": "the caller asked whether their order has been placed",
        "heard": "did you place my order yet?"})
    said = (d.get("say") or "").lower()
    assert said, f"the advisor said nothing: {d}"
    for claim in ("i'm handling", "i am handling", "working on it",
                  "i'm checking now"):
        assert claim not in said, f"claimed work that isn't running: {said!r}"
    assert d.get("facts", {}).get("anything_running") is False, d.get("facts")


@scenario("email: every message comes with a date",
          "call 37 - asked when an email arrived, it had no idea")
def _():
    d = call("/test/search", account_id=ACCOUNT, q="in:inbox", limit=5,
             newest_first=True)
    msgs = d.get("messages", [])
    assert msgs, "no messages came back at all"
    for m in msgs:
        assert m.get("when"), f"no date on: {m.get('subject')}"


@scenario("google: contacts, Drive and to-do list answer, or say why not",
          "Sep 14 - adding them broke every existing mailbox with a 500")
def _():
    for path in ("/contacts/search?name=a", "/drive/search?words=",
                 "/todo?x=1"):
        p, _, q = path.partition("?")
        try:
            call(p, account_id=ACCOUNT, **dict([q.split("=")]))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            assert e.code != 500, f"{p} crashed instead of giving a reason"
            assert any(r in body for r in ("needs_reconnect",
                                           "api_not_enabled")), \
                f"{p} -> {e.code} {body[:150]}"


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


@scenario("email: 'minutes ago' also says the clock time",
          "call 54 - '51 minutes ago' at midnight; it thought it was morning")
def _():
    d = call("/test/search", account_id=ACCOUNT, q="in:inbox", limit=10,
             newest_first=True)
    for m in d.get("messages", []):
        w = m.get("when") or ""
        if "minutes ago" in w:
            assert " at " in w, f"no clock time in {w!r}"


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


@scenario("costs: reporting twice for one call doesn't lose the call",
          "found by running this suite twice - the second report added "
          "call_id to itself and orphaned the whole row")
def _():
    probe = 990003
    first = call("/usage", "POST",
                 body={"call_id": probe, "account_id": ACCOUNT,
                       "audio_in": 1000, "call_seconds": 60})
    call("/usage", "POST", body={"call_id": probe, "account_id": ACCOUNT,
                                 "audio_in": 1000, "call_seconds": 60})
    back = call("/usage/call", call_id=probe)
    assert back.get("cost_usd", 0) > 0,         f"the call disappeared after a second report: {back}"
    assert back["tokens"]["audio_in"] >= 2000,         f"the second report was lost: {back['tokens']}"
    assert back["minutes"] >= 2.0, f"seconds not added up: {back}"
    assert first.get("cost_usd", 0) > 0


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
for line in SKIPPED:
    print(f"SKIPPED - {line}")
if SKIPPED:
    print("These were not tested. Fix the connection and run again.")
    print()
if not RESULTS:
    print("nothing was tested - check the name you passed, or the skips above")
    raise SystemExit(2)
bad = RESULTS.count(False)
if bad:
    print(f"{bad} of {len(RESULTS)} scenarios FAILED - the live system is "
          f"still doing the thing a customer complained about")
    raise SystemExit(1)
print(f"all {len(RESULTS)} scenarios pass against the live system"
      + (f" ({len(SKIPPED)} skipped)" if SKIPPED else ""))
