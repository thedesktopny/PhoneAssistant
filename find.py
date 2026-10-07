"""Finding something out, without a browser.

A question is not an errand. "Which buttons turn Shabbos mode on" and
"how much is this printer at Best Buy" need pages read, not a browser
driven: the browser is what the shops put walls in front of. The fridge
question (calls 42-62) spawned twenty browser jobs over two days, hit a
human check on Frigidaire's own site, and came back with four different
sets of buttons, one for the wrong model.

This reads the search engine's copy of the pages instead. Nobody blocks
that, it takes seconds, and every answer says which site it came from
and that it is a copy, not a live page. The browser is kept for things
that must be DONE on a site, and for a live detail the caller asks for
by name - and those are started elsewhere, never from here.

Freshness is a code, not a feeling:
  knowledge  the model's own memory, no page read - fine for how-to
  indexed    read from a search copy of a page; age unknown, usually days
  dated      the page carries a date, given back as 'as_of'
  none       nothing usable came back
"""
from core import *                                   # noqa: F401,F403
from core import _re_scrub
from rules import is_blocked, blocked_terms_in, BLOCKED_REPLY
from ai import _openai_chat, ASK_SYSTEM

# Questions that need a real page behind the answer. Anything with a model
# number, a price, stock, hours, a place or a shop in it: the model's
# memory is where the invented button combinations came from (call 52).
NEEDS_SOURCE = _re_scrub.compile(
    r"(?i)\b[A-Z]{2,}[-\s]?\d{3,}[A-Z0-9-]*\b|\b\d{3,}[A-Z]{2,}\b"
    r"|\b(price|prices|cost|costs|how much|cheap|cheapest|deal|sale|"
    r"discount|in stock|available|availability|hours|open|opens|closed|"
    r"closes|today|tonight|this week|now|current|currently|latest|newest|"
    r"near|nearby|nearest|address|phone number|where (can|do) i|"
    r"buy|order|sell|sells|stock|listing|listings|model|models|exact|"
    r"specific|instructions|manual|steps|button|buttons|settings?|"
    r"features?|options?|specs?|trims?|best|top rated|compare|which one|"
    r"which ones)\b"
    # "which minivans have leather seats" - a list of what exists, not a
    # fact anyone carries in their head
    r"|\b(which|what)\b.{0,40}\b(have|has|with|come|comes|include)\b")

FIND_SYSTEM = """You answer a question for someone on a phone call, using
ONLY the pages given to you. They are older, cannot see a screen, and
will act on what you say.

Rules:
- Every fact must come from one of the pages. Say which site each fact
  came from, in plain words: "Frigidaire's own site says..." or "Best Buy
  lists it at about $699". Never say a price as if it were exact - it is
  what the page listed when it was copied.
- If the pages disagree, say so, and prefer the maker's own site or the
  shop's own site over a video or a forum.
- If the pages do not answer the question, say found=false and in one
  sentence say what they DO cover. Never fill the gap from memory. Never
  invent a button, a step, a part number or a price.
- Two or three short spoken sentences. No lists, no markdown, no URLs,
  no web addresses.
Reply with JSON only:
{"answer": "...", "found": true, "used": [1, 3]}
'used' is the numbers of the pages the answer came from."""

FETCH_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
PAGE_CHARS = 4000
MAX_PAGES = 5

# A dated line on a page, for 'as_of'. Only the clear forms.
DATED = _re_scrub.compile(
    r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? "
    r"\d{1,2},? \d{4}|\d{4}-\d{2}-\d{2})\b")


def needs_source(question: str) -> bool:
    return bool(NEEDS_SOURCE.search(question or ""))


def _site(url: str) -> str:
    bits = (url or "").split("/")
    host = bits[2].lower() if len(bits) > 2 else ""
    return host[4:] if host.startswith("www.") else host


def _strip_html(raw: str) -> str:
    raw = _re_scrub.sub(r"(?is)<(script|style|noscript|svg|nav|footer|"
                        r"header)\b.*?</\1>", " ", raw)
    raw = _re_scrub.sub(r"(?s)<[^>]+>", " ", raw)
    raw = _re_scrub.sub(r"&(nbsp|#160);", " ", raw)
    raw = _re_scrub.sub(r"&amp;", "&", raw)
    return " ".join(raw.split())


def _fetch_text(url: str) -> str:
    """One plain fetch of a page, no browser. Empty when it will not give
    us a page - a wall, an error, not HTML. Never retried."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": FETCH_UA,
                                                   "Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=8) as r:
            if "html" not in (r.headers.get("Content-Type") or ""):
                return ""
            text = _strip_html(r.read(400000).decode("utf-8", "ignore"))
    except Exception:
        return ""
    if _re_scrub.search(r"(?i)press (and|&) hold|verify you are human|"
                        r"are you a robot|access denied|captcha", text[:1500]):
        return ""
    return text[:PAGE_CHARS * 2]


def _tavily_pages(q: str) -> list:
    """Pages with their text, from the search provider's own copy."""
    payload = json.dumps({
        "api_key": TAVILY_API_KEY, "query": q, "search_depth": "advanced",
        "include_raw_content": True, "include_answer": False,
        "max_results": MAX_PAGES,
    }).encode()
    req = urllib.request.Request(
        "https://api.tavily.com/search", data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode())
    out = []
    for x in d.get("results", [])[:MAX_PAGES]:
        text = (x.get("raw_content") or x.get("content") or "")
        out.append({"title": x.get("title", "")[:120], "url": x.get("url", ""),
                    "site": _site(x.get("url", "")),
                    "text": " ".join(text.split())[:PAGE_CHARS],
                    "date": (x.get("published_date") or "")[:10]})
    return out


def _serper_pages(q: str) -> list:
    """Without Tavily: a plain search, then a plain fetch of the top pages.
    Slower, and a shop with a wall gives nothing - but the search snippet
    still stands in."""
    payload = json.dumps({"q": q, "num": 6}).encode()
    req = urllib.request.Request(
        "https://google.serper.dev/search", data=payload,
        headers={"X-API-KEY": SERPER_API_KEY,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode())
    out = []
    for x in d.get("organic", [])[:MAX_PAGES]:
        url = x.get("link", "")
        text = _fetch_text(url) if len(out) < 3 else ""
        out.append({"title": x.get("title", "")[:120], "url": url,
                    "site": _site(url),
                    "text": (text or x.get("snippet") or "")[:PAGE_CHARS],
                    "date": (x.get("date") or "")[:10]})
    return out


def _pages_for(q: str) -> list:
    if TAVILY_API_KEY:
        return _tavily_pages(q)
    if SERPER_API_KEY:
        return _serper_pages(q)
    return []


def _from_memory(question: str, account_id, call_id) -> dict:
    d = _openai_chat(model=MODEL_BROWSER, account_id=account_id,
                     call_id=call_id, messages=[
                         {"role": "system", "content": ASK_SYSTEM},
                         {"role": "user", "content": question[:600]}])
    said = (d["choices"][0]["message"].get("content") or "").strip()
    return {"answer": said[:900], "found": bool(said), "freshness": "knowledge",
            "sources": [], "as_of": ""}


def find_out(question: str, account_id: int = 0, call_id: int = 0) -> dict:
    """The one way to find something out. Decides in code where the
    answer comes from, and says so in the result."""
    t0 = time.time()
    question = " ".join((question or "").split())[:400]
    ref = f"call {call_id}" if call_id else "find"

    def done(out: dict, how: str) -> dict:
        out["took_ms"] = int((time.time() - t0) * 1000)
        out["how"] = how
        out.setdefault("found", False)
        out.setdefault("sources", [])
        out.setdefault("as_of", "")
        emit("find", ref,
             f"{how}: {'found' if out['found'] else 'NOT found'} in "
             f"{out['took_ms'] / 1000:.1f}s - {question[:90]}"
             + (" <- " + ", ".join(s["site"] for s in out["sources"])
                if out["sources"] else ""),
             "info" if out["found"] else "warn", account_id or None)
        return out

    if not question:
        return done({"answer": "", "freshness": "none",
                     "reason": "no_question"}, "nothing")
    if is_blocked(question):
        return done({"blocked": True, "answer": BLOCKED_REPLY,
                     "freshness": "none", "reason": "blocked"}, "refused")
    if not OPENAI_API_KEY:
        return done({"answer": "", "freshness": "none",
                     "reason": "no_model"}, "nothing")

    if not needs_source(question):
        try:
            out = _from_memory(question, account_id, call_id)
        except Exception as e:
            return done({"answer": "", "freshness": "none",
                         "reason": f"model_failed: {str(e)[:80]}"}, "memory")
        if is_blocked(out["answer"]):
            return done({"blocked": True, "answer": BLOCKED_REPLY,
                         "freshness": "none", "reason": "blocked"}, "refused")
        return done(out, "memory")

    try:
        pages = [p for p in _pages_for(question) if p.get("text")]
    except Exception as e:
        return done({"answer": "", "freshness": "none",
                     "reason": f"search_failed: {str(e)[:80]}"}, "search")
    if not pages:
        return done({"answer": "", "freshness": "none",
                     "reason": "no_pages"}, "search")
    # Pages are the open web. One stray word is noise; two forbidden
    # subjects across the set means the question really is about one.
    if len(blocked_terms_in(" ".join(p["title"] + " " + p["text"][:600]
                                     for p in pages))) >= 2:
        return done({"blocked": True, "answer": BLOCKED_REPLY,
                     "freshness": "none", "reason": "blocked"}, "refused")

    shown = "\n\n".join(
        f"PAGE {i + 1} - {p['site']} - {p['title']}"
        + (f" (dated {p['date']})" if p['date'] else "")
        + f"\n{p['text']}" for i, p in enumerate(pages))
    try:
        d = _openai_chat(model=MODEL_BROWSER, account_id=account_id,
                         call_id=call_id, messages=[
                             {"role": "system", "content": FIND_SYSTEM},
                             {"role": "user", "content":
                                 f"QUESTION: {question}\n\n{shown}"}])
        raw = (d["choices"][0]["message"].get("content") or "").strip()
        act = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    except Exception as e:
        return done({"answer": "", "freshness": "none",
                     "reason": f"model_failed: {str(e)[:80]}"}, "search")
    answer = str(act.get("answer") or "")[:900]
    if is_blocked(answer):
        return done({"blocked": True, "answer": BLOCKED_REPLY,
                     "freshness": "none", "reason": "blocked"}, "refused")
    used = []
    for n in act.get("used") or []:
        try:
            if 1 <= int(n) <= len(pages):
                used.append(pages[int(n) - 1])
        except (TypeError, ValueError):
            pass
    if not used and act.get("found"):
        used = pages[:1]
    sources = [{"site": p["site"], "title": p["title"], "url": p["url"]}
               for p in used]
    as_of = ""
    for p in used:
        if p.get("date"):
            as_of = p["date"]
            break
        m = DATED.search(p["text"][:1500])
        if m:
            as_of = m.group(1)
            break
    return done({"answer": answer, "found": bool(act.get("found")) and
                 bool(answer), "freshness": "dated" if as_of else "indexed",
                 "sources": sources, "as_of": as_of,
                 "pages_read": len(pages)}, "search")
