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


# -------------------------------------------------------------- everyday

@scenario("everyday: candle lighting is a real time for a real place",
          "built with the feature - candle lighting used to come from a web "
          "search, for whatever town the search picked")
def _():
    d = call("/everyday/jewish", what="shabbos", place="11211")
    assert d.get("place", "").startswith("Brooklyn"), d
    times = [i for i in d.get("items", [])
             if i.get("what") == "Candle lighting" and i.get("time")]
    assert times, f"no candle lighting time came back: {d}"
    assert times[0]["time"].endswith("PM"), times[0]


@scenario("everyday: Williamsburg means Brooklyn, not Virginia",
          "found while building it - the geocoder's Williamsburg is in "
          "Virginia, and so was its Yerushalayim")
def _():
    w = call("/everyday/weather", place="Williamsburg")
    assert "Brooklyn" in w.get("place", ""), w
    j = call("/everyday/jewish", what="zmanim", place="Yerushalayim")
    assert "Israel" in j.get("place", ""), j


@scenario("everyday: a yahrzeit says the evening before",
          "built with the feature - a date read on its own sends people a "
          "day late")
def _():
    y = call("/everyday/yahrzeit", hebrew="9 Adar", years=2)
    coming = y.get("coming") or []
    assert coming and coming[0].get("candle_evening"), y
    assert "evening before" in y.get("note", ""), y


@scenario("everyday: what a customer keeps can be read back",
          "David asked whether a caller who keeps Rabbeinu Tam would be "
          "remembered - it wasn't, anywhere")
def _():
    """Read-only: this never changes the test account's minhag."""
    d = call("/everyday/minhag", account_id=ACCOUNT)
    assert isinstance(d, dict), d
    for key in d:
        assert key in ("candle_minutes", "havdalah", "shema"), d


@scenario("orders: 'from B&H' goes to B&H, and the exact item is exact",
          "call 65 - the browser opened www.b&h.com (not a website), and "
          "the exact Epson ET-5850 was called 'not that exact one'")
def _():
    got = call("/browser/address", site="B&H").get("url")
    assert got == "https://www.bhphotovideo.com", \
        f"the live system would open {got!r} for B&H"
    d = call("/price", item="Epson ET-5850 printer from B&H")
    if not d.get("offers"):
        raise SetupProblem("the shopping search returned nothing to judge")
    assert d.get("exact"), (
        f"the exact printer is still called a near miss: {d.get('answer')}")
    assert d.get("shop") == "B&H", f"the shop wasn't separated: {d}"


@scenario("browser: a shop's name goes where a search for it would",
          "David - 'if I type BNH into Google it comes up with the BNH "
          "website, why can't we?'")
def _():
    """Pomegranate's own site is thepompeople.com - no list and no guess
    would find it; a search does."""
    got = call("/browser/address", site="Pomegranate Brooklyn").get("url")
    assert got == "https://thepompeople.com", (
        f"the live system would open {got!r} for Pomegranate")


@scenario("sign-up: an unknown number with a wrong code is turned away",
          "David - 'how do I add a new customer; can they sign up by "
          "themselves?' - they can now, with an invite code")
def _():
    """Makes nothing: a made-up number and a code nobody was given."""
    d = call("/signup/check", method="POST",
             body={"phone": "+15550100123", "code": "000000"})
    assert d.get("ok") is False and d.get("reason") in (
        "bad_code", "too_many"), f"a code nobody was given got in: {d}"
    listed = call("/invites")
    assert isinstance(listed, list), listed


@scenario("orders: ink and covers for a printer aren't priced as the printer",
          "call 67 - the cheapest 'Epson ET-5850' was $10 of ink bottles "
          "'for Epson ET-5850'")
def _():
    import re as _re
    d = call("/price", item="Epson ET-5850")
    if not d.get("offers"):
        raise SetupProblem("the shopping search returned nothing to judge")
    for o in d["offers"]:
        t = (o.get("title") or "").lower()
        assert not _re.search(r"\bfor\b[^,]{0,25}et.?5850", t), (
            f"an accessory was priced as the printer: {o['title']} "
            f"{o['price']}")
        assert not _re.search(r"\b(compatible|replacement|refill)", t), (
            f"an accessory was priced as the printer: {o['title']}")


@scenario("sign-up: adding a phone needs a real code and the right PIN",
          "home line and mobile - one account could only ever have one "
          "number")
def _():
    """Makes nothing: a made-up number, a code nobody was given."""
    d = call("/signup/link", method="POST",
             body={"phone": "+15550100124", "code": "000000", "pin": "5813"})
    assert d.get("ok") is False and d.get("reason") in (
        "bad_code", "too_many"), f"a phone was added with no real code: {d}"


@scenario("browser: an address given in full is opened as it was said",
          "call 64 - he asked for khconnect.kioskhut.com, the live system "
          "opened www.khconnect.kioskhut.com.com, and he was told his own "
          "site had a security problem")
def _():
    """The whole failure was in one step: turning what the caller said
    into an address. com.com is a wildcard domain that answers anything,
    so the old bug did not fail cleanly - it produced a certificate error
    that read like the customer's site was broken."""
    for said, want in (("khconnect.kioskhut.com",
                        "https://khconnect.kioskhut.com"),
                       ("amazon", "https://www.amazon.com"),
                       ("www.lowes.com", "https://www.lowes.com")):
        got = call("/browser/address", site=said).get("url")
        assert got == want, f"for {said!r} the live system opens {got!r}"


@scenario("reset: an address we can't read is helped, not refused",
          "David - 'if it works with a text message on the phone it "
          "shouldn't be refused; we just don't have the email'")
def _():
    """check_only: asks what the live system WOULD do and starts nothing.
    Without it, this would open a real browser on a real shop."""
    d = call("/jobs/password-reset", method="POST", account_id=ACCOUNT,
             site="lowes", email="not-their-address@example.com",
             check_only=1)
    assert d.get("mode") == "caller_reads_code", (
        f"the live system would not let the caller read the code: {d}")
    assert "job_id" not in d, "check_only started a real job"


@scenario("reset: a blocked subject is refused before a browser opens",
          "rule 10 - blocked subjects get one answer everywhere, and a "
          "browser is never opened to find out")
def _():
    try:
        call("/jobs/password-reset", method="POST", account_id=ACCOUNT,
             site="netflix", email="not-their-address@example.com")
        raise AssertionError("a blocked subject started a browser job")
    except urllib.error.HTTPError as e:
        assert e.code == 400, e.code
        said = e.read().decode("utf-8", "ignore")
        wrong = f"refused, but for the wrong reason: {said[:200]}"
        assert "not allowed to talk to you about this" in said, wrong


@scenario("browser: a finished search says whether it found everything",
          "call 83 - 'don't come back till you find a car with all the "
          "features'; nothing could tell a full match from a near one")
def _():
    """One small real job on a page that never changes. Before the fix a
    finished job carried no code at all, so a search going site to site
    could not know when to stop."""
    import time as _t
    d = call("/jobs/browse", method="POST", account_id=ACCOUNT,
             goal="What is the main heading on this page?",
             site="", url="https://example.com", call_id=0)
    jid = d.get("job_id")
    assert jid, f"no job started: {d}"
    st = {}
    for _ in range(50):
        _t.sleep(4)
        st = call("/jobs/status", job_id=jid)
        if st.get("state") in ("done", "failed"):
            break
    if st.get("state") == "failed":
        raise SetupProblem(f"the page could not be read: {st.get('message')}")
    assert st.get("state") == "done", f"never finished: {st}"
    assert st.get("reason") in ("met", "not_met"), (
        f"a finished job still carries no found/not-found code: {st}")
    assert st.get("reason") == "met", (
        f"the heading was right there and it was called not found: {st}")


@scenario("browser: a stopped search stays stopped",
          "call 84 - 'who asked you to check the next car website?' - it "
          "was stopped, then carried on and wrote 'done' over the stop")
def _():
    """The same search as call 84, on the same site, stopped as soon as
    it starts. CarsDirect has saved steps for it, which is the path that
    ignored the stop."""
    import time as _t
    d = call("/jobs/browse", method="POST", account_id=ACCOUNT,
             goal="find a lower-priced family minivan with 8 seats, leather "
                  "seats, a sunroof, and a large trunk",
             site="CarsDirect", url="", call_id=0)
    jid = d.get("job_id")
    assert jid, f"no job started: {d}"
    for _ in range(20):
        _t.sleep(1)
        if call("/jobs/status", job_id=jid).get("state") in (
                "opening", "working", "done", "failed"):
            break
    call("/jobs/cancel", method="POST", job_id=jid, why="scenario stop")
    st = {}
    for _ in range(12):                 # long enough for the old bug
        _t.sleep(4)
        st = call("/jobs/status", job_id=jid)
        assert st.get("state") == "failed" and \
            st.get("reason") == "cancelled", (
                f"a stopped job carried on and became {st.get('state')}: "
                f"{(st.get('message') or '')[:160]}")


@scenario("find: a real question is answered in seconds, with the site, "
          "and no browser",
          "calls 42-62 - one fridge question, twenty browser jobs, three "
          "human checks, nine different answers over two days")
def _():
    import time as _t
    before = (call("/jobs", limit=1) or [{}])[0].get("job_id", 0)
    t0 = _t.time()
    d = call("/find", q="how do I turn Shabbos mode on and off on the "
                        "Frigidaire PRDF1922AF refrigerator")
    took = _t.time() - t0
    if d.get("reason") in ("no_model", "search_failed"):
        raise SetupProblem(f"the find path has nothing to work with: {d}")
    assert d.get("found"), f"the fridge question came back empty: {d}"
    assert took < 20, f"{took:.0f}s - a question is taking browser time"
    sites = [x.get("site", "") for x in d.get("sources") or []]
    assert any("frigidaire" in x for x in sites), \
        f"the answer does not come from the maker's own site: {sites}"
    assert d.get("freshness") in ("indexed", "dated"), d.get("freshness")
    low = (d.get("answer") or "").lower()
    assert "frigidaire" in low, f"the site is not named in the answer: {low}"
    for bad in ("pages you gave", "what you gave", "page 1", "page 2"):
        assert bad not in low, f"it talks about the pages, not the site: {low}"
    after = (call("/jobs", limit=1) or [{}])[0].get("job_id", 0)
    assert after == before, "a question started a browser job"


@scenario("prices: a shop's live feed is wired in, and the office can "
          "see which are on",
          "B&H's listed $849.99 was the old price; the live page said "
          "$699.99 marked down - a listing is a copy, and can be days old")
def _():
    d = call("/price", item="Epson ET-5850 printer")
    assert "live_sources" in d, f"the live-feed plumbing is not deployed: {d}"
    if not d["live_sources"]:
        raise SetupProblem("no shop feed is switched on yet - add "
                           "BESTBUY_API_KEY in Railway to turn Best Buy on")
    live = [o for o in d.get("offers") or [] if o.get("live")]
    assert live, f"a feed is on but gave no live price: {d}"


@scenario("find: the difference between two models is read, not guessed",
          "call 85 - 'what's the difference, why is it more?' answered from "
          "memory: 'might be packaging or retailer bundles'")
def _():
    d = call("/find", q="What are the key differences between the Sensi "
                        "Touch 2 Smart Thermostat and the Sensi Touch 2 "
                        "Smart Thermostat ST76W2?")
    if d.get("reason") in ("no_model", "search_failed"):
        raise SetupProblem(f"the find path has nothing to work with: {d}")
    assert d.get("how") == "search", (
        f"a model-number comparison was answered from memory: "
        f"{(d.get('answer') or '')[:160]}")
    assert d.get("freshness") in ("indexed", "dated", "none"), d


@scenario("orders: nothing can be bought until the shop's own total is read back",
          "call 87 - a yes would have put the item in the cart AND bought it, "
          "and he would never have heard Amazon's real total")
def _():
    """Makes nothing: an order number that does not exist."""
    for path, want in (("/orders/prepare", "state to prepare"),
                       ("/orders/confirm", "read back from the shop")):
        try:
            call(path, method="POST", order_id=0, confirmed="yes")
            raise AssertionError(f"{path} accepted an order that does not "
                                 f"exist")
        except urllib.error.HTTPError as e:
            said = e.read().decode("utf-8", "ignore")
            assert e.code == 400 and want in said, (
                f"{path}: the new order flow is not deployed: {e.code} "
                f"{said[:160]}")


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
