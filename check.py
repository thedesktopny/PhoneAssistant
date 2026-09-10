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


@check("required tools exist")
def _():
    inst = agent.Assistant({"account_id": 1, "name": "T", "pin": "1"},
                           "+1555", 1)
    names = {getattr(t, "__name__", "") for t in inst.tools}
    for t in ("connect_email", "check_email", "mark_read", "end_call",
              "save_site_login", "sign_in_to_site", "confirm_order"):
        assert t in names, f"tool missing: {t}"


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
