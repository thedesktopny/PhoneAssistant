"""
Run this BEFORE every git push:   python check.py

It boots the backend and the voice agent without touching the internet,
and fails loudly if anything that has broken before is broken again.
Add a line to CHECKS every time something new breaks - that's how the
list earns its keep.
"""
import io
import os
import re
import sys

os.environ.setdefault("GOOGLE_CLIENT_ID", "x")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "x")
os.environ.setdefault("PUBLIC_URL", "https://example.com")
os.environ.setdefault("ENCRYPTION_KEY",
                      "yVNCegp4QZYxqPeZtybDoMIwk2P_PCA98G0zeVQ-_bk=")
os.environ.setdefault("BACKEND_URL", "https://example.com")
os.environ.setdefault("LIVEKIT_URL", "wss://x")
os.environ.setdefault("LIVEKIT_API_KEY", "x")
os.environ.setdefault("LIVEKIT_API_SECRET", "x")
os.environ.setdefault("OPENAI_API_KEY", "x")
os.environ.setdefault("DATABASE_URL", "sqlite:///check_tmp.db")

def everywhere(name, value):
    """Set a name in every backend module that has one, and give back a
    function that puts them all back.

    A star-import hands each module its own reference, so patching one
    module's copy leaves the others on the real thing - which is how a
    test reached the live Stripe API after a function moved house. A
    check should not have to know which file something lives in.
    """
    import glob
    import sys as _sys
    changed = []
    for f in sorted(glob.glob("*.py")):
        mod = _sys.modules.get(f[:-3])
        if mod is not None and hasattr(mod, name):
            changed.append((mod, getattr(mod, name)))
            setattr(mod, name, value)

    def restore():
        for mod, was in changed:
            setattr(mod, name, was)
    return restore


def source(which: str = "backend") -> str:
    """The backend source, whichever file it lives in.

    Checks used to read main.py directly, so moving a function into
    another file broke checks that had nothing to do with the move. A
    check should care that the rule holds somewhere in the backend, not
    which file it is typed in.
    """
    import glob
    skip = {"check.py", "scenarios.py", "probe.py"}
    if which == "agent":
        files = ["agent.py"]
    else:
        files = [f for f in sorted(glob.glob("*.py"))
                 if f not in skip and f != "agent.py"]
    return chr(10).join(io.open(f, encoding="utf-8").read()
                          for f in files)


FAILS = []


def check(name):
    def wrap(fn):
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as e:
            print(f"  FAIL {name}: {e}")
            FAILS.append(name)
        return fn
    return wrap


# ------------------------------------------------------------ backend
print("backend")


@check("main.py imports and the server builds")
def _():
    global main
    import main


@check("every admin page and API route still exists")
def _():
    paths = {r.path for r in main.app.routes}
    for p in ("/admin", "/events", "/calls/turn", "/calls/start",
              "/onboard/start", "/onboard/status", "/jobs/status",
              "/orders/status", "/followups", "/usage", "/usage/summary",
              "/browser/where", "/email/mark_read", "/sms/incoming",
              "/link/start", "/link/callback"):
        assert p in paths, f"route missing: {p}"


@check("admin panel renders with all tabs")
def _():
    from fastapi.testclient import TestClient
    c = TestClient(main.app, raise_server_exceptions=False, base_url="https://t")
    c.post("/admin/login", json={"password": os.environ.get(
        "ADMIN_PASSWORD", "changeme")})
    html = c.get("/admin").text
    for tab in ("p-live", "p-costs", "p-overview", "p-calls", "p-orders"):
        assert tab in html, f"tab missing: {tab}"


@check("scrubber hides secrets but leaves normal speech alone")
def _():
    keep = ["I need to verify your PIN before we continue.",
            "Let me confirm the password back to you."]
    hide = ["the password 'Desktop2020!'", "password is Hunter22",
            "capital D, e, s, k, t, o, p, two, zero"]
    for t in keep:
        assert main.scrub(t) == t, f"scrubber mangled: {t}"
    for t in hide:
        assert main.scrub(t) != t, f"scrubber leaked: {t}"


@check("browser helpers survive a page navigation")
def _():
    class P:
        url = "https://x"
        n = 0

        def query_selector(self, s):
            self.n += 1
            if self.n < 2:
                raise Exception("Execution context was destroyed, "
                                "most likely because of a navigation")
            return "EL"

        def inner_text(self, s): return "hello"
        def wait_for_load_state(self, *a, **k): pass
        def wait_for_timeout(self, ms): pass
    assert main.q(P(), "x") == "EL"


@check("no raw page calls outside the safe helpers")
def _():
    src = source()
    start = src.index("# --------------------------------------------------- assisted Gmail sign-in")
    # stop at the admin page - its JavaScript legitimately calls .click()
    body = src[start:src.index("ADMIN_HTML = ")]
    for bad, use in (("page.query_selector(", "q()"),
                     ("page.goto(", "do_goto()"),
                     ("page.fill(", "do_fill()"),
                     ("page.inner_text(", "page_text()"),
                     ("page.url", "page_url()"),
                     (".click()", "do_click()")):
        # allowed only inside the helper definitions above 'start'
        assert bad not in body, f"raw {bad} found - use {use} instead"


@check("nothing shadows a page helper (the q= landmine)")
def _():
    """A local named q, do_click, ... makes the real helper unreachable for
    the whole function - an UnboundLocalError the moment someone follows
    rule 3 and calls it."""
    import ast
    helpers = {"q", "q_all", "page_text", "page_url", "do_click", "do_fill",
               "do_goto", "settle"}
    tree = ast.parse(source())
    bad = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name in helpers:
            continue
        uses = {n.func.id for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if not uses & helpers:
            continue          # doesn't touch a page, can't be bitten
        for node in ast.walk(fn):
            if (isinstance(node, ast.Name) and node.id in helpers
                    and isinstance(node.ctx, ast.Store)):
                bad.add(f"{fn.name}() assigns to '{node.id}'")
        for arg in fn.args.args + fn.args.kwonlyargs:
            if arg.arg in helpers:
                bad.add(f"{fn.name}() has an argument named '{arg.arg}'")
    assert not bad, "; ".join(sorted(bad))


@check("blocked topics are refused on the browser path too")
def _():
    """The filter used to run only on web search and texts, so a blocked
    topic was reachable just by browsing to it."""
    from fastapi.testclient import TestClient
    c = TestClient(main.app, raise_server_exceptions=False, base_url="https://t")
    c.post("/admin/login", json={"password": os.environ.get(
        "ADMIN_PASSWORD", "changeme")})
    for path in ("/jobs/browse?account_id=1&goal=read+me+the+latest+news",
                 "/jobs/site-search?account_id=1&site=x&query=sports+scores"):
        d = c.post(path).json()
        assert d.get("blocked"), f"not refused: {path} -> {str(d)[:150]}"


@check("no site needs hand-written setup (the treadmill)")
def _():
    """An unknown site must fall through to the general agent, never to a
    dead end that someone has to go and configure."""
    src = source()
    for dead in ("No setup for", "No order page known"):
        assert dead not in src, (f"'{dead}' still refuses unknown sites - "
                                 f"call _agent_fallback() instead")
    for fn in ("_run_site_login", "_run_site_orders", "_run_site_search"):
        body = src[src.index(f"def {fn}("):]
        body = body[:body.index("\ndef ", 10)]
        assert "_agent_fallback(" in body, f"{fn} has no fallback path"


@check("the browser's model is configurable and nothing is hard-coded")
def _():
    src = source()
    assert '"model": "gpt' not in src, \
        "a model name is hard-coded again - use MODEL_BROWSER/SUMMARY/TEXT"
    assert main.MODEL_BROWSER, "MODEL_BROWSER is empty"
    assert "/models" in {r.path for r in main.app.routes}, \
        "/models is gone - you can't see what the account can run"


@check("browser model tokens reach the costs page")
def _():
    """These were never counted before, so upgrading the model would have
    raised the bill invisibly."""
    from fastapi.testclient import TestClient
    c = TestClient(main.app, raise_server_exceptions=False, base_url="https://t")
    c.post("/admin/login", json={"password": os.environ.get(
        "ADMIN_PASSWORD", "changeme")})
    r = c.post("/usage", json={"call_id": 999998, "account_id": 1,
                               "brain_in": 100000, "brain_out": 10000}).json()
    assert r.get("cost_usd", 0) > 0, f"brain tokens priced at zero: {r}"
    d = c.get("/usage/call?call_id=999998").json()
    assert "browser brain" in d.get("breakdown_usd", {}), \
        f"no browser-brain line in the breakdown: {d}"


@check("a learned search is reusable for a different subject")
def _():
    """Recipes used to bake the typed words in, so the recipe for
    'search for paper towels' typed 'paper towels' at the next caller who
    asked for milk."""
    creds = {"username": "u", "password": "p"}
    # what gets written down when the agent types the task's subject
    assert main._as_placeholder("paper towels", "paper towels") == \
        "TASK_SUBJECT"
    assert main._as_placeholder("towels", "paper towels") == "TASK_SUBJECT"
    # anything that isn't the subject is kept literally
    assert main._as_placeholder("14 Elm St", "paper towels") == "14 Elm St"
    # and it comes back as the NEXT caller's subject
    assert main._recipe_value("TASK_SUBJECT", creds, "milk") == "milk"
    assert main._recipe_value("SAVED_PASSWORD", creds, "milk") == "p"
    assert main._recipe_value("nothing special", creds, "milk") == \
        "nothing special"


@check("an OpenAI failure isn't blamed on Browserbase")
def _():
    """A bad OPENAI_API_KEY on the backend broke every browser job while
    the log said Browserbase had rejected the key."""
    e = Exception("HTTP Error 401: Unauthorized")
    e._from_openai = True
    said = main._browser_error(e)
    assert "OpenAI" in said and "BACKEND" in said, said
    plain = main._browser_error(Exception("HTTP Error 401: Unauthorized"))
    assert "Browserbase" in plain, plain


@check("a decision is read even when the model chats around it")
def _():
    """Swapping the browser model is a Railway variable, so the parser has
    to cope with whatever style the new model replies in."""
    cases = [
        '{"action":"click","index":2}',
        '```json\n{"action":"click","index":2}\n```',
        'Sure - here is the next step:\n{"action":"click","index":2}\nThat '
        'should open the orders page.',
        '{"action":"type","index":1,"text":"a }{ brace in a string"}',
    ]
    for raw in cases:
        got = main._first_json(raw)
        assert got.get("action"), f"could not read a decision from: {raw!r}"
    assert main._first_json("no json at all here") == {}
    assert main._first_json("") == {}


@check("spoken times are the caller's clock, not UTC")
def _():
    """Every time the assistant ever said was 4-5 hours ahead: it formatted
    UTC as if it were local. A 3pm email was read out as 7pm."""
    from datetime import datetime, timedelta, timezone as _tz
    import time as _time
    now = datetime.now(_tz.utc)
    two_h = now - timedelta(hours=2)
    said = main._when(int(two_h.timestamp() * 1000))
    want = two_h.astimezone(main._tz())
    hour = want.strftime("%I:%M %p").lstrip("0")
    assert hour in said, \
        f"said {said!r}, but the caller's clock says {hour}"
    # and the relative wording still works
    five = now - timedelta(minutes=5)
    said = main._when(int(five.timestamp() * 1000))
    assert "minutes ago" in said, said
    assert five.astimezone(main._tz()).strftime("%I:%M %p").lstrip("0") \
        in said, f"'{said}' gives no clock time - the model can't place it"
    assert main._when(int(now.timestamp() * 1000)) == "just now"
    assert main._when("nonsense") == ""


@check("a one-time code is cleaned to ASCII before it's typed in")
def _():
    """Speech-to-text turned a spoken code into Chinese numerals, and
    isalnum() let them straight through to the site."""
    from fastapi.testclient import TestClient
    c = TestClient(main.app, raise_server_exceptions=False, base_url="https://t")
    c.post("/admin/login", json={"password": os.environ.get(
        "ADMIN_PASSWORD", "changeme")})
    main._JOBS[424242] = {"code": None}
    r = c.post("/jobs/code", json={"job_id": 424242, "code": "二九二二二六"})
    assert r.json().get("ok") is False, "unreadable code was accepted"
    c.post("/jobs/code", json={"job_id": 424242, "code": " 29-22 26 "})
    assert main._JOBS[424242]["code"] == "292226", main._JOBS[424242]
    main._JOBS.pop(424242, None)


@check("knowing you're signed in doesn't depend on a word list")
def _():
    """A list of English retailer phrases only ever covers the shops
    someone already added. signed_in() must settle the obvious cases with
    no model call, and must never block a customer when it can't tell."""
    class Page:
        def __init__(self, body, pw=False, url="https://x/account"):
            self.body, self.pw, self.url = body, pw, url

        def query_selector(self, sel):
            return "EL" if ('password' in sel and self.pw) else None

        def inner_text(self, _):
            return self.body

        def wait_for_load_state(self, *a, **k): pass

        def wait_for_timeout(self, ms): pass

    # a password box is decisive, whatever else the page says
    ok, why = main.signed_in(Page("Deliver to David Your Orders", pw=True))
    assert ok is False, why
    # obvious wording, settled without spending anything
    assert main.signed_in(Page("Deliver to David Airmont 10952"))[0] is True
    assert main.signed_in(Page("Sign in or create account"))[0] is False
    # a page in a language no word list covers, and no key to ask with
    key, main.OPENAI_API_KEY = main.OPENAI_API_KEY, ""
    try:
        ok, why = main.signed_in(Page("ההזמנות שלי - שלום דוד"))
        assert ok is True, f"blocked a customer it could not read: {why}"
    finally:
        everywhere("OPENAI_API_KEY", key)


@check("losing the proxy doesn't also lose the signed-in session")
def _():
    """One API call asks for the proxy AND creates the session that keeps
    the customer logged in. A 402 for the proxy used to fail both, so every
    job started logged out and the site demanded a new code each time."""
    import json
    seen = []

    class Fake:
        def __init__(self, body):
            self.body = json.loads(body.decode())

        def read(self):
            return b'{"id": "sess_kept"}'

        def __enter__(self):
            seen.append("proxies" in self.body)
            if "proxies" in self.body:
                raise Exception("HTTP Error 402: Payment Required")
            return self

        def __exit__(self, *a):
            return False

    real_open, real_key = main.urllib.request.urlopen, main.BROWSERBASE_API_KEY
    everywhere("BROWSERBASE_API_KEY", "test")
    main.PROXY_STATUS["proxies_enabled"] = None
    main.urllib.request.urlopen = lambda req, timeout=0: Fake(req.data)
    try:
        sid = main._bb_session("ctx_abc", "US", "NY", "")
    finally:
        main.urllib.request.urlopen = real_open
        everywhere("BROWSERBASE_API_KEY", real_key)
    assert sid == "sess_kept", "gave up on the session when the proxy failed"
    assert seen == [True, False], f"expected a retry without proxies: {seen}"
    assert main.PROXY_STATUS["proxies_enabled"] is False, \
        "claimed a proxy the plan never granted"


@check("a finished sign-in isn't read back as the answer to a lookup")
def _():
    """'Signed in and saved the session' summarised against 'what were my
    recent orders' came out as 'I couldn't find any order details'."""

    src = source()
    i = src.index("def job_answer(")
    body = src[i:src.index("\n@app.", i + 10)]
    assert 'row.kind == "site_login"' in body, \
        "job_answer still summarises a sign-in as if it were a lookup"


@check("the page snapshot is one call, not hundreds")
def _():
    """Asking the browser about each element separately took over two
    minutes for a single step on a big shop, and often returned nothing -
    so the model picked numbers for elements that weren't there."""
    src = source()
    body = src[src.index("def _page_snapshot("):]
    body = body[:body.index("\ndef ", 10)]
    for slow in ("el.is_visible()", "el.get_attribute(", "el.inner_text()",
                 "el.evaluate("):
        assert slow not in body, \
            f"{slow} is back in the snapshot - one round trip per element"
    assert "page_eval(" in body, "the snapshot should run in the page"
    # every entry must be addressable without holding a live handle
    class P:
        url = "https://x"

        def evaluate(self, js, arg=None):
            return [{"tag": "input", "type": "text", "label": "Search"},
                    {"tag": "button", "type": "", "label": "Sign in"}]

        def inner_text(self, _): return "hello"
        def wait_for_load_state(self, *a, **k): pass
        def wait_for_timeout(self, ms): pass
    items, _text = main._page_snapshot(P())
    assert [it["idx"] for it in items] == [0, 1], items
    assert "Sign in" in items[1]["desc"], items


@check("a job stops when the caller hangs up")
def _():
    """A Target job was still running twenty minutes after the call ended,
    spending browser time and model calls on an answer nobody would hear."""
    assert "/jobs/cancel_for_call" in {r.path for r in main.app.routes}
    src = source()
    for fn in ("_run_browse", "_run_checkout"):
        body = src[src.index(f"def {fn}("):]
        body = body[:body.index("\ndef ", 10)]
        assert '"cancelled"' in body, f"{fn} never checks for a hang-up"
    agent_src = open("agent.py", encoding="utf-8").read()
    assert "/jobs/cancel_for_call" in agent_src, \
        "the agent never tells the backend the call ended"


@check("RULE: no stored time is formatted by hand")
def _():
    """Everything in the database is UTC. Formatting one directly shows it
    hours out - in the admin panel, the live log, and the history handed to
    the model. They all go through local_str()."""
    src = source()
    import re as _re
    bad = _re.findall(r"\.(?:at|last_ok|started_at|done_at|placed_at|"
                      r"linked_at)\.strftime\(", src)
    assert not bad, f"{len(bad)} timestamp(s) formatted by hand - use " \
                    f"local_str()"
    from datetime import datetime as _dt, timezone as _tzc
    noon = _dt(2026, 9, 10, 16, 7, tzinfo=_tzc.utc)
    got = main.local_str(noon)
    assert "12:07 PM" in got, f"UTC 4:07pm should read 12:07pm in NY: {got}"
    assert main.local_str(None) == ""


@check("RULE: nothing decides what to do by reading English")
def _():
    """A message that merely contained the word 'password' made the agent
    ask a customer to read their password out again. Anything that DECIDES
    reads a reason code; prose is for people."""
    src = open("agent.py", encoding="utf-8").read()
    import re as _re
    for phrase in ('"password is wrong" in', '"password" in msg',
                   '"check out" in str(e)', '"not signed in" in msg'):
        assert phrase not in src, \
            f"still branching on prose: {phrase} - use the reason code"
    main_src = source()
    for model in ("class Job(", "class Onboard("):
        body = main_src[main_src.index(model):]
        body = body[:body.index("\nclass ")]
        assert "reason = Column" in body, f"{model} has no reason code"


@check("RULE: nothing keeps running after the caller hangs up")
def _():
    """A Target job ran for twenty minutes after the call ended, and a
    half-finished Google sign-in did the same."""
    paths = {r.path for r in main.app.routes}
    for p in ("/jobs/cancel_for_call", "/onboard/cancel"):
        assert p in paths, f"no way to stop work at {p}"
    src = source()
    for fn in ("_run_browse", "_run_checkout", "_run_signin"):
        body = src[src.index(f"def {fn}("):]
        body = body[:body.index("\ndef ", 10)]
        assert "cancelled" in body, f"{fn} never notices a hang-up"
    agent_src = open("agent.py", encoding="utf-8").read()
    for p in ("/jobs/cancel_for_call", "/onboard/cancel"):
        assert p in agent_src, f"the agent never calls {p} when a call ends"


@check("RULE: every call the agent makes carries the service token")
def _():
    """/calls/end was posted without it for months, so no call duration or
    PIN result was ever saved."""
    import re as _re
    src = open("agent.py", encoding="utf-8").read()
    calls = _re.findall(r"await c\.(?:post|request)\((.{0,220}?)\)\n",
                        src, _re.S)
    missing = [c.split("\n")[0].strip()[:60] for c in calls
               if "headers=AUTH" not in c]
    assert not missing, f"posted without the token: {missing}"


@check("a second cost report doesn't destroy the first")
def _():
    """Merging two reports for one call added EVERY number together -
    including call_id, so call 41 became call 82 and its cost vanished.
    Found by running scenarios.py twice."""
    from fastapi.testclient import TestClient
    c = TestClient(main.app, raise_server_exceptions=False, base_url="https://t")
    c.post("/admin/login", json={"password": os.environ.get(
        "ADMIN_PASSWORD", "changeme")})
    body = {"call_id": 970001, "account_id": 7, "kind": "voice",
            "audio_in": 1000, "call_seconds": 60}
    c.post("/usage", json=body)
    c.post("/usage", json=dict(body, kind="browser"))
    d = c.get("/usage/call?call_id=970001").json()
    assert d.get("cost_usd", 0) > 0, f"the call was lost on merge: {d}"
    assert d["tokens"]["audio_in"] == 2000, \
        f"counters should add up: {d['tokens']}"
    assert d["minutes"] == 2.0, f"seconds should add up: {d}"
    rows = c.get("/usage/summary?days=1").json()
    assert rows["calls"] >= 1, "a voice call stopped counting as one"


@check("a page that stops responding ends the job, not 24 wasted steps")
def _():
    """On Target it repeated 'fill username, fill password' twenty-four
    times and never noticed the page hadn't moved."""
    first = main._stuck_note(1)
    assert "changed nothing" in first, first
    second = main._stuck_note(2)
    assert "same thing a third time" in second, second
    assert "goto" in second, "it should be told to navigate directly instead"
    assert "Stop repeating" in main._stuck_note(3)
    src = source()
    body = src[src.index("def _run_browse("):]
    body = body[:body.index("\ndef ", 10)]
    assert "STUCK_LIMIT" in body, "_run_browse never gives up on a dead page"
    assert 'reason="stuck"' in body, "a stuck page needs its own reason code"


@check("changing a username never wipes the saved password")
def _():
    """Call 43: the caller asked to change only his Target username. The
    assistant sent an empty password, we stored it over the real one, and
    the next sign-in said 'no saved login' for an account that was there."""
    acct = 970777
    main.save_site_login(acct, "testsite", "old@x.com", "realpassword")
    out = main.save_site_login(acct, "testsite", "new@x.com", "")
    assert out.get("password_unchanged"), f"password was overwritten: {out}"
    creds = main.use_site_login(acct, "testsite", purpose="check")
    assert creds.get("password") == "realpassword", \
        "the stored password did not survive a username change"
    assert creds.get("username") == "new@x.com", "the username didn't change"
    # and a brand-new login still demands one
    try:
        main.save_site_login(acct, "brandnew", "a@b.com", "")
    except Exception as e:
        assert "password" in str(e).lower(), e
    else:
        raise AssertionError("a new login was saved with no password")
    main.forget_site_login(acct, "testsite")


@check("listening to the agent doesn't count as the caller being absent")
def _():
    """Call 43 was cut off for 'no answer' 13 seconds after the agent
    stopped talking, because the clock ran from the caller's last words
    through the agent's own 30-second reply."""
    src = open("agent.py", encoding="utf-8").read()
    body = src[src.index("async def watchdog("):]
    body = body[:body.index("\n    async def ", 10)]
    assert 'max(last_heard["at"], last_heard["agent_done"])' in body, \
        "the silence clock still ignores when the agent was speaking"
    assert 'last_heard["agent_done"] = time.monotonic()' in src, \
        "nothing records when the agent finished a turn"


@check("running out of browser credit says so, not 'wrong country'")
def _():
    """Browserbase refused a plain browser with 402, and the log said the
    browser was in the wrong country - which sent someone hunting through
    proxy settings for an account that had simply run out."""
    said = main._browser_error(Exception("HTTP Error 402: Payment Required"))
    assert "out of sessions or minutes" in said, said
    five = main._browser_error(Exception(
        "WebSocket error: wss://connect.browserbase.com/ 500 Internal"))
    assert "Browserbase" in five and "dashboard" in five, five
    src = source()
    body = src[src.index("def _bb_session("):]
    body = body[:body.index("\ndef ", 10)]
    assert "_flag_account_limit(" in body, \
        "a 402 without a proxy request is still blamed on geography"


@check("a rotted selector hands over to the agent instead of giving up")
def _():
    """Walmart swapped its email box for a combined phone-or-email field.
    The hand-written selector missed and the job just failed - the exact
    treadmill the fallback exists to stop. Config is an optimisation; the
    agent is the plan."""
    src = source()
    inner = src[src.index("def _do_site_login("):]
    inner = inner[:inner.index("\ndef ", 10)]
    # (the Gmail sign-in has its own wording and no agent fallback - this
    # is only about shop logins)
    for dead in ("No username box", "No password box"):
        assert dead not in inner, \
            f"'{dead}' still ends the job - hand over to the agent instead"
    assert "_agent_fallback(" not in inner, (
        "the handover must not run inside the playwright block - starting a "
        "second sync_playwright inside the first one crashes the job")
    assert inner.count("return (f") >= 3, \
        "not every selector miss hands over to the agent"
    outer = src[src.index("def _run_site_login("):]
    outer = outer[:outer.index("\ndef ", 10)]
    assert "_agent_fallback(" in outer, "nothing performs the handover"


@check("a handed-over job can still be answered")
def _():
    """_do_site_login dropped the job from _JOBS when it handed over, so
    the caller's answer came back 'that job is no longer running' and
    every handed-over job timed out waiting for a reply that was refused."""
    src = source()
    inner = src[src.index("def _do_site_login("):]
    inner = inner[:inner.index("\ndef ", 10)]
    assert "_JOBS.pop(" not in inner, \
        "the browser step still drops the job before the handover runs"
    outer = src[src.index("def _run_site_login("):]
    outer = outer[:outer.index("\ndef ", 10)]
    assert "_JOBS.pop(" in outer, "nothing cleans the job up afterwards"


@check("a captcha is not something a phone caller can be asked to do")
def _():
    """Walmart asked us to 'activate and hold the button to confirm you're
    human'. We passed that on to a caller who cannot see the browser."""
    for wording in ("press and hold to continue",
                    "activating and holding the button to confirm you're "
                    "human",
                    "please complete the captcha",
                    "confirm you're human"):
        assert main.looks_like_bot_check(wording), f"missed: {wording!r}"
    assert not main.looks_like_bot_check("hold on, your order is loading")
    assert not main.looks_like_bot_check("")
    src = source()
    body = src[src.index("def _run_browse("):]
    body = body[:body.index("\ndef ", 10)]
    i = body.index('a == "ask_user"')
    assert "looks_like_bot_check(question)" in body[i:i + 400], \
        "a captcha question is still passed on to the caller"


@check("element [0] can be clicked")
def _():
    """`0 or -1` is -1 in Python, so the first link on every page was
    unclickable. On Kohl's it was the sign-in link; the agent burned every
    step being told 'there is no [-1]'."""
    assert main._action_index({"index": 0}) == 0, "zero became -1 again"
    assert main._action_index({"index": "3"}) == 3
    assert main._action_index({"index": 12}) == 12
    assert main._action_index({}) == -1
    assert main._action_index({"index": None}) == -1
    assert main._action_index({"index": "abc"}) == -1
    assert main._action_index({"index": True}) == -1
    src = source()
    assert 'act.get("index", -1) or -1' not in src, "the or-trap is back"
    assert src.count("_action_index(act)") >= 2, \
        "browse and checkout must both use the safe reader"


@check("an A-B-A-B loop is spotted, not just a page that won't change")
def _():
    """Home Depot went search, error, refresh, search, error, refresh six
    times. The page changed on every step, so 'did anything happen?' never
    tripped."""
    circle = ["type:3:pen", "goto:https://x", "type:3:pen", "goto:https://x",
              "type:3:pen", "goto:https://x"]
    assert main._going_in_circles(circle), "missed an alternating loop"
    progress = ["type:3:pen", "click:5:", "click:9:", "goto:https://y",
                "click:2:", "done:0:"]
    assert not main._going_in_circles(progress), "real progress flagged"
    assert not main._going_in_circles([]), "empty history flagged"
    assert main._action_sig({"action": "goto", "url": "https://a"}) \
        != main._action_sig({"action": "goto", "url": "https://b"})
    assert main._action_sig({"action": "click", "index": 0}) == "click:0:"


@check("a practical task isn't mistaken for a forbidden topic")
def _():
    """Call 45: the caller asked how to turn on Sabbath mode on his fridge
    and was told "I am not allowed to talk to you about this" five times
    until he hung up. The blocked list is about discussing subjects, not
    about tasks that happen to contain one of the words."""
    fine = ["how do I turn on the sabbath mode on my fridge",
            "shabbos mode on my refrigerator",
            "where can I buy kosher chicken near me",
            "what time does the store close before the holiday",
            "order a wedding gift for my niece",
            # Jewish subjects are allowed - this service is for Jewish people
            "what time is candle lighting in monsey this friday",
            "what is the halacha about borer on shabbos",
            "when does the fast end tonight",
            "find me a shul near 11221",
            # and these are real places and names in the community
            "directions to church avenue brooklyn",
            # practical things that merely mention another religion
            "what time does the supermarket close on christmas",
            "is the post office closed for easter",
            "directions to the church on avenue j"]
    for t in fine:
        assert not main.is_blocked(t), f"an ordinary task was blocked: {t}"
    still = ["read me the news", "tell me a joke", "what was the score",
             "tell me about other religions",
             "which religion is the true one"]
    for t in still:
        assert main.is_blocked(t), f"this should still be blocked: {t}"


@check("a card held by Stripe never reaches the database")
def _():
    """Storing real card numbers puts this business inside PCI. With Stripe
    configured, only the token is kept - and the checkout must say so
    rather than silently typing an empty card number into a form."""
    import json as _json
    calls = []

    def fake_stripe(path, fields):
        calls.append((path, fields))
        return {"id": "pm_test_123",
                "card": {"brand": "visa", "last4": "4242",
                         "exp_month": 12, "exp_year": 2034}}

    undo_key = everywhere("STRIPE_SECRET_KEY", "sk_test_probe")
    undo_post = everywhere("_stripe", fake_stripe)
    try:
        from fastapi.testclient import TestClient
        c = TestClient(main.app, raise_server_exceptions=False,
                       base_url="https://t")
        c.post("/admin/login", json={"password": os.environ.get(
            "ADMIN_PASSWORD", "changeme")})
        r = c.post("/cards", json={"account_id": 960001,
                                   "number": "4242 4242 4242 4242",
                                   "exp": "12/34", "cvv": "123",
                                   "name_on_card": "D Tester"}).json()
        assert r.get("last4") == "4242", r
        assert r.get("brand") == "Visa", f"brand should come from Stripe: {r}"
        assert calls and calls[0][0] == "payment_methods", calls
        assert calls[0][1]["card[exp_year]"] == 2034, calls[0][1]
        listed = c.get("/cards?account_id=960001").json()
        assert listed and listed[0]["last4"] == "4242"
    finally:
        undo_key()
        undo_post()

    # the digits must not be anywhere in the stored secret
    db = main.Session()
    row = (db.query(main.PaymentCard)
             .filter_by(account_id=960001).order_by(
                 main.PaymentCard.id.desc()).first())
    blob = row.secret_blob
    held = main.vault_get(blob)
    db.delete(row)
    db.commit()
    db.close()
    assert "4242424242424242" not in _json.dumps(held), \
        "the full card number is still being stored"
    assert held.get("stripe_pm") == "pm_test_123", held
    assert not held.get("number"), "a number was kept alongside the token"
    src = source()
    assert "NOT\n" in src or "available to type in" in src, \
        "checkout is never told the card can't be typed into a form"


@check("a card still saves while Stripe approval is pending")
def _():
    """Stripe refuses card digits from a server until the account is
    approved for phone orders. With the key set but approval pending, the
    caller's card must still save - not fail on the phone."""
    def refuses(path, fields):
        raise main.HTTPException(
            400, "Sending credit card numbers directly to the Stripe API "
                 "is generally unsafe.")

    undo_key = everywhere("STRIPE_SECRET_KEY", "sk_test_probe")
    undo_post = everywhere("_stripe", refuses)
    try:
        from fastapi.testclient import TestClient
        c = TestClient(main.app, raise_server_exceptions=False,
                       base_url="https://t")
        c.post("/admin/login", json={"password": os.environ.get(
            "ADMIN_PASSWORD", "changeme")})
        r = c.post("/cards", json={"account_id": 960002,
                                   "number": "4242 4242 4242 4242",
                                   "exp": "12/34", "cvv": "123"})
        assert r.status_code == 200, \
            f"a caller's card failed to save: {r.status_code} {r.text[:160]}"
        assert r.json().get("last4") == "4242", r.json()
    finally:
        undo_key()
        undo_post()
    db = main.Session()
    for row in db.query(main.PaymentCard).filter_by(account_id=960002).all():
        db.delete(row)
    db.commit()
    db.close()


@check("a blocked page falls through to the next result by itself")
def _():
    """Searching a fridge model puts the manufacturer's support page first,
    and manufacturers block robots. Going back to the agent to choose the
    second result cost the caller half a minute of silence, which is when
    he hung up."""
    src = source()
    body = src[src.index("def _run_browse("):]
    body = body[:body.index("\ndef ", 10)]
    assert "spares" in body, "no fallback list is carried into the job"
    i = body.index("looks_like_bot_check(text)")
    window = body[i:i + 500]
    assert "spares.pop(0)" in window, \
        "a bot check still ends the job instead of trying the next source"
    assert "do_goto(page, nxt" in window, window[:200]


@check("a PDF is read, not browsed")
def _():
    """An appliance manual is a PDF, and a PDF has no text in a browser at
    all. We were scraping videos for something the manufacturer's own
    manual states plainly - which is how Claude answered and we couldn't."""
    assert main.looks_like_pdf("https://x.com/manual/A16366306.pdf")
    assert main.looks_like_pdf("https://x.com/a.PDF?v=2")
    assert not main.looks_like_pdf("https://youtube.com/watch?v=abc")
    assert not main.looks_like_pdf("")
    src = source()
    body = src[src.index("def _run_browse("):]
    body = body[:body.index("\ndef ", 10)]
    assert "looks_like_pdf(start)" in body, \
        "a browse job still opens a PDF in a browser, where it reads blank"
    # (the import at the top doesn't open anything - this is where one does)
    assert body.index("looks_like_pdf(start)") \
        < body.index("with sync_playwright()"), \
        "the PDF check must happen before a browser is started"
    # and a manual should outrank a video in the results
    hits = ["https://youtube.com/watch?v=a",
            "https://frigidaire.com/manual.pdf",
            "https://reddit.com/r/x"]
    hits.sort(key=lambda u: 0 if main.looks_like_pdf(u) else 1)
    assert hits[0].endswith(".pdf"), hits


@check("one stray word in a search result doesn't block the whole search")
def _():
    """A question about Google security assessors was refused because one
    result snippet contained the word "news". Snippets are scraped web
    text - a single word in one is not what the caller asked about."""
    stray = ("Security news and updates for assessors. CASA validation "
             "letters explained.")
    assert len(main.blocked_terms_in(stray)) == 1, stray
    assert main.is_blocked(stray), "a caller saying this should still stop"
    # but as search RESULTS it must get through
    real = ("Breaking news headlines today. Latest news, sports scores and "
            "entertainment.")
    assert len(main.blocked_terms_in(real)) >= 2, \
        "genuinely-news results must still be caught"
    src = source()
    body = src[src.index("def tool_web_search("):]
    body = body[:body.index("\ndef ", 10)]
    assert "blocked_terms_in(snippets)) >= 2" in body, \
        "a single stray word in a snippet can still refuse a whole search"
    assert "is_blocked(out.get(\"answer\"" in body, \
        "the spoken answer must still be judged strictly"


@check("an account-linking link can't be forged")
def _():
    """/link/start took account_id straight from the address with nothing
    checking it. Anyone could send a customer a link carrying THEIR
    account number - the customer would sign into their own Gmail and the
    mailbox would attach to the sender's account, who could then ring in
    and have that person's email read to them."""
    from fastapi.testclient import TestClient
    c = TestClient(main.app, raise_server_exceptions=False,
                   base_url="https://t", follow_redirects=False)
    # the old forgeable form must not work any more
    r = c.get("/link/start?account_id=1")
    assert r.status_code == 400, \
        f"a bare account number still starts a link: {r.status_code}"
    assert "expired" in r.text.lower(), r.text[:200]
    # a tampered or stale ticket is refused
    good = main._make_link_token(1, 30)
    assert main._check_link_token(good) == 1
    flipped = good[:-1] + ("1" if good[-1] == "0" else "0")
    assert main._check_link_token(flipped) is None, \
        "a changed signature was accepted"
    assert main._check_link_token("2." + good.split(".", 1)[1]) is None, \
        "the account number could be swapped"
    assert main._check_link_token(main._make_link_token(1, -10)) is None, \
        "an expired ticket was accepted"
    assert main._check_link_token("") is None
    # and nothing hands out a raw link any more
    src = source()
    assert "/link/start?account_id=" not in src, \
        "something still builds a forgeable link"


@check("Google's answer can't attach a mailbox to someone else's account")
def _():
    """The state Google hands back to /link/callback used to be the bare
    account number. Anyone could build a Google sign-in link carrying THEIR
    number, skip our signed link entirely, and have a victim's mailbox land
    on their account."""
    from fastapi.testclient import TestClient
    c = TestClient(main.app, raise_server_exceptions=False,
                   base_url="https://t", follow_redirects=False)
    r = c.get("/link/callback?state=1&code=x")
    assert r.status_code == 400 and "expired" in r.text.lower(), \
        f"a bare account number was accepted as state: {r.status_code}"
    r = c.get("/link/callback?state=x&error=access_denied")
    assert r.status_code == 200 and "Nothing was connected" in r.text, \
        "saying no on Google's screen should get a plain page, not a crash"
    src = source()
    body = src[src.index("def link_start("):]
    body = body[:body.index("\n@app.", 10)]
    assert "state=str(" not in body and "_flow(state=t)" in body, \
        "the Google link must carry the signed ticket as its state"
    body = src[src.index("def link_callback("):]
    assert "_check_link_token(state)" in body[:600], \
        "the callback must check the signed state"


@check("connect codes work, and can't be guessed or tried forever")
def _():
    from fastapi.testclient import TestClient
    c = TestClient(main.app, raise_server_exceptions=False,
                   base_url="https://t", follow_redirects=False)
    db = main.Session()
    acct = main.Account(name="Code Tester", pin="1234")
    db.add(acct)
    db.commit()
    db.refresh(acct)
    acct_id = acct.id
    db.add(main.PhoneNumber(number="+18455550199", account_id=acct_id))
    db.commit()
    db.close()
    main._CONNECT_FAILS.clear()
    code = main._connect_code(acct_id)
    assert len(code) == 6 and code.isdigit(), code
    assert code != main._connect_code(acct_id + 1), "codes must differ"
    assert c.get("/connect").status_code == 200
    # right number and code -> a signed link for THAT customer
    r = c.post("/connect", json={"phone": "(845) 555-0199",
                                 "code": code[:3] + " " + code[3:]})
    assert r.status_code == 200, r.text
    t = r.json()["url"].split("t=", 1)[1]
    import urllib.parse
    assert main._check_link_token(urllib.parse.unquote(t)) == acct_id
    page = c.get(r.json()["url"]).text
    assert "Code Tester" in page, "the confirm page must say whose account"
    # an old code is dead
    old = main._connect_code(acct_id, int(main.time.time() // 3600) - 2)
    if old != code:
        r = c.post("/connect", json={"phone": "8455550199", "code": old})
        assert r.status_code == 400, "a code from hours ago still works"
    # wrong codes and unknown numbers get the SAME answer
    wrong = "000000" if code != "000000" else "111111"
    a = c.post("/connect", json={"phone": "8455550199", "code": wrong})
    b = c.post("/connect", json={"phone": "2125550000", "code": wrong})
    assert a.status_code == b.status_code == 400
    assert a.json() == b.json(), "the page reveals who is a customer"
    # and it stops after a handful of tries, even with the right code
    for _ in range(6):
        c.post("/connect", json={"phone": "8455550199", "code": wrong})
    r = c.post("/connect", json={"phone": "8455550199", "code": code})
    assert r.status_code == 429, f"no limit on guessing: {r.status_code}"
    main._CONNECT_FAILS.clear()
    # the assistant's code comes from an authorised endpoint only
    assert c.get(f"/link/code?account_id={acct_id}").status_code in (401, 403)


@check("contacts, Drive and to-do list: asked for, and read only where it should be")
def _():
    want = ("contacts", "drive", "tasks", "gmail.modify", "calendar")
    for w in want:
        assert any(sc.endswith("/" + w) for sc in main.SCOPES), \
            f"not asking Google for {w}"
    assert os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE"), \
        "unticking one box on Google's screen would crash the connection"
    paths = {r.path for r in main.app.routes}
    for p in ("/contacts/search", "/contacts/add", "/drive/search",
              "/drive/read", "/todo", "/todo/add", "/todo/done"):
        assert p in paths, f"route missing: {p}"
    src = source()
    for bad in ("files().delete(", "files().update(", "files().emptyTrash(",
                "permissions().create(", "people().deleteContact(",
                "deleteContentRange", "deleteDimension", "values().clear("):
        assert bad not in src, \
            f"something can now {bad} - the assistant never deletes or shares"


@check("adding a permission doesn't break everyone already connected")
def _():
    """Credentials were built with scopes=SCOPES. A refresh then asks Google
    for every scope on the list, and Google refuses the whole refresh if
    the customer never granted one. Adding Contacts, Drive and Tasks made
    every existing connection fail - reading email included."""
    src = source()
    body = src[src.index("def token_permissions("):]
    for chunk in (src[src.index("def gmail_client("):
                      src.index("def gmail_client(") + 900],
                  src[src.index("def google_client("):
                      src.index("def google_client(") + 900],
                  body[:3000]):
        assert "scopes=SCOPES" not in chunk,             "a stored token is refreshed while demanding every scope"
    # Flow is the one place SCOPES belongs: that's the consent request
    assert "Flow.from_client_config(cfg, scopes=SCOPES" in src


@check("a permission they haven't given comes back as a reason, not a crash")
def _():
    """Everyone connected before Contacts/Drive/Tasks were added lacks those
    permissions. Google refuses; the caller must hear "reconnect to allow
    that", not "something went wrong"."""
    import httplib2
    from fastapi.testclient import TestClient
    from googleapiclient.errors import HttpError
    real = main.google_client

    def refusing(body, status=403):
        def fake(*a, **k):
            raise HttpError(httplib2.Response({"status": status}), body)
        return fake
    c = TestClient(main.app, raise_server_exceptions=False, base_url="https://t")
    c.headers["Authorization"] = f"Bearer {main.SERVICE_TOKEN}" \
        if getattr(main, "SERVICE_TOKEN", "") else ""
    try:
        everywhere("google_client", refusing(
            b'{"error":{"details":[{"reason":'
            b'"ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}'))
        r = c.get("/drive/search?account_id=1&words=x")
        if r.status_code in (401, 403) and r.json().get("detail") not in (
                "needs_reconnect",):
            # auth refused before we got there - call the handler directly
            import asyncio
            from starlette.requests import Request as SReq
            req = SReq({"type": "http", "method": "GET", "path": "/drive/search",
                        "headers": [], "query_string": b""})
            exc = HttpError(httplib2.Response({"status": 403}),
                            b"ACCESS_TOKEN_SCOPE_INSUFFICIENT")
            resp = asyncio.run(main.google_refused(req, exc))
            assert resp.status_code == 403 and b"needs_reconnect" in resp.body
            exc = HttpError(httplib2.Response({"status": 403}),
                            b"People API has not been used in project 1")
            resp = asyncio.run(main.google_refused(req, exc))
            assert b"api_not_enabled" in resp.body, resp.body
        else:
            assert r.status_code == 403, r.status_code
            assert r.json()["detail"] == "needs_reconnect", r.text
    finally:
        everywhere("google_client", real)


@check("Drive search can't be broken by a quote, and files read as words")
def _():
    seen = {}

    class Call:
        def __init__(self, out): self.out = out
        def execute(self): return self.out

    class Files:
        def list(self, **k):
            seen.setdefault("q", []).append(k["q"])
            return Call({"files": []})

    class Svc:
        def files(self): return Files()
    real = main.google_client
    everywhere("google_client", lambda *a, **k: Svc())
    try:
        main.tool_drive_search(1, "Moshe's lease")
    finally:
        everywhere("google_client", real)
    assert all("Moshe\\'s lease" in q for q in seen["q"]), seen
    assert any("fullText" in q for q in seen["q"]), \
        "nothing named that should fall back to searching the contents"
    # a Word document comes out as words
    import io as _io, zipfile
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:p><w:r><w:t>Rent is "
                   "due on the first &amp; late after the fifth</w:t></w:r>"
                   "</w:p></w:document>")
    out = main.document_text(buf.getvalue())
    assert "Rent is due on the first & late" in out["text"], out
    assert main.document_text(bytes(range(256)) * 10).get("error"), \
        "a binary file must not be read out as gibberish"
    assert main.document_text(b"hello there")["text"] == "hello there"


@check("Drive editing: words changed only where meant, columns found by name")
def _():
    """A caller says "change the phone number for Moshe" - not "cell C4".
    And "change Monday to Tuesday" in a letter with three Mondays must ask
    first, not quietly change all three."""
    h = ["Name", "Phone number", "Address"]
    assert main._column_number(h, "phone") == 2
    assert main._column_number(h, "Address") == 3
    assert main._column_number(h, "C") == 3
    assert main._column_number(h, "email") == 0, "guessed a column"
    assert main._column_number(["Home phone", "Work phone"], "phone") == 0, \
        "two columns match - it must ask, not pick one"
    assert (main._col_letters(1), main._col_letters(28)) == ("A", "AB")

    class Call:
        def __init__(self, out): self.out = out
        def execute(self): return self.out
    batches = []
    state = {"mime": main.DOC}

    class Files:
        def get(self, **k):
            return Call({"id": "d", "name": "Letter", "mimeType": state["mime"]})
        def export(self, **k):
            return Call(b"See you Monday. Not Monday week, Monday.")

    class Docs:
        def documents(self): return self
        def batchUpdate(self, **k):
            batches.append(k)
            return Call({"replies": [{"replaceAllText":
                                      {"occurrencesChanged": 3}}]})

    class Drive:
        def files(self): return Files()
    real = main.google_client
    everywhere("google_client", lambda a, api, *x, **k: Docs() if api == "docs" else Drive())
    try:
        out = main.tool_doc_replace(1, "d", "monday", "Tuesday")
        assert out["found"] == 3 and not out["changed"] and not batches, \
            "changed three places without asking"
        out = main.tool_doc_replace(1, "d", "monday", "Tuesday",
                                    all_of_them=True)
        assert out["changed"] and len(batches) == 1
        assert main.tool_doc_replace(1, "d", "Friday", "x")["found"] == 0
        # a Word file can't be edited in place: reason code, not a crash
        state["mime"] = ("application/vnd.openxmlformats-officedocument."
                         "wordprocessingml.document")
        try:
            main.tool_doc_add(1, "d", "hi")
            raise AssertionError("edited a Word file in place")
        except main.HTTPException as e:
            assert e.detail == "not_editable", e.detail
    finally:
        everywhere("google_client", real)
    paths = {r.path for r in main.app.routes}
    for pth in ("/drive/doc/create", "/drive/doc/add", "/drive/doc/replace",
                "/drive/sheet/create", "/drive/sheet", "/drive/sheet/add_row",
                "/drive/sheet/update", "/drive/copy_editable",
                "/drive/save_pdf", "/drive/email"):
        assert pth in paths, f"route missing: {pth}"


@check("to-do list: dated things first, dates said the way people say them")
def _():
    class Call:
        def __init__(self, out): self.out = out
        def execute(self): return self.out

    class Tasks:
        def list(self, **k):
            return Call({"items": [
                {"id": "a", "title": "call the plumber"},
                {"id": "b", "title": "pay gas bill", "due": "2026-09-18T00:00:00.000Z"},
                {"id": "c", "title": ""}]})

    class Svc:
        def tasks(self): return Tasks()
    real = main.google_client
    everywhere("google_client", lambda *a, **k: Svc())
    try:
        out = main.tool_tasks_list(1)
    finally:
        everywhere("google_client", real)
    assert [t["title"] for t in out["tasks"]] == ["pay gas bill",
                                                  "call the plumber"], out
    assert out["tasks"][0]["due_spoken"] == "Friday, September 18", out


@check("cards are added on Stripe's page, and only charged the way we mean")
def _():
    """No card number is said aloud or reaches us: the card page hands the
    customer to Stripe. The finish page asks Stripe what happened rather
    than trusting the address bar, and a charge can't repeat or run away."""
    from fastapi.testclient import TestClient
    c = TestClient(main.app, raise_server_exceptions=False,
                   base_url="https://t", follow_redirects=False)
    assert c.get("/card").status_code == 200
    db = main.Session()
    acct = main.Account(name="Card Tester", pin="1234")
    db.add(acct)
    db.commit()
    db.refresh(acct)
    aid = acct.id
    db.add(main.PhoneNumber(number="+18455550177", account_id=aid))
    db.commit()
    db.close()
    main._CONNECT_FAILS.clear()
    # an email connect code is not a card code, and the other way round
    assert main._connect_code(aid, purpose="card") != main._connect_code(aid)
    r = c.post("/card", json={"phone": "8455550177",
                              "code": main._connect_code(aid)})
    assert r.status_code == 400, "an email code opened the card page"
    assert not main._connect_code_ok(aid, main._connect_code(aid, purpose="card"))

    calls = []
    real_call, real_key = main._stripe_call, main.STRIPE_SECRET_KEY
    state = {"session": {}, "charge_error": None}

    def fake(method, path, fields=None, idem=""):
        calls.append((method, path, dict(fields or {}), idem))
        if path == "customers":
            return {"id": "cus_T"}
        if path == "checkout/sessions":
            return {"url": "https://checkout.stripe.com/c/pay/cs_T"}
        if path.startswith("checkout/sessions/"):
            return state["session"]
        if path == "payment_intents":
            if state["charge_error"]:
                raise state["charge_error"]
            return {"id": "pi_T", "status": "succeeded"}
        raise AssertionError(path)
    undo_call = everywhere("_stripe_call", fake)
    undo_key = everywhere("STRIPE_SECRET_KEY", "sk_test_x")
    try:
        r = c.post("/card", json={"phone": "(845) 555-0177",
                                  "code": main._connect_code(aid, purpose="card")})
        assert r.status_code == 200, r.text
        assert r.json()["url"].startswith("https://checkout.stripe.com/")
        made = [x for x in calls if x[1] == "checkout/sessions"][0][2]
        assert made["mode"] == "setup", "the card page must SAVE, not charge"
        assert made["client_reference_id"] == str(aid)
        # an unfinished session, or one for another customer, saves nothing
        state["session"] = {"status": "open", "mode": "setup"}
        assert c.get("/card/done?session_id=cs_T").status_code == 400
        state["session"] = {"status": "complete", "mode": "setup",
                            "client_reference_id": str(aid),
                            "customer": "cus_SOMEONE_ELSE"}
        assert c.get("/card/done?session_id=cs_T").status_code == 400
        done = {"status": "complete", "mode": "setup",
                "client_reference_id": str(aid), "customer": "cus_T",
                "setup_intent": {"payment_method": {
                    "id": "pm_T", "card": {"brand": "visa", "last4": "4242",
                                           "exp_month": 12, "exp_year": 2030}}}}
        state["session"] = done
        r = c.get("/card/done?session_id=cs_T")
        assert r.status_code == 200 and "4242" in r.text, r.text[:200]
        c.get("/card/done?session_id=cs_T")        # reloading the page
        db = main.Session()
        cards = db.query(main.PaymentCard).filter_by(account_id=aid).all()
        card_id = cards[0].id
        db.close()
        assert len(cards) == 1, "reloading the finish page saved it twice"
        assert "4242424242424242" not in str(main.vault_get(cards[0].secret_blob))
        # charging
        out = main.stripe_charge(aid, card_id, 1250, "gas bill", "order-1")
        assert out["charged"] and out["amount"] == "$12.50", out
        pi = [x for x in calls if x[1] == "payment_intents"][-1]
        assert pi[3] == "order-1", "no idempotency key - a retry could charge twice"
        assert pi[2]["off_session"] == "true" and pi[2]["amount"] == "1250"
        for bad, why in ((10, "too_small"),
                         (main.CHARGE_LIMIT_CENTS + 1, "over_limit")):
            try:
                main.stripe_charge(aid, card_id, bad, "x", "k")
                raise AssertionError(f"charged {bad} cents")
            except main.HTTPException as e:
                assert e.detail == why, e.detail
        try:
            main.stripe_charge(aid + 999, card_id, 1000, "x", "k2")
            raise AssertionError("charged a card that isn't theirs")
        except main.HTTPException as e:
            assert e.detail == "no_card"
        state["charge_error"] = main.StripeError("card_declined", "Declined",
                                                 "insufficient_funds")
        out = main.stripe_charge(aid, card_id, 1000, "x", "k3")
        assert out == {**out, "charged": False, "reason": "declined"}, out
        state["charge_error"] = main.StripeError(
            "authentication_required", "Needs auth")
        assert main.stripe_charge(aid, card_id, 1000, "x", "k4")["reason"] \
            == "needs_authentication"
    finally:
        undo_call()
        undo_key()
        main._CONNECT_FAILS.clear()
    # charging is never open to the public
    r = c.post("/charges", json={"account_id": aid, "card_id": card_id,
                                 "amount": "5", "what_for": "x", "key": "k"})
    assert r.status_code in (401, 403), f"/charges without auth: {r.status_code}"


@check("only a real code is ever typed into a code box")
def _():
    """Call 57: the caller had no code, the model called /jobs/code with
    the words "another way", they survived the cleaner as "anotherway",
    got typed into Amazon's box, and Amazon said the code was wrong - so
    the caller was blamed for a mistake we made."""
    from fastapi.testclient import TestClient
    c = TestClient(main.app, raise_server_exceptions=False, base_url="https://t")
    c.post("/admin/login", json={"password": os.environ.get(
        "ADMIN_PASSWORD", "changeme")})
    main._JOBS[424243] = {"code": None}
    for junk in ("another way", "resend", "I didn't get one", "", "???"):
        r = c.post("/jobs/code", json={"job_id": 424243, "code": junk})
        assert r.json().get("ok") is False, f"{junk!r} was sent to the site"
        assert not main._JOBS[424243]["code"], f"{junk!r} was stored"
    for good in ("123456", "1 2 3 4 5 6", "code is 4821"):
        r = c.post("/jobs/code", json={"job_id": 424243, "code": good})
        assert r.json().get("ok") is True, f"{good!r} was refused"
        main._JOBS[424243]["code"] = None
    main._JOBS.pop(424243, None)


@check("a caller is told where the code was sent, and a wrong one is retried")
def _():
    """Call 57: all it said was "Amazon sent them a code". The page said
    "to your phone ***-***-**96" and the caller asked, fairly, why we
    couldn't tell them where to look."""
    seen = ("Enter verification code For your security, we've sent the code "
            "to your phone ***-***-**96. Resend code")
    assert main.code_destination(seen) == \
        "sent the code to your phone ***-***-**96", main.code_destination(seen)
    assert main.code_destination("Enter the code we emailed to j***@gmail.com")
    assert main.code_destination("Please enter your password") == ""
    assert main.CODE_BAD.search("The code you entered is not valid.")
    assert not main.CODE_BAD.search("Enter the code we sent you.")
    src = source()
    body = src[src.index("def _do_site_login("):]
    body = body[:body.index("\ndef ", 10)]
    assert "code_destination(seen)" in body, \
        "the code prompt doesn't say where the code went"
    assert "for code_try in range(3)" in body, \
        "one wrong digit still ends the whole sign-in"
    assert 'reason="bad_code"' in body and 'reason="no_code"' in body, \
        "a code failure has no reason code, so nothing can act on it"
    assert "screen: {page_text" not in body, \
        "the raw page is still read out to the caller on a failure"


@check("a code that was emailed is read from their inbox, not asked for")
def _():
    """Call 57: the caller was told to go and find a code. He has no
    screen - that is the whole reason he rings us. When the code arrives
    by email and we can already read that mailbox, we fetch it."""
    import time as _t
    import google_tools
    real = google_tools.tool_search_email
    now = int(_t.time() * 1000)
    try:
        google_tools.tool_search_email = lambda *a, **k: {"messages": [
            {"from": "account-update@amazon.com", "at_ms": now,
             "subject": "Your Amazon verification code is 418302",
             "snippet": "Never share this code."}]}
        assert main.code_from_email(1, "amazon", now - 5000) == "418302"
        # an order number in an ordinary email is not a code
        google_tools.tool_search_email = lambda *a, **k: {"messages": [
            {"from": "amazon.com", "at_ms": now, "subject": "Shipped 2 items",
             "snippet": "order 112-3334445"}]}
        assert main.code_from_email(1, "amazon", now - 5000) == ""
        # and a code from before this sign-in is never reused
        google_tools.tool_search_email = lambda *a, **k: {"messages": [
            {"from": "amazon.com", "at_ms": now - 3600000,
             "subject": "Your sign-in code: 123456", "snippet": ""}]}
        assert main.code_from_email(1, "amazon", now - 5000) == ""
        # nor one from a different site
        google_tools.tool_search_email = lambda *a, **k: {"messages": [
            {"from": "security@walmart.com", "at_ms": now,
             "subject": "Your verification code is 999111", "snippet": ""}]}
        assert main.code_from_email(1, "amazon", now - 5000) == ""
    finally:
        google_tools.tool_search_email = real
    src = source()
    body = src[src.index("def _do_site_login("):]
    body = body[:body.index(chr(10) + "def ", 10)]
    assert "code_from_email(account_id, site, code_since)" in body,         "the sign-in still asks the caller before looking in their email"
    assert "read the sign-in code from their email" in body,         "nothing records that the code was fetched"
    for leak in ("{mailed}", "{code}", "code: {"):
        assert leak not in body, "a one-time code is written into a log line"


@check("the advisor decides from facts, and can't claim work that isn't running")
def _():
    """Call 58: 'I don't have that phone with me now' got 'Understood, I'm
    handling that' while nothing at all was running. Judgement now happens
    in one place, against the real state, and the one claim the voice model
    kept getting wrong is checked in code."""
    db = main.Session()
    acct = main.Account(name="Advice Tester", pin="1234")
    db.add(acct)
    db.commit()
    db.refresh(acct)
    aid = acct.id
    db.add(main.Job(account_id=aid, call_id=77001, kind="site_login",
                    site="amazon", state="failed", reason="bad_code",
                    message="Amazon refused the code."))
    db.commit()
    db.close()

    facts = main.call_state(aid, 77001)
    assert facts["anything_running"] is False, facts["running"]
    assert facts["finished"][0]["reason"] == "bad_code", facts["finished"]
    assert facts["now"] and facts["name"] == "Advice Tester"

    import advisor
    real = advisor._openai_chat
    try:
        # the model tries the exact mistake from call 58
        advisor._openai_chat = lambda *a, **k: {"choices": [{"message": {
            "content": '{"say": "Understood, I am handling that now.",'
                       ' "next": "", "why": "x"}'}}]}
        out = main.advise(aid, 77001, "the caller cannot reach the code")
        assert out.get("corrected"), "a false 'handling it' went through"
        assert out["say"].startswith("Nothing is running"), out["say"]
        # an honest answer is left alone
        advisor._openai_chat = lambda *a, **k: {"choices": [{"message": {
            "content": '{"say": "Amazon would not take the code, so I have '
                       'stopped. Shall I try later?", "next": "", "why": "x"}'
        }}]}
        out = main.advise(aid, 77001, "the code was refused")
        assert not out.get("corrected") and "stopped" in out["say"], out
        # a blocked subject still gets the fixed refusal
        advisor._openai_chat = lambda *a, **k: {"choices": [{"message": {
            "content": '{"say": "Here are today\'s news headlines and sports '
                       'scores.", "next": "", "why": "x"}'}}]}
        assert main.advise(aid, 77001, "they asked for the news")["say"] \
            == main.BLOCKED_REPLY
        # and a model that falls over never produces a guess
        def boom(*a, **k):
            raise RuntimeError("no model")
        advisor._openai_chat = boom
        assert main.advise(aid, 77001, "anything")["say"] == ""
    finally:
        advisor._openai_chat = real
    paths = {r.path for r in main.app.routes}
    for p in ("/advise", "/state", "/calls/review", "/reviews"):
        assert p in paths, f"route missing: {p}"


@check("every finished call is read back, and bad claims are flagged")
def _():
    """Nobody should have to ring in to report that the assistant said
    something untrue. After each call the record is read back against what
    actually ran, and anything unsupported becomes a note for the office."""
    db = main.Session()
    call = main.Call(id=77002, account_id=1, from_number="+1555",
                     started_at=main.datetime.utcnow())
    db.add(call)
    for who, text in (("agent", "Hello, please tell me your PIN."),
                      ("caller", "I don't have that phone with me now."),
                      ("agent", "Understood. I'm handling that.")):
        db.add(main.CallTurn(call_id=77002, who=who, text=text))
    db.commit()
    db.close()

    import advisor
    real = advisor._openai_chat
    try:
        advisor._openai_chat = lambda *a, **k: {"choices": [{"message": {
            "content": '{"problems": [{"quote": "Understood. I am handling '
                       'that.", "why": "nothing was running", "severity": '
                       '"high"}], "verdict": "claimed work that never '
                       'started"}'}}]}
        out = main.review_call(77002)
        assert len(out.get("problems", [])) == 1, out
        db = main.Session()
        notes = (db.query(main.Followup).filter_by(reason="call_review",
                                                   call_id=77002).all())
        db.close()
        assert notes, "the office never hears about it"
        assert "handling" in notes[0].note
        # a clean call leaves no note
        advisor._openai_chat = lambda *a, **k: {"choices": [{"message": {
            "content": '{"problems": [], "verdict": "fine"}'}}]}
        main.review_call(77002)
        db = main.Session()
        again = (db.query(main.Followup).filter_by(reason="call_review",
                                                   call_id=77002).all())
        db.close()
        assert len(again) == 1, "a clean call raised a note anyway"
    finally:
        advisor._openai_chat = real
    # and it happens on its own when a call ends
    src = source()
    body = src[src.index("def call_end("):]
    body = body[:body.index(chr(10) + "@app.", 10)]
    assert "background.add_task(review_call" in body, \
        "calls are only reviewed when someone asks by hand"


@check("the office can see everything done on a customer's behalf")
def _():
    """The live log carries every step of every job and scrolls away. A
    person asking "what did it do for my mother today?" needs a short list
    in plain words, with no password or card number in it."""
    from fastapi.testclient import TestClient
    c = TestClient(main.app, raise_server_exceptions=False, base_url="https://t")
    c.post("/admin/login", json={"password": os.environ.get(
        "ADMIN_PASSWORD", "changeme")})
    main.record_change(1, "email", "sent", "sent to a@b.com: \"the rent\"",
                       call_id=4242)
    main.record_change(1, "payment", "charged", "charged $12.50 to Visa "
                       "ending 4242", undo="refundable from Stripe")
    rows = c.get("/changes?limit=10").json()
    assert rows and rows[0]["detail"].startswith("charged"), rows[:1]
    assert rows[0]["undo"], "no note about whether it can be undone"
    assert any(r["call_id"] == 4242 for r in rows), "not tied to the call"
    assert c.get("/changes?area=email").json()[0]["area"] == "email"
    # it goes through the scrubber like everything else
    main.record_change(1, "login", "saved", "the password is Hunter22")
    assert "Hunter22" not in c.get("/changes?limit=3").text
    # and the office has somewhere to look
    html = c.get("/admin").text
    for bit in ("p-changes", "p-reviews", "p-know", "p-blocks",
                "loadChanges", "loadReviews", "loadKnow", "saveKnow",
                "loadBlocks", "What it did", "Call checks",
                "Who they are", "Blocked by"):
        assert bit in html, f"admin panel is missing {bit}"
    # the money and email paths actually record something
    src = source()
    for fn in ("def test_send(", "def email_reply(", "def email_action(",
               "def cal_create(", "def contacts_add(", "def todo_add(",
               "def logins_save(", "def _drive_did(", "def stripe_charge("):
        body = src[src.index(fn):]
        body = body[:body.index(chr(10) + "def ", 10) if chr(10) + "def "
                    in body[10:] else len(body)][:2500]
        assert "record_change(" in body, f"{fn} leaves no record"


@check("menu text is never reported to a caller as 'nothing found'")
def _():
    """Call 59: the Amazon orders page and a product search both handed
    back the first 1800 characters of the page - the menus - so the caller
    was told he had no orders and that the paper didn't exist. He had
    both, and he knew it."""
    real = main._summarise_page
    nav = ("Skip to main content Deliver to Spring Valley All Departments "
           "Alexa Skills Amazon Autos Amazon Devices Amazon Fresh Customer "
           "Service Registry Gift Cards Sell on Amazon Your Account")

    class FakePage:
        def __init__(self, text): self.text = text
    real_text = main.page_text
    try:
        everywhere("page_text", lambda page, limit=0: page.text)
        # the summariser honestly finds nothing in a page of menus
        everywhere("_summarise_page", lambda text, q: "NOTHING_RELEVANT")
        answer, raw = main.page_answer(FakePage(nav), "their recent orders")
        assert answer == "", f"menu text came back as an answer: {answer!r}"
        assert raw, "the raw page should still be available"
        # a summariser that parrots the menus is caught too
        everywhere("_summarise_page", lambda text, q: nav[:120])
        assert main.page_answer(FakePage(nav), "orders")[0] == "",             "a summary made of menu items was accepted"
        # a real answer gets through
        everywhere("_summarise_page", lambda text, q: (
            "Two orders: a printer cable delivered Tuesday, and copy paper "
            "arriving Friday for $41.99."))
        got, _ = main.page_answer(FakePage("...orders..."), "orders")
        assert "copy paper" in got, got
    finally:
        everywhere("_summarise_page", real)
        everywhere("page_text", real_text)
    src = source()
    for fn in ("def _run_site_orders(", "def _run_site_search("):
        body = src[src.index(fn):]
        body = body[:body.index(chr(10) + "def ", 10)]
        assert "page_answer(" in body, f"{fn} still reports raw page text"
        assert 'reason="no_results"' in body,             f"{fn} can't tell 'nothing readable' from 'nothing there'"


@check("what we know about a customer is kept, and holds no secrets")
def _():
    """A caller shouldn't have to explain twice that he is hard of hearing,
    that "the office" means his bookkeeper, or that he only orders after
    Sunday. The notes are built after each call and read before the next."""
    from fastapi.testclient import TestClient
    db = main.Session()
    acct = main.Account(name="Notes Tester", pin="1234")
    db.add(acct)
    db.commit()
    db.refresh(acct)
    aid = acct.id
    db.add(main.Call(id=78010, account_id=aid, from_number="+1555",
                     started_at=main.datetime.utcnow()))
    for who, text in (("agent", "Hello."), ("caller", "Speak up please."),
                      ("agent", "Of course."),
                      ("caller", "My son Moshe orders my paper.")):
        db.add(main.CallTurn(call_id=78010, who=who, text=text))
    db.commit()
    db.close()

    made = ("Hard of hearing - speak up and slow down." + chr(10)
            + "- His son Moshe orders his copy paper." + chr(10)
            + "His PIN is 1234 and the password is Hunter22.")
    import advisor
    real = advisor._openai_chat
    try:
        advisor._openai_chat = lambda *a, **k: {
            "choices": [{"message": {"content": made}}]}
        notes = main.learn_about_caller(78010)["notes"]
        assert "Hard of hearing" in notes and "Moshe" in notes, notes
        assert "Hunter22" not in notes, "a password was written into the notes"
        assert not notes.splitlines()[1].startswith("-"), \
            "bullets left in - they get read aloud"

        c = TestClient(main.app, raise_server_exceptions=False,
                       base_url="https://t")
        c.post("/admin/login", json={"password": os.environ.get(
            "ADMIN_PASSWORD", "changeme")})
        c.post("/profile", json={"account_id": aid,
                                 "by_hand": "Daughter Rivky handles money."})
        main.learn_about_caller(78010)
        got = c.get("/profile?account_id=" + str(aid)).json()
        assert "Rivky" in got["by_hand"], "staff notes were overwritten"
        for_model = got["for_the_assistant"]
        assert "Rivky" in for_model and "Moshe" in for_model
        assert for_model.index("Rivky") < for_model.index("Moshe"), \
            "what a person wrote should come before what a model guessed"
        # a model asked for notes often writes ABOUT the notes; that is
        # not a fact about the person and was being read back as one
        for junk in ("No notes available.", "None.", "Nothing to add",
                     "N/A"):
            advisor._openai_chat = (lambda t: (lambda *a, **k: {"choices": [
                {"message": {"content": t}}]}))(junk)
            assert main.learn_about_caller(78010).get("skipped"), junk
        kept = c.get("/profile?account_id=" + str(aid)).json()["notes"]
        assert "No notes" not in kept, kept
    finally:
        advisor._openai_chat = real
    src = source()
    body = src[src.index("def call_end("):]
    body = body[:body.index(chr(10) + "@app.", 10)]
    assert "learn_about_caller" in body, "the notes are only kept by hand"


@check("a page is ranked before it is shown, so the button is in the list")
def _():
    """Job 88: it found the copy paper, opened it, then never found "Add to
    Cart". A page was handed over as the first 60 things on it in the
    page's own order, and Amazon's menu is more than 60 things."""
    js = main._SNAPSHOT_JS
    for must in ("add to (cart|basket|bag)", "check ?out", "score",
                 "nav, header, footer", "cand.sort", "slice(0, limit)"):
        assert must in js, f"the page is still taken in page order: {must}"
    assert "args.limit" in js and "args.want" in js, \
        "the goal's own words don't count towards what is shown"
    # the scan budget must be spent on controls we can use, not on the
    # thousands of hidden menu links a shop keeps at the top of its page
    assert "cand.length >= 600" in js,         "a page of hidden menus can exhaust the budget before the products"
    # sixty identical "Add to cart" buttons must not crowd out sixty
    # different product names
    assert "dupes.has(key)" in js and "dupes.add(key)" in js,         "duplicates are still removed after ranking, not before"
    # markers from the last look must go, or a number can point at
    # something from the previous screen
    assert "removeAttribute('data-pa-idx')" in js,         "old element numbers are never cleared"
    assert js.index("removeAttribute('data-pa-idx')") <         js.index("setAttribute('data-pa-idx'"),         "old numbers are cleared after the new ones are set"
    assert js.index("dupes.has(key)") < js.index("cand.sort"),         "duplicates must go before the ranking, or they fill every slot"
    assert "seen > 1500" not in js, "the old budget is still there"
    src = source()
    assert "_page_snapshot(page, want=goal)" in src, \
        "the browse loop doesn't tell the page reader what it is after"
    body = src[src.index("def _page_snapshot("):]
    body = body[:body.index(chr(10) + "def ", 10)]
    assert 'page_eval(page, _SNAPSHOT_JS, {"limit"' in body
    # ranking runs in the browser, so check the scoring by reading it back
    for start, want in (("tag === 'button'", "+= 4"),
                        ("typed.includes(tag)) score", "+= 3"),
                        ("nav, header, footer", "-= 4")):
        i = js.index(start)
        assert want in js[i:i + 220], (start, want)


@check("a page that ignores us doesn't cost the customer their login")
def _():
    """Being stuck threw the saved session away. The next call then needed
    a fresh sign-in and another code read out over the phone - for what was
    usually a button we simply hadn't seen."""
    src = source()
    i = src.rindex("if stuck >= STUCK_LIMIT:")
    block = src[i:i + 900]
    assert "looks_signed_out(text)" in block, \
        "a stuck page still wipes a good login"
    assert "_forget_context(account_id, site_key)" in block


@check("a model that answers in prose is reminded, then reported honestly")
def _():
    """Job 89 reached the cart, and the browser model replied "I'm unable
    to perform the checkout" instead of an action. The caller was told "I
    couldn't work out what to do next", which blames nothing and helps
    nobody."""
    calls = []
    real = main._openai_chat

    def prose(*a, **k):
        calls.append(a[0] if a else k.get("messages"))
        return {"choices": [{"message": {
            "content": "I'm unable to perform the checkout for you."}}]}
    try:
        everywhere("_openai_chat", prose)
        act = main._decide("add to cart", "https://x", "text", [], [])
        assert act["action"] == "give_up" and act.get("refused"), act
        assert "wouldn't carry on" in act["answer"], act
        assert len(calls) == 2, "it gave up without reminding the model once"
        assert any("customer's OWN browser" in str(m) for m in calls[1]),             "the reminder never said whose browser it is"

        # and when the reminder works, the action is used
        state = {"n": 0}

        def second_time(*a, **k):
            state["n"] += 1
            body = ('{"action": "click", "index": 3}' if state["n"] > 1
                    else "I can't do that.")
            return {"choices": [{"message": {"content": body}}]}
        everywhere("_openai_chat", second_time)
        act = main._decide("add to cart", "https://x", "text", [], [])
        assert act["action"] == "click", act
    finally:
        everywhere("_openai_chat", real)
    src = source()
    assert 'reason="model_refused"' in src,         "a refusal is still reported as an ordinary give-up"


@check("nothing that browses can press Place your order")
def _():
    """A prompt that says "do not buy" is a wish. Pressing the button that
    spends the money is refused in code unless the job was started to buy
    and the caller has said yes."""
    for label in ("button: Place your order", "input/submit: Buy Now",
                  "button: Pay now", "a: Complete purchase",
                  "button: Submit my order", "button: Confirm and pay"):
        assert main.BUY_BUTTONS.search(label), f"would be pressed: {label}"
    for safe in ("button: Proceed to checkout", "a: Change address",
                 "button: Add to Cart", "a: Your Orders", "button: Continue"):
        assert not main.BUY_BUTTONS.search(safe), f"blocked wrongly: {safe}"
    src = source()
    body = src[src.index("def _run_browse("):]
    body = body[:body.index(chr(10) + "def ", 10)]
    i = body.index('if a == "click":')
    block = body[i:i + 2600]
    assert "BUY_BUTTONS.search" in block and 'payload.get("may_buy")' in block,         "a browsing job can still press the button that spends the money"
    assert block.index("BUY_BUTTONS") < block.index("do_click"),         "it is checked after the click, which is no check at all"
    paths = {r.path for r in main.app.routes}
    assert "/jobs/checkout" in paths
    body = src[src.index("def job_checkout("):]
    body = body[:body.index(chr(10) + "@app.", 10)]
    assert '"may_buy": False' in body,         "the checkout step could buy something"


@check("a long answer isn't cut off before the caller hears it")
def _():
    """A list of saved Amazon addresses stopped mid-word, and a list of
    cards stopped at "Visa ending 6125, expi". Every job answer was being
    cut to 500 characters on its way into the database."""
    long_answer = "Visa ending 1234, not expired. " * 40      # ~1200 chars
    db = main.Session()
    job = main.Job(account_id=1, kind="browse", site="amazon",
                   state="working")
    db.add(job)
    db.commit()
    db.refresh(job)
    jid = job.id
    db.close()
    main._job_set(jid, "done", long_answer)
    db = main.Session()
    back = db.query(main.Job).filter_by(id=jid).first().message
    db.close()
    assert len(back) > 900, f"cut to {len(back)} characters"
    assert back.endswith("expired. "), back[-40:]


@check("a search that worked is not read back as a failure")
def _():
    """Call 60: a second search really did find three solar house numbers,
    and the caller was told "I couldn't get more results". The runner now
    stores a spoken answer, and /jobs/answer was summarising that summary
    again - the second pass had nothing left and said so."""
    from fastapi.testclient import TestClient
    c = TestClient(main.app, raise_server_exceptions=False, base_url="https://t")
    c.post("/admin/login", json={"password": os.environ.get(
        "ADMIN_PASSWORD", "changeme")})
    found = ("Here are a few options: ISUNMEA 9 Inch Solar Lighted House "
             "Number for $22.99, DIBMS 9 inch for $22.99, and CARPESUN for "
             "$22.99, all delivered tomorrow.")
    db = main.Session()
    job = main.Job(account_id=1, kind="site_search", site="amazon",
                   state="done", message=found)
    db.add(job)
    db.commit()
    db.refresh(job)
    jid = job.id
    db.close()
    hit = {"n": 0}
    real = main._summarise_page

    def counted(text, q):
        hit["n"] += 1
        return "NOTHING_RELEVANT"
    try:
        everywhere("_summarise_page", counted)
        got = c.get("/jobs/answer?job_id=%d&question=the search" % jid).json()
        assert got["answer"] == found, got
        assert hit["n"] == 0, "it summarised an answer that was already one"
        # a genuinely raw page still gets summarised
        db = main.Session()
        raw = main.Job(account_id=1, kind="site_orders", site="amazon",
                       state="done", message="word " * 400)
        db.add(raw)
        db.commit()
        db.refresh(raw)
        rid = raw.id
        db.close()
        got = c.get("/jobs/answer?job_id=%d" % rid).json()
        assert hit["n"] == 1, "raw page text was passed on unsummarised"
        assert "nothing readable" in got["answer"], got
        assert "doesn't exist" in got["answer"], got
    finally:
        everywhere("_summarise_page", real)


@check("a page is given time to draw before it is read")
def _():
    """Job 103 on a search results page: "there is no [16] - the page
    offers 9 things you can use". Amazon draws its results after the shell,
    and we were reading in between, then wandering off to the next page of
    results that didn't exist yet."""
    src = source()
    body = src[src.index("def _run_browse("):]
    body = body[:body.index(chr(10) + "def ", 10)]
    i = body.index("_page_snapshot(page, want=goal)")
    block = body[i:i + 500]
    assert "for _ in range(3)" in block, "it still reads a half-drawn page"
    assert "len(items) >= 15" in block and "settle(page" in block, block[:200]


@check("a real step is not mistaken for getting nowhere")
def _():
    """Call 60 and jobs 101-106: it searched, opened a product, went back -
    three real steps - and was told each time that nothing had happened,
    because "did the page change?" compared the first 200 characters, and
    on a shop those are the same menu on every page."""
    class FakePage:
        def __init__(self, text): self.text = text
    real = main.page_text
    menu = "Skip to main content Deliver to All Departments Alexa Skills " * 12
    try:
        everywhere("page_text", lambda page, limit=0: page.text)
        results = menu + "ISUNMEA 9 Inch Solar Lighted House Numbers $22.99 "
        product = menu + "DIBMS 9 inch Solar House Numbers, 4.3 stars, $22.99 "
        a = main._body_mark(FakePage(results + "x" * 2000))
        b = main._body_mark(FakePage(product + "y" * 2000))
        assert a != b, "two different pages look identical to the detector"
        assert a == main._body_mark(FakePage(results + "x" * 2000)),             "the same page looks different each time it is read"
    finally:
        everywhere("page_text", real)
    src = source()
    body = src[src.index("def _run_browse("):]
    body = body[:body.index(chr(10) + "def ", 10)]
    assert "page_text(page, 200)" not in body,         "it still decides from the menu at the top of the page"
    assert "_body_mark(page)" in body and "url_before" in body


@check("what a page said is kept after leaving it")
def _():
    """Job 108 opened a product, went back, opened another, went back -
    sixteen times - and answered nothing. Each step it saw only the page in
    front of it, so a page it had left may as well never have been read."""
    seen = {}
    real = main._openai_chat

    def capture(msgs, **k):
        seen["msg"] = str(msgs)
        return {"choices": [{"message": {"content":
                '{"action":"click","index":1,"found":"ISUNMEA is 9 inches, '
                '$22.99","why":"read it"}'}}]}
    try:
        everywhere("_openai_chat", capture)
        act = main._decide("compare two", "https://x", "text", [], [],
                           findings=["DIBMS is 9 inches, $22.99"])
        assert "WHAT YOU HAVE WRITTEN DOWN SO FAR" in seen["msg"],             "notes are not given back to it"
        assert "DIBMS is 9 inches" in seen["msg"]
        assert act.get("found", "").startswith("ISUNMEA"), act
    finally:
        everywhere("_openai_chat", real)
    js_prompt = main.BROWSE_SYSTEM
    assert '"found"' in js_prompt and "before you leave a page" in         js_prompt.lower(), "nothing tells it to write things down"
    src = source()
    body = src[src.index("def _run_browse("):]
    body = body[:body.index(chr(10) + "def ", 10)]
    assert "findings.append(noted[:300])" in body, "notes are never kept"
    assert "call_id, findings," in body or "call_id, findings)" in body,         "notes are never handed back"
    # and a run that runs out of steps still gives back what it read
    i = body.index("Ran out of steps before finishing.")
    assert "if findings:" in body[i - 600:i],         "notes are thrown away when the steps run out"


@check("nothing may claim it did something it never did")
def _():
    """Job 115 on Best Buy: a saved shortcut for "product_price" was
    replayed for a goal that said add to cart and check out. One search box
    was typed into, and the answer came back "the item successfully went
    into the cart. At checkout the site asks you to sign in." Neither had
    happened."""
    for doing in ("add one to the cart", "proceed to checkout",
                  "sign in and check my orders", "send it to my son",
                  "book me an appointment", "place the order"):
        assert main.DOING_GOAL.search(doing), f"treated as a lookup: {doing}"
    for looking in ("what does copy paper cost", "when do they close",
                    "how do I clean the filter", "what is my balance"):
        assert not main.DOING_GOAL.search(looking),             f"a plain lookup can no longer use a saved shortcut: {looking}"
    for pretending in ("The item successfully went into the cart.",
                       "I have added it to your basket.",
                       "You are signed in now.", "The order was placed."):
        assert main.claims_action(pretending), f"missed: {pretending}"
    for honest in ("It costs $18.70 and delivery is free.",
                   "The page shows two options.",
                   "The cart page says no items are selected."):
        assert not main.claims_action(honest), f"flagged wrongly: {honest}"
    src = source()
    body = src[src.index("def _run_browse("):]
    body = body[:body.index(chr(10) + "def ", 10)]
    assert "DOING_GOAL.search(goal)" in body,         "a saved shortcut can still answer a goal that asks for an action"
    i = body.index('if a == "done":')
    assert "claims_action(answer)" in body[i:i + 2400],         "an answer claiming an action is still taken at its word"


@check("a refusal is named, not just counted as 'blocked'")
def _():
    """"Walmart blocked us" is not actionable. A puzzle no one may solve
    for a phone caller, a fingerprint wall, an address refusal, a rate
    limit and a plain login wall are five different problems, and only
    three of them are worth another try."""
    cases = {
        "Robot or human? Activate and hold the button to confirm that you "
        "are human.": ("puzzle", False),
        "Access Denied You don't have permission to access this server.":
            ("ip_block", True),
        "Oops!! Something went wrong. Please refresh page":
            ("site_error", True),
        "Attention Required! Cloudflare. Checking your browser before "
        "accessing.": ("fingerprint", False),
        # Amazon's silent refusal says BOTH "something went wrong" and
        # "automated access" - it is a wall, not an outage
        "Sorry! Something went wrong. To discuss automated access to "
        "Amazon data please contact api-services-support":
            ("fingerprint", False),
        "Too many requests. Please slow down.": ("rate_limit", True),
        "Please sign in to continue to checkout.": ("login_wall", True),
        "This item is not available in your country.": ("geo_block", True),
    }
    for text, (kind, retry) in cases.items():
        got = main.classify_block(text)
        assert got["kind"] == kind, f"{text[:40]!r} -> {got['kind']}"
        assert got["worth_retrying"] is retry, text[:40]
        assert got["advice"], "no advice on what would change it"
    # who is doing the blocking, where the page says so
    assert main.classify_block("Attention Required! Cloudflare")["vendor"] \
        == "cloudflare"
    assert main.classify_block(
        "Access Denied. Reference #18.2f3b1c")["vendor"] == "akamai"
    assert main.classify_block(
        "px-captcha please press and hold")["vendor"] == "perimeterx"

    # it is written down, with what the page said, scrubbed
    from fastapi.testclient import TestClient
    c = TestClient(main.app, raise_server_exceptions=False, base_url="https://t")
    c.post("/admin/login", json={"password": os.environ.get(
        "ADMIN_PASSWORD", "changeme")})
    main.record_block(1, "walmart", "Activate and hold the button to "
                                    "confirm that you are human.",
                      "https://www.walmart.com", 4242)
    main.record_block(1, "lowes", "Access Denied You don't have permission",
                      "https://www.lowes.com/search")
    got = c.get("/blocks?days=1").json()
    kinds = {b["site"]: b["kind"] for b in got["by_site"]}
    assert kinds.get("walmart") == "puzzle", got["by_site"]
    assert kinds.get("lowes") == "ip_block", got["by_site"]
    assert any(b["site"] == "walmart" and b["worth_retrying"] is False
               for b in got["by_site"])
    assert got["recent"] and got["recent"][0]["saw"], \
        "what the page said is not kept, so nothing can be named later"

    # and every place a site refuses us records it
    src = source()
    for fn in ("def _run_browse(", "def _run_site_search("):
        body = src[src.index(fn):]
        body = body[:body.index(chr(10) + "def ", 10)]
        assert "record_block(" in body, f"{fn} refusals go unnamed"
    # a job that FINISHES by reporting a wall - which is what probe.py
    # does - must still record it, or the report shows nothing at all
    body = src[src.index("def _run_browse("):]
    body = body[:body.index(chr(10) + "def ", 10)]
    i = body.index('if a == "done":')
    assert "record_block(" in body[i:i + 1200],         "a job that reports a block instead of failing records nothing"


@check("a price is for the thing they asked for, not a lookalike")
def _():
    """Shopping listings match loosely. Asked for an ECCO New Jersey, the
    listings came back with the Byway, the S Lite and the Move - and the
    cheapest of those was the wrong shoe at the right price."""
    made = {"shopping": [
        {"title": "ECCO New Jersey Leather Slip-On", "source": "Zappos",
         "price": "$126.00", "link": "https://z"},
        {"title": "ECCO Byway Slip-On Sneakers", "source": "6pm.com",
         "price": "$67.49", "link": "https://s"},
        {"title": "ECCO Move Men's Slip On", "source": "Nordstrom Rack",
         "price": "$79.97", "link": "https://n"},
        {"title": "ECCO New Jersey Bike Toe Derby", "source": "Amazon",
         "price": "$131.91", "link": "https://a"},
    ]}
    real_key, real_call = main.SERPER_API_KEY, main._serper_shopping
    try:
        everywhere("SERPER_API_KEY", "x")
        everywhere("_serper_shopping", lambda item: made)
        got = main.shopping_prices("ECCO New Jersey mens shoes")
        shops = [o["shop"] for o in got["offers"]]
        assert got["exact"] is True, got
        assert "6pm.com" not in shops and "Nordstrom Rack" not in shops,             f"a different shoe was priced as theirs: {shops}"
        assert shops == ["Zappos", "Amazon"], shops
        assert got["offers"][0]["price"] == "$126.00", got["offers"][0]
        # when nothing matches exactly it says so, instead of pretending
        made["shopping"] = [made["shopping"][1], made["shopping"][2]]
        got = main.shopping_prices("ECCO New Jersey mens shoes")
        assert got["exact"] is False, got
        assert not got["offers"], "a different shoe was offered as theirs"
        assert "exact item" in got["answer"], got
        # and with nothing to quote, the caller is told plainly, so the
        # assistant falls back to reading the shop pages themselves
    finally:
        main.SERPER_API_KEY, main._serper_shopping = real_key, real_call


@check("the public pages Google verification needs are there")
def _():
    """Verification wants a privacy policy and terms on a domain you own,
    a homepage explaining the app, and the Limited Use disclosure for
    restricted scopes. Without those the submission is refused."""
    from fastapi.testclient import TestClient
    c = TestClient(main.app, raise_server_exceptions=False, base_url="https://t")
    for path in ("/", "/privacy", "/terms", "/signup", "/connect", "/card"):
        r = c.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        assert "<html" in r.text.lower(), f"{path} isn't a web page"
    priv = c.get("/privacy").text
    assert "Limited Use" in priv, \
        "the privacy policy is missing Google's Limited Use disclosure"
    assert "api-services-user-data-policy" in priv, \
        "it must link Google's User Data Policy"
    for must in ("not sold", "not used to train"):
        assert must in priv, f"the policy should say data is {must}"
    # the machine health check must survive the homepage becoming a page
    assert c.get("/health").json().get("ok") is True, \
        "nothing answers a plain health check any more"


@check("caller country routing")
def _():
    assert main._where_for_phone("+13476752334")[0] == "US"
    assert main._where_for_phone("+442079460958")[0] == "GB"
    assert main._where_for_phone("+16475551234")[0] == "CA"


@check("cost maths produce a number")
def _():
    from fastapi.testclient import TestClient
    c = TestClient(main.app, raise_server_exceptions=False, base_url="https://t")
    c.post("/admin/login", json={"password": os.environ.get(
        "ADMIN_PASSWORD", "changeme")})
    r = c.post("/usage", json={"call_id": 999999, "account_id": 1,
                               "audio_in": 1000, "audio_out": 1000,
                               "call_seconds": 60}).json()
    assert r.get("cost_usd", 0) > 0


@check("a WAF refusal is filed as a block, not as unclear")
def _():
    """macys answered 'Access denied to the page, unable to proceed' and
    verdict() filed it UNCLEAR, so the 16-site sweep under-reported the
    blocks by one."""
    import probe
    denied = {"state": "failed", "reason": "",
              "message": "Access denied to the page, unable to proceed"}
    assert probe.verdict(denied) == "BLOCKED", probe.verdict(denied)
    # a page that merely stopped responding is still genuinely unclear
    stuck = {"state": "failed", "reason": "stuck",
             "message": "The page stopped responding to anything it tried."}
    assert probe.verdict(stuck) == "UNCLEAR", probe.verdict(stuck)
    # and the verdicts that already worked must not move
    for job, want in (({"state": "failed", "reason": "bot_check",
                       "message": "asking for a human check"}, "BLOCKED"),
                      ({"state": "done", "message": "SIGN_IN"}, "SIGN_IN"),
                      ({"state": "done", "message": "GUEST_OK"}, "GUEST_OK"),
                      ({"state": "done", "message": "LOGIN_WALL"},
                       "LOGIN_WALL")):
        assert probe.verdict(job) == want, (job, probe.verdict(job))


# ------------------------------------------------------------ agent
print("voice agent")


@check("agent.py imports")
def _():
    global agent
    import agent


@check("every tool's schema builds (the auto_report crash)")
def _():
    from livekit.agents.llm.utils import build_legacy_openai_schema
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    n = 0
    for t in inst.tools:
        build_legacy_openai_schema(t, internally_tagged=True)
        n += 1
    assert n >= 40, f"only {n} tools"


@check("the PIN is checked against the database, not a default")
def _():
    """/accounts has never returned a "pin" key, but verify_pin compared
    against self.account.get("pin", "1234") - so the fallback ran on every
    call and anyone who said 1234 was let into somebody else's mailbox.
    Every other fixture here hands the Assistant a pin, which is exactly
    why nothing caught it: the tests had a key production never receives.
    This one builds the account dict the way find_account() really does."""
    import asyncio
    from fastapi.testclient import TestClient
    src = open("agent.py", encoding="utf-8").read()
    assert 'self.account.get("pin"' not in src, \
        ("verify_pin is reading a PIN off the account dict again - "
         "/accounts never sends one, so the fallback becomes the real PIN")
    assert '"/accounts/verify_pin"' in src, \
        "verify_pin is not asking the backend to compare the PIN"
    c = TestClient(main.app, raise_server_exceptions=False,
                   base_url="https://t")
    c.post("/admin/login", json={"password": os.environ.get(
        "ADMIN_PASSWORD", "changeme")})
    db = main.Session()
    acct = main.Account(name="Pin Tester", pin="8675")
    db.add(acct)
    db.commit()
    db.refresh(acct)
    acct_id = acct.id
    db.add(main.PhoneNumber(number="+18455550188", account_id=acct_id))
    db.commit()
    db.close()

    real = next(r for r in c.get("/accounts").json()
                if r["account_id"] == acct_id)
    assert "pin" not in real, "/accounts must never hand a PIN to the agent"

    saved = agent.backend_post

    async def fake_post(path, payload, params=None):
        r = c.post(path, json=payload, params=params)
        assert r.status_code == 200, r.text
        return r.json()

    def pin_tool(inst):
        return next(t for t in inst.tools
                    if getattr(t, "__name__", "") == "verify_pin")

    agent.backend_post = fake_post
    try:
        inst = agent.Assistant(real, "+18455550188", 1)
        said = asyncio.run(pin_tool(inst)(None, "1234"))
        assert said.startswith("PIN incorrect"), \
            f"the old default still opens the gate: {said}"
        assert not inst.verified, "a wrong PIN set verified"

        said = asyncio.run(pin_tool(inst)(None, "8675"))
        assert said.startswith("PIN correct"), \
            f"the PIN in the database was refused: {said}"
        assert inst.verified, "the right PIN did not verify the caller"

        other = agent.Assistant(real, "+18455550188", 2)
        asyncio.run(pin_tool(other)(None, "1111"))
        assert not other.verified, "any four digits got through"

        blank = agent.Assistant(real, "+18455550188", 3)
        asyncio.run(pin_tool(blank)(None, ""))
        assert not blank.verified, "an empty PIN got through"
    finally:
        agent.backend_post = saved


@check("no assignment to a read-only LiveKit property (the ring-out bug)")
def _():
    from livekit.agents import Agent
    props = {k for k, v in vars(Agent).items() if isinstance(v, property)}
    src = open("agent.py", encoding="utf-8").read()
    for m in re.finditer(r"(?:agent_obj|self)\.(\w+)\s*=[^=]", src):
        assert m.group(1) not in props, f"assigns read-only: {m.group(1)}"


@check("every self._method the agent calls is actually defined")
def _():
    src = open("agent.py", encoding="utf-8").read()
    called = set(re.findall(r"self\._(\w+)\(", src))
    defined = {a or b for a, b in re.findall(
        r"    def _(\w+)\(|    async def _(\w+)\(", src)}
    missing = {c for c in called if c not in defined}
    assert not missing, f"missing methods: {missing}"


@check("session.start happens before the hangup watchdog")
def _():
    src = open("agent.py", encoding="utf-8").read()
    assert src.index("await session.start(room=ctx.room, agent=agent_obj)") \
        < src.index("asyncio.create_task(watchdog())")


@check("every job a tool starts is watched (the 'keep waiting?' bug)")
def _():
    """A tool that sets self.job_id without starting a watcher leaves the
    model with nothing to do but poll - and it fills the silence by asking
    the caller whether they want to keep waiting."""
    import ast
    tree = ast.parse(open("agent.py", encoding="utf-8").read())
    bad = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # a real job start, not "self.job_id = None" in __init__
        sets_job = any(
            isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Attribute) and t.attr == "job_id"
                    for t in n.targets)
            and not (isinstance(n.value, ast.Constant)
                     and n.value.value is None)
            for n in ast.walk(fn))
        if not sets_job:
            continue
        watches = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("_watch_job", "_start_watch")
            for n in ast.walk(fn))
        if not watches:
            bad.append(fn.name)
    assert not bad, (f"starts a job but never watches it: {', '.join(bad)} "
                     f"- add self._watch_job(...)")


@check("no polling chatter in tool results or descriptions")
def _():
    """Tool text outranks the system prompt in practice. If a result says
    'check again shortly', the model checks again and talks while it waits,
    whatever the prompt says."""
    src = open("agent.py", encoding="utf-8").read()
    # note: lower-case "let me check again" is fine - the prompt quotes it
    # as a phrase the model must NOT say.
    banned = ["Check again", "every 15 seconds", "in a few seconds",
              "Poll check", "poll get_site_result"]
    found = [b for b in banned if b in src]
    assert not found, (f"polling chatter still in agent.py: {found} - "
                       f"say 'I will tell you when it changes' instead")


@check("the voice model is a variable, not hard-coded")
def _():
    """It is ~94% of the cost of a call. Changing it must not need a code
    change, so you can try a cheaper one and change back in a minute."""
    src = open("agent.py", encoding="utf-8").read()
    assert "RealtimeModel(model=REALTIME_MODEL" in src, \
        "the voice model is hard-coded again - use REALTIME_MODEL"
    assert agent.REALTIME_MODEL, "REALTIME_MODEL is empty"


@check("an expired session doesn't make it ask for the password again")
def _():
    """The agent decided by looking for the word 'password' anywhere in the
    failure text. 'There is still a password box on the page' means the
    session expired - and it asked the customer to read their password out
    for no reason."""
    stale = agent.login_failure_line(
        "amazon", "signed_out",
        "Not signed in to amazon - there is still a password box on the "
        "page.", 0)
    assert "do NOT ask for the password" in stale, stale
    assert "say it once more" not in stale, stale

    wrong = agent.login_failure_line(
        "amazon", "bad_password", "Amazon says the password is wrong.", 1)
    assert "password is wrong" in wrong, wrong
    twice = agent.login_failure_line(
        "amazon", "bad_password", "Amazon says the password is wrong.", 2)
    assert "Do NOT ask for it again" in twice, twice

    other = agent.login_failure_line("amazon", "", "the page timed out", 0)
    assert "timed out" in other and "password" not in other, other


@check("a username that isn't an email is questioned before it's saved")
def _():
    """Call 40: the caller said 'my email address is chesky163', we saved
    'chesky163', never read it back, and then failed to sign in with it on
    two separate calls without ever mentioning it."""
    assert agent.username_warning("chesky163"), \
        "a bare name was accepted as an email-style username"
    assert agent.username_warning("chesky163@"), "a cut-off address passed"
    assert agent.username_warning("") , "an empty username passed"
    assert agent.username_warning("chesky163@gmail.com") == "", \
        "a real address was wrongly questioned"
    # and it must be raised BEFORE anything is stored
    src = open("agent.py", encoding="utf-8").read()
    body = src[src.index("async def save_site_login("):]
    body = body[:body.index("\n    @function_tool")]
    assert body.index("_login_confirmed") < body.index('backend_post("/logins"'), \
        "save_site_login stores the login before confirming it"


@check("a failed sign-in tells the caller which username was used")
def _():
    """It offered a password reset link for what was a username problem."""
    said = agent.login_failure_line("target", "stuck", "no response", 0,
                                    "chesky163")
    assert "chesky163" in said and "email" in said.lower(), said
    assert "password" not in said.split("Do NOT")[0].lower(), said
    fine = agent.login_failure_line("target", "", "page timed out", 0,
                                    "chesky163@gmail.com")
    assert "chesky163@gmail.com" in fine, fine


@check("the topic rule separates doing from discussing, and says it once")
def _():
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    said = inst.instructions
    assert "If they want something DONE, do it" in said, \
        "the instructions no longer distinguish a task from a discussion"
    assert "Sabbath mode" in said, "the appliance example is gone"
    assert "Say that line ONCE" in said and "more than twice" in said, \
        "nothing stops it repeating the refusal until the caller hangs up"
    # Jewish subjects allowed, comparing faiths not, and no paskening
    assert "JEWISH RELIGIOUS MATTERS" in said, \
        "Jewish subjects are no longer explicitly allowed"
    for gone in ("Jewish law", "Halachot", "religious discussions"):
        assert gone not in said.split("JEWISH RELIGIOUS MATTERS")[0], \
            f"'{gone}' is still on the forbidden list"
    assert "not a rav" in said and "shailah" in said, \
        "nothing tells it to send a real question to their rav"
    assert "Do not DISCUSS other religions" in said, \
        "discussing other faiths is no longer ruled out"
    assert "A place, a date or a name is not a discussion" in said, \
        "practical mentions of another religion will be refused again"


@check("a half-answer from search can be turned into a real one")
def _():
    """Call 46: asked how to switch Shabbos mode on a Thermador fridge, the
    assistant invented three different button combinations and then said it
    couldn't identify the brand. The answer was the first search result -
    but search returns summaries, not pages, so it filled the gap itself."""
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    names = {getattr(t, "__name__", "") for t in inst.tools}
    assert "find_out" in names, "there is no way to find something out"
    for old in ("web_search", "ask_ai", "look_it_up", "read_page"):
        assert old not in names, (
            f"{old} is back. Four ways to find things out, chosen by "
            f"reading English, is how a question became a browser job.")
    tools = {getattr(t, "__name__", ""): t for t in inst.tools}
    doc = tools["find_out"].__doc__ or ""
    assert "CANNOT SEE THIS CONVERSATION" in doc, (
        "find_out doesn't warn that it's blind to the call. A caller asked "
        "about his ice maker and the question sent was 'how to turn on the "
        "icemaker for the'.")
    said = inst.instructions
    assert "WRITE THE WHOLE QUESTION" in said, \
        "nothing tells it to put the model number into the question"
    assert "/find" in {r.path for r in main.app.routes}, "/find is gone"
    assert "USE find_out" in said, \
        "the instructions don't say where exact detail comes from"
    assert "SAY WHERE IT CAME FROM AND HOW OLD" in said, \
        "a copied price can be read out as today's"
    # It must be free to answer general knowledge straight away - forbidding
    # that made it browse for a minute and a half over something it knew.
    assert "ANSWER FROM WHAT YOU KNOW FIRST" in said, \
        "it is being made to search for things it already knows"
    assert "Looking it up is SLOWER" in said, \
        "nothing tells it that searching what it knows wastes the call"
    # but never guess about the caller's own things, and never fake a source
    assert "never from memory, always from a tool" in said, \
        "it may now guess about the caller's own email and orders"
    assert "NEVER dress a guess up as a source" in said, \
        "it can claim a page said something it invented"
    src = open("agent.py", encoding="utf-8").read()
    body = src[src.index("async def find_out("):]
    body = body[:body.index("\n    @function_tool")]
    assert "log_turn(" in body, \
        "find_out leaves no trace, so nobody can tell if it ever ran"
    assert "never say 'the page says'" in body, \
        "a not-found result no longer warns against faking a source"
    assert "lists it at about" in body, \
        "a listed price can be read out as the price"


@check("a page that failed to load is never described")
def _():
    """Call 48: the Frigidaire support page was blocked by a bot check, and
    twenty seconds later the assistant said "the page says that depending
    on your model..." - describing a page it had never read."""
    src = open("agent.py", encoding="utf-8").read()
    body = src[src.index("async def get_site_result("):]
    body = body[:body.index("\n    @function_tool")]
    assert "nothing from that page" in body, \
        "a failed page read no longer tells it that it has nothing"
    assert "do not describe its contents" in body, body[-200:]
    watcher = src[src.index("def _watch_job("):]
    watcher = watcher[:watcher.index("\n    @function_tool")]
    assert 'kind == "browse"' in watcher and "NOTHING" in watcher, \
        "the watcher lets a failed page read pass as an answer"


@check("one garbled turn doesn't switch the language")
def _():
    """Call 48 opened with a mangled transcription and the assistant
    answered in Hebrew, so the caller had to ask for English."""
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    said = inst.instructions
    assert "Greet them in English" in said, "it can drift on the greeting"
    assert "garbled turn is NOT that" in said, \
        "nothing stops a misheard turn being read as a language choice"


@check("an old answer isn't repeated as established fact")
def _():
    """Call 49: the assistant opened with "we were looking into that
    earlier" and repeated the wrong button combination from call 48 - the
    one the caller had already rejected. Conversation history was labelled
    "you already know this", so a hallucination became a remembered fact."""
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1, history="- (voice) assistant: it is "
                                               "the v and + buttons")
    said = inst.instructions
    assert "you already know this" not in said, \
        "history is still presented to the model as known fact"
    assert "NOT a record of facts" in said, said[:300]
    assert "assume the last" in said and "get it right this time" in said, \
        "asking again doesn't prompt it to re-check"


@check("it can't claim to be working when nothing is running")
def _():
    """Call 49: it searched once, never read a page, then said "almost
    there" for two and a half minutes with nothing running at all."""
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    said = inst.instructions
    assert "ONLY SAY YOU ARE WORKING" in said, \
        "nothing stops it narrating progress on work it never started"
    assert "find_out\nis NOT one of" in said, \
        "a finished lookup can still be treated as something to wait for"
    src = open("agent.py", encoding="utf-8").read()
    body = src[src.index("async def find_out("):]
    body = body[:body.index("\n    @function_tool")]
    assert body.count("Nothing is still running") >= 3, \
        "a find_out result doesn't say it has already returned"


@check("a question never opens a browser")
def _():
    """Calls 42-62: one fridge question became twenty browser jobs, three
    human checks and nine different answers. Call 83: a minivan question
    hit four car-site walls. A question is answered from the search
    engine's copy of the pages in seconds; the browser is for things that
    must be DONE on a site."""
    src = open("agent.py", encoding="utf-8").read()
    body = src[src.index("async def find_out("):]
    body = body[:body.index("\n    @function_tool")]
    assert '"/find"' in body, "find_out does not use the find path"
    for word in ("jobs/browse", "_watch_job", "job_id", "LOOKUP_WAIT"):
        assert word not in body, f"find_out starts a browser job ({word})"
    assert "_lookups.get(key)" in body and \
        body.index("_lookups.get(key)") < body.index('"/find"'), \
        "it asks before checking whether this already came back empty"
    assert 'self._lookups[key] = "failed"' in body, \
        "an empty result is never recorded, so it can be repeated for ever"
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    assert len(inst.tools) <= 78, (
        f"{len(inst.tools)} tools. Four question tools became one; the "
        f"count must not creep back up.")


@check("'search again' is a complaint, not an instruction")
def _():
    """A caller saying "search again" is saying the last answer was wrong -
    they don't know the assistant has tools. On call 52 it took the word
    literally and re-ran the browser search that had just failed."""
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    said = inst.instructions
    assert "NOT HOW TO GET IT" in said, \
        "caller wording is still being read as a choice of tool"
    assert "does NOT mean run" in said, \
        "'search again' can still re-run a search that just failed"


@check("a shop listing doesn't outrank the manual")
def _():
    """The first result for a model number is the page selling it, which
    tells an owner nothing. The lookup kept landing there and getting
    stuck."""
    src = source()
    body = src[src.index("def _run_browse("):]
    body = body[:body.index("\ndef ", 10)]
    i = body.index("def _rank(")
    rank = body[i:i + 600]
    for must in ("looks_like_pdf", "manual", "/p/"):
        assert must in rank, f"{must} isn't considered when ranking sources"


@check("a document linked in an email can be read")
def _():
    """Call 53: an emailed receipt had links to the invoice and receipt as
    PDFs, and the assistant said it couldn't open them - even though PDF
    reading had been added the day before. Nothing connected an email to
    it."""
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    names = {getattr(t, "__name__", "") for t in inst.tools}
    assert "read_document" in names, \
        "an emailed invoice or statement still can't be opened"
    src = open("agent.py", encoding="utf-8").read()
    body = src[src.index("async def read_document("):]
    body = body[:body.index("\n    @function_tool")]
    # it must not be able to invent a web address
    assert "last_email_body" in body and "wasn't in the message" in body, \
        "it could be talked into opening a link the caller never sent"
    assert "LOOKUP_WAIT" in body, "it would leave a gap to fill with chatter"


@check("an etymology question isn't a religious discussion")
def _():
    """Call 53: "which religion is the name Raizi coming from" was refused
    twice. The phrase "which religion" had been added to the blocked list
    as unambiguous, and it isn't - that's a question about a word."""
    fine = ["which religion is the name Raizi coming from",
            "where does the name Raizi come from",
            "what is the origin of the name Chaim"]
    for t in fine:
        assert not main.is_blocked(t), f"etymology was blocked: {t}"
    still = ["which religion is the true one", "what is the best religion",
             "tell me about other religions"]
    for t in still:
        assert main.is_blocked(t), f"this should still be blocked: {t}"
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    assert "Where does the name Raizi come from" in inst.instructions, \
        "the instructions don't say a name's origin is a fact about a word"


@check("the silence prompt can't turn into a repeat of the last answer")
def _():
    """Call 53: the assistant explained how to build a sukkah, then said
    the identical paragraph again 32 seconds later. The silence watchdog
    had asked the model to check if they were still there, and instead of
    asking, it repeated itself."""
    src = open("agent.py", encoding="utf-8").read()
    body = src[src.index("async def watchdog("):]
    body = body[:body.index("\n    async def ", 10)]
    assert "Are you still there?" in body, \
        "the prompt is still left to the model to word"
    assert "generate_reply" not in body.split("warned_at = now")[1][:400], \
        "the silence prompt can still come back as anything the model likes"
    assert agent.SILENCE_WARN >= 30, \
        "20 seconds isn't long enough for an older caller to think"


@check("old watchers are cancelled, not left talking over each other")
def _():
    """Every watcher can make the agent speak, and they were never stopped
    - after three lookups in one call, three were running at once."""
    src = open("agent.py", encoding="utf-8").read()
    body = src[src.index("def _start_watch("):]
    body = body[:body.index("\n    def ", 10)]
    assert "old.cancel()" in body, "watchers still pile up over a long call"
    assert "self._watchers = [t]" in body, \
        "the list still grows instead of holding only the current one"


@check("email can be replied to, tidied and read, not just listened to")
def _():
    """A phone-only caller could hear their email but do nothing with it -
    no reply, no filing, and an attached invoice was unreadable."""
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    names = {getattr(t, "__name__", "") for t in inst.tools}
    for t in ("reply_to_email", "forward_email", "tidy_email",
              "read_attachment", "save_draft"):
        assert t in names, f"missing: {t}"
    paths = {r.path for r in main.app.routes}
    for p in ("/email/reply", "/email/forward", "/email/action",
              "/email/attachments", "/email/attachment", "/email/draft"):
        assert p in paths, f"route missing: {p}"


@check("the assistant can use contacts, Drive and the to-do list")
def _():
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    names = {getattr(t, "__name__", "") for t in inst.tools}
    for t in ("contact_details", "save_contact", "find_in_drive",
              "read_drive_file", "to_do_list", "add_to_do", "tick_off_to_do"):
        assert t in names, f"missing: {t}"
    src = open("agent.py", encoding="utf-8").read()
    body = src[src.index("async def save_contact("):]
    body = body[:body.index("\n    @function_tool")]
    assert body.index("caller_said") < body.index("backend_post"), \
        "a contact can be saved before they agree"
    # a missing permission is decided by the reason code
    class E(Exception):
        pass
    e = E("403")
    e.response = type("R", (), {"text": '{"detail":"needs_reconnect"}'})()
    assert "connect" in agent.google_refusal(e, "their Drive")
    assert agent.google_refusal(E("500 boom"), "x") == ""


@check("no file is created, changed or sent without a spoken yes")
def _():
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    names = {getattr(t, "__name__", "") for t in inst.tools}
    for t in ("create_document", "create_spreadsheet", "add_to_document",
              "change_document_words", "read_spreadsheet",
              "add_spreadsheet_row", "change_spreadsheet_cell",
              "make_editable_copy", "save_as_pdf", "send_drive_file"):
        assert t in names, f"missing: {t}"
    src = open("agent.py", encoding="utf-8").read()
    for fn in ("add_to_document", "change_document_words",
               "add_spreadsheet_row", "change_spreadsheet_cell",
               "send_drive_file"):
        body = src[src.index(f"async def {fn}("):]
        body = body[:body.index("\n    @function_tool")]
        assert "said_yes(caller_said)" in body, f"{fn} has no yes check"
        assert body.index("said_yes") < body.index("backend_post"), \
            f"{fn} acts before they agree"
    yes = ["yes", "yes please", "sure, go ahead now", "okay do it",
           "yeah that's right"]
    no = ["", "no", "no thanks", "wait", "hold on a second", "hmm",
          "I don't know", "not yet"]
    for t in yes:
        assert agent.said_yes(t), f"'{t}' wasn't taken as yes"
    for t in no:
        assert not agent.said_yes(t), f"'{t}' was taken as yes"
    assert agent.cells("Moshe; 845 555 0101 ;Monsey") == \
        ["Moshe", "845 555 0101", "Monsey"]


@check("the assistant can hand out a card code")
def _():
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    names = {getattr(t, "__name__", "") for t in inst.tools}
    assert "card_setup_code" in names
    src = open("agent.py", encoding="utf-8").read()
    assert "the best way is card_setup_code" in src, \
        "the order instructions should offer the card page first"


@check("the assistant knows what time it is instead of guessing")
def _():
    """Call 54: asked the time at 12:01 AM, it said "about 10:15 in the
    morning". It had the date and no clock, so it made one up."""
    import asyncio
    from datetime import datetime
    from zoneinfo import ZoneInfo
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    tool = next(t for t in inst.tools
                if getattr(t, "__name__", "") == "what_time_is_it")
    said = asyncio.run(tool(None))
    now = datetime.now(ZoneInfo("America/New_York"))
    assert now.strftime("%I:%M %p").lstrip("0") in said or \
        now.strftime("%p") in said, said
    assert "call started at" in inst.instructions, \
        "the instructions don't say when the call started"
    assert "Never guess a time" in inst.instructions


@check("the assistant can't put words in a code box or invent a way out")
def _():
    src = open("agent.py", encoding="utf-8").read()
    body = src[src.index("async def _code_to_site("):]
    body = body[:body.index("\n    @function_tool")]
    assert "len(digits) < 3" in body, \
        "the code path still forwards whatever it is given"
    body = src[src.index("async def try_another_way("):]
    body = body[:body.index("\n    @function_tool")]
    assert "shop sign-in" in body, \
        "with no Google sign-in running it says nothing useful, and the " \
        "model invents 'requesting another method'"
    for reason in ("bad_code", "no_code"):
        line = agent.login_failure_line("amazon", reason, "msg", 0, "u@x.com")
        assert line and "amazon" in line.lower(), reason
    assert "password" in agent.login_failure_line(
        "amazon", "bad_code", "msg", 0, "u@x.com").lower(), \
        "a wrong code must not send it back to asking for the password"


@check("an expired Google connection is explained, not reported as a crash")
def _():
    """20 Sep: both mailboxes hit Google's 7-day limit for unverified apps.
    Every email call answered 500, which the assistant reads out as
    "something went wrong" instead of "it needs connecting again"."""
    import asyncio
    from google.auth.exceptions import RefreshError
    from starlette.requests import Request as SReq
    req = SReq({"type": "http", "method": "GET", "path": "/test/unread",
                "headers": [], "query_string": b""})
    resp = asyncio.run(main.google_token_dead(
        req, RefreshError("invalid_grant: Token has been expired or revoked.")))
    assert resp.status_code == 403 and b"connection_expired" in resp.body
    line = agent.google_refusal(
        Exception('{"detail":"connection_expired"}'), "their email")
    assert "expired" in line and "email_connect_code" in line, line
    src = open("agent.py", encoding="utf-8").read()
    for fn in ("check_email", "recent_email", "search_email"):
        body = src[src.index(f"async def {fn}("):]
        body = body[:body.index(chr(10) + "    @function_tool")]
        assert "google_refusal" in body, f"{fn} has no reconnect message"


@check("a code the caller can't get ends the sign-in, not the call")
def _():
    """Call 58: 'I don't have that phone with me now' got 'Understood, I'm
    handling that' - and the caller listened to silence for 45 seconds and
    hung up. Nothing was being handled."""
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    names = {getattr(t, "__name__", "") for t in inst.tools}
    assert "stop_that" in names, "no way to give up on a code"
    text = inst.instructions
    assert "stop_that" in text and "I'm handling it" in text,         "the instructions don't cover a caller who can't get the code"
    assert "DIGITS ONLY" in text, "nothing says only digits go in a code box"
    paths = {r.path for r in main.app.routes}
    assert "/jobs/cancel" in paths, "a single job still can't be stopped"


@check("the voice model asks instead of improvising, and a crash says why")
def _():
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    names = {getattr(t, "__name__", "") for t in inst.tools}
    assert "what_now" in names, "no way to ask for a decision"
    text = inst.instructions
    assert "what_now" in text and "NEVER say you are working on something" \
        in text, "the instructions still leave it to improvise"
    src = open("agent.py", encoding="utf-8").read()
    body = src[src.index("def auto_report("):]
    body = body[:body.index("ADDRESS_RULE")]
    assert "ask_advisor(" in body, \
        "a crashed tool still answers with a shrug instead of the facts"
    # the advisor decides words, never permission
    for fn in ("send_email", "confirm_order"):
        i = src.find(f"async def {fn}(")
        if i < 0:
            continue
        j = src.find(chr(10) + "    @function_tool", i)
        b = src[i:j if j > 0 else len(src)]
        assert "ask_advisor" not in b, \
            f"{fn} must not take its go-ahead from the advisor"


@check("'nothing readable' is never told to a caller as 'you have none'")
def _():
    line = agent.login_failure_line("amazon", "no_results", "m", 0, "u@x.com")
    assert "do NOT say they have no orders" in line, line
    assert "doesn't exist" in line, line


@check("the assistant is told what it already knows about the caller")
def _():
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1, "", "Hard of hearing - speak up.")
    text = inst.instructions
    assert "WHAT WE KNOW ABOUT THIS PERSON" in text
    assert "Hard of hearing" in text, "the notes never reach the model"
    blank = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                            "+1555", 1)
    assert "first time" in blank.instructions,         "with no notes it should say so, not show an empty heading"
    src = open("agent.py", encoding="utf-8").read()
    assert 'backend_get("/profile"' in src,         "nothing loads the notes when a call starts"


@check("a caller can hear the whole basket before deciding")
def _():
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    names = {getattr(t, "__name__", "") for t in inst.tools}
    assert "review_checkout" in names
    text = inst.instructions
    assert "CHECKING A BASKET BEFORE BUYING" in text
    assert "more in it than they asked for" in text,         "nothing tells it to read out what was already in the cart"


@check("a correction stops the old work, and prices come from shop pages")
def _():
    """Call 61: "Ecco" was heard as "Echo", and when the caller corrected
    it the assistant said it was "still finishing the previous search" and
    waited on work that was already worthless. Then "where is it cheapest"
    was answered from a web search with an outlet shop's street address."""
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    names = {getattr(t, "__name__", "") for t in inst.tools}
    for t in ("stop_that", "find_best_price"):
        assert t in names, f"missing: {t}"
    text = inst.instructions
    assert "WHEN THEY CORRECT YOU" in text and "stop_that" in text
    assert "still finishing" in text and "is genuinely still running" in text, \
        "it must not claim to be finishing work the caller corrected, but may say so about work that really is running"
    assert "find_best_price" in text and "Do NOT use find_out for" \
        in text, "prices can still be answered from search summaries"
    paths = {r.path for r in main.app.routes}
    assert "/jobs/price" in paths
    # the price job must read shop pages and write each one down
    # it must not try to drive Google: Google answers a browser with a
    # captcha, and the first price job died at the front door
    src = source()
    body = src[src.index("def job_price("):]
    body = body[:body.index(chr(10) + "@app.", 10)]
    assert "tool_web_search(" in body, "the price job still browses Google"
    # and no browse job may navigate to a search engine at all
    run = src[src.index("def _run_browse("):]
    run = run[:run.index(chr(10) + "def ", 10)]
    assert "duckduckgo" in run and "kept off the search" in run,         "a job can still drive Google and be met with a puzzle"
    assert '"google." ' in body or '"google."' in body,         "search engines are not filtered out of the shop list"
    goal = main.PRICE_GOAL.format(item="shoes", shops="  1. https://shop.example")
    assert "https://shop.example" in goal, "the shops found are never handed to the job"
    for must in ("found", "never from a search summary", "Buy nothing"):
        assert must in goal, must


@check("the standing instructions stay small enough to be followed")
def _():
    """Every word here is read on every turn of every call: it is the
    slowest, most expensive and most easily forgotten place to put
    anything. It was 5,675 words. Procedure belongs in tool results, which
    cost nothing until the tool is used, and judgement belongs with the
    advisor, which can see the real state.

    If this fails, do not raise the number - move the new rule into the
    result of the tool it applies to."""
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    words = len(inst.instructions.split())
    assert words <= 3200, (
        f"the instructions are back up to {words} words. Move the newest "
        f"section into the tool result it belongs to.")
    # Counted on the assistant itself, not across the file: the sign-up
    # agent's tools exist only on a first call from an unknown number and
    # are never sent on a customer's turns. It has its own, smaller limit.
    tools = inst.tools
    assert len(tools) <= 80, (
        f"{len(tools)} tools. Each one's name and description is sent on "
        f"every turn too - merge the near-duplicates rather than adding.")
    signup_tools = agent.Signup("+15550100", 1, lambda a: None).tools
    assert len(signup_tools) <= 5, (
        f"the sign-up agent has {len(signup_tools)} tools - it only needs "
        f"to check a code, create the account and hang up")


@check("every file has every name it uses")
def _():
    """Moving code between files breaks quietly: the function still
    parses, the app still starts, and the NameError arrives on a call, in
    front of a customer. Splitting main.py did it twice - _CONNECT_FAILS
    and LINK_LIFE_MIN were left behind - and both were found here rather
    than by a caller.

    Only names that another backend file defines are reported, so an
    ordinary local variable is never mistaken for a missing import."""
    import ast
    import builtins
    import glob
    import importlib

    files = [f for f in sorted(glob.glob("*.py"))
             if f not in ("check.py", "scenarios.py", "probe.py", "agent.py")]
    elsewhere = set()
    for f in files:
        tree = ast.parse(io.open(f, encoding="utf-8").read())
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                elsewhere.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        elsewhere.add(t.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target,
                                                                ast.Name):
                elsewhere.add(node.target.id)

    problems = []
    for f in files:
        mod = importlib.import_module(f[:-3])
        have = set(dir(mod)) | set(dir(builtins))
        tree = ast.parse(io.open(f, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            local = {a.arg for a in node.args.args + node.args.kwonlyargs}
            if node.args.vararg:
                local.add(node.args.vararg.arg)
            if node.args.kwarg:
                local.add(node.args.kwarg.arg)
            reads = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    if isinstance(sub.ctx, ast.Store):
                        local.add(sub.id)
                    else:
                        reads.add(sub.id)
                elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.ClassDef)):
                    local.add(sub.name)
                elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for a in sub.names:
                        local.add((a.asname or a.name).split(".")[0])
                elif isinstance(sub, ast.ExceptHandler) and sub.name:
                    local.add(sub.name)
            for name in reads - local - have:
                if name in elsewhere:
                    problems.append("%s: %s() uses %s, which lives in "
                                    "another file and is never imported"
                                    % (f, node.name, name))
    assert not problems, (chr(10) + "         ").join(
        sorted(set(problems))[:8])


@check("nothing an email tool does is permanent")
def _():
    """A caller cannot see what just happened, so every action has to be
    reversible - trash is recoverable, labels can be put back, and there
    is no permanent delete anywhere."""
    for undoable in ("archive", "star", "important", "spam"):
        assert undoable in main.MESSAGE_ACTIONS, f"{undoable} is missing"
    for undo in ("unarchive", "unstar", "not_spam"):
        assert undo in main.MESSAGE_ACTIONS, f"no way to undo: {undo}"
    src = source()
    assert "messages().delete(" not in src, \
        "a permanent delete has appeared - trash only, it is recoverable"
    body = src[src.index("def tool_message_action("):]
    body = body[:body.index("\ndef ", 10)]
    assert "untrash" in body, "trash can't be undone"


@check("sending or forwarding needs a spoken yes first")
def _():
    """Forwarding sends the whole original to somebody else. Neither it
    nor a reply may go without the caller actually agreeing."""
    src = open("agent.py", encoding="utf-8").read()
    for fn in ("reply_to_email", "forward_email"):
        body = src[src.index(f"async def {fn}("):]
        body = body[:body.index("\n    @function_tool")]
        assert "caller_said" in body, f"{fn} can send with no confirmation"
        assert body.index("caller_said") < body.index("backend_post"), \
            f"{fn} sends before checking they agreed"


@check("required tools exist")
def _():
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    names = {getattr(t, "__name__", "") for t in inst.tools}
    for t in ("connect_email", "check_email", "mark_read", "end_call",
              "save_site_login", "sign_in_to_site", "confirm_order",
              "recent_email"):
        assert t in names, f"tool missing: {t}"


@check("email results keep the sender, date and read state")
def _():
    """These were trimmed to a display name, so when a caller asked for the
    sender's address the model had nothing and made one up."""
    line = agent._describe(1, {
        "from": "Coinbase Bytes <newsletter@mail.coinbase.com>",
        "subject": "Why are privacy tokens outperforming bitcoin?",
        "when": "Aug 13 at 9:02 AM", "category": "Promotions",
        "unread": True})
    for must in ("newsletter@mail.coinbase.com", "Aug 13", "Promotions",
                 "unread"):
        assert must in line, f"{must!r} was dropped before the model: {line}"
    src = open("agent.py", encoding="utf-8").read()
    assert '.split("<")[0]' not in src, \
        "an email tool is trimming the sender again - use _describe()"


@check("the call record is saved (duration and PIN status)")
def _():
    """/calls/end was posted without the service token, so every call was
    stored as 0 seconds and PIN 'no'."""
    src = open("agent.py", encoding="utf-8").read()
    i = src.index("/calls/end")
    assert "headers=AUTH" in src[i - 200:i + 200], \
        "/calls/end is posted without headers=AUTH - it will 401"


@check("an address the caller gives in full is not turned into nonsense")
def _():
    """Call 64: he asked for khconnect.kioskhut.com and we opened
    https://www.khconnect.kioskhut.com.com. com.com answers everything
    with a wildcard, so the browser reported a certificate error and he
    was told his own site had a security problem. Three minutes of his
    call, and the address was ours."""
    _undo_site = everywhere("official_site", lambda name: "")
    try:
        import browser
        for said, want in (
                ("amazon", "https://www.amazon.com"),
                ("Amazon", "https://www.amazon.com"),
                ("khconnect.kioskhut.com", "https://khconnect.kioskhut.com"),
                ("www.lowes.com", "https://www.lowes.com"),
                ("shop.example.co.uk", "https://shop.example.co.uk"),
                ("https://etsy.com/orders", "https://etsy.com/orders"),
                ("amazon/gp/orders", "https://www.amazon.com/gp/orders"),
                ("", "")):
            got = browser.site_url(said)
            assert got == want, f"site_url({said!r}) gave {got!r}, wanted {want!r}"
        src = source()
        assert 'www.{site}.com' not in src, \
            "a runner is still building an address by hand instead of site_url()"
    finally:
        _undo_site()


@check("a shop's name becomes the shop's real address")
def _():
    """Call 65: "from B&H" opened https://www.b&h.com, which is not a
    website, and the caller was told B&H "isn't reachable"."""
    _undo_site = everywhere("official_site", lambda name: "")
    try:
        import browser
        for said, want in (("B&H", "https://www.bhphotovideo.com"),
                           ("b and h", "https://www.bhphotovideo.com"),
                           ("B&H Photo-Video-Audio", "https://www.bhphotovideo.com"),
                           ("Trader Joe's", "https://www.traderjoes.com"),
                           ("Barnes & Noble", "https://www.barnesandnoble.com"),
                           ("Home Depot", "https://www.homedepot.com"),
                           ("Lowe's website", "https://www.lowes.com")):
            got = browser.site_url(said)
            assert got == want, f"{said!r} -> {got!r}, wanted {want!r}"
        for said in ("B&H", "Macy's", "Stop & Shop", "Dick's", "A&P", "Joe's Deli",
                     "some shop", "AT&T"):
            host = browser.site_url(said).split("/")[2]
            assert not any(ch in host for ch in "&' "), \
                f"{said!r} became {host!r} - that can't be a web address"
    finally:
        _undo_site()


@check("a shop's name is looked up the way a search would, not guessed")
def _():
    """David: "if I type BNH into Google it comes up with the BNH website -
    why can't we?" Now it does. These are the real top results for each
    name; the answer is the first one that is the shop itself rather than
    a page about it."""
    import search
    real = {
        "b&h": ["https://www.bhphotovideo.com/", "https://www.youtube.com/x",
                "https://www.facebook.com/bhphoto"],
        "pomegranate brooklyn": ["https://www.yelp.com/biz/pomegranate",
                                 "https://thepompeople.com/",
                                 "https://www.tripadvisor.com/x"],
        "seasons kosher supermarket": ["https://www.linkedin.com/company/x",
                                       "https://seasonskosher.com/"],
        "instacart": ["https://www.instacart.com/", "https://en.wikipedia.org/x"],
        "nowhere at all": ["https://www.yelp.com/x", "https://www.facebook.com/y"],
    }
    asked = []

    def fake(name):
        asked.append(name)
        return {"organic": [{"link": u} for u in real[name.lower()]]}

    undo_key = everywhere("SERPER_API_KEY", "x")
    undo_fn = everywhere("_serper_site", fake)
    search._SITE_CACHE.clear()
    try:
        got = {n: search.official_site(n) for n in
               ("B&H", "Pomegranate Brooklyn", "Seasons kosher supermarket",
                "Instacart", "nowhere at all")}
        search.official_site("B&H")                 # asked again
    finally:
        undo_fn()
        undo_key()
        search._SITE_CACHE.clear()
    assert got["B&H"] == "https://www.bhphotovideo.com", got
    assert got["Pomegranate Brooklyn"] == "https://thepompeople.com", \
        "a directory page was taken for the shop"
    assert got["Seasons kosher supermarket"] == "https://seasonskosher.com"
    assert got["Instacart"] == "https://www.instacart.com", \
        "the thing they asked for was thrown out as a directory"
    assert got["nowhere at all"] == "", "a directory was used as a last resort"
    assert asked.count("B&H") == 1, "the same name was searched twice"

    import browser
    undo = everywhere("official_site", lambda name: "https://thepompeople.com")
    try:
        assert browser.site_url("Pomegranate Brooklyn") == \
            "https://thepompeople.com"
        # an address they gave is never replaced by a search
        assert browser.site_url("khconnect.kioskhut.com") == \
            "https://khconnect.kioskhut.com"
    finally:
        undo()


@check("the shop isn't part of the item's name, and a model number decides")
def _():
    """Call 65: "Epson ET-5850 printer from B&H" was reported as "not that
    exact one" - "from" and "b&h" were counted as words of the printer's
    name, and ET-5850 was split into "et" (dropped) and "5850". These are
    the three real listings that came back."""
    import search
    real = [
        {"source": "AllPrintHeads.com", "price": "$395.00",
         "title": "Multifunction Printer Epson EcoTank ET-5850 25 ppm WiFi "
                  "Black"},
        {"source": "B&H Photo-Video-Audio", "price": "$699.99",
         "title": "Epson EcoTank Pro All-in-One Supertank Printer ET-5850"},
        {"source": "Sears", "price": "$1,438.44",
         "title": "Epson EcoTank Pro ET-5850 All-in-One Cartridge-Free "
                  "Supertank"},
        {"source": "Somewhere", "price": "$299.00",
         "title": "Epson EcoTank ET-4850 All-in-One Printer"},
    ]
    asked = []

    def fake(q):
        asked.append(q)
        return {"shopping": real}

    undo_key = everywhere("SERPER_API_KEY", "x")
    undo_fn = everywhere("_serper_shopping", fake)
    try:
        d = search.shopping_prices("Epson ET-5850 printer from B&H")
    finally:
        undo_fn()
        undo_key()
    assert asked == ["Epson ET-5850 printer"], \
        f"the shop went into the search as part of the item: {asked}"
    assert d["exact"], f"the exact printer was called a near miss: {d['answer']}"
    assert "could not find that exact" not in d["answer"], d["answer"]
    assert not any("4850" in o["title"] for o in d["offers"]), \
        "a different model was priced as the one they asked for"
    assert d["shop"] == "B&H", d["shop"]
    assert d["at_shop"] and d["at_shop"][0]["price"] == "$699.99", d["at_shop"]


@check("a listing that is FOR the item is not the item")
def _():
    """Call 67: ink "for Epson ET-5850" at $10 was the cheapest ET-5850.
    The real printer's own title says "Cartridge-Free" and printers come
    with paper trays - so this can't be a list of nouns."""
    import search
    titles = [
        ("Ink Technologies", "$10.00", "T522 Ink Bottles for Epson ET-5850"),
        ("DigitalDeckCovers", "$35.99", "Dust Cover for Epson EcoTank ET-5850"),
        ("ClickInks", "$45.41", "Compatible 522 Ink Set Epson ET-5850 4 Pack"),
        ("B&H Photo-Video-Audio", "$699.99", "Epson EcoTank Pro ET-5850 "
         "All-in-One Cartridge-Free Supertank Printer"),
        ("AllPrintHeads.com", "$395.00", "Multifunction Printer Epson EcoTank "
         "ET-5850 25 ppm WiFi 250-sheet paper tray"),
    ]
    undo_key = everywhere("SERPER_API_KEY", "x")
    undo_fn = everywhere("_serper_shopping", lambda q: {"shopping": [
        {"source": s, "price": p, "title": t} for s, p, t in titles]})
    try:
        printer = search.shopping_prices("Epson ET-5850")
        ink = search.shopping_prices("ink for Epson ET-5850")
    finally:
        undo_fn()
        undo_key()
    shops = [o["shop"] for o in printer["offers"]]
    assert shops == ["AllPrintHeads.com", "B&H Photo-Video-Audio"], shops
    assert printer["exact"]
    assert [o["shop"] for o in ink["offers"]] == ["Ink Technologies",
                                                   "ClickInks"], \
        "asking for ink found printers and dust covers"


@check("a password or PIN being given never reaches the call log")
def _():
    """Call 67: a password spelled a few characters at a time was written
    to the call log in pieces - one line at a time, nothing looked like a
    password. Invented values only, here."""
    state = {"on": False, "turns": 0}
    convo = [
        ("assistant", "Can you please say your PIN?", False),
        ("user", "Four, seven, one, nine.", True),
        ("assistant", "Thanks, the PIN is confirmed. What can I do?", False),
        ("user", "Order a printer from B&H.", False),
        ("assistant", "Please tell me the password for that account, one "
                      "character at a time.", False),
        ("user", "capital K", True),
        ("assistant", "Got it, the first character is a capital K.", True),
        ("assistant", "No problem, take your time and let me know the next "
                      "characters whenever you're ready.", False),
        ("user", "lowercase q, then 4 7", True),
        ("assistant", "So far: capital K, lowercase q, then 47.", True),
        ("user", "then z z. That's it.", True),
        ("assistant", "The username I have is a@example.com. Right?", False),
        ("user", "Yes.", False),
        ("assistant", "Your login is saved and stored encrypted.", False),
        ("user", "Great, now order it.", False),
        # call 68: "Got it." in the middle of a PIN is not the end of it
        ("assistant", "Please say your PIN.", False),
        ("user", "Six, two, nine, four.", True),
        ("assistant", "Got it.", False),
        ("user", "Six, two, nine, four.", True),
        ("assistant", "Thanks, your PIN is verified.", False),
        # calls 82 and 83: the read-back carried the password itself
        ("assistant", "Please say the password one character at a time.",
         False),
        ("user", "capital K, then 9 6 3 2", True),
        ("assistant", "I read three sixes at the end. Could you say it "
                      "again?", True),
        ("assistant", "Let me confirm the password one more time: the "
                      "digit 9, the digit 6, the digit 3, the digit 2. Is "
                      "every character right?", True),
        ("user", "Yes.", True),
        ("assistant", "Thanks, the password is saved.", False),
        # call 57: given before it was asked for
        ("user", "The username is c@example.com.", False),
        ("user", "And the password is capital T lowercase v", True),
        ("user", "then 3 8", True),
        ("assistant", "Thanks. Let me read that back: capital T, "
                      "lowercase v, 38.", True),
        ("assistant", "Your login is saved.", False),
    ]
    for role, text, hide in convo:
        stored = agent.keep_or_blank(state, role, text)
        if hide:
            assert stored == agent.BLANKED, f"written down: {role}: {text}"
        else:
            assert stored == text, f"blanked for no reason: {role}: {text}"
    src = io.open("agent.py", encoding="utf-8").read()
    ep = src[src.index("def _on_item(ev):"):][:1800]
    assert "stored = keep_or_blank(secret, role, text)" in ep
    assert "log_turn(call_id, who, stored)" in ep, \
        "the call log still gets the raw words"
    assert '"text": stored' in ep, "their memory still gets the raw words"


@check("passwords and PINs already in the records can be blanked, and only them")
def _():
    """Before call 67's fix, a spelled password and every spoken PIN were
    written to the call log, their memory and the live log. This cleans
    them with the same rule the voice side now uses. Invented values."""
    from fastapi.testclient import TestClient
    cl = TestClient(main.app, raise_server_exceptions=False,
                    base_url="https://t")
    cl.post("/admin/login", json={"password": os.environ.get(
        "ADMIN_PASSWORD", "changeme")})
    convo = [
        ("agent", "Can you please say your PIN?"),
        ("caller", "Nine, three, eight, two."),
        ("agent", "Thanks, the PIN is confirmed. What can I do?"),
        ("caller", "Save my shop login."),
        ("agent", "Please tell me the password, one character at a time."),
        ("caller", "capital W, lowercase r, then 5 5"),
        ("agent", "So far: capital W, lowercase r, 55."),
        ("caller", "That's all."),
        ("agent", "The username I have is b@example.com. Right?"),
        ("caller", "Yes, now order the kettle."),
    ]
    db = main.Session()
    call = main.Call(account_id=None, from_number="+15550100888")
    db.add(call)
    db.commit()
    db.refresh(call)
    cid = call.id
    for who, text in convo:
        db.add(main.CallTurn(call_id=cid, who=who, text=text))
        db.add(main.Event(kind="call", ref=f"call {cid}",
                          text=f"{who}: {text}", level="info"))
    db.commit()
    db.close()

    dry = cl.post("/privacy/blank_secrets", params={"dry_run": 1}).json()
    db = main.Session()
    untouched = [t.text for t in db.query(main.CallTurn)
                 .filter_by(call_id=cid).order_by(main.CallTurn.id).all()]
    db.close()
    assert untouched == [t for _, t in convo], "a dry run changed something"
    assert dry["blanked"]["call_turns"] >= 4, dry

    done = cl.post("/privacy/blank_secrets", params={"dry_run": 0}).json()
    db = main.Session()
    after = [t.text for t in db.query(main.CallTurn)
             .filter_by(call_id=cid).order_by(main.CallTurn.id).all()]
    log = [e.text for e in db.query(main.Event)
           .filter_by(kind="call", ref=f"call {cid}")
           .order_by(main.Event.id).all()]
    db.close()
    for n in (1, 5, 6, 7):
        assert after[n] == agent.BLANKED, f"still written: {convo[n][1]}"
    for n in (0, 2, 3, 4, 8, 9):
        assert after[n] == convo[n][1], f"blanked for no reason: {convo[n][1]}"
    assert not any("capital W" in e or "Nine, three" in e for e in log), \
        "the live log still has it"
    again = cl.post("/privacy/blank_secrets", params={"dry_run": 0}).json()
    assert not any(again["blanked"].values()), \
        f"running it twice changed more: {again}"
    assert done["dry_run"] is False


@check("fixed words are spoken the one way this voice can")
def _():
    """Call 68: say() needs a text-to-speech voice, OpenAI's realtime model
    reports supports_say=False, and every say() raised - the failed B&H
    sign-in was never announced and "Are you still there?" was heard once
    in 68 calls."""
    import asyncio
    from livekit.plugins import openai as _oa
    rt = _oa.realtime.RealtimeModel(model="gpt-realtime")
    assert not rt.capabilities.supports_say, \
        "this voice can say() now - speak_exactly will use it, which is fine"

    class Caps:
        supports_say = False

    class Model:
        capabilities = Caps()

    class Fake:
        llm, tts = Model(), None

        def __init__(self):
            self.said, self.asked = [], []

        def say(self, words, **k):
            self.said.append(words)

        def generate_reply(self, **k):
            self.asked.append(k.get("instructions", ""))

    s = Fake()
    assert asyncio.run(agent.speak_exactly(s, "Are you still there?"))
    assert not s.said and 'nothing else: "Are you still there?"' in s.asked[0]
    s2 = Fake()
    assert not asyncio.run(agent.speak_exactly(s2, "Hold on.", mid_tool=True))
    assert not s2.said and not s2.asked, "it tried to speak from inside a tool"
    s3 = Fake()
    s3.tts = object()
    asyncio.run(agent.speak_exactly(s3, "Hello."))
    assert s3.said == ["Hello."], "with a real TTS voice, say() is the way"
    src = io.open("agent.py", encoding="utf-8").read()
    assert src.count("session.say(words") == 1 and "sess.say(" not in src, \
        "say() is called somewhere it will raise"
    wd = src[src.index("async def watchdog("):
              src.index("async def hangup_when_asked(")]
    assert 'speak_exactly(session, "Are you still there?")' in wd, \
        "the silence prompt no longer goes through speak_exactly"


@check("the panel can watch a browser while it works")
def _():
    """Until now a wall could only be deduced from a job's steps after the
    fact. Browserbase keeps a live view of a running browser and a
    recording of a finished one; both are one click away now."""
    paths = {r.path for r in main.app.routes}
    assert "/browser/live" in paths and "/browser/session" in paths, paths
    import admin_page
    page = admin_page.ADMIN_HTML
    assert "loadLive()" in page and "liverows" in page,         "the panel has no way to list running browsers"
    assert "Watch a browser working" in page
    assert "browserbase.com/sessions" in page,         "no link to the recordings"
    src = io.open("main.py", encoding="utf-8").read()
    i = src.index("def browser_live(")
    body = src[i:i + 2500]
    assert "require_auth(request)" in body,         "a live view of a customer's browser behind no password"
    assert "status=RUNNING" in body, "it would list finished sessions too"


@check("the admin panel's script actually runs")
def _():
    """`async var chArea = "";` sat in the page for weeks. A browser stops
    reading a script at a syntax error, so every button and table below
    that line did nothing, and every Python check here passed happily:
    they test the file, not the page it builds.

    node is used when it is there - it is the same engine the browser
    uses. Without it, the patterns that have actually broken this page."""
    import re as _re
    import shutil
    import subprocess
    import tempfile
    import admin_page
    page = admin_page.ADMIN_HTML
    scripts = _re.findall(r"<script[^>]*>(.*?)</script>", page, _re.S)
    assert scripts, "no script found in the admin page at all"
    js = "\n".join(scripts)

    for bad, why in (
            (r"\basync\s+(var|const|let)\b", "async in front of a variable"),
            (r"\basync\s+(if|for|while|return|switch)\b",
             "async in front of a statement"),
            (r"\bfunction\s*\(\s*\)\s*\{[^}]*\bawait\b",
             "await inside a function that is not async")):
        hit = _re.search(bad, js)
        assert not hit, (f"{why}: ...{js[max(0, hit.start() - 60):hit.end() + 40]}...")

    node = shutil.which("node")
    if not node:
        return                      # the patterns above are the fallback
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(js)
        path = f.name
    try:
        r = subprocess.run([node, "--check", path], capture_output=True,
                           text=True, timeout=60)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    assert r.returncode == 0, (
        "the admin page's script does not parse, so the browser stops "
        "reading it there and everything below is dead:\n"
        + (r.stderr or r.stdout)[:600])


@check("a job that is getting nowhere stops instead of holding the caller")
def _():
    """Call 70: twelve steps, a hundred and ten seconds, a sign-in that
    was never going to happen. Circles were spotted at step 5 and it
    carried on to step 12 - because spotting them CLEARED the record of
    what it had been doing, so the evidence started again from nothing."""
    import browser

    # 1. the record is not wiped when a warning is given
    src = source()
    body = src[src.index("def _run_browse("):]
    body = body[:body.index("\ndef ", 10)]
    i = body.index("_going_in_circles(sigs)")
    window = body[i:i + 1200]
    assert "sigs.clear()" not in window, \
        "spotting a loop still clears the evidence, so the limit is never hit"
    assert "dead_ends >= 3" in body, "error pages are still ordinary steps"
    assert "CALL_BUDGET" in body and "took_too_long" in body, \
        "a caller can still be held for as long as the model likes"

    # 2. with the record kept, the same action three times is caught and
    #    stays caught - it took four warnings to stop before
    sigs = []
    fired = 0
    for _ in range(6):
        sigs.append("click:3:log in")
        if browser._going_in_circles(sigs):
            fired += 1
    assert fired >= 3, f"only fired {fired} times across six repeats"

    # 3. B&H's own error pages, and pages that are not errors
    for dead in ("Page Not Found. Start Over",
                 "404 - we can't find that page",
                 "Sorry, the page you requested was not found"):
        assert browser.DEAD_END.search(dead), f"missed an error page: {dead}"
    for fine in ("Epson EcoTank Pro ET-5850 - $699.99 In Stock",
                 "Sign in to your account",
                 "Your cart has 1 item"):
        assert not browser.DEAD_END.search(fine), \
            f"an ordinary page was called an error page: {fine}"

    # 4. what the caller hears when the two minutes are up
    a = agent.Assistant({"account_id": 1, "name": "T"}, "+1555", 1)
    a.job_site = "B&H"
    words = a._failure_words({"kind": "site_login", "reason": "took_too_long"})
    assert "two minutes" in words and "B&H" in words, words
    assert "holding" in words, words

    # 5. a job with nobody on the phone keeps its old, longer leash
    assert "if call_id and time.time() - t0 > int(" in body and \
        'payload.get("budget") or CALL_BUDGET' in body, \
        "a background job would be cut off by a caller's clock"
    assert 60 <= browser.CALL_BUDGET <= 180, \
        f"{browser.CALL_BUDGET}s is not a sensible time to hold someone"


@check("claiming to have sent something is caught on the call, not hours later")
def _():
    """Call 76: "I've sent you a link", twice, with no sending tool run on
    the whole call. A new customer waited for a text that was never coming
    and hung up. The after-call review caught it; that is no use to him."""
    said = agent.CLAIMED_SEND
    for claim in ("I've sent you a link to connect your email.",
                  "I have sent the link again.",
                  "I just texted it to you.",
                  "I emailed that to you a moment ago.",
                  "I\u2019ve sent it to your phone."):
        assert said.search(claim), f"missed a claim: {claim}"
    for fine in ("Amazon sent a code to your phone.",
                 "The site says it sent an email.",
                 "Would you like me to text it to you?",
                 "I'll send it once you say yes.",
                 "They sent it yesterday."):
        assert not said.search(fine), f"wrongly treated as a claim: {fine}"

    spoke, facts = [], []

    class Ctx:
        def __init__(self):
            self.items = []

        def copy(self):
            return self

        def add_message(self, role, content):
            facts.append(content)

    class Fake:
        llm = tts = None

        def say(self, words, **k):
            spoke.append(words)

        def generate_reply(self, **k):
            spoke.append(k.get("instructions", ""))

    import asyncio

    class Stand:
        """Only what check_the_claim touches. A real Assistant cannot be
        used: chat_ctx is read-only on a LiveKit agent (rule 4)."""

        def __init__(self, ran):
            self.ran_tools = set(ran)
            self.chat_ctx = Ctx()
            self.owned_up = False

        async def update_chat_ctx(self, ctx, **k):
            return None

    def run_one(ran):
        obj = Stand(ran)
        real = agent.log_turn

        async def quiet(*a, **k):
            return None
        agent.log_turn = quiet

        async def drive():
            # as a real call does it: from inside the running loop, then
            # give the background correction a moment to finish
            agent.check_the_claim(Fake(), obj,
                                  "I've sent you a link to connect your "
                                  "email.", 1)
            await asyncio.sleep(0.05)
        try:
            asyncio.run(drive())
        finally:
            agent.log_turn = real
        return obj

    # nothing was sent: the caller is told, and the record is corrected
    spoke.clear()
    facts.clear()
    obj = run_one([])
    assert spoke, "a false claim went uncorrected"
    assert agent.SENT_NOTHING in " ".join(spoke), spoke
    assert any("nothing on this call sent anything" in f for f in facts), facts
    assert any("email_connect_code" in f for f in facts), \
        "it is not pointed at a way that works"
    assert obj.owned_up is True

    # something really was sent: leave it alone
    spoke.clear()
    facts.clear()
    run_one(["send_text"])
    assert not spoke, "it apologised for a text it really did send"

    # and it owns up once, not on every turn
    spoke.clear()
    obj = run_one([])

    async def again():
        agent.check_the_claim(Fake(), obj, "I've sent it again.", 1)
        await asyncio.sleep(0.05)
    asyncio.run(again())
    assert len(spoke) == 1, f"it apologised {len(spoke)} times"

    src = io.open("agent.py", encoding="utf-8").read()
    assert "note_tool_ran(self, fn.__name__)" in src and \
        "agent_obj.ran_tools.add(name)" in src, \
        "tool calls are not recorded, so no claim can be checked"
    assert "check_the_claim(session, agent_obj, text, call_id)" in src, \
        "nothing checks what is said as it is said"


@check("Google's tap prompt is said TO the caller, in fixed words")
def _():
    """Call 82: "Google sent a prompt to their phone. Tell them to tap Yes"
    - read out word for word to the man holding the phone. It was an
    instruction to the assistant, and he heard himself talked about."""
    b = io.open("browser.py", encoding="utf-8").read()
    taps = [b[i:i + 400] for i in range(len(b))
            if b.startswith('_ob_set(sid, "needs_tap"', i)]
    assert len(taps) >= 3, f"found {len(taps)} tap prompts"
    import re as _re
    for t in taps:
        t = _re.sub(r'"\s*\n\s*f?"', "", t)      # join the pieces
        said = t[:t.index(")") + 1] if ")" in t else t
        assert "your phone" in said, f"not said to the caller: {said[:160]}"
        for wrong in ("Tell them", "their phone", "them to"):
            assert wrong not in said, f"talks about the caller: {said[:160]}"
    a = io.open("agent.py", encoding="utf-8").read()
    i = a.index('if st == "needs_tap":')
    assert 'return ("exact", msg)' in a[i:i + 400], \
        "the tap prompt is still handed to the model to phrase"
    w = a[a.index("async def _watch(self"):a.index("def _start_watch(")]
    assert "isinstance(line, tuple)" in w and "speak_exactly(" in w, \
        "the watcher cannot say fixed words"


@check("a claim that something was asked for is caught when nothing asked")
def _():
    """Call 82: "We've asked Google to send a text code" - the sign-in had
    already failed and no tool ran. He waited for a text nobody asked
    for."""
    import asyncio
    import time as _t
    ask = agent.CLAIMED_ASK
    for claim in ("We\u2019ve asked Google to send a text code.",
                  "We've asked Google to send a text code.",
                  "I have requested a new code from Amazon.",
                  "I've asked the office to call you back."):
        assert ask.search(claim), f"missed a claim: {claim}"
    for fine in ("I asked you to read the code out.",
                 "Google sent a code to your phone.",
                 "Shall I ask Google to send a text code?",
                 "The office will call you tomorrow."):
        assert not ask.search(fine), f"wrongly treated as a claim: {fine}"

    spoke, facts = [], []

    class Ctx:
        items = []

        def copy(self):
            return self

        def add_message(self, role, content):
            facts.append(content)

    class Fake:
        llm = tts = None

        def generate_reply(self, **k):
            spoke.append(k.get("instructions", ""))

    class Stand:
        def __init__(self, tool_at, live=False):
            self.ran_tools = set(tool_at)
            self.tool_at = dict(tool_at)
            self.job_live = live
            self.chat_ctx = Ctx()

        async def update_chat_ctx(self, ctx, **k):
            return None

    async def say(obj, words):
        agent.check_the_claim(Fake(), obj, words, 1)
        await asyncio.sleep(0.05)

    real_log = agent.log_turn

    async def no_log(*a, **k):
        return None
    agent.log_turn = no_log
    try:
        # call 82: connect_email ran minutes ago, the sign-in is over
        old = Stand({"connect_email": _t.monotonic() - 400})
        asyncio.run(say(old, "We've asked Google to send a text code."))
        assert spoke and agent.ASKED_NOTHING in spoke[0], spoke
        assert any("connect_email" in f for f in facts), facts
        spoke.clear()
        asyncio.run(say(old, "We've asked Google to send it again."))
        assert not spoke, "it apologised twice"
        # a tool really asked, just now: leave it alone
        asyncio.run(say(Stand({"try_another_way": _t.monotonic()}),
                        "We've asked Google to send a text code."))
        assert not spoke, "it apologised for something that was asked for"
        # a sign-in is still running: leave it alone
        asyncio.run(say(Stand({}, live=True),
                        "I've asked Google to send a code."))
        assert not spoke, "it apologised while a sign-in was running"
    finally:
        agent.log_turn = real_log
    src = io.open("agent.py", encoding="utf-8").read()
    i = src.index("async def leave_note_for_office(")
    assert 'note_tool_ran(self, "leave_note_for_office")' in \
        src[i:i + 800], "a note to the office is not counted as asking"


@check("a price no page gave is caught on the call")
def _():
    """Call 83: the search found CarMax at $13,599 and Car and Driver at
    $38,935. Six seconds later the caller heard a Kia Carnival at $34,500,
    a Pacifica at $37,000 and an Odyssey at $35,000 - on no page at all -
    and then the real answer."""
    import asyncio
    found = ("Cheapest price shown is CarMax at $13,599 for a Kia Sedona. "
             "The others I found are Car and Driver at $38,935 for a 2026 "
             "Kia Carnival, and AutoFinder at $40,100 for a Ram ProMaster "
             "City.")
    made_up = ("1. A Kia Carnival, lower-range model, around $34,500 at a "
               "dealer near the New York area. 2. A Chrysler Pacifica, base "
               "model, starting at about $37,000. 3. A Honda Odyssey, "
               "entry-level trim, around $35,000.")
    known = agent.amounts_in(found, bare=True)
    assert agent.invented_amounts(made_up, known) == {34500, 37000, 35000}
    for fine in ("the lowest price is at CarMax for a Kia Sedona at "
                 "$13,599, and a 2026 Kia Carnival at $38,935.",
                 "That is about $39,000.",
                 "With $40,100 and $13,599 together, that is $53,699."):
        assert not agent.invented_amounts(fine, known), fine

    spoke, facts = [], []

    class Out:
        role = None
        text_content = None

        def __init__(self, output):
            self.output = output

    class Ctx:
        def __init__(self, items):
            self.items = items

        def copy(self):
            return self

        def add_message(self, role, content):
            facts.append(content)

    class Fake:
        llm = tts = None

        def generate_reply(self, **k):
            spoke.append(k.get("instructions", ""))

    class Stand:
        instructions = "You are a phone assistant."

        def __init__(self, known=(), items=()):
            self.known_amounts = set(known)
            self.chat_ctx = Ctx(list(items))

        async def update_chat_ctx(self, ctx, **k):
            return None

    async def say(obj, words):
        agent.check_the_prices(Fake(), obj, words, 1)
        await asyncio.sleep(0.05)

    real_log = agent.log_turn

    async def no_log(*a, **k):
        return None
    agent.log_turn = no_log
    try:
        obj = Stand(known)
        asyncio.run(say(obj, made_up))
        assert spoke and agent.MADE_UP_PRICE in spoke[0], spoke
        assert any("$34,500" in f for f in facts), facts
        spoke.clear()
        asyncio.run(say(Stand(known), "CarMax has a Kia Sedona at $13,599."))
        assert not spoke, "a real price was called made up"
        # a price in a tool result already in its record counts
        asyncio.run(say(Stand((), [Out("Total: $212.47 with delivery")]),
                        "The total is $212.47."))
        assert not spoke, "a price from a tool result was called made up"
    finally:
        agent.log_turn = real_log
    src = io.open("agent.py", encoding="utf-8").read()
    assert "note_amounts(self, result)" in src, \
        "tool results are not read for prices"
    assert "note_amounts(agent_obj, text)" in src, \
        "a price the caller says is not counted as real"
    assert "check_the_prices(session, agent_obj, text, call_id)" in src, \
        "nothing checks what is said"


@check("'don't come back until you find it' - one search, site after site")
def _():
    """Call 83: "don't come back to me till you find a car that has all the
    features" - and it came back after every blocked site to ask whether
    to try another. Here: two blocked sites, one without leather, then one
    with everything. The caller hears 'still looking' and the find -
    never a question in between, never a failure."""
    import asyncio
    import types
    b = io.open("browser.py", encoding="utf-8").read()
    assert '"met":true' in b and 'reason="met" if act.get("met") is True' \
        in b, "a browse job does not say whether it found everything"

    script = {
        "TrueCar": {"state": "failed", "reason": "bot_check",
                    "message": "press and hold"},
        "Edmunds": {"state": "failed", "reason": "ip_blocked",
                    "message": "403"},
        "CarGurus": {"state": "done", "reason": "not_met",
                     "message": "A 2019 Honda Odyssey at $24,990. Leather "
                                "is not listed."},
        "Carvana": {"state": "done", "reason": "met",
                    "message": "A 2020 Kia Sedona EX at $21,590 with 8 "
                               "seats, leather seats and a sunroof."},
    }
    jobs, cancelled, spoke, facts = {}, [], [], []

    class Resp:
        def __init__(self, d):
            self.d = d

        def json(self):
            return self.d

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, params=None, **k):
            if url.endswith("/jobs/browse"):
                jid = len(jobs) + 1
                jobs[jid] = params["site"]
                return Resp({"job_id": jid})
            if url.endswith("/jobs/cancel"):
                cancelled.append(params["job_id"])
            return Resp({})

    async def status(path, **params):
        site = jobs[params["job_id"]]
        return dict(script.get(site) or {"state": "working"}, kind="browse")

    class Sess:
        llm = tts = None

        def generate_reply(self, **k):
            spoke.append(k.get("instructions", ""))

    class Ctx:
        items = []

        def copy(self):
            return self

        def add_message(self, role, content):
            facts.append(content)

    class Stand:
        account_id = call_id = 1
        session = Sess()
        chat_ctx = Ctx()
        job_live = True

        async def update_chat_ctx(self, ctx, **k):
            return None

        async def _record_outcome(self, d):
            facts.append("recorded " + d.get("reason", ""))

    for name in ("_hunt", "_hunt_report", "_cancel_job"):
        setattr(Stand, name, getattr(agent.Assistant, name))

    async def fast(*a, **k):
        await real_sleep(0)
    real_sleep = asyncio.sleep

    async def no_log(*a, **k):
        return None
    saved = (agent.httpx, agent.backend_get, agent.log_turn, agent.asyncio)
    agent.httpx = types.SimpleNamespace(AsyncClient=Client)
    agent.backend_get = status
    agent.log_turn = no_log
    agent.asyncio = types.SimpleNamespace(
        sleep=fast, CancelledError=asyncio.CancelledError,
        create_task=asyncio.create_task)
    try:
        obj = Stand()
        asyncio.run(obj._hunt("minivan, 8 seats, leather, sunroof",
                              ["TrueCar", "Edmunds", "CarGurus", "Carvana",
                               "CarMax"]))
        assert list(jobs.values()) == ["TrueCar", "Edmunds", "CarGurus",
                                       "Carvana"], jobs
        heard = " | ".join(spoke)
        assert "21,590" in spoke[-1], f"the find was not told: {heard}"
        for wrong in ("another", "Would you like", "human check",
                      "blocking", "Shall I"):
            assert wrong not in " ".join(spoke[:-1]), \
                f"it came back to ask before it was done: {heard}"
        assert obj.job_live is False, "still marked busy after it ended"

        # nothing anywhere: one report, from what each site really did
        jobs.clear()
        spoke.clear()
        facts.clear()
        script["Carvana"] = dict(script["CarGurus"])
        asyncio.run(Stand()._hunt("minivan", ["TrueCar", "Edmunds",
                                              "Carvana"]))
        last = spoke[-1]
        assert "TrueCar and Edmunds would not let us in" in last, last
        assert "Carvana had nothing with all of it" in last, last
        assert "24,990" in last and "office" in last, last
        assert any("ended with no full match" in f for f in facts), facts

        # stopped part-way: the job running is cancelled too
        jobs.clear()
        script["Slow"] = None

        async def stop_it():
            t = asyncio.create_task(Stand()._hunt("minivan", ["Slow"]))
            for _ in range(20):
                await real_sleep(0)
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        asyncio.run(stop_it())
        assert cancelled, "stopping the search left its job running"
    finally:
        (agent.httpx, agent.backend_get, agent.log_turn,
         agent.asyncio) = saved
    src = io.open("agent.py", encoding="utf-8").read()
    i = src.index("async def stop_that(")
    assert "hunt.cancel()" in src[i:i + 900], "stop_that leaves it running"
    i = src.index("def _gone(p):")
    assert "hunt.cancel()" in src[i:i + 500], \
        "it keeps searching after the caller hangs up"


@check("nothing from an earlier call starts before the caller asks")
def _():
    """Call 84: two seconds after the PIN, before he had said a word, the
    car search from the night before started on its own. "Who asked you
    to check the next car website now?" """
    a = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                        "+1555", 1)
    a.verified = True
    assert a.heard_request is False
    said = a._not_asked_yet()
    assert said and "has not asked for anything yet" in said, said
    a.heard_request = True
    assert a._not_asked_yet() is None
    src = io.open("agent.py", encoding="utf-8").read()
    for name in ("do_on_website", "find_best_price", "sign_in_to_site",
                 "reset_site_password", "check_site_orders", "search_site"):
        i = src.index(f"    async def {name}(")
        body = src[i:src.index("    @function_tool", i + 10)]
        assert "self._not_asked_yet()" in body, \
            f"{name} can start before the caller has asked for anything"
    i = src.index("agent_obj.heard_request = True")
    assert "stored != BLANKED" in src[i - 200:i], \
        "the PIN being said would count as asking for something"
    assert "Never carry" in a.instructions


@check("a stopped job stays stopped")
def _():
    """Call 84: the search was stopped, and twenty seconds later the same
    job carried on clicking and wrote "done" over the top."""
    import browser
    wrote = []
    real = browser.Session

    class NoDb:
        def __init__(self):
            wrote.append("db")

    browser._JOBS[987654] = {"cancelled": True}
    browser.Session = NoDb
    try:
        browser._job_set(987654, "done", "The best options are...")
        assert not wrote, "a stopped job was written over"
    finally:
        browser.Session = real
        browser._JOBS.pop(987654, None)
    logged = []
    ok, text = browser._replay_recipe(
        None, [{"action": "click", "desc": "ok"}], {}, logged.append,
        stopped=lambda: True)
    assert ok is False and not text, "saved steps ran after the stop"
    src = io.open("browser.py", encoding="utf-8").read()
    assert 'stopped=lambda: (_JOBS.get(jid) or {}).get("cancelled")' in src


@check("Google's 'choose a way' screen picks what works instead of giving up")
def _():
    """Call 84: Google offered "Tap Yes on your phone or tablet" and "Use
    your phone or tablet to get a security code". Neither was on our list,
    so the sign-in died - though tapping Yes had worked a minute before."""
    src = io.open("browser.py", encoding="utf-8").read()
    i = src.index("def pick_from_selection(page):")
    body = src[i:i + 2200]
    for want in ("Tap Yes on your phone", "get a security code"):
        assert want in body, f"'{want}' is still not something it can pick"
    assert body.index("text=/Phone call/i") < \
        body.index("text=/Tap Yes on your phone/i"), \
        "a text or a call should still come first"
    assert 'reason="no_method"' in src
    assert 'reason="tap_timeout"' in src
    i = src.index("def describe_code_screen(page):")
    assert "security code that their own phone" in src[i:i + 2600]
    # the number Google shows can change - and it is said when it does
    j = src.index("now_num = tap_number(page)")
    loop = src[j - 900:j + 700]
    assert "seen_num" in loop and "'now ' if changed" in loop, \
        "a new number on the screen is never told to the caller"
    assert "if not num:\n                            num = tap_number" \
        not in src, "the number is still only read once"


@check("a sign-in that got past the password is tried again without it")
def _():
    """Call 84: Google accepted the password; the next step failed. He
    said "try again" - refused. "Start over" - asked to spell the password
    a third time, and the sign-in never restarted, though he was told "we
    did try again"."""
    import asyncio
    a = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                        "+1555", 1)
    a.verified = True
    a.heard_request = True
    a.onboard_sid = None
    a.login_ok = {"email": "someone@gmail.com", "password": "Secret12345"}
    a.signin_tries = 1
    said = a._signin_failed({
        "state": "failed", "reason": "no_method",
        "message": "Google offered no verification method we can use. "
                   "url=https://accounts.google.com/x | screen: Verify"})
    assert "will not need to say it again" in said, said
    assert "password left empty" in said, said
    assert "url=" not in said and "screen:" not in said, said
    assert "left a note" not in said, said
    a.login_ok = None
    said = a._signin_failed({"state": "failed", "reason": "tap_timeout",
                             "message": "They never approved the prompt."})
    assert "do not leave another" in said, said

    started, real_post, real_log = [], agent.backend_post, agent.log_turn

    async def fake_post(path, payload, params=None):
        started.append((path, dict(payload)))
        return {"session_id": 5}

    async def no_log(*x, **k):
        return None
    agent.backend_post, agent.log_turn = fake_post, no_log
    a._start_watch = lambda *x, **k: None
    tool = agent.Assistant.connect_email
    try:
        # a password Google accepted is used again, with no spelling back
        a.login_ok = {"email": "someone@gmail.com", "password": "Secret12345"}
        a.password_confirmed = False
        out = asyncio.run(tool(a, None, "Someone@gmail.com", ""))
        assert started and started[-1][1].get("password") == \
            "Secret12345", (out, [(x, sorted(y)) for x, y in started])
        assert "Sign-in started" in out, out
        # no accepted password: it must be asked for, nothing starts
        started.clear()
        a.login_ok = None
        out = asyncio.run(tool(a, None, "someone@gmail.com", ""))
        assert not started and "Ask them for it" in out, out
        # a first password is still spelled back, and says nothing started
        a.password_confirmed = False      # as after any failed sign-in
        out = asyncio.run(tool(a, None, "someone@gmail.com", "Abc12"))
        assert not started and "NOTHING HAS STARTED YET" in out, out
    finally:
        agent.backend_post, agent.log_turn = real_post, real_log
    src = io.open("agent.py", encoding="utf-8").read()
    i = src.index("async def try_another_way(")
    body = src[i:i + 2000]
    assert "text a code instead" not in body, \
        "it still promises a text that Google may never send"
    assert "popped up again" in body, \
        "nothing says a repeated prompt is not a reason to switch"
    i = src.index("async def _connect_state(")
    assert "left a note for the office and someone will call" not in \
        src[i:i + 3000], "the status tool still ends the sign-in for good"


@check("a note really left for the office is not called a false claim")
def _():
    """Call 84: "I've sent a note to the office" - true, the note was in
    the record - and the call was marked as having made it up, twice: on
    the call and in the review afterwards."""
    import asyncio
    spoke = []

    class Fake:
        llm = tts = None

        def generate_reply(self, **k):
            spoke.append(k)

    class Ctx:
        items = []

        def copy(self):
            return self

        def add_message(self, **k):
            pass

    class Stand:
        def __init__(self):
            self.ran_tools = {"leave_note_for_office"}
            self.chat_ctx = Ctx()

        async def update_chat_ctx(self, ctx, **k):
            return None

    async def go():
        agent.check_the_claim(Fake(), Stand(),
                              "I've sent a note to the office.", 1)
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert not spoke, "it apologised for a note it really did leave"
    import advisor
    src = io.open("advisor.py", encoding="utf-8").read()
    i = src.index("def review_call(")
    body = src[i:i + 3000]
    assert "query(Followup)" in body, "the reviewer cannot see notes left"
    assert 'if t.who == "tool"' in body, "the reviewer cannot see tools run"


@check("fixed words have a voice of their own")
def _():
    """Call 84: "Are you still there?" came out as "Remember, if you're
    ready to continue..." and, a minute later, as a whole earlier answer
    again. Words handed to the model are a suggestion; say() with a voice
    of its own says exactly them."""
    src = io.open("agent.py", encoding="utf-8").read()
    i = src.index("session = AgentSession(")
    assert "tts=fixed_voice" in src[i:i + 400], \
        "the session has no voice for fixed words"
    j = src.index("fixed_voice = openai.TTS(")
    assert j < i and "except Exception" in src[j:i], \
        "a voice that fails to load must not stop the call being answered"
    from livekit.plugins import openai as lk_openai
    lk_openai.TTS(model=agent.FIXED_WORDS_MODEL, voice=agent.REALTIME_VOICE,
                  api_key="x")


@check("a question is answered from pages, with the site named - no browser")
def _():
    """Calls 42-62: one fridge question, twenty browser jobs, a human
    check on Frigidaire's own site, and four different sets of buttons.
    A question is read from the search engine's copy of the pages; the
    answer says which site, and carries a freshness code."""
    import find
    pages = [{"title": "Shabbat Mode - Frigidaire Support",
              "url": "https://www.frigidaire.com/support/x", "site":
              "frigidaire.com", "text": "To turn on Sabbath mode press "
              "and hold the Lock button for 5 seconds. Updated Mar 3, "
              "2026.", "date": ""},
             {"title": "fridge tips", "url": "https://forum.example/y",
              "site": "forum.example", "text": "my cousin says hold the "
              "plus button", "date": ""}]
    asked = []

    def fake_pages(q):
        asked.append(q)
        return pages

    def fake_chat(messages, model="", **k):
        assert "frigidaire.com" in messages[-1]["content"]
        return {"choices": [{"message": {"content":
                '{"answer": "Frigidaire\'s own site says to press and hold '
                'the Lock button for five seconds.", "found": true, '
                '"used": [1]}'}}]}
    undo = everywhere("_pages_for", fake_pages)
    undo2 = everywhere("_openai_chat", fake_chat)
    undo3 = everywhere("OPENAI_API_KEY", "x")
    try:
        out = find.find_out("how do I turn on Sabbath mode on the "
                            "Frigidaire PRDF1922AF", 1, 1)
    finally:
        undo()
        undo2()
        undo3()
    assert asked, "no pages were looked for"
    assert out["found"] is True and out["how"] == "search", out
    assert out["sources"] and out["sources"][0]["site"] == "frigidaire.com"
    assert out["freshness"] == "dated" and out["as_of"] == "Mar 3, 2026", out
    assert "took_ms" in out
    assert "frigidaire" in out["answer"].lower()
    src = io.open("find.py", encoding="utf-8").read()
    for word in ("sync_playwright", "connect_over_cdp", "page.goto"):
        assert word not in src, f"find.py drives a browser: {word}"
    # questions that need a page behind them, and ones that don't
    for q in ("how much is the Epson ET-5850 at Best Buy",
              "PRDF1922AF shabbos mode", "cheapest ecco new jersey shoes",
              "what time does Costco in Brooklyn open",
              "which minivans have leather seats and a sunroof"):
        assert find.needs_source(q), f"would answer from memory: {q}"
    for q in ("how do you make a cup of tea", "what is a sukkah",
              "how many ounces in a pound"):
        assert not find.needs_source(q), f"would search for: {q}"


@check("a question with nothing behind it says so, and is never filled in")
def _():
    """The pages did not answer. found is false, the answer says what
    they do cover, and nothing is made up to fill the gap."""
    import find

    def fake_chat(messages, model="", **k):
        return {"choices": [{"message": {"content":
                '{"answer": "The pages cover the dishwasher, not the '
                'fridge.", "found": false, "used": []}'}}]}
    undo = everywhere("_pages_for", lambda q: [
        {"title": "t", "url": "https://a.com/x", "site": "a.com",
         "text": "dishwasher cycle guide", "date": ""}])
    undo2 = everywhere("_openai_chat", fake_chat)
    undo3 = everywhere("OPENAI_API_KEY", "x")
    try:
        out = find.find_out("PRDF1922AF ice maker button", 1, 1)
        none = find.find_out("PRDF1922AF ice maker button", 1, 1)
    finally:
        undo()
        undo2()
        undo3()
    assert out["found"] is False and out["sources"] == [], out
    assert out["freshness"] == "indexed"
    undo = everywhere("_pages_for", lambda q: [])
    undo3 = everywhere("OPENAI_API_KEY", "x")
    try:
        none = find.find_out("PRDF1922AF ice maker button", 1, 1)
    finally:
        undo()
        undo3()
    assert none["found"] is False and none["reason"] == "no_pages", none
    assert none["freshness"] == "none"


@check("a blocked subject is refused before any page is looked for")
def _():
    import find

    def boom(q):
        raise AssertionError("a search ran for a blocked subject")
    undo = everywhere("_pages_for", boom)
    undo2 = everywhere("_openai_chat", boom)
    undo3 = everywhere("OPENAI_API_KEY", "x")
    try:
        out = find.find_out("what are today's sports scores", 1, 1)
    finally:
        undo()
        undo2()
        undo3()
    assert out.get("blocked") and out["answer"] == main.BLOCKED_REPLY, out
    # and pages that turn out to be about one are refused too
    undo = everywhere("_pages_for", lambda q: [
        {"title": "latest news and politics", "url": "https://n.com/x",
         "site": "n.com", "text": "election news sports gossip", "date": ""}])
    undo3 = everywhere("OPENAI_API_KEY", "x")
    try:
        out = find.find_out("PRDF1922AF latest", 1, 1)
    finally:
        undo()
        undo3()
    assert out.get("blocked"), out


@check("/find is on the backend, needs the token, and jobs show their goal")
def _():
    from fastapi.testclient import TestClient
    c = TestClient(main.app, raise_server_exceptions=False, base_url="https://t")
    if getattr(main, "SERVICE_TOKEN", ""):
        assert c.get("/find", params={"q": "x"}).status_code in (401, 403)
    c.headers["Authorization"] = f"Bearer {main.SERVICE_TOKEN}" \
        if getattr(main, "SERVICE_TOKEN", "") else ""
    undo = everywhere("_pages_for", lambda q: [])
    undo3 = everywhere("OPENAI_API_KEY", "x")
    try:
        r = c.get("/find", params={"q": "PRDF1922AF manual"})
    finally:
        undo()
        undo3()
    assert r.status_code == 200 and r.json()["reason"] == "no_pages", r.text
    src = io.open("main.py", encoding="utf-8").read()
    i = src.index('@app.get("/jobs")')
    assert '"goal": goal' in src[i:i + 1200], \
        "the jobs list still hides what each job was asked to do"


@check("a shop's own live price replaces its listing, and says so")
def _():
    """B&H's listed $849.99 was the old price; the live page said $699.99
    marked down. A shop that publishes its prices gives them live, no
    wall, fully allowed. The feed's price stands in for the listing."""
    import feeds
    import search
    sample = {"products": [
        {"sku": 1, "name": "Epson EcoTank ET-5850 Printer",
         "salePrice": 699.99, "regularPrice": 849.99,
         "onlineAvailability": True, "inStoreAvailability": False,
         "url": "https://www.bestbuy.com/x", "freeShipping": True}]}
    asked = []

    def fake_get(url):
        asked.append(url)
        return sample
    undo = everywhere("_bestbuy_get", fake_get)
    undo2 = everywhere("BESTBUY_API_KEY", "k")
    try:
        got = feeds.bestbuy_prices("Epson ET-5850 printer")
    finally:
        undo()
        undo2()
    assert asked and "search=epson" in asked[0] and "et-5850" in asked[0], \
        asked
    assert "apiKey=k" in asked[0]
    o = got[0]
    assert o["shop"] == "Best Buy" and o["price"] == "$699.99" and \
        o["amount"] == 699.99 and o["live"] is True, o
    assert o["was"] == "$849.99" and "in stock online" in o["delivery"], o
    # no key: nothing asked, nothing returned
    undo = everywhere("_bestbuy_get", lambda url: 1 / 0)
    undo2 = everywhere("BESTBUY_API_KEY", "")
    try:
        assert feeds.bestbuy_prices("Epson ET-5850") == []
        assert feeds.live_sources() == []
    finally:
        undo()
        undo2()

    # the listing for Best Buy is dropped; the live price stands in
    listing = {"shopping": [
        {"source": "Best Buy", "title": "Epson EcoTank ET-5850 Printer",
         "price": "$849.99", "link": "https://bestbuy.com/x"},
        {"source": "B&H Photo", "title": "Epson EcoTank ET-5850 Printer",
         "price": "$799.99", "link": "https://bhphotovideo.com/x"}]}
    undo = everywhere("_serper_shopping", lambda item: listing)
    undo2 = everywhere("live_prices", lambda item, shop="": [
        dict(o, live=True) for o in [{
            "shop": "Best Buy", "title": "Epson EcoTank ET-5850 Printer",
            "price": "$699.99", "amount": 699.99, "was": "$849.99",
            "delivery": "in stock online", "link": ""}]])
    undo3 = everywhere("SERPER_API_KEY", "k")
    undo4 = everywhere("live_sources", lambda: ["best buy"])
    try:
        out = search.shopping_prices("Epson ET-5850 printer")
    finally:
        undo()
        undo2()
        undo3()
        undo4()
    shops = [(o["shop"], o["price"], bool(o.get("live"))) for o in out["offers"]]
    assert shops[0] == ("Best Buy", "$699.99", True), shops
    assert ("Best Buy", "$849.99", False) not in shops, \
        "the stale listing is still read out next to the live price"
    assert "live price from the shop itself" in out["answer"], out["answer"]
    assert out["live_sources"] == ["best buy"]
    # and the caller hears which is which
    src = io.open("agent.py", encoding="utf-8").read()
    i = src.index("async def find_best_price(")
    body = src[i:i + 4000]
    assert "LIVE from the shop right now" in body and "as listed" in body, \
        "a listed price and a live one sound the same to the caller"


@check("comparing two listings reads both, and a model number needs a page")
def _():
    """Call 85: two Amazon listings of the Sensi Touch 2, $166.59 and
    $209.99. The difference was answered from memory ("might be packaging
    or retailer bundles"), then from ONE page ("no real difference"), and
    the $43 gap was never explained."""
    import find
    for q in ("What are the key differences between the Sensi Touch 2 "
              "Smart Thermostat and the Sensi Touch 2 Smart Thermostat "
              "ST76W2?",
              "Sensi ST76W2 vs ST55",
              "compare the Ninja AF101 and AF161"):
        assert find.needs_source(q), f"would answer from memory: {q}"
    for q in ("how do you make a cup of tea", "what is a sukkah",
              "what does a thermostat do"):
        assert not find.needs_source(q), f"would search for: {q}"
    src = io.open("browser.py", encoding="utf-8").read()
    i = src.index("BROWSE_SYSTEM = ")
    body = src[i:i + 6000]
    assert "read BOTH" in body and "met is false until both" in body, \
        "a comparison can still be answered from one page"
    for what in ("seller", "used/renewed", "how many in the box"):
        assert what in body, f"nothing says to look at {what}"


@check("every job a tool starts leaves a line in the call record")
def _():
    """Call 85: two jobs ran on Amazon and the call record showed neither -
    only find_out and end_call. Nobody reading the call could tell what
    the system had done."""
    src = io.open("agent.py", encoding="utf-8").read()
    for name in ("search_site", "do_on_website", "find_best_price",
                 "check_site_orders", "sign_in_to_site", "find_out",
                 "confirm_order", "connect_email", "review_checkout"):
        i = src.index(f"    async def {name}(")
        body = src[i:src.index("    @function_tool", i + 10)]
        assert "log_turn(" in body, f"{name} leaves no line in the record"
    i = src.index("    async def search_site(")
    body = src[i:src.index("    @function_tool", i + 10)]
    assert "NOT sign in to their account and buys nothing" in body, \
        "a caller asking 'did you go into my account' has no clear answer"
    assert "real total from" in body


@check("a real price with cents is never called made up")
def _():
    """Call 86: Amazon's search said "$17.59" and "$9.99". The guard kept
    only 17 and 9, called the real prices made up twice, and the caller -
    who had just heard them - said "I actually do see the prices"."""
    import asyncio
    found = ("The best matches for Munbyn 4x6 packaging stickers include: "
             "MUNBYN Thermal Direct Shipping Labels (Pack of 500) for $17.59, "
             "originally $26.38. MUNBYN 4x6 Thermal Printer Labels (220 "
             "Sheets) for $9.99, originally $16.99.")
    said = ("I found two options. One is a pack of 500 MUNBYN Thermal Direct "
            "Shipping Labels for $17.59, and another is 220 sheets of MUNBYN "
            "4x6 Thermal Printer Labels for $9.99.")
    known = agent.amounts_in(found, bare=True)
    assert {17.59, 9.99, 26.38, 16.99} <= known, sorted(known)
    assert agent.invented_amounts(said, known) == set(), \
        "real prices with cents are still called made up"
    assert agent.invented_amounts("$1,299.95 and $0.99", agent.amounts_in(
        "1,299.95 then 0.99", bare=True)) == set()
    # and a really invented one is still caught
    assert agent.invented_amounts("about $12.49", known) == {12.49}

    # the whole path, as it ran: the job's message noted, then said
    spoke = []

    class Fake:
        llm = tts = None

        def generate_reply(self, **k):
            spoke.append(k)

    class Ctx:
        items = []

        def copy(self):
            return self

        def add_message(self, **k):
            pass

    class Stand:
        instructions = ""
        chat_ctx = Ctx()

        async def update_chat_ctx(self, ctx, **k):
            return None
    obj = Stand()
    agent.note_amounts(obj, found)

    async def go():
        agent.check_the_prices(Fake(), obj, said, 1)
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert not spoke, "it apologised for prices Amazon really gave"


@check("'stop that' about something already finished stops nothing")
def _():
    """Call 86: "I've stopped that search" - about a search that had
    finished a minute before. job_id stays set after a job ends."""
    src = io.open("agent.py", encoding="utf-8").read()
    i = src.index("async def stop_that(")
    body = src[i:i + 2500]
    j = body.index('jid = getattr(self, "job_id", None)')
    k = body.index("/jobs/cancel")
    assert '"job_live"' in body[j:k], \
        "a finished job can still be 'stopped' and announced as stopped"
    assert "not say you stopped anything" in body[j:k], \
        "nothing tells it not to announce a stop that never happened"
    i = src.index("async def find_out(")
    doc = src[i:i + 1200]
    assert "search_site that shop" in doc, \
        "what one shop has now can still go to a web search"


@check("an order goes in the basket, the shop's checkout is read back, "
       "and only that total can be bought")
def _():
    """Call 87: "why are you jumping to shipping? The cart comes first."
    The order asked for our saved address, and a yes would have added it
    to the cart AND bought it in one go, stopping only 20% over - he would
    never have heard Amazon's real total. Now: basket, read back, yes to
    THAT total, and nothing else may be bought."""
    import browser
    src = io.open("browser.py", encoding="utf-8").read()
    # the first half can never buy
    m = io.open("main.py", encoding="utf-8").read()
    i = m.index('def order_prepare(')
    body = m[i:m.index("\n@app.", i)]
    assert '"may_buy": False' in body and '"order_id": order_id' in body, \
        "the basket job could press the button that buys"
    assert "PREPARE_GOAL" in body
    for need in ("same option", "Add to Cart", "may NOT press it",
                 '"total"', "anything else is in the basket"):
        assert need in browser.PREPARE_GOAL, f"PREPARE_GOAL lacks: {need}"
    # confirm only a total that was read back
    i = m.index('def order_confirm(')
    body = m[i:m.index("\n@app.", i)]
    assert 'row.state != "ready"' in body and "row.final_total" in body, \
        "an order can still be bought without its real total read back"
    # the runner: a done basket makes the order ready at that total
    i = src.index("def _run_browse(")
    run = src[i:src.index("\ndef ", i + 10)]
    assert 'payload.get("order_id")' in run and "_basket_matches(" in run
    # and placing it stops if the total is not the one they said yes to
    i = src.index("def _run_checkout(")
    co = src[i:src.index("\ndef ", i + 10)]
    assert 'reason="total_changed"' in co and "approved" in co, \
        "the order can be placed at a total nobody agreed to"
    assert co.index("total_changed") < co.index('"Placing now."'), \
        "the total is compared after the button is pressed"
    assert "already_in_basket" in co and "already_in_basket" in \
        browser.CHECKOUT_SYSTEM, "the second half would add it again"
    assert browser._money_of("$35.97") == 35.97
    assert browser._money_of("1,299.00 USD") == 1299.0
    assert browser._money_of("") == 0.0
    # the assistant: basket first for a shop they have a login on
    a = io.open("agent.py", encoding="utf-8").read()
    i = a.index("async def draft_order(")
    body = a[i:a.index("    @function_tool", i)]
    assert "has_login" in body and "_prepare_order(site)" in body
    assert body.index("_prepare_order(site)") < body.index("no address yet"), \
        "it still asks for an address before trying the shop's own"
    assert "log_turn(" in body, "a written-down order leaves no line"
    i = a.index("async def _prepare_order(")
    body = a[i:a.index("    @function_tool", i)]
    assert "Should I place this order?" in body and \
        "Nothing has been bought" in body
    i = a.index("async def confirm_order(")
    body = a[i:a.index("    @function_tool", i)]
    assert "NOT placed - nothing was bought" in body, \
        "a refused confirm could still be heard as placed"


@check("a saved recipe never clicks the last search's product")
def _():
    """Call 87: looking up labels, the saved Amazon recipe clicked "Sensi
    Touch 2 Smart Thermostat" - the product from the search before."""
    import browser
    steps = [{"action": "type", "desc": "input: Search Amazon",
              "text": "TASK_SUBJECT"},
             {"action": "click", "desc": "a: Sensi Touch 2 Smart Thermostat "
                                         "Wi-Fi, Alexa, Energy Star"},
             {"action": "click", "desc": "button: Add to Cart"}]
    kept = browser._generic_steps(
        "find the difference between the Sensi Touch 2 Smart Thermostat "
        "and the ST76W2", steps)
    assert kept == steps[:1], kept
    plain = [{"action": "click", "desc": "a: Hello, sign in"},
             {"action": "type", "desc": "input: email", "text": "x"}]
    assert browser._generic_steps("sign in to amazon", plain) == plain


@check("a website job says 'a minute' once, not four times")
def _():
    """Call 87: "this might take up to a minute", "I'm still checking",
    "still working on it", "still checking, thanks for your patience" -
    four in ninety seconds. "Stay with them" invited every one."""
    src = io.open("agent.py", encoding="utf-8").read()
    i = src.index("    async def do_on_website(")
    body = src[i:src.index("    @function_tool", i + 10)]
    assert "stay with" not in body, "it is still told to keep talking"
    assert "ONCE" in body and "say nothing about it" in body


@check("one order, one basket: a second yes does not add it twice")
def _():
    """Call 89: "yes" twice wrote two orders and ran two basket jobs. The
    labels went into the Amazon cart twice, and the first job's finished
    checkout ($39.16) was never heard - its watcher was replaced."""
    import asyncio
    a = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                        "+1555", 1)
    a.verified = True
    a.heard_request = True
    a.order_id = 3
    a.order_item = "1 x MUNBYN labels"
    posted = []
    real_get, real_post = agent.backend_get, agent.backend_post

    async def fake_get(path, **params):
        return {"state": "preparing"} if path == "/orders/status" else []

    async def fake_post(path, payload, params=None):
        posted.append(path)
        return {"order_id": 4}
    agent.backend_get, agent.backend_post = fake_get, fake_post
    try:
        out = asyncio.run(agent.Assistant.draft_order(
            a, None, "amazon", "MUNBYN labels 4-pack", 1, "35.97"))
    finally:
        agent.backend_get, agent.backend_post = real_get, real_post
    assert not posted, "a second order was written while one was open"
    assert "ALREADY open" in out and "cancel_order first" in out, out
    m = io.open("main.py", encoding="utf-8").read()
    i = m.index("def order_prepare(")
    body = m[i:m.index("\n@app.", i)]
    assert '"preparing", "placing"' in body and "already being put" in body, \
        "two basket jobs can run for the same shop"
    assert 'j.state not in ("done", "failed")' in body, \
        "a basket job that failed blocks every later order on that shop"
    # and a call's own order whose job has ended is not 'open'
    a.order_id = 4
    posted.clear()

    async def ended_get(path, **params):
        return ({"state": "preparing", "job_state": "failed"}
                if path == "/orders/status" else [])
    agent.backend_get, agent.backend_post = ended_get, fake_post
    try:
        asyncio.run(agent.Assistant.draft_order(
            a, None, "amazon", "MUNBYN labels 4-pack", 1, "35.97"))
    finally:
        agent.backend_get, agent.backend_post = real_get, real_post
    assert "/orders/draft" in posted, \
        "an order whose basket job failed still blocks a new one"


@check("a site's question reaches the caller as a question, and the answer "
       "reaches the site")
def _():
    """Call 89: Amazon asked which address. It came out as "still working
    on it" for four minutes; the job gave up; then "the basement one" was
    refused because only something shaped like a code could get through,
    and he was told the site had not accepted it."""
    src = io.open("agent.py", encoding="utf-8").read()
    w = src[src.index("    async def _watch(self"):
            src.index("    def _start_watch(")]
    assert 'state in ("needs_input"' in w and "as a question" in w, \
        "a question from a job is still turned into a progress update"
    assert w.index("as a question") < w.index("Update the caller now"), \
        "the question branch must come before the update branch"
    i = src.index("async def answer_website_question(")
    body = src[i:i + 2000]
    assert "already stopped" in body and "site refused their answer" in body, \
        "a job that stopped waiting is still blamed on the site"
    assert 'd.get("ok") is False' in body, \
        "a refused answer is still reported as passed on"
    # the backend takes words for a question, digits for a code
    from fastapi.testclient import TestClient
    c = TestClient(main.app, raise_server_exceptions=False, base_url="https://t")
    c.headers["Authorization"] = f"Bearer {main.SERVICE_TOKEN}" \
        if getattr(main, "SERVICE_TOKEN", "") else ""
    db = main.Session()
    job = main.Job(account_id=1, kind="browse", site="amazon",
                   state="needs_input", payload="{}")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()
    main._JOBS[jid] = {}
    try:
        r = c.post("/jobs/code", json={"job_id": jid,
                                       "code": "the basement one, Screenshot"})
        assert r.status_code == 200 and r.json().get("ok") is True, r.text
        assert main._JOBS[jid]["code"] == "the basement one, Screenshot"
        db = main.Session()
        db.query(main.Job).filter_by(id=jid).first().state = "needs_code"
        db.commit()
        db.close()
        r = c.post("/jobs/code", json={"job_id": jid, "code": "basement"})
        assert r.json().get("ok") is False, \
            "words were typed into a code box"
    finally:
        main._JOBS.pop(jid, None)
    import browser
    assert "keep the one already selected" in browser.PREPARE_GOAL, \
        "the basket job still stops to ask which address"
    i = src.index("    async def search_site(")
    assert "INSIDE one product" in src[i:i + 900]


@check("an unfinished order is remembered on the next call")
def _():
    """Call 90: "I'd like to order the same thing I tried earlier" - "I
    only keep track during each call; once the call ends the details
    aren't stored." Two orders sat in the database, one read back at
    $39.16. Then it searched B&H, which nobody had mentioned."""
    src = io.open("agent.py", encoding="utf-8").read()
    i = src.index("async def entrypoint(")
    body = src[i:]
    assert 'backend_get("/orders"' in body and "UNFINISHED ORDERS" in body, \
        "unfinished orders are not loaded at the start of a call"
    assert body.index("UNFINISHED ORDERS") < body.index(
        "agent_obj = Assistant(account, caller, call_id, history, known)"), \
        "the orders are loaded after the assistant is built"
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    assert "Never say details are not stored" in inst.instructions
    tools = {getattr(t, "__name__", ""): t for t in inst.tools}
    assert "never your" in (tools["search_site"].__doc__ or ""), \
        "search_site may still pick a shop nobody named"
    import advisor
    assert "orders_in_progress" in advisor.call_state(1)
    assert "orders_in_progress" in advisor.ADVISOR_SYSTEM


@check("a basket already holding the item is put right, not doubled")
def _():
    """Call 90: the labels were already in the Amazon cart from the call
    before. The basket job added them again, the review page said
    quantity 3 at $113.97 for an order of one, and the caller was asked
    'Should I place this order?'."""
    import browser
    g = browser.PREPARE_GOAL
    assert "Open the basket FIRST" in g and "do not add it again" in g, \
        "the basket job still adds the item without looking"
    assert '"quantity"' in g and '"other_items"' in g
    assert browser._basket_matches.__doc__
    src = io.open("browser.py", encoding="utf-8").read()
    i = src.index("def _run_browse(")
    run = src[i:src.index("\ndef ", i + 10)]
    assert "_basket_matches(" in run, \
        "the review page is called ready whatever it shows"
    m = io.open("main.py", encoding="utf-8").read()
    i = m.index("def order_prepare(")
    assert '"check"' in m[i:i + 900], "a 'check' order can never be re-run"
    a = io.open("agent.py", encoding="utf-8").read()
    i = a.index("async def _prepare_order(")
    body = a[i:a.index("    @function_tool", i)]
    assert '== "check"' in body and "Do NOT ask to place it" in body, \
        "a basket that does not match is still offered for a yes"
    # the state decision itself
    real = browser.Session

    class Row:
        quantity = 1

    class Q:
        def __init__(self, *a):
            pass

        def filter_by(self, **k):
            return self

        def first(self):
            return Row()

    class Db:
        def query(self, *a):
            return Q()

        def close(self):
            pass
    browser.Session = Db
    try:
        assert browser._basket_matches(1, {"quantity": 3}) == (
            "check", "The basket has 3 of this item; they asked for 1.")
        assert browser._basket_matches(1, {"quantity": 1,
                                           "other_items": True})[0] == "check"
        assert browser._basket_matches(1, {"quantity": "1"}) == ("ready", "")
        assert browser._basket_matches(1, {})[0] == "ready"
    finally:
        browser.Session = real


@check("an order waiting for a yes does not count as work in progress")
def _():
    """Call 90: "Still with you. This one is slow" five seconds after the
    checkout had been read back - and later, with the hold lines used up,
    108 seconds of dead air and "Are you still there?" while a job ran."""
    src = io.open("agent.py", encoding="utf-8").read()
    wd = src[src.index("async def watchdog("):
             src.index("async def hangup_when_asked(")]
    i = wd.index("busy = bool(")
    assert '"order_id"' not in wd[i:i + 200], \
        "an open order keeps the watchdog 'busy' for the rest of the call"


@check("a basket job starts in the basket")
def _():
    """Call 90: "change the quantity from 3 to 1" went to Orders, Awaiting
    delivery, Buy it again - two minutes, never the cart."""
    import browser
    assert browser.CART_PAGES["amazon"].endswith("/gp/cart/view.html")
    assert browser.CART_GOAL.search("change the quantity in the cart")
    assert browser.CART_GOAL.search("remove the extra labels from my basket")
    assert not browser.CART_GOAL.search("list the options for this item")
    src = io.open("browser.py", encoding="utf-8").read()
    i = src.index("def _run_browse(")
    run = src[i:src.index("\ndef ", i + 10)]
    assert "CART_PAGES.get(site" in run and "CART_GOAL.search(goal)" in \
        run[run.index("CART_PAGES.get(site") - 200:run.index(
            "CART_PAGES.get(site")], \
        "a cart goal still starts on the home page"


@check("the address and the card are chosen before the yes")
def _():
    """David: "I have a lot of addresses and credit cards saved, and we
    still need to choose one before we confirm." The basket job kept
    whichever the shop had selected and went straight to 'Should I place
    this order?'."""
    import browser
    g = browser.PREPARE_GOAL
    for need in ("NOTE", '"chosen_address"', '"chosen_card"',
                 '"addresses"', '"cards"', "OTHER saved addresses"):
        assert need in g, f"the basket job does not report: {need}"
    for col in ("ship_label", "pay_label"):
        assert hasattr(main.Order, col), f"orders cannot keep {col}"
    src = io.open("browser.py", encoding="utf-8").read()
    i = src.index("def _run_checkout(")
    co = src[i:src.index("\ndef ", i + 10)]
    assert "order.ship_label" in co and "order.pay_label" in co, \
        "the order is placed with whatever the shop had selected"
    assert "select exactly those" in browser.CHECKOUT_SYSTEM
    a = io.open("agent.py", encoding="utf-8").read()
    i = a.index("def _watch_basket(")
    body = a[i:a.index("    @function_tool", i)]
    assert "ask which they want" in body and \
        body.index("ask which they want") < body.index(
            "Should I place this order?"), \
        "it still goes straight from the read-back to the yes"
    i = a.index("async def review_checkout(")
    body = a[i:a.index("    @function_tool", i)]
    assert '"order_id": oid' in body and "_watch_basket(" in body, \
        "changing the address does not update the order's total"
    m = io.open("main.py", encoding="utf-8").read()
    i = m.index("def job_checkout(")
    body = m[i:m.index("\n@app.", i)]
    assert "order_id: int = 0" in body and 'payload["order_id"]' in body
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    assert "several addresses and cards saved" in inst.instructions
    # a change job that reads the checkout writes the choice to the order
    assert 'act.get("chosen_address")' in src and \
        '"ship_label"' in src[src.index("def _run_browse("):]


@check("a saved address they name is looked for on the shop, not argued about")
def _():
    """Call 91: more than ten addresses on Amazon, four noted - the page
    cuts the list short. "You see Screenshot?" "The addresses listed don't
    show that." "I for sure have more." "I'm not hiding anything." He hung
    up. The list is what one page showed; the shop has the rest."""
    import browser
    import advisor
    g = browser.PREPARE_GOAL
    assert "cut short" in g and "at most twelve" in g,         "the basket job still notes only the addresses on the first screen"
    assert "cut short" in main.CHECKOUT_CHANGES,         "a change still gives up on the first screen of addresses"
    src = io.open("agent.py", encoding="utf-8").read()
    i = src.index("def _watch_basket(")
    body = src[i:src.index("    @function_tool", i)]
    assert "do not argue" in body and "their words finds it" in body,         "a name not in the read-back is still argued about"
    assert "say how many" in body, "twenty cards would be read out"
    assert "Never argue" in advisor.ADVISOR_SYSTEM


@check("every call leaves a note: asked, done with its numbers, unfinished")
def _():
    """David: "each account has call notes - after each call we save what
    the user did, with links - so 'last week I asked you' always has an
    answer." Call 90 showed why: two orders sat in the records and the
    assistant said nothing was stored."""
    import advisor
    import json as _json
    db = main.Session()
    call = main.Call(account_id=1, from_number="+1555", verified=1)
    db.add(call)
    db.commit()
    cid = call.id
    for who, text, tool in (
            ("caller", "I'd like to order the labels again", ""),
            ("agent", "Putting it in your Amazon basket", ""),
            ("tool", "order 6 written down: 1 x MUNBYN labels", "draft_order"),
            ("caller", "my password is Abc12345 by the way", ""),
            ("agent", "Should I place this order?", "")):
        db.add(main.CallTurn(call_id=cid, who=who, text=text, tool=tool))
    db.add(main.Order(account_id=1, call_id=cid, site="amazon",
                      item="MUNBYN labels", quantity=1, state="ready",
                      final_total="$39.16"))
    db.commit()
    db.close()
    seen = {}

    def fake_chat(messages, model="", **k):
        seen["user"] = messages[-1]["content"]
        return {"choices": [{"message": {"content":
            '{"summary": "Wanted the MUNBYN labels again; order 6 read back '
            'at $39.16, not placed.", "asked": ["order the labels again"], '
            '"done": ["order 6 put in the Amazon basket, read back at '
            '$39.16"], "unfinished": ["order 6 not placed - they hung up"], '
            '"remember": ["password is Abc12345"]}'}}]}
    undo = everywhere("_openai_chat", fake_chat)
    undo2 = everywhere("OPENAI_API_KEY", "x")
    try:
        out = advisor.write_call_note(cid)
    finally:
        undo()
        undo2()
    assert "order 6" in seen["user"] and "$39.16" in seen["user"], \
        "the note writer is not shown the orders of the call"
    assert out["refs"] == [{"kind": "order", "id": 6}], out
    assert "Abc12345" not in _json.dumps(out), "a password reached the note"
    notes = advisor.notes_for(1, 5)
    assert notes and notes[0]["call_id"] == cid, notes
    assert notes[0]["refs"] == [{"kind": "order", "id": 6}]
    block = advisor.notes_block(1, 5)
    assert f"call {cid}" in block and "Unfinished:" in block, block
    assert "Abc12345" not in block
    # and the advisor sees every note
    state = advisor.call_state(1)
    assert state["earlier_calls"] and state["earlier_calls"][0]["call"] == cid
    assert "earlier_calls" in advisor.ADVISOR_SYSTEM
    # a text leaves a plain note, scrubbed
    advisor.write_text_note(1, "my pin is 4321, read my mail",
                            "Here are your last emails")
    top = advisor.notes_for(1, 1)[0]
    assert top["channel"] == "sms" and "4321" not in top["summary"], top
    # it runs after every call, and texts write theirs
    src = io.open("main.py", encoding="utf-8").read()
    i = src.index('@app.post("/calls/end")')
    assert "background.add_task(write_call_note, call_id)" in src[i:i + 1500]
    i = src.index("async def sms_incoming(")
    assert "write_text_note(acct.id, kept, reply)" in src[i:i + 6000]
    assert "/notes" in {r.path for r in main.app.routes}


@check("a call starts with the notes of the last five, and the office sees them")
def _():
    src = io.open("agent.py", encoding="utf-8").read()
    i = src.index("async def entrypoint(")
    body = src[i:]
    assert 'backend_get("/notes"' in body and "limit=5" in body[
        body.index('backend_get("/notes"'):body.index('backend_get("/notes"')
        + 200], "the last five notes are not loaded at the start of a call"
    assert body.index('backend_get("/notes"') < body.index(
        'backend_get("/memory"'), "raw lines still come before the notes"
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1, history="- Oct 7 (call 91): labels",
                           known="")
    assert "notes of earlier calls and texts" in inst.instructions
    assert "what_now with their words" in inst.instructions, \
        "'last week I asked you' has nowhere to go"
    from fastapi.testclient import TestClient
    c = TestClient(main.app, raise_server_exceptions=False, base_url="https://t")
    c.headers["Authorization"] = f"Bearer {main.SERVICE_TOKEN}" \
        if getattr(main, "SERVICE_TOKEN", "") else ""
    r = c.get("/notes", params={"account_id": 1, "limit": 3})
    assert r.status_code == 200 and isinstance(r.json(), list), r.text
    r = c.get("/calls", params={"limit": 5})
    assert r.status_code == 200 and all("note" in x for x in r.json()), \
        "the calls list does not carry each call's note"
    page = io.open("admin_page.py", encoding="utf-8").read()
    assert "c.note" in page, "the admin panel does not show the note"


@check("a checkout that has not come back is never read out")
def _():
    """Call 93: 45 seconds into the basket job the caller heard the item,
    "the home address saved on your Amazon account", the Visa and "$42.69
    including tax", then "Should we go ahead and place this order?". None
    of it had arrived."""
    import asyncio
    spoke, facts = [], []

    class Fake:
        llm = tts = None

        def generate_reply(self, **k):
            spoke.append(k.get("instructions", ""))

    class Ctx:
        items = []

        def copy(self):
            return self

        def add_message(self, role, content):
            facts.append(content)

    class Stand:
        job_site = "Amazon"
        chat_ctx = Ctx()

        def __init__(self, pending):
            self.basket_pending = pending
            self.owned_up_checkout = False

        async def update_chat_ctx(self, ctx, **k):
            return None

    real_log = agent.log_turn

    async def no_log(*x, **k):
        return None
    agent.log_turn = no_log

    async def say(obj, words):
        agent.check_the_checkout(Fake(), obj, words, 1)
        await asyncio.sleep(0.05)
    try:
        obj = Stand(True)
        asyncio.run(say(obj, "The item is in the basket, and here's what it "
                             "shows at checkout: the shipping address is the "
                             "home address saved on your Amazon account, and "
                             "the total, including tax, comes to $42.69."))
        assert spoke and "Amazon's checkout has not come back" in spoke[0], \
            spoke
        assert any("was invented" in f for f in facts), facts
        spoke.clear()
        asyncio.run(say(obj, "The total comes to $39.16."))
        assert not spoke, "it apologised twice for one job"
        # harmless lines while the job runs
        asyncio.run(say(Stand(True), "I'm adding it to your Amazon basket "
                                     "now - nothing is being bought yet."))
        assert not spoke, "an honest holding line was called invented"
        # once the job is back, a total is fine
        asyncio.run(say(Stand(False), "The total comes to $39.16."))
        assert not spoke
    finally:
        agent.log_turn = real_log
    src = io.open("agent.py", encoding="utf-8").read()
    i = src.index("def _watch_basket(")
    body = src[i:i + 1500]
    assert "self.basket_pending = True" in body and \
        "self.basket_pending = False" in body
    assert "check_the_checkout(session, agent_obj, text, call_id)" in src


@check("a basket job moves on with the step's own button, and stops looping sooner")
def _():
    """Call 93: on Amazon's payment page the job re-noted the cards four
    times, 'going round in circles', for three minutes, and failed with
    nothing - the 'Use this payment method' button was right there."""
    import browser
    g = browser.PREPARE_GOAL
    assert "Use this payment method" in g and "never by selecting" in g
    assert "the tax as the page" in g, "the read-back has no tax or delivery"
    src = io.open("browser.py", encoding="utf-8").read()
    i = src.index("def _run_browse(")
    run = src[i:src.index("\ndef ", i + 10)]
    k = run.index("going round in circles")
    assert "Use this payment method" in run[k:k + 700], \
        "the loop nudge does not point at the button that moves on"
    assert 'stuck >= (2 if payload.get("order_id")' in run, \
        "a basket job still loops four times before giving up"


@check("the same thing asked for again is the same order, not a second row")
def _():
    """Call 93: 'the same labels' wrote order 7 for the item of order 6."""
    from fastapi.testclient import TestClient
    c = TestClient(main.app, raise_server_exceptions=False, base_url="https://t")
    c.headers["Authorization"] = f"Bearer {main.SERVICE_TOKEN}" \
        if getattr(main, "SERVICE_TOKEN", "") else ""
    body = {"account_id": 1, "site": "Amazon", "item": "MUNBYN labels, "
            "880 pieces in 4 rolls", "quantity": 1, "expected_price": "$35.97",
            "call_id": 1}
    r1 = c.post("/orders/draft", json=body).json()
    r2 = c.post("/orders/draft", json=dict(body, item="munbyn   labels, "
                                            "880 pieces in 4 rolls",
                                            call_id=2)).json()
    assert r1["order_id"] == r2["order_id"], (r1, r2)
    r3 = c.post("/orders/draft", json=dict(body, item="Epson ink")).json()
    assert r3["order_id"] != r1["order_id"], "a different item reused an order"
    db = main.Session()
    row = db.query(main.Order).filter_by(id=r1["order_id"]).first()
    assert row.call_id == 2 and row.state == "draft" and not row.final_total
    db.close()


@check("picking an unfinished order back up reads the shop's checkout into it")
def _():
    """Call 94: "the labels from last call" - the checkout was read twice
    with no order open, so "place it now?" could not have gone anywhere;
    the first read stopped on the payment page ("items not shown") and
    still asked for a yes; and a hold line was spoken in the middle of
    the read-back."""
    src = io.open("agent.py", encoding="utf-8").read()
    i = src.index("    async def review_checkout(")
    body = src[i:src.index("    @function_tool", i + 10)]
    assert 'backend_get("/orders"' in body and "self.order_id = oid" in body, \
        "the unfinished order is not picked up"
    assert body.index('backend_get("/orders"') < body.index("/jobs/checkout"), \
        "the order is looked for after the job has started"
    assert "Do NOT ask whether to place anything" in body, \
        "a half-read checkout can still be offered for a yes"
    assert "log_turn(" in body
    w = src[src.index("    async def _watch(self"):src.index("    def _start_watch(")]
    assert w.index("self.job_live = False") < w.index("if line or said:"), \
        "the job still counts as live while its result is being read out"
    assert "draft_order with that item" in src, \
        "nothing says how to carry an unfinished order on"
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    assert "never remark on them" in inst.instructions, \
        "a sneeze still gets 'Bless you'"
    assert '"addresses"' in main.CHECKOUT_GOAL and \
        "Other addresses" in main.CHECKOUT_GOAL, \
        "a checkout read does not collect the saved choices"
    assert "met is true only on the" in main.CHECKOUT_GOAL, \
        "a payment page can still count as the checkout"


@check("a change the caller asked for is on the review page, or the order is not ready")
def _():
    """The first test by hand: 'deliver to the Screenshot address, card
    ending 9090'. The job went straight to the review page, read back
    Rodney Street and the Visa 6158, and the order was marked ready for
    a yes."""
    import browser
    ok = browser._wanted_in
    assert ok("Screenshot", "SCREENSHOT - 12 Main St, Spring Valley 10977")
    assert not ok("Screenshot", "Desktop, BASEMENT 144 RODNEY ST")
    assert ok("ending 9090", "Visa 9090") and ok("9090", "Visa ending 9090")
    assert not ok("ending 9090", "Visa 6158")
    assert ok("the Monsey one", "Monsey - 5 Elm Rd")
    assert ok("", "anything") and not ok("Monsey", "")
    st, why = browser._change_applied(
        {"deliver_to": "Screenshot", "pay_with": "ending 9090"},
        {"chosen_address": "Desktop, BASEMENT 144 RODNEY ST",
         "chosen_card": "Visa 6158"})
    assert st == "check" and "address was NOT changed" in why and \
        "card was NOT changed" in why, (st, why)
    assert browser._change_applied(
        {"deliver_to": "Screenshot", "pay_with": "9090"},
        {"chosen_address": "SCREENSHOT 12 Main", "chosen_card": "Visa 9090"}
    ) == ("ready", "")
    assert browser._change_applied({}, {}) == ("ready", "")
    src = io.open("browser.py", encoding="utf-8").read()
    i = src.index("def _run_browse(")
    run = src[i:src.index("\ndef ", i + 10)]
    assert "_change_applied(payload, act)" in run and \
        run.index("_basket_matches(") < run.index("_change_applied("), \
        "a change not made can still make an order ready"
    m = io.open("main.py", encoding="utf-8").read()
    assert '"deliver_to": deliver_to, "pay_with": pay_with' in m, \
        "the job does not know what change was asked for"
    assert "comes FIRST" in main.CHECKOUT_CHANGES and \
        "NOT changed" in main.CHECKOUT_CHANGES
    a = io.open("agent.py", encoding="utf-8").read()
    i = a.index("def _watch_basket(")
    assert "review_checkout again with" in a[i:i + 4000], \
        "a change not made has no way to be tried again"


@check("a saved address or card the caller named is picked by the system")
def _():
    """David's first test by hand, twice: the job saw 'Screenshot' in the
    list and pressed on with Rodney Street. A radio button has no words
    of its own, so nothing in the listing told the choices apart."""
    import browser
    items = [{"idx": 0, "desc": "a: Change"},
             {"idx": 1, "desc": "input/radio: Desktop BASEMENT 144 RODNEY ST"},
             {"idx": 2, "desc": "input/radio: SCREENSHOT 12 Main St Spring "
                                "Valley 10977"},
             {"idx": 3, "desc": "label: SCREENSHOT 12 Main St"},
             {"idx": 4, "desc": "button: Use this address"},
             {"idx": 5, "desc": "input/radio: Visa ending in 6158"},
             {"idx": 6, "desc": "input/radio: Visa ending in 9090"}]
    assert browser._pick_choice(items, "Screenshot")["idx"] == 2
    assert browser._pick_choice(items, "ending 9090")["idx"] == 6
    assert browser._pick_choice(items, "Monsey") is None
    assert browser._pick_choice([{"idx": 0, "desc": "a: Screenshot"}],
                                "Screenshot") is None, \
        "a link with the words would be clicked as if it were the choice"
    js = browser._SNAPSHOT_JS
    assert "el.labels" in js and "closest('label')" in js, \
        "a radio button still has no words of its own"
    src = io.open("browser.py", encoding="utf-8").read()
    i = src.index("def _run_browse(")
    run = src[i:src.index("\ndef ", i + 10)]
    assert "_pick_choice(items, wanted)" in run and "picked.add(kind)" in run
    # and the picked row's own button is pressed, never another row's
    assert "_USE_ROW_BUTTON_JS, hit[\"idx\"]" in run, \
        "the choice is selected but the old row's button gets pressed"
    js = browser._USE_ROW_BUTTON_JS
    assert "DOCUMENT_POSITION_FOLLOWING" in js and "radio.checked" in js, \
        "the button pressed may belong to a choice before the picked one"
    assert "NONE: checked=" in js, "a failure to press leaves no evidence"
    assert "deliver to this address" in js and "use this payment method" in js
    # on the review page, Change beside the section is opened by the
    # system before the job may finish (fourth run: finished without it)
    assert "_open_change_if_needed(page, payload, picked" in run and \
        run.index("_open_change_if_needed(") < run.index(
            'if a == "done":\n                    answer = act.get'), \
        "a basket job can finish without ever opening Change"
    oj = browser._OPEN_CHANGE_JS
    assert "beside" in oj and "payment" not in oj.split("change")[0].lower()
    assert browser._open_change_if_needed.__doc__
    assert run.index("_pick_choice(items, wanted)") < run.index(
        "act = _decide("), "the choice is picked after the model has acted"


@check("the checkout and the basket belong to the order flow")
def _():
    """Call 95: "repeat the Amazon checkout" went to a website job three
    times. The first added the labels again - quantity 2, $75.98; the next
    two ran with no site, signed out, and asked the caller for an Amazon
    email. Then "I only want one" was answered "it didn't reach the site"."""
    import asyncio
    for goal in ("repeat the Amazon checkout process and get the tax",
                 "open the cart and read the order total",
                 "proceed to checkout on Amazon"):
        assert agent.CHECKOUT_GOAL_WORDS.search(goal), goal
    for goal in ("list every option for the MUNBYN labels",
                 "check my Verizon bill"):
        assert not agent.CHECKOUT_GOAL_WORDS.search(goal), goal
    a = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                        "+1555", 1)
    a.verified = True
    a.heard_request = True
    out = asyncio.run(agent.Assistant.do_on_website(
        a, None, "repeat the Amazon checkout and get the tax", "", "", ""))
    assert "review_checkout" in out and "Nothing has started" in out, out
    import browser
    assert browser.ADD_TO_CART.search("button: Add to Cart")
    assert browser.ADD_TO_CART.search("add it to the basket")
    assert not browser.ADD_TO_CART.search("repeat the checkout")
    src = io.open("browser.py", encoding="utf-8").read()
    i = src.index("def _run_browse(")
    run = src[i:src.index("\ndef ", i + 10)]
    assert 'ADD_TO_CART.search(it["desc"])' in run and \
        'not payload.get("order_id")' in run[run.index(
            'ADD_TO_CART.search(it["desc"])'):run.index(
            'ADD_TO_CART.search(it["desc"])') + 200], \
        "a website job can still add things to the basket"
    i = src.index("    async def do_on_website(") if False else 0
    s2 = io.open("agent.py", encoding="utf-8").read()
    i = s2.index("    async def do_on_website(")
    body = s2[i:s2.index("    @function_tool", i + 10)]
    assert 'backend_get("/logins"' in body and "site = name" in body, \
        "a shop with a saved login is still browsed signed out"
    i = s2.index("async def answer_website_question(")
    body = s2[i:i + 2500]
    assert 'st == "done"' in body and "already finished" in body


@check("a basket job chooses the address and card from last time, and a card when none is")
def _():
    """Call 95: Amazon's payment page had no card selected; 'Use this
    payment method' did nothing until one was, and the job went stuck."""
    import browser
    assert "if NONE is" in browser.PREPARE_GOAL and \
        "select the first one listed" in browser.PREPARE_GOAL
    m = io.open("main.py", encoding="utf-8").read()
    i = m.index("def order_prepare(")
    body = m[i:m.index("\n@app.", i)]
    assert '"deliver_to": deliver_to' in body and '"pay_with": card' in body, \
        "the basket job does not carry the last chosen address and card"
    assert "row.pay_label" in body and "row.ship_label" in body


@check("every page has its folded lists opened before it is read")
def _():
    """Calls 91 and 95: four of a dozen addresses and a few of twenty cards
    were read as the whole list - the rest were behind "See more". Every
    site folds lists away; the system now opens them itself, on any site,
    before the model reads the page."""
    import re as _re
    import browser
    js = browser._UNFOLD_JS
    m = _re.search(r"const open = /(.+?)/i;", js)
    n = _re.search(r"const never = /(.+?)/i;", js)
    assert m and n, "the unfold rules are not where they should be"
    opener = _re.compile(m.group(1).replace("\\\\", "\\"), _re.I)
    never = _re.compile(n.group(1).replace("\\\\", "\\"), _re.I)
    for t in ("See more", "Show all", "View all", "Show 12 more",
              "More options", "More payment methods", "Other addresses",
              "Show details", "Expand", "See all 14"):
        assert opener.search(t) and not never.search(t), f"not opened: {t}"
    for t in ("Place your order", "Add to Cart", "Remove", "Sign out",
              "See less", "Show fewer", "Buy now"):
        assert not (opener.search(t) and not never.search(t)), \
            f"would be pressed: {t}"
    assert "stays" in js and "location.pathname" in js, \
        "a link that leaves the page could be followed"
    src = io.open("browser.py", encoding="utf-8").read()
    i = src.index("def _run_browse(")
    run = src[i:src.index("\ndef ", i + 10)]
    assert "_unfold(page, history)" in run and "unfolded" not in run,         "unfolding is still once per address, and a checkout keeps one"
    assert "paUnfolded" in js, "something opened once could be pressed again"
    assert run.index("_unfold(page, history)") < run.index(
        "items, text = _page_snapshot(page, want=goal)"), \
        "the page is read before it is unfolded"


@check("a button every row has carries its row's name")
def _():
    """Running the basket flow by itself: on Amazon's results page the
    "Add to Cart" kept in the listing was the first in the page - the
    220-sheet labels at $9.99 - and that went into the basket instead of
    the 880-piece pack. The basket job had also started on the home page,
    not in the basket it was told to open first."""
    import browser
    js = browser._SNAPSHOT_JS
    assert "add to (cart|basket|bag)|buy now|select" in js and \
        "label = label + ' | ' + nt" in js, \
        "one Add to Cart still stands for every product on the page"
    src = io.open("browser.py", encoding="utf-8").read()
    i = src.index("def _run_browse(")
    run = src[i:src.index("\ndef ", i + 10)]
    j = run.index("CART_PAGES.get(site")
    assert "order_id" not in run[j - 250:j], \
        "a basket job still starts on the home page"
    # the guards still recognise the longer labels
    assert browser.ADD_TO_CART.search("button: Add to Cart | MUNBYN 880 PCs")
    assert browser.BUY_BUTTONS.search("button: Buy Now | MUNBYN 880 PCs")


@check("a job a restart cut off is ended, and its order can be read back again")
def _():
    """Job 241 started on the old build seconds before a deploy and was
    left 'working' for ever; its order stayed 'preparing'. With one basket
    job per shop, that would have blocked every later Amazon order."""
    db = main.Session()
    j = main.Job(account_id=1, kind="browse", site="amazon", state="working")
    db.add(j)
    db.commit()
    o = main.Order(account_id=1, site="amazon", item="restart test",
                   quantity=1, state="preparing", job_id=j.id)
    db.add(o)
    db.commit()
    jid, oid = j.id, o.id
    db.close()
    assert main.end_what_a_restart_cut_off() >= 1
    db = main.Session()
    j = db.query(main.Job).filter_by(id=jid).first()
    o = db.query(main.Order).filter_by(id=oid).first()
    assert j.state == "failed" and j.reason == "restarted", (j.state, j.reason)
    assert o.state == "draft" and not o.final_total, o.state
    db.close()
    src = io.open("main.py", encoding="utf-8").read()
    i = src.index('app = FastAPI(title="Phone Assistant")')
    assert "\nend_what_a_restart_cut_off()" in src[i:i + 3000], \
        "the tidy-up is defined but never run at start-up"


@check("the office can read how texting is set up, without any secret")
def _():
    """David, with the BulkVS number panel open: 'is this what we
    configured?' Nothing on our side said what was in place."""
    from fastapi.testclient import TestClient
    c = TestClient(main.app, raise_server_exceptions=False, base_url="https://t")
    c.headers["Authorization"] = f"Bearer {main.SERVICE_TOKEN}" \
        if getattr(main, "SERVICE_TOKEN", "") else ""
    r = c.get("/sms/setup")
    assert r.status_code == 200, r.text
    d = r.json()
    for key in ("provider", "sending_number", "texts_go_out",
                "credentials_set", "incoming_webhook_must_be"):
        assert key in d, f"missing {key}: {d}"
    assert d["incoming_webhook_must_be"].endswith("/sms/incoming")
    text = r.text.lower()
    for secret in (main.BULKVS_PASS, main.TELNYX_API_KEY, main.TWILIO_TOKEN):
        assert not secret or secret.lower() not in text, "a secret leaked"
    assert "password" not in text and "token" not in text


@check("a picture sent by text is looked at and answered")
def _():
    """David, 8 Oct: 'why is it not allowed to send pictures?' - it was
    our own limit: an MMS got 'I can't open pictures yet'. The picture is
    fetched and shown to the model with the words that came with it."""
    from fastapi.testclient import TestClient
    turn = main._text_turn("what is this", "QUJD")
    assert turn["content"][1]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,QUJD")
    assert main._text_turn("hi") == {"role": "user", "content": "hi"}
    seen, sent = {}, []

    class Acct:
        id = 1

    def fake_brain(account_id, incoming, image_b64=""):
        seen["incoming"], seen["image"] = incoming, image_b64
        return "It is a receipt for $12.40 from the pharmacy."
    undo = [everywhere("_fetch_picture", lambda url: "QUJD"),
            everywhere("text_brain", fake_brain),
            everywhere("tool_send_sms",
                       lambda to, msg: sent.append((to, msg)) or {"sent": True}),
            everywhere("account_for_number", lambda n: Acct())]
    c = TestClient(main.app, raise_server_exceptions=False, base_url="https://t")
    try:
        r = c.post("/sms/incoming", json={
            "To": ["18459831774"], "From": "15550100777", "Message": "",
            "MediaURLs": ["https://media.example/pic.jpg"]})
        assert r.status_code == 200 and r.json().get("mms") is None or \
            r.json().get("ok"), r.text
        assert seen.get("image") == "QUJD", "the picture never reached the model"
        assert "picture" in seen.get("incoming", "").lower()
        assert sent and "receipt" in sent[-1][1], sent
        # a picture that cannot be fetched is said so, not blamed on rules
        sent.clear()
        undo.append(everywhere("_fetch_picture", lambda url: ""))
        r = c.post("/sms/incoming", json={
            "To": ["18459831774"], "From": "15550100777", "Message": "",
            "MediaURLs": ["https://media.example/pic.jpg"]})
        assert sent and "couldn't open that picture" in sent[-1][1], sent
    finally:
        for u in undo:
            u()
    src = io.open("main.py", encoding="utf-8").read()
    assert "can't open pictures yet" not in src
    db = main.Session()
    rows = (db.query(main.Memory).filter_by(account_id=1, channel="sms")
              .order_by(main.Memory.id.desc()).limit(2).all())
    db.close()
    assert any("(sent a picture)" in (m.text or "") for m in rows), \
        "the record does not say a picture was sent"


@check("a text is never called sent when it cannot be delivered")
def _():
    """Call 76: a new customer with no email was told twice "I've sent you
    a link". Texts do not leave this system - the number's messaging
    registration is still pending - and the provider answering 200 was
    being read as delivered."""
    import main as _m
    undo_on = everywhere("SMS_DELIVERS", False)
    undo_p = everywhere("SMS_PROVIDER", "bulkvs")
    undo_f = everywhere("SMS_FROM", "+14845182072")
    try:
        got = _m.tool_send_sms("+18455550101", "hello")
    finally:
        undo_f()
        undo_p()
        undo_on()
    assert got.get("sent") is False, \
        "a text was called sent while nothing is being delivered"
    assert "pending" in (got.get("error") or ""), got

    src = io.open("agent.py", encoding="utf-8").read()
    i = src.index("async def text_setup_link(")
    body = src[i:src.index("@function_tool", i)]
    assert "did NOT go out" in body and "Do NOT tell them you sent" in body, \
        "a failed text still comes back as something the model can gloss"
    assert "email_connect_code" in body and "connect_email" in body, \
        "nothing points at the two ways that actually work"
    inst = agent.Assistant({"account_id": 1, "name": "T"}, "+1555", 1)
    where = inst.instructions.index("CONNECTING THEIR EMAIL")
    section = inst.instructions[where:where + 700]
    assert "NEVER offer to text or email them a link" in section, \
        "it may still offer a link to somebody with no internet"


@check("an order is written down before anything else is asked for")
def _():
    """Call 78: he chose a $15.61 window kit, gave 91 Penn Street - and
    the assistant ran a fresh price search, decided the item was not in
    the listings, and the order was gone. Nothing was holding it."""
    inst = agent.Assistant({"account_id": 1, "name": "T"}, "+1555", 1)
    steps = inst.instructions[inst.instructions.index("SHOPPING AND ORDERS"):]
    steps = steps[:steps.index("CHECKING A BASKET")]
    assert "MOMENT they say yes to an item, draft_order" in steps, \
        "the order is still only written down at the end"
    assert steps.index("draft_order") < steps.index("save_address"), \
        "the address is still taken before the order exists"
    assert "Never start a new search while an order is open" in steps, steps
    assert "Never ask such a shop's address or card" in steps, \
        "it still asks for an address the shop already has (call 87)"

    src = io.open("agent.py", encoding="utf-8").read()
    i = src.index("async def draft_order(")
    body = src[i:src.index("@function_tool", i)]
    assert "Still needed" in body and "no address yet" in body, \
        "a half-finished order does not say what it is missing"
    assert "do NOT" in body and "another search" in body, \
        "nothing stops it wandering back into a search"
    assert "not chosen yet" in body, \
        "a missing address still reads as the site's saved one"


@check("nobody is cut off mid-sentence when the call runs long")
def _():
    """Call 83: fifteen minutes, cut off while it was checking another car
    site. Nothing was said at all - the goodbye was handed to a model busy
    elsewhere and the line simply went dead."""
    src = io.open("agent.py", encoding="utf-8").read()
    wd = src[src.index("async def watchdog("):
             src.index("async def hangup_when_asked(")]
    assert "NEARLY_UP" in wd and 'warned_time["said"]' in wd, \
        "there is still no warning before the limit"
    i = wd.index("MAX_CALL_SECONDS - NEARLY_UP")
    j = wd.index("if total > MAX_CALL_SECONDS:")
    assert i < j, "the warning must come before the hang-up"
    end = wd[j:j + 1200]
    assert "speak_exactly(" in end, \
        "the goodbye is still a suggestion to a busy model"
    assert "generate_reply" not in end, end[:200]
    assert "carry on from where we are" in end,         "it does not tell them how to carry on"
    assert agent.MAX_CALL_SECONDS >= 1200, \
        f"{agent.MAX_CALL_SECONDS}s is not long enough for real work"
    assert 60 <= agent.NEARLY_UP <= 300, agent.NEARLY_UP


@check("a saved login is never offered where it makes no sense")
def _():
    """Call 83: "which saved login should I use first, Amazon or Walmart?"
    asked in the middle of looking for a minivan. The advisor is handed
    every fact about the caller and read that list as a menu."""
    import advisor
    state = advisor.call_state(1)
    assert "saved_logins" not in state, \
        "the list is still named as if it were a choice to offer"
    assert "logins_saved_for_these_shops_only" in state, state.keys()
    p = advisor.ADVISOR_SYSTEM
    assert "no login is ever needed to read a public page" in p, \
        "nothing tells it a login belongs to one shop only"
    assert "Never ask for something the task does not need" in p


@check("the caller hears a word while a job runs, and only then")
def _():
    """Call 70: "Signing in now, about a minute", then eighty-one seconds
    of nothing until the caller asked "Hello? Are you still here?" - and
    the job ran for nearly two minutes. The watchdog measured only the
    CALLER's silence; ours was never measured."""
    src = io.open("agent.py", encoding="utf-8").read()
    wd = src[src.index("async def watchdog("):
             src.index("async def hangup_when_asked(")]
    assert "HOLD_LINES" in wd and "speak_exactly(session, line)" in wd, \
        "nothing is said while a job runs - the line goes dead"
    assert "if not busy:" in wd and 'held["n"] = 0' in wd, \
        "a holding line could be said when nothing is running"
    assert 'now - last_heard["agent_done"] > HOLD_EVERY' in wd, \
        "it should be OUR silence that is measured, not theirs"
    assert 'held["n"] < len(HOLD_LINES)' in wd, \
        "a stuck job would repeat 'still working' for the whole call"
    assert 12 <= agent.HOLD_EVERY <= 30, \
        f"{agent.HOLD_EVERY}s between holding lines is not a natural pause"
    assert len(agent.HOLD_LINES) >= 3, agent.HOLD_LINES
    for line in agent.HOLD_LINES:
        low = line.lower()
        assert len(line.split()) <= 14, f"too long to interrupt with: {line}"
        assert not any(w in low for w in ("done", "finished", "signed in",
                                          "success", "complete", "ready")), \
            f"a holding line claims something finished: {line}"
        assert any(w in low for w in ("still", "working", "going",
                                      "trying")), line
    # what these really take: a B&H sign-in ran 1m50 against "about a minute"
    for tool in ("sign_in_to_site", "connect_email"):
        i = src.find(f"async def {tool}(")
        if i > 0:
            body = src[i:i + 3000]
            assert "about a minute" not in body, \
                f"{tool} still promises about a minute"


@check("how a job ended is written into the conversation, word for word")
def _():
    """Call 67 said "still in progress", call 68 "finished successfully",
    both about a sign-in that had failed; and call 68 read "$699.99, down
    from $849.99" out as $849.99. The outcome now goes into the model's own
    record as a fact, with the exact answer."""
    import asyncio
    a = agent.Assistant({"account_id": 1, "name": "T"}, "+1555", 1)
    a.job_site = "B&H"
    kept = []

    async def fake_update(ctx, **k):
        kept.append(ctx)

    a.update_chat_ctx = fake_update
    asyncio.run(a._record_outcome({
        "state": "failed", "kind": "site_login", "reason": "bot_check",
        "message": "b&h wants a human to complete a check by hand"}))
    asyncio.run(a._record_outcome({
        "state": "done", "kind": "browse", "message": "The top match is the "
        "Epson EcoTank Pro ET-5850 for $699.99, down from $849.99."}))
    assert len(kept) == 2, "nothing was written into the conversation"

    def last_text(ctx):
        item = ctx.items[-1]
        return item.text_content if hasattr(item, "text_content") else str(item)
    failed, done = last_text(kept[0]), last_text(kept[1])
    assert "FAILED" in failed and "B&H" in failed and \
        "Never say it succeeded" in failed, failed
    assert "$699.99, down from $849.99" in done, "the exact answer was lost"
    assert "the first is today's price" in done, done
    assert kept[0].items[-1].role == "system"


@check("'did it ever work before?' is answered by the record, not the history")
def _():
    """Call 69: "It worked before because the site let me in then" - B&H
    never had. Call 68's false "sign-in finished successfully" was in the
    call history and was believed. And retrying a site that blocks every
    time was offered as if it might help; guest checkout was invented."""
    import asyncio
    from fastapi.testclient import TestClient
    cl = TestClient(main.app, raise_server_exceptions=False,
                    base_url="https://t")
    cl.post("/admin/login", json={"password": os.environ.get(
        "ADMIN_PASSWORD", "changeme")})
    db = main.Session()
    job = main.Job(account_id=1, kind="site_login", site="b&h-check",
                   state="failed", reason="bot_check",
                   message="b&h wants a human to complete a check")
    db.add(job)
    db.commit()
    db.refresh(job)
    jid = job.id
    for n in range(3):
        db.add(main.Block(site="b&h-check", kind="puzzle",
                          job_id=jid if n == 2 else None))
    db.commit()
    db.close()
    d = cl.get("/jobs/status", params={"job_id": jid}).json()
    assert d.get("ever_signed_in") is False, d
    assert d.get("blocks_today") == 3 and d.get("worth_retrying") is False, d

    a = agent.Assistant({"account_id": 1, "name": "T"}, "+1555", 1)
    a.job_site = "B&H"
    kept = []

    async def fake_update(ctx, **k):
        kept.append(ctx)

    a.update_chat_ctx = fake_update
    asyncio.run(a._record_outcome(d))
    fact = kept[-1].items[-1].text_content
    assert "NEVER let us sign in" in fact, fact
    assert "Trying again will not help" in fact, fact
    words = a._failure_words(d)
    assert "trying again won't help" in words, words
    src = io.open("agent.py", encoding="utf-8").read()
    w = src[src.index("    async def _watch(self, kind, fetch, describe):"):]
    assert "guest checkout or anything else that has not" in w[:4000], \
        "after a failure it can still offer things nobody has tried"


@check("the 'protected by reCAPTCHA' badge is not a human check")
def _():
    """Reading frames put the reCAPTCHA badge's words into B&H's cart page
    and the cart was taken for a wall - on every site with the badge, a
    job would have stopped for nothing. The badge asks nothing."""
    import signals
    cart = ("Subtotal $699.99 Shipping FREE Est. Tax: $62.12 Total: $762.11 "
            "Begin Checkout [inside a frame] Pay in 4 interest-free payments "
            "with PayPal. [inside a frame] protected by reCAPTCHA Privacy - "
            "Terms")
    assert not signals.looks_like_bot_check(cart), "a badge stopped the job"
    assert signals.classify_block(cart)["kind"] != "puzzle"
    for real in ("Before we continue... Press & Hold to confirm you are a "
                 "human (and not a bot).",
                 "[inside a frame] I'm not a robot reCAPTCHA Privacy - Terms",
                 "Please complete the captcha to continue"):
        assert signals.looks_like_bot_check(real), f"missed a real check: {real}"


@check("a human check inside a frame is named, and what it said is kept")
def _():
    """B&H: the check was in a frame, whose words come after 4,000
    characters of menu. The runner saw it; the classifier only read the
    menu and filed it as "unknown", keeping the menu as the evidence."""
    import signals
    menu = "Press Enter for Accessibility for blind people " * 120
    text = (menu + "\n[inside a frame] Before we continue... Press & Hold "
            "to confirm you are a human (and not a bot). Reference ID a0c0")
    assert len(menu) > 4000
    got = signals.classify_block(text, "https://www.bhphotovideo.com/a/cart")
    assert got["kind"] == "puzzle", got["kind"]
    assert "Press & Hold" in got["saw"], f"kept the menu instead: {got['saw'][:80]}"


@check("a failed job is said in words the model can't turn around")
def _():
    """Call 67: told to say B&H's sign-in had failed, the voice model said
    "it's still in progress", and the caller waited until he was hung up
    on. A failure is now spoken with say(), like "Are you still there?"."""
    a = agent.Assistant({"account_id": 1, "name": "T"}, "+1555", 1)
    a.job_site = "B&H"
    words = a._failure_words({"kind": "site_login", "reason": "bot_check"})
    assert "B&H" in words and "human check" in words and "sign-in" in words
    assert "progress" not in words
    assert a._failure_words({"reason": "cancelled"}) == ""
    src = io.open("agent.py", encoding="utf-8").read()
    w = src[src.index("    async def _watch(self, kind, fetch, describe):"):]
    w = w[:w.index("    def _start_watch(")]
    assert "await self._record_outcome(d)" in w, \
        "how the job ended is not written into the conversation as a fact"
    assert "Say exactly this first, word for word" in w, \
        "a failure is still left to the model to phrase"
    assert "sess.say(" not in w, \
        "say() raises with this voice - call 68's failure was never heard"
    assert "self.job_live = False" in w, "a finished job still counts as busy"
    assert 'getattr(agent_obj, "job_live", False)' in src, \
        "the silence watchdog still treats a failed job as running"


@check("saved here is never mistaken for signed in, and history is used")
def _():
    """Call 67: "I'm already connected to your account" with nothing
    signed in; and "can you remind me?" about what the history showed."""
    src = io.open("agent.py", encoding="utf-8").read()
    i = src.index("async def what_is_saved(")
    assert "does not mean you are signed in" in src[i:i + 2000]
    inst = agent.Assistant({"account_id": 1, "name": "T"}, "+1555", 1)
    assert "never ask them to repeat it" in inst.instructions


@check("the voice side says 'not that exact one' whenever it is true")
def _():
    """The backend said "I could not find that exact one"; the voice tool
    built its own sentence from the prices and dropped it."""
    src = io.open("agent.py", encoding="utf-8").read()
    i = src.index("async def find_best_price(")
    body = src[i:src.index("@function_tool", i)]
    assert 'quick.get("exact")' in body, "whether it's exact is never read"
    assert "Say that FIRST" in body, "the near miss isn't said first"
    assert "shop: str" in body and "shop=shop" in body, \
        "a shop they name still goes in as part of the item"


# -------------------------------------------------------------- sign-up
print("new customers")


@check("a new person signs up only with a code the office gave, once")
def _():
    """David: "how do I add a new customer - can they sign up by
    themselves?" They couldn't: an unknown number was told to call the
    office and hung up on. Now the office makes a code and they do the
    rest by voice - but a code, used once, is the only way in."""
    import signup
    inv = signup.make_invite("check: freelancer", 14)
    phone = "+1 (845) 555-0771"
    assert len(inv["code"]) == 6 and inv["code"].isdigit(), inv
    assert signup.check_invite(phone, "111111")["reason"] == "bad_code"
    assert signup.check_invite(phone, inv["code"])["ok"]
    assert signup.complete_signup(phone, inv["code"], "yossi", "ben-david",
                                  "1234")["reason"] == "weak_pin"
    assert signup.complete_signup(phone, inv["code"], "yossi", "",
                                  "4729")["reason"] == "name_needed"
    done = signup.complete_signup(phone, inv["code"], "yossi", "ben-david",
                                  "4729")
    assert done["ok"] and done["name"] == "Yossi Ben-David", done
    assert signup.check_invite("+18455550772", inv["code"])["reason"] == \
        "bad_code", "one code signed up two people"
    assert signup.check_invite(phone, "222222")["reason"] == \
        "already_customer"
    assert signup.check_invite("", inv["code"])["reason"] == "no_number", \
        "a hidden number was signed up - we could never know them again"
    db = main.Session()
    nums = [p.number for p in db.query(main.PhoneNumber)
            .filter_by(account_id=done["account_id"]).all()]
    stored = [r.code_hash for r in db.query(main.Invite).all()]
    db.close()
    assert nums == ["+18455550771"], nums
    assert inv["code"] not in stored, "the invite code was stored as it is"
    listed = signup.list_invites()
    assert not any(inv["code"] in str(r) for r in listed), \
        "the office list shows the code again"
    assert listed[0]["state"] == "used" and \
        listed[0]["name"] == "Yossi Ben-David", listed[0]


@check("guessing invite codes is stopped")
def _():
    import signup
    phone = "+18455550999"
    for i in range(signup.TRIES_PER_PHONE):
        signup.check_invite(phone, f"90000{i}")
    assert signup.check_invite(phone, "900009")["reason"] == "too_many"


@check("nobody gets a PIN anyone could guess - by phone or by hand")
def _():
    """The admin form filled in 1234 for every customer made by hand."""
    import signup
    for weak in ("1234", "4321", "0000", "7777", "123456", "6789"):
        assert signup.weak_pin(weak), weak
    for fine in ("4729", "2580", "1357", "8812"):
        assert not signup.weak_pin(fine), fine
    from fastapi.testclient import TestClient
    cl = TestClient(main.app, raise_server_exceptions=False,
                    base_url="https://t")
    cl.post("/admin/login", json={"password": os.environ.get(
        "ADMIN_PASSWORD", "changeme")})
    bad = cl.post("/accounts", json={"name": "Weak Pin",
                                     "phone": "+18455550881", "pin": "1234"})
    empty = cl.post("/accounts", json={"name": "No Pin",
                                       "phone": "+18455550882"})
    good = cl.post("/accounts", json={"name": "Good Pin",
                                      "phone": "+18455550883", "pin": "4729"})
    assert bad.status_code == 400, "1234 was accepted"
    assert empty.status_code == 400, "an account was made with no PIN"
    assert good.status_code == 200, good.text[:200]
    import admin_page
    page = admin_page.ADMIN_HTML
    assert 'id="k" value="1234"' not in page, "the form still fills in 1234"
    assert "set themselves up by phone. This is" not in page, \
        "the panel still says customers set themselves up with no code"
    assert "makeInvite()" in page and "loadInvites()" in page


@check("a customer can add a second phone themselves, proved by their PIN")
def _():
    """Home line and mobile: one account had one number and nobody could
    add another. From the phone we know they get a one-time code; they
    ring from the new phone, say it, and their PIN proves it is them."""
    import signup
    inv = signup.make_invite("check: second phone", 14)
    home = "+1 (845) 555-0661"
    made = signup.complete_signup(home, inv["code"], "rivka", "stein", "5813")
    assert made["ok"], made
    aid = made["account_id"]

    link = signup.make_link_code(aid)
    mobile = "+18455550662"
    seen = signup.check_invite(mobile, link["code"])
    assert seen.get("link") and seen.get("first_name") == "Rivka", seen
    # a phone code can never make a brand-new account
    assert signup.complete_signup(mobile, link["code"], "someone", "else",
                                  "7391")["reason"] == "link_code"
    assert signup.complete_link(mobile, link["code"], "1111")["reason"] == \
        "wrong_pin"
    done = signup.complete_link(mobile, link["code"], "5813")
    assert done["ok"] and done["account_id"] == aid, done
    assert "+18455550662" in done["phones"] and "+18455550661" in done["phones"]
    assert signup.check_invite("+18455550663", link["code"])["reason"] == \
        "bad_code", "the phone code worked twice"
    kinds = {i["kind"] for i in signup.list_invites()}
    assert {"add a phone", "new customer"} <= kinds, kinds

    # the office can add and remove - but never the last number
    assert signup.add_phone(aid, "+18455550664")["ok"]
    assert signup.add_phone(aid, "+18455550664")["reason"] == "already_customer"
    assert signup.remove_phone(aid, "+18455550664")["ok"]
    assert signup.remove_phone(aid, "+18455550662")["ok"]
    assert signup.remove_phone(aid, "+18455550661")["reason"] == \
        "last_number", "a customer was left with no number at all"


@check("a caller with a phone code is added by PIN, not signed up again")
def _():
    import asyncio
    sent = []

    async def fake_post(path, payload, params=None):
        sent.append(path)
        if path == "/signup/check":
            return {"ok": True, "link": True, "first_name": "Rivka"}
        if path == "/signup/link":
            return ({"ok": False, "reason": "wrong_pin"}
                    if payload["pin"] != "5813" else
                    {"ok": True, "account_id": 9, "name": "Rivka Stein",
                     "phones": ["+18455550661", "+18455550662"]})
        return {"ok": False}

    async def quiet(*a, **k):
        return None

    got = {}

    def on_up(acct, welcome=""):
        got["acct"], got["welcome"] = acct, welcome
        return agent.Assistant(acct, "+18455550662", 7)

    real_post, real_log = agent.backend_post, agent.log_turn
    agent.backend_post, agent.log_turn = fake_post, quiet
    try:
        s = agent.Signup("+18455550662", 7, on_up)

        async def run():
            said = await s.check_invite_code(None, "5 5 5 1 2 3")
            new_acct = await s.create_my_account(None, "X", "Y", "7391", "yes")
            wrong = await s.confirm_my_pin(None, "1111")
            right = await s.confirm_my_pin(None, "5813")
            return said, new_acct, wrong, right
        said, new_acct, wrong, right = asyncio.run(run())
    finally:
        agent.backend_post, agent.log_turn = real_post, real_log
    assert "adds this phone to Rivka" in said, said
    assert "already exists" in new_acct and "/signup/complete" not in sent, \
        "a phone code was used to make a new account"
    assert "doesn't match" in wrong, f"a wrong PIN was not refused: {wrong}"
    assert isinstance(right, agent.Assistant),         f"the full assistant didn't take over: {right}"
    assert "either phone" in got["welcome"], got["welcome"]
    assert got["acct"]["account_id"] == 9, got

    inst = agent.Assistant({"account_id": 1, "name": "T"}, "+1555", 1)
    assert "add_another_phone" in [t.id for t in inst.tools],         "a customer can't ask to add a phone"
    src = io.open("agent.py", encoding="utf-8").read()
    assert 'os.environ.get("OFFICE_CONTACT"' in src, \
        "someone with no code is still told to 'call the office' with no way to"
    assert "their email so you can read it to them" in         src[src.index("def signed_up("):][:1500],         "a new customer isn't told they can connect their email"


@check("an unknown caller can sign up, and carries straight on as a customer")
def _():
    import asyncio
    sent = []

    async def fake_post(path, payload, params=None):
        sent.append(path)
        if path == "/signup/check":
            return {"ok": True}
        return {"ok": True, "account_id": 42, "name": "Yossi Ben-David"}

    async def quiet(*a, **k):
        return None

    handed = {}

    def on_up(acct):
        handed["acct"] = acct
        return agent.Assistant(acct, "+18455550199", 7)

    real_post, real_log = agent.backend_post, agent.log_turn
    agent.backend_post, agent.log_turn = fake_post, quiet
    try:
        s = agent.Signup("+18455550199", 7, on_up)

        async def run():
            early = await s.create_my_account(None, "Yossi", "Ben-David",
                                              "4729", "yes")
            await s.check_invite_code(None, "4 5 3 1 2 8")
            unsure = await s.create_my_account(None, "Yossi", "Ben-David",
                                               "4729", "")
            done = await s.create_my_account(None, "Yossi", "Ben-David",
                                             "4729", "yes, that's right")
            return early, unsure, done
        early, unsure, done = asyncio.run(run())
    finally:
        agent.backend_post, agent.log_turn = real_post, real_log
    assert "invite code first" in early, "signed up with no code checked"
    assert "/signup/complete" not in sent[:1], sent
    assert "wait for a clear yes" in unsure, "signed up without a yes"
    assert isinstance(done, agent.Assistant), \
        "the full assistant didn't take the call over"
    assert handed["acct"]["account_id"] == 42
    assert "4729" not in early + unsure, "the PIN was written into a reply"

    src = io.open("agent.py", encoding="utf-8").read()
    ep = src[src.index("async def entrypoint("):]
    assert "Signup(caller, call_id, signed_up)" in ep, \
        "an unknown number is still just told to call the office"
    assert "nonlocal account, agent_obj" in ep, \
        "the log, memory and hang-up would still follow the sign-up agent"
    i = ep.index("def signed_up(")
    body = ep[i:i + 1500]
    assert "fresh._hangup = agent_obj._hangup" in body, \
        "after signing up, 'goodbye' could never hang up"
    assert "fresh.verified = True" in body
    assert ep.index("await session.start(") < ep.index("create_task(watchdog())")


# ------------------------------------------------------------- everyday
print("everyday questions")


def _everyday_offline(answers):
    """Put everyday.py on canned answers - shaped like the real ones from
    Hebcal, Open-Meteo and zippopotam - and fix today's date, so these
    checks never touch the network and never go stale."""
    import datetime as _dt
    import everyday
    real_fetch, real_today = everyday._fetch_json, everyday._today
    seen = []

    def fake(url, keep_s=600):
        seen.append(url)
        for part, answer in answers:
            if part in url:
                return answer(url) if callable(answer) else answer
        raise AssertionError(f"unexpected lookup: {url}")

    everyday._fetch_json = fake
    everyday._today = lambda: _dt.date(2026, 9, 23)
    everyday._CACHE.clear()

    def restore():
        everyday._fetch_json, everyday._today = real_fetch, real_today
    return everyday, seen, restore


_ZIP_11211 = {"places": [{"place name": "Brooklyn",
                          "state abbreviation": "NY",
                          "latitude": "40.7095", "longitude": "-73.9563"}]}


@check("the weather is for where they live, in words")
def _():
    """Asked with no place, it must use their saved address - not a guess,
    and not the office's town."""
    ev, seen, restore = _everyday_offline([
        ("zippopotam.us/us/11211", _ZIP_11211),
        ("api.open-meteo.com", {
            "current": {"temperature_2m": 63.3, "apparent_temperature": 55.0,
                        "weather_code": 3},
            "daily": {"time": ["2026-09-23", "2026-09-24"],
                      "temperature_2m_max": [68.1, 64.5],
                      "temperature_2m_min": [54.6, 52.1],
                      "precipitation_probability_max": [0, 40],
                      "weather_code": [3, 61]}}),
    ])
    db = main.Session()
    acct = main.Account(name="Weather Check")
    db.add(acct)
    db.commit()
    db.refresh(acct)
    aid = acct.id
    db.add(main.Address(account_id=aid, line1="1 Lee Ave", city="Brooklyn",
                        state="NY", zip="11211", is_default=1))
    db.commit()
    db.close()
    try:
        w = ev.weather(aid, "", 1)
    finally:
        restore()
    assert any("11211" in u for u in seen), f"didn't use their zip: {seen}"
    assert w["place"] == "Brooklyn, NY 11211", w
    assert w["now"].startswith("63 degrees and cloudy"), w["now"]
    assert "feels like 55" in w["now"], w["now"]
    days = w["days"]
    assert [d["day"] for d in days] == ["today", "tomorrow"], days
    assert days[1]["sky"] == "light rain" and days[1]["rain_chance"] == 40


@check("with no address and no place, it asks rather than guesses")
def _():
    ev, seen, restore = _everyday_offline([])
    try:
        w = ev.weather(None, "", 1)
        j = ev.jewish_calendar(None, "shabbos", "")
    finally:
        restore()
    assert w.get("reason") == "no_place", w
    assert j.get("reason") == "no_place", j
    assert not seen, f"it looked something up anyway: {seen}"


@check("the town they mean: Williamsburg is Brooklyn, Lakewood is NJ")
def _():
    """Williamsburg came back as Virginia and Yerushalayim as a hamlet in
    Virginia. The nearest town of that name wins - unless another one is
    twenty times bigger."""
    import everyday
    assert everyday.NEIGHBOURHOOD_ZIPS["williamsburg"] == "11211"
    assert everyday.NEIGHBOURHOOD_ZIPS["boro park"] == "11219"
    lakewoods = [
        {"name": "Lakewood", "admin1": "Colorado", "country_code": "US",
         "timezone": "America/Denver", "population": 155984},
        {"name": "Lakewood", "admin1": "New Jersey", "country_code": "US",
         "timezone": "America/New_York", "population": 135158}]
    assert everyday._likeliest(lakewoods)["admin1"] == "New Jersey"
    jerusalems = [
        {"name": "Jerusalem", "admin1": "Virginia", "country_code": "US",
         "timezone": "America/New_York", "population": 0},
        {"name": "Jerusalem", "admin1": "Jerusalem", "country_code": "IL",
         "timezone": "Asia/Jerusalem", "population": 801000}]
    assert everyday._likeliest(jerusalems)["country_code"] == "IL"
    ev, seen, restore = _everyday_offline([
        ("zippopotam.us/us/11211", _ZIP_11211)])
    try:
        got = ev.where(None, "Williamsburg")
    finally:
        restore()
    assert got["zip"] == "11211", got
    assert "Brooklyn" in got["label"], got


@check("Jerusalem lights candles at 40 minutes, everywhere else at 18")
def _():
    import everyday
    assert everyday.candle_minutes(
        {"country": "IL", "label": "Jerusalem, Israel"}) == 40
    assert everyday.candle_minutes(
        {"country": "IL", "label": "Bnei Brak, Israel"}) == 18
    assert everyday.candle_minutes(
        {"country": "US", "label": "Brooklyn, NY 11211"}) == 18
    assert everyday._loc_params({"country": "IL", "hebcal": {}})["i"] == "on", \
        "Israel keeps one day of Yom Tov"


@check("Shabbos times come with Rabbeinu Tam, and the parsha isn't a holiday")
def _():
    ev, seen, restore = _everyday_offline([
        ("zippopotam.us/us/11211", _ZIP_11211),
        ("hebcal.com/shabbat", {"items": [
            {"category": "candles", "title": "Candle lighting: 6:29pm",
             "date": "2026-09-25T18:29:00-04:00"},
            {"category": "holiday", "title": "Sukkot I",
             "date": "2026-09-26"},
            {"category": "havdalah", "title": "Havdalah: 7:25pm",
             "date": "2026-09-27T19:25:00-04:00"}]}),
        ("hebcal.com/zmanim", {"times": {
            "tzeit72min": "2026-09-27T20:03:00-04:00"}}),
        ("hebcal.com/converter", {"hd": 12, "hm": "Tishrei", "hy": 5787,
                                  "events": ["Parashat Vezot Haberakhah"]}),
    ])
    try:
        s = ev.shabbos(None, "11211")
        h = ev.hebrew_date()
    finally:
        restore()
    candles = [i for i in s["items"] if i["what"] == "Candle lighting"]
    assert candles and candles[0]["time"] == "6:29 PM", s["items"]
    assert candles[0]["day"] == "Friday, September 25", candles
    assert s["rabbeinu_tam"] == "8:03 PM", s
    assert any("b=18" in u for u in seen if "shabbat" in u), \
        "candle lighting minutes were not sent"
    assert h["events"] == [], "the parsha was read out as today's holiday"
    assert h["parsha"] == "Vezot Haberakhah", h
    assert h["spoken"] == "the 12th of Tishrei, 5787", h


@check("a Hebrew date is read however it's said, or not at all")
def _():
    import everyday
    for said, want in (("9 Adar", (9, "Adar")),
                       ("the 9th of Adar", (9, "Adar")),
                       ("Adar 9", (9, "Adar")),
                       ("Adar II 14", (14, "Adar2")),
                       ("14 adar sheni", (14, "Adar2")),
                       ("Teves 10", (10, "Tevet")),
                       ("15th of Shevat", (15, "Shvat")),
                       ("1st of Tishrei", (1, "Tishrei"))):
        got = everyday.parse_hebrew_date(said)
        assert got == want, f"{said!r} read as {got}, wanted {want}"
    for nonsense in ("sometime in winter", "Adar", "32 Nisan", ""):
        assert everyday.parse_hebrew_date(nonsense) is None, nonsense
    # 5784 had two Adars (spring 2024); 5785 and 5786 did not; 5787 does
    for hy, leap in ((5784, True), (5785, False), (5786, False),
                     (5787, True)):
        assert everyday.hebrew_leap(hy) is leap, hy


@check("a yahrzeit in a year with two Adars gives both, and the evening")
def _():
    """Someone who passed in an ordinary Adar: in a leap year the minhag
    is not one thing. Giving one date quietly takes a side. And a date
    said on its own sends people a day late - the candle is lit the
    evening before."""
    def converter(url):
        if "g2h=1" in url:
            return {"hd": 12, "hm": "Tishrei", "hy": 5787, "events": []}
        table = {("5787", "Adar1"): (2027, 2, 15),
                 ("5787", "Adar2"): (2027, 3, 17),
                 ("5788", "Adar"): (2028, 3, 6),
                 ("5789", "Adar"): (2029, 2, 23)}
        hy = url.split("hy=")[1].split("&")[0]
        hm = url.split("hm=")[1].split("&")[0]
        gy, gm, gd = table[(hy, hm)]
        return {"gy": gy, "gm": gm, "gd": gd}

    ev, seen, restore = _everyday_offline([("hebcal.com/converter",
                                            converter)])
    try:
        y = ev.yahrzeit("", False, "8 Adar", 2)
    finally:
        restore()
    assert y["hebrew_date"] == "the 8th of Adar", y["hebrew_date"]
    first = y["coming"][0]
    assert "Adar I" in first["hebrew"], first
    assert first["day"] == "Monday, February 15", first
    assert first["candle_evening"] == "Sunday, February 14", first
    assert first.get("or_in_adar_ii", {}).get("day") == \
        "Wednesday, March 17", "Adar II was not offered"
    assert "rav" in first["note"], "the leap-year question was decided for them"
    second = y["coming"][1]
    assert "Adar, 5788" in second["hebrew"] and not second.get("note"), second
    assert "evening before" in y["note"], y["note"]


@check("'what's my day' still answers when Google doesn't")
def _():
    """An expired Google connection must not cost them the weather and
    candle lighting. Each part fails on its own and is named."""
    import google_tools
    ev, seen, restore = _everyday_offline([
        ("hebcal.com/converter", {"hd": 12, "hm": "Tishrei", "hy": 5787,
                                  "events": []})])
    real = (google_tools.tool_list_events, google_tools.tool_tasks_list,
            google_tools.tool_unread_summary)
    real_w, real_s = ev.weather, ev.shabbos

    def expired(*a, **k):
        raise RuntimeError("invalid_grant: Token has been expired or revoked")

    google_tools.tool_list_events = expired
    google_tools.tool_tasks_list = expired
    google_tools.tool_unread_summary = expired
    ev.weather = lambda *a, **k: {"place": "Brooklyn", "now": "63 degrees",
                                  "days": [{"high": 68, "low": 55,
                                            "sky": "cloudy",
                                            "rain_chance": 0}]}
    ev.shabbos = lambda *a, **k: {"items": []}
    try:
        d = ev.my_day(1)
    finally:
        (google_tools.tool_list_events, google_tools.tool_tasks_list,
         google_tools.tool_unread_summary) = real
        ev.weather, ev.shabbos = real_w, real_s
        restore()
    assert d["date"] == "Wednesday, September 23", d
    assert d["hebrew_date"] == "the 12th of Tishrei, 5787", d
    assert d["weather"]["now"] == "63 degrees", d
    missing = " ".join(d["missing"])
    assert "calendar" in missing and "expired" in missing, missing
    assert "to-do" in missing and "email" in missing, missing


@check("what they keep is read however they say it, or not at all")
def _():
    import everyday
    for said in ("Rabbeinu Tam", "rabenu tam", "I keep RT", "72 minutes",
                 "72"):
        assert everyday._read_havdalah(said) in ("rabbeinu_tam", "72"), said
    assert everyday._read_havdalah("the regular time") == "tzeis"
    assert everyday._read_havdalah("50 minutes") == "50"
    assert everyday._read_havdalah("give me both") == ""
    assert everyday._read_havdalah("whatever my father did") is None
    assert everyday._read_shema("Magen Avraham") == "mga"
    assert everyday._read_shema("the Gra") == "gra"
    assert everyday._read_shema("we're Chabad") == "tanya"
    assert everyday._read_shema("the rebbe's") is None


@check("a minhag once said is kept, applied, and never guessed")
def _():
    """Asked "will it remember I keep Rabbeinu Tam?" - it didn't: the
    profile learner was told to drop anything about religion, and the
    times read out both opinions regardless. Now it is a setting, and the
    code applies it."""
    ev, seen, restore = _everyday_offline([
        ("zippopotam.us/us/11211", _ZIP_11211),
        ("hebcal.com/shabbat", {"items": [
            {"category": "candles", "title": "Candle lighting: 6:25pm",
             "date": "2026-09-25T18:25:00-04:00"},
            {"category": "havdalah", "title": "Havdalah (72 min): 7:56pm",
             "date": "2026-09-27T19:56:00-04:00"}]}),
        ("hebcal.com/zmanim", {"times": {
            "tzeit72min": "2026-09-27T19:56:00-04:00"}}),
    ])
    db = main.Session()
    acct = main.Account(name="Minhag Check")
    db.add(acct)
    db.commit()
    db.refresh(acct)
    aid = acct.id
    db.add(main.Address(account_id=aid, line1="1 Lee Ave", city="Brooklyn",
                        state="NY", zip="11211", is_default=1))
    db.commit()
    db.close()
    try:
        bad = ev.set_minhag(aid, 5, "whatever my father did", "")
        assert bad["problems"] and not bad["saved"], bad
        assert not ev.minhag_of(aid).get("havdalah"), "a guess was saved"

        ev.set_minhag(aid, 22, "Rabbeinu Tam", "")
        seen.clear()
        s = ev.shabbos(aid)
        asked = [u for u in seen if "hebcal.com/shabbat" in u][0]
    finally:
        restore()
    assert "m=72" in asked and "M=on" not in asked, \
        f"havdalah wasn't asked for at Rabbeinu Tam: {asked}"
    assert "b=22" in asked, f"their 22 minutes weren't used: {asked}"
    havdalah = [i for i in s["items"] if i["what"].startswith("Havdalah")]
    assert "Rabbeinu Tam" in havdalah[0]["what"], havdalah
    assert s["rabbeinu_tam"] == "", "a second opinion was offered anyway"
    assert "their minhag" in s["note"], s["note"]
    # the custom of the place still wins in Jerusalem
    assert ev.candle_minutes({"country": "IL", "label": "Jerusalem, Israel"},
                             {"candle_minutes": 22}) == 40
    assert ev.candle_minutes({"country": "US", "label": "Brooklyn"},
                             {"candle_minutes": 22}) == 22
    db = main.Session()
    said = [c.detail for c in db.query(main.Change)
            .filter_by(account_id=aid, area="minhag").all()]
    db.close()
    assert any("Rabbeinu Tam" in d for d in said), \
        "the office can't see the minhag was changed"


@check("zmanim list only their shita once they've said it")
def _():
    import everyday
    whose = {key: who for key, _, who in everyday.ZMANIM_SPOKEN}
    assert whose["sofZmanShmaBaalHatanya"] == "tanya"
    assert whose["sofZmanShmaMGA"] == "mga" and whose["sofZmanShma"] == "gra"
    times = {key: "2026-09-23T09:00:00-04:00" for key in whose}
    ev, seen, restore = _everyday_offline([
        ("zippopotam.us/us/11211", _ZIP_11211),
        ("hebcal.com/zmanim", {"times": times})])
    real = ev.minhag_of
    try:
        ev.minhag_of = lambda aid: {"shema": "tanya"}
        mine = [t["name"] for t in ev.zmanim(1, "11211")["times"]]
        ev.minhag_of = lambda aid: {}
        anyone = [t["name"] for t in ev.zmanim(1, "11211")["times"]]
    finally:
        ev.minhag_of = real
        restore()
    assert "Latest Shema, Baal HaTanya" in mine, mine
    assert not any("Magen Avraham" in n or ", Gra" in n for n in mine), mine
    assert "Latest Shema, Magen Avraham" in anyone
    assert "Latest Shema, Gra" in anyone
    assert not any("Tanya" in n for n in anyone), \
        "a third opinion was read to someone who never asked"


@check("the memory keeps minhagim instead of dropping them as 'religion'")
def _():
    import advisor
    p = advisor.PROFILE_SYSTEM
    assert "minhagim" in p, "the profile learner doesn't know to keep them"
    assert "health, religion or family" not in p, \
        "the learner is still told to drop anything about religion"
    src = io.open("agent.py", encoding="utf-8").read()
    i = src.index("async def remember_minhag(")
    assert "self.verified" in src[i:i + 900], "it saves without the PIN"
    assert "call remember_minhag" in src, "the instructions never mention it"


@check("the everyday tools exist and my_day needs the PIN")
def _():
    src = io.open("agent.py", encoding="utf-8").read()
    for tool in ("weather", "jewish_calendar", "yahrzeit_dates", "my_day"):
        assert f"async def {tool}(" in src, f"{tool} is missing"
        assert tool in src[src.index("EVERYDAY QUESTIONS"):][:900], \
            f"the instructions never mention {tool}"
    i = src.index("async def my_day(")
    assert "self.verified" in src[i:i + 800], \
        "my_day reads their calendar and mail without the PIN"
    for route in ("/everyday/weather", "/everyday/jewish",
                  "/everyday/yahrzeit", "/everyday/my_day"):
        assert route in {r.path for r in main.app.routes}, route


# ------------------------------------------------- forgotten passwords
print("password reset")


@check("a reset with no address or username asks, before a browser opens")
def _():
    """With nothing to type into "who is this account for", there is
    nothing to do - and nothing worth spending a browser on."""
    opened = []

    def no_browser(*a, **k):
        opened.append(a)
        raise AssertionError("a browser was opened with no account to reset")

    import json as _json
    undo_boxes = everywhere("list_mailboxes", lambda aid: [])
    undo_logins = everywhere("list_site_logins", lambda aid: [])
    undo_open = everywhere("_open_with_session", no_browser)
    try:
        db = main.Session()
        job = main.Job(account_id=1, kind="password_reset", site="lowes",
                       state="queued", payload=_json.dumps({}))
        db.add(job)
        db.commit()
        db.refresh(job)
        jid = job.id
        db.close()
        main._run_reset(jid, 1, "lowes")
        db = main.Session()
        row = db.query(main.Job).filter_by(id=jid).first()
        state, reason = row.state, row.reason
        db.close()
    finally:
        undo_open()
        undo_logins()
        undo_boxes()
    assert not opened, "it opened a browser anyway"
    assert state == "failed" and reason == "email_needed", (state, reason)


@check("an address we can't read is not refused - the caller reads the code")
def _():
    """David: "if it works with a text message on the phone, it shouldn't
    be refused - we just don't have the email." The site only sends its
    code to the real owner, so the site is the lock, not us. Refusing
    every unconnected address protected nothing."""
    from fastapi.testclient import TestClient
    cl = TestClient(main.app, raise_server_exceptions=False,
                    base_url="https://t")
    cl.post("/admin/login", json={"password": os.environ.get(
        "ADMIN_PASSWORD", "changeme")})
    undo = everywhere("list_mailboxes",
                      lambda aid: [{"email": "theirs@gmail.com"}])
    try:
        other = cl.post("/jobs/password-reset", params={
            "account_id": 1, "site": "lowes", "check_only": 1,
            "email": "old.address@yahoo.com"})
        mine = cl.post("/jobs/password-reset", params={
            "account_id": 1, "site": "lowes", "check_only": 1,
            "email": "Theirs@Gmail.com"})
    finally:
        undo()
    assert other.status_code == 200, f"refused: {other.text[:200]}"
    assert other.json()["mode"] == "caller_reads_code", other.json()
    assert mine.json()["mode"] == "reads_mailbox", mine.json()

    src = source()
    k = src.index("def _run_reset(")
    body = src[k:k + 30000]
    assert 'reason="code_to_phone"' not in body, \
        "a texted code still ends the reset instead of asking the caller"
    assert '"needs_code"' in body and "ask_caller(" in body, \
        "the reset never hands a code request to the caller"
    assert "code_tries >= 3" in body, \
        "one misheard digit would end the reset - give three goes"


@check("the new password never reaches a log, a job message or the model")
def _():
    """A password written into a job message is a password in the database,
    the live log and the admin panel. The value exists in one local
    variable and goes to the vault; everything a person can read gets the
    placeholder's name instead."""
    src = source()
    i = src.index("def _run_reset(")
    body = src[i:src.index("\ndef ", i + 20)] if "\ndef " in src[i + 20:] \
        else src[i:]
    for line in body.splitlines():
        if "new_pw" not in line:
            continue
        for leak in ("_job_set(", "emit(", "history.append(", "print("):
            assert leak not in line, \
                f"the new password is being written out: {line.strip()}"
    assert "NEW_PASSWORD" in body, \
        "the model should be given a placeholder, not the password"
    assert 'f"({shown' in body or "(new password)" in body, \
        "the typed value is not redacted in the step history"


@check("a made-up password is strong, unambiguous and never repeated")
def _():
    """A password nobody says out loud can be long and awkward. It still
    has to satisfy the rules every site publishes, or the reset fails on
    the last screen with the old password already dead."""
    import browser
    seen = set()
    for _ in range(40):
        pw = browser._new_password()
        seen.add(pw)
        assert len(pw) == 16, f"length {len(pw)}"
        assert any(c.islower() for c in pw), pw
        assert any(c.isupper() for c in pw), pw
        assert any(c.isdigit() for c in pw), pw
        assert any(c in "!#$%&*+=?@" for c in pw), pw
        for bad in "lI1O0":
            assert bad not in pw, f"{bad!r} is easy to misread: {pw}"
    assert len(seen) == 40, "the same password came back twice"
    short = browser._new_password(10, symbols=False)
    assert len(short) == 10 and short.isalnum(), short


@check("nothing is saved as their password unless it was really typed in")
def _():
    """The model saying "changed" does not make it changed. If no new
    password was ever typed into a box, saving one would lock them out of
    their own account - the old password would still be the live one and
    we would have thrown it away."""
    src = source()
    i = src.index("def _run_reset(")
    body = src[i:i + 20000]
    assert "typed_pw" in body, "there is no record of whether it was typed"
    assert "if not typed_pw" in body, \
        "the 'changed' path does not check that a password was typed"
    j = body.index('if a == "changed"')
    assert body.index("save_it(", j) > body.index("if not typed_pw", j), \
        "it saves before checking that anything was typed"


@check("a reset never opens the password it is replacing")
def _():
    """A password being thrown away is still a password. Decrypting it
    would put it in memory and write a secret-access row for nothing:
    all the reset needs is the username."""
    src = source()
    i = src.index("def _run_reset(")
    body = src[i:i + 20000]
    assert "use_site_login" not in body, \
        "the reset decrypts the old password, which it never uses"
    assert "list_site_logins" in body, \
        "it should read the username from the list that stays encrypted"


@check("a human check on a reset page is a stop, not a puzzle")
def _():
    src = source()
    i = src.index("def _run_reset(")
    body = src[i:i + 20000]
    assert "looks_like_bot_check" in body and "record_block" in body, \
        "a wall on the reset page would go unnamed"
    assert "block_reason" in body, \
        "the caller's side needs the reason code, not the wording"


@check("a reset job will not press a button that ends the account")
def _():
    import browser
    for wording in ("Delete my account", "Close account",
                    "Deactivate my account", "Cancel membership",
                    "Unsubscribe from all email"):
        assert browser.DANGER_BUTTONS.search(wording), \
            f"would have pressed {wording!r}"
    for fine in ("Reset password", "Forgot your password?", "Continue",
                 "Send me a link", "Change password", "Save password"):
        assert not browser.DANGER_BUTTONS.search(fine), \
            f"refused something harmless: {fine!r}"
    src = source()
    i = src.index("def _run_reset(")
    assert "DANGER_BUTTONS" in src[i:i + 20000], \
        "_run_reset does not check the buttons it clicks"


@check("the reset link is picked out of the mail, not the unsubscribe link")
def _():
    """A reset mail carries a dozen links: the logo, the app stores, the
    help centre, unsubscribe. Following the first one that mentions the
    shop opens the homepage and the job reports success having changed
    nothing - or worse, unsubscribes them."""
    import google_tools
    rank = google_tools._rank_reset_link
    for never in ("https://www.lowes.com/unsubscribe?id=99",
                  "https://email.lowes.com/optout/abc123",
                  "https://www.lowes.com/help/privacy"):
        assert rank(never, "lowes") < 0, f"would have followed {never}"
    real = "https://www.lowes.com/u/reset-password?token=9f8a7b6c5d4e3f2a1b0c"
    for lesser in ("https://www.lowes.com/",
                   "https://www.lowes.com/l/about.html",
                   "https://apps.apple.com/app/lowes"):
        assert rank(real, "lowes") > rank(lesser, "lowes"), \
            f"{lesser} outranked the real reset link"


@check("the subjects sites really use are recognised as reset mail")
def _():
    import google_tools
    hit = google_tools.RESET_MAIL
    for subject in ("Amazon Password Assistance",
                    "Reset your password",
                    "Reset Your Lowe's Password",
                    "Your password reset request",
                    "Forgot your password?",
                    "Password recovery for your account",
                    "Here is the link to change your password",
                    "Set a new password for your Etsy account"):
        assert hit.search(subject), f"missed {subject!r}"
    for ordinary in ("Your order has shipped",
                     "Change your delivery address",
                     "Your receipt from Costco",
                     "2 items are back in stock"):
        assert not hit.search(ordinary), f"treated as reset mail: {ordinary!r}"


@check("a real reset mail is read, and somebody else's is left alone")
def _():
    """End to end on the reading side, against the shape of mail shops
    actually send: HTML only, the link wrapped in a tracking domain, and
    a dozen other links around it. Also the case that matters most - a
    reset mail for a DIFFERENT site sitting in the same inbox must not be
    used to change this site's password."""
    import time as _t
    import google_tools
    now = int(_t.time() * 1000)
    real_search = google_tools.tool_search_email
    real_body = google_tools._mail_text

    html_mail = (
        '<html><body><a href="https://www.lowes.com/">'
        '<img src="logo.png"></a>'
        '<p>We received a request to reset your password.</p>'
        '<a href="https://click.e.lowes.com/u/reset-password'
        '?token=8fbe1d4a9c7e2b6f0d3a5c81&amp;e=1">Reset my password</a>'
        '<a href="https://www.lowes.com/l/help.html">Need help?</a>'
        '<a href="https://email.lowes.com/unsubscribe/xyz">Unsubscribe</a>'
        '<a href="https://apps.apple.com/app/lowes">Get the app</a>'
        '</body></html>')

    def fake_search(aid, query, limit=5, which="", newest_first=False):
        return {"messages": [
            {"id": "other", "from": "no-reply@target.com", "at_ms": now,
             "subject": "Reset your Target password", "snippet":
                 "We received a request to reset your password."},
            {"id": "theirs", "from": "no-reply@e.lowes.com", "at_ms": now,
             "subject": "Reset Your Lowe’s Password", "snippet":
                 "We received a request to reset your password."},
        ]}

    def fake_body(aid, msg_id, which="", limit=60000):
        if msg_id == "other":
            return ('<a href="https://click.target.com/reset?token=zzz1234567'
                    '890abcdef">Reset</a>')
        return html_mail

    google_tools.tool_search_email = fake_search
    google_tools._mail_text = fake_body
    try:
        got = google_tools.reset_from_email(1, "lowes", now - 60000)
    finally:
        google_tools.tool_search_email = real_search
        google_tools._mail_text = real_body
    assert got, "the reset mail was not found at all"
    link = got.get("link", "")
    assert "reset-password" in link and "token=" in link, link
    assert "lowes" in link, f"it followed another shop's link: {link}"
    assert "unsubscribe" not in link and "apps.apple" not in link, link
    assert "&amp;" not in link, f"the HTML escaping was left in: {link}"

    # And nothing older than the request is ever reopened.
    google_tools.tool_search_email = fake_search
    google_tools._mail_text = fake_body
    try:
        stale = google_tools.reset_from_email(1, "lowes", now + 600000)
    finally:
        google_tools.tool_search_email = real_search
        google_tools._mail_text = real_body
    assert not stale, "it reused a link that arrived before we asked"


@check("every way a reset can fail is something the agent can say")
def _():
    """A reason code the voice side has never heard of comes out as "it
    didn't work" with no explanation the caller can act on."""
    src = source()
    i = src.index("def _run_reset(")
    body = src[i:i + 20000]
    reasons = set(re.findall(r'reason="([a-z_]+)"', body))
    agent_src = io.open("agent.py", encoding="utf-8").read()
    generic = {"cancelled", "stuck", "site_error", "bot_check",
               "rate_limited", "geo_block", "login_needed", "ip_block"}
    for r in reasons:
        assert r in agent_src or r in generic, \
            f"nothing in agent.py knows what to say about {r!r}"
    for must in ("email_needed", "no_reset_mail", "link_unreadable",
                 "no_code", "bad_code"):
        assert must in reasons, f"{must} is never reported"


@check("a password reset needs a spoken yes")
def _():
    src = io.open("agent.py", encoding="utf-8").read()
    i = src.index("async def reset_site_password(")
    body = src[i:src.index("@function_tool", i)]
    assert "said_yes(caller_said)" in body, \
        "it would reset a password without being asked to"
    assert "self.verified" in body, "it does not check the PIN"
    assert body.index("said_yes") < body.index("/jobs/password-reset"), \
        "it starts the job before checking they agreed"


# ------------------------------------------------------------ result
# Windows won't delete a file that's still open, and the database pool holds
# it - so close the pool first, or check_tmp.db is left behind every run.
try:
    main.engine.dispose()
except Exception:
    pass
try:
    os.remove("check_tmp.db")
except Exception:
    pass

print()
if FAILS:
    print(f"DO NOT PUSH - {len(FAILS)} check(s) failed: {', '.join(FAILS)}")
    sys.exit(1)
print("all checks passed - safe to push")
