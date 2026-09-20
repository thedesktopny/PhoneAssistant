"""
Run this BEFORE every git push:   python check.py

It boots the backend and the voice agent without touching the internet,
and fails loudly if anything that has broken before is broken again.
Add a line to CHECKS every time something new breaks - that's how the
list earns its keep.
"""
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
    src = open("main.py", encoding="utf-8").read()
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
    tree = ast.parse(open("main.py", encoding="utf-8").read())
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
    src = open("main.py", encoding="utf-8").read()
    for dead in ("No setup for", "No order page known"):
        assert dead not in src, (f"'{dead}' still refuses unknown sites - "
                                 f"call _agent_fallback() instead")
    for fn in ("_run_site_login", "_run_site_orders", "_run_site_search"):
        body = src[src.index(f"def {fn}("):]
        body = body[:body.index("\ndef ", 10)]
        assert "_agent_fallback(" in body, f"{fn} has no fallback path"


@check("the browser's model is configurable and nothing is hard-coded")
def _():
    src = open("main.py", encoding="utf-8").read()
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
        main.OPENAI_API_KEY = key


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
    main.BROWSERBASE_API_KEY = "test"
    main.PROXY_STATUS["proxies_enabled"] = None
    main.urllib.request.urlopen = lambda req, timeout=0: Fake(req.data)
    try:
        sid = main._bb_session("ctx_abc", "US", "NY", "")
    finally:
        main.urllib.request.urlopen = real_open
        main.BROWSERBASE_API_KEY = real_key
    assert sid == "sess_kept", "gave up on the session when the proxy failed"
    assert seen == [True, False], f"expected a retry without proxies: {seen}"
    assert main.PROXY_STATUS["proxies_enabled"] is False, \
        "claimed a proxy the plan never granted"


@check("a finished sign-in isn't read back as the answer to a lookup")
def _():
    """'Signed in and saved the session' summarised against 'what were my
    recent orders' came out as 'I couldn't find any order details'."""

    src = open("main.py", encoding="utf-8").read()
    i = src.index("def job_answer(")
    body = src[i:src.index("\n@app.", i + 10)]
    assert 'row.kind == "site_login"' in body, \
        "job_answer still summarises a sign-in as if it were a lookup"


@check("the page snapshot is one call, not hundreds")
def _():
    """Asking the browser about each element separately took over two
    minutes for a single step on a big shop, and often returned nothing -
    so the model picked numbers for elements that weren't there."""
    src = open("main.py", encoding="utf-8").read()
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
    src = open("main.py", encoding="utf-8").read()
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
    src = open("main.py", encoding="utf-8").read()
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
    main_src = open("main.py", encoding="utf-8").read()
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
    src = open("main.py", encoding="utf-8").read()
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
    src = open("main.py", encoding="utf-8").read()
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
    src = open("main.py", encoding="utf-8").read()
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
    src = open("main.py", encoding="utf-8").read()
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
    src = open("main.py", encoding="utf-8").read()
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
    src = open("main.py", encoding="utf-8").read()
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
    src = open("main.py", encoding="utf-8").read()
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

    real_key, real_post = main.STRIPE_SECRET_KEY, main._stripe
    main.STRIPE_SECRET_KEY, main._stripe = "sk_test_probe", fake_stripe
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
        main.STRIPE_SECRET_KEY, main._stripe = real_key, real_post

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
    src = open("main.py", encoding="utf-8").read()
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

    real_key, real_post = main.STRIPE_SECRET_KEY, main._stripe
    main.STRIPE_SECRET_KEY, main._stripe = "sk_test_probe", refuses
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
        main.STRIPE_SECRET_KEY, main._stripe = real_key, real_post
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
    src = open("main.py", encoding="utf-8").read()
    body = src[src.index("def _run_browse("):]
    body = body[:body.index("\ndef ", 10)]
    assert "spares" in body, "no fallback list is carried into the job"
    i = body.index("looks_like_bot_check(text)")
    window = body[i:i + 500]
    assert "spares.pop(0)" in window, \
        "a bot check still ends the job instead of trying the next source"
    assert "do_goto(page, nxt" in window, window[:200]
    agent_src = open("agent.py", encoding="utf-8").read()
    rp = agent_src[agent_src.index("async def read_page("):]
    rp = rp[:rp.index("\n    @function_tool")]
    assert '"urls": spares' in rp, \
        "read_page doesn't pass the other results as fallbacks"


@check("a PDF is read, not browsed")
def _():
    """An appliance manual is a PDF, and a PDF has no text in a browser at
    all. We were scraping videos for something the manufacturer's own
    manual states plainly - which is how Claude answered and we couldn't."""
    assert main.looks_like_pdf("https://x.com/manual/A16366306.pdf")
    assert main.looks_like_pdf("https://x.com/a.PDF?v=2")
    assert not main.looks_like_pdf("https://youtube.com/watch?v=abc")
    assert not main.looks_like_pdf("")
    src = open("main.py", encoding="utf-8").read()
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
    src = open("main.py", encoding="utf-8").read()
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
    src = open("main.py", encoding="utf-8").read()
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
    src = open("main.py", encoding="utf-8").read()
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
    src = open("main.py", encoding="utf-8").read()
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
    src = open("main.py", encoding="utf-8").read()
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
        main.google_client = refusing(
            b'{"error":{"details":[{"reason":"ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}')
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
        main.google_client = real


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
    main.google_client = lambda *a, **k: Svc()
    try:
        main.tool_drive_search(1, "Moshe's lease")
    finally:
        main.google_client = real
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
    main.google_client = lambda a, api, *x, **k: Docs() if api == "docs" \
        else Drive()
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
        main.google_client = real
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
    main.google_client = lambda *a, **k: Svc()
    try:
        out = main.tool_tasks_list(1)
    finally:
        main.google_client = real
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
    main._stripe_call, main.STRIPE_SECRET_KEY = fake, "sk_test_x"
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
        main._stripe_call, main.STRIPE_SECRET_KEY = real_call, real_key
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
    src = open("main.py", encoding="utf-8").read()
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
    assert "read_page" in names, \
        "nothing can open a search result and read the actual page"
    # the lookup tools run in another process and see none of the call
    tools = {getattr(t, "__name__", ""): t for t in inst.tools}
    for t in ("ask_ai", "look_it_up"):
        if t in tools:
            doc = tools[t].__doc__ or ""
            assert "CANNOT SEE THIS CONVERSATION" in doc, (
                f"{t} doesn't warn that it's blind to the call. A caller "
                f"asked about his ice maker and the question sent was "
                f"'how to turn on the icemaker for the'.")
    assert "WRITE THE WHOLE QUESTION" in inst.instructions, \
        "nothing tells it to put the model number into the question"
    assert "ask_ai" in names, (
        "there is no way to ask a bigger model. The voice model is tuned "
        "for speech, not knowledge - it invented three different fridge "
        "answers rather than admit it didn't know, and browsing for a "
        "minute is the wrong fix for something a good model simply knows.")
    assert "/ask" in {r.path for r in main.app.routes}, "/ask is gone"
    assert "look_it_up" in names, (
        "there is no single tool that searches AND reads the page. Telling "
        "the model to do it in two steps failed on three separate calls - "
        "it searched, narrated progress, and answered from headlines.")
    said = inst.instructions
    assert "USE look_it_up" in said, \
        "the instructions still send it to web_search for exact detail"
    # It must be free to answer general knowledge straight away - forbidding
    # that made it browse for a minute and a half over something it knew.
    assert "ANSWER FROM WHAT YOU KNOW FIRST" in said, \
        "it is being made to search for things it already knows"
    assert "Looking it up is SLOWER" in said, \
        "nothing tells it that searching what it knows wastes the call"
    # the tool's own description must not invite needless lookups either
    tools = {getattr(t, "__name__", ""): t for t in inst.tools}
    doc = (tools["web_search"].__doc__ or "")
    assert "ONLY when you don't already know it" in doc, \
        "web_search still reads as 'search for any fact'"
    # but never guess about the caller's own things, and never fake a source
    assert "never from memory, always from a tool" in said, \
        "it may now guess about the caller's own email and orders"
    assert "NEVER dress a guess up as a source" in said, \
        "it can claim a page said something it invented"
    assert "read_page" in said, "it is never told how to get the real detail"
    src = open("agent.py", encoding="utf-8").read()
    body = src[src.index("async def web_search("):]
    body = body[:body.index("\n    @function_tool")]
    assert "log_turn(" in body, \
        "web_search leaves no trace, so nobody can tell if it ever ran"
    assert "never say 'the " in body, \
        "the search result no longer warns against faking a source"


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
    assert "web_search is NOT one of" in said, \
        "a finished search can still be treated as something to wait for"
    src = open("agent.py", encoding="utf-8").read()
    body = src[src.index("async def web_search("):]
    body = body[:body.index("\n    @function_tool")]
    assert "This search is FINISHED" in body, \
        "the search result doesn't say it has already returned"


@check("a lookup holds the call, so there's no gap to fill with chatter")
def _():
    """Three wordings of "say it once then be quiet" all failed - one
    caller was told "this will take about a minute" three times in ten
    seconds. The model cannot speak while it is inside a tool call, so the
    lookup waits there instead of returning and leaving a silence."""
    src = open("agent.py", encoding="utf-8").read()
    body = src[src.index("async def look_it_up("):]
    body = body[:body.index("\n    @function_tool")]
    assert "LOOKUP_WAIT" in body, \
        "look_it_up returns immediately again, leaving a gap to fill"
    assert "sess.say(" in body, \
        "the one announcement must be fixed words, not left to the model"
    assert 'state == "done"' in body and "Tell them that now" in body, \
        "the answer should come back from the tool, not a later update"
    # and it must still hand over if it runs long, not hold the call for ever
    assert "_watch_job(" in body, "a slow lookup would hold the call open"
    assert agent.LOOKUP_WAIT >= 30, "the wait is too short to be useful"
    assert agent.LOOKUP_WAIT <= 180, "that would hold a caller far too long"


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


@check("the same failed lookup isn't run again")
def _():
    """Call 52: the ice maker lookup failed, and the assistant ran the
    identical search twice more while the caller waited four and a half
    minutes and then hung up."""
    src = open("agent.py", encoding="utf-8").read()
    body = src[src.index("async def look_it_up("):]
    body = body[:body.index("\n    @function_tool")]
    assert "_lookups" in body, "nothing remembers that a lookup failed"
    assert body.index("_lookups.get(key)") < body.index("jobs/browse"), \
        "it starts the job before checking whether this already failed"
    assert 'self._lookups[key] = "failed"' in body, \
        "a failure is never recorded, so it can be repeated for ever"


@check("a shop listing doesn't outrank the manual")
def _():
    """The first result for a model number is the page selling it, which
    tells an owner nothing. The lookup kept landing there and getting
    stuck."""
    src = open("main.py", encoding="utf-8").read()
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
    body = src[src.index("async def submit_site_code("):]
    body = body[:body.index("\n    @function_tool")]
    assert "len(digits) < 3" in body, \
        "submit_site_code still forwards whatever it is given"
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


@check("nothing an email tool does is permanent")
def _():
    """A caller cannot see what just happened, so every action has to be
    reversible - trash is recoverable, labels can be put back, and there
    is no permanent delete anywhere."""
    for undoable in ("archive", "star", "important", "spam"):
        assert undoable in main.MESSAGE_ACTIONS, f"{undoable} is missing"
    for undo in ("unarchive", "unstar", "not_spam"):
        assert undo in main.MESSAGE_ACTIONS, f"no way to undo: {undo}"
    src = open("main.py", encoding="utf-8").read()
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
