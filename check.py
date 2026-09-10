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
    assert "minutes ago" in main._when(
        int((now - timedelta(minutes=5)).timestamp() * 1000))
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
