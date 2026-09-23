"""Searching the web, and what the shops are asking.

Done through the search provider rather than by driving a browser:
a search engine answers a robot with a puzzle, and the job dies at
the front door having found nothing.

shopping_prices gives a price per shop in about two seconds - the
same block Google puts at the top of its page - and only for
listings that are actually the thing that was asked for. The
cheapest lookalike is still the wrong answer.
"""
from core import *                                   # noqa: F401,F403
from core import _re_scrub
from rules import is_blocked, blocked_terms_in, BLOCKED_REPLY


def _search_serper(q: str) -> dict:
    payload = json.dumps({"q": q, "num": 5}).encode()
    req = urllib.request.Request(
        "https://google.serper.dev/search", data=payload,
        headers={"X-API-KEY": SERPER_API_KEY,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode())

    answer = ""
    if d.get("answerBox"):
        ab = d["answerBox"]
        answer = ab.get("answer") or ab.get("snippet") or ""
    elif d.get("knowledgeGraph"):
        kg = d["knowledgeGraph"]
        bits = [kg.get("title", ""), kg.get("description", "")]
        for k in ("address", "phone", "hours", "website"):
            if kg.get(k):
                bits.append(f"{k}: {kg[k]}")
        answer = ". ".join(b for b in bits if b)

    results = [{"title": x.get("title", ""),
                "snippet": (x.get("snippet") or "")[:300],
                "url": x.get("link", "")}
               for x in d.get("organic", [])[:4]]

    for p in d.get("places", [])[:3]:
        results.append({
            "title": p.get("title", ""),
            "snippet": " ".join(filter(None, [
                p.get("address", ""),
                f"phone {p['phoneNumber']}" if p.get("phoneNumber") else "",
            ]))[:300],
            "url": "",
        })

    return {"answer": answer[:800], "results": results}


def _search_tavily(q: str) -> dict:
    payload = json.dumps({
        "api_key": TAVILY_API_KEY, "query": q,
        "search_depth": "basic", "include_answer": True, "max_results": 4,
    }).encode()
    req = urllib.request.Request(
        "https://api.tavily.com/search", data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode())
    return {
        "answer": (d.get("answer") or "")[:800],
        "results": [{"title": x.get("title", ""),
                     "snippet": (x.get("content") or "")[:300],
                     "url": x.get("url", "")}
                    for x in d.get("results", [])[:4]],
    }


def _serper_shopping(item: str) -> dict:
    """The shopping block for one query. Its own function so a test can
    stand in for the network without reaching into urllib."""
    payload = json.dumps({"q": item, "num": 20}).encode()
    req = urllib.request.Request(
        "https://google.serper.dev/shopping", data=payload,
        headers={"X-API-KEY": SERPER_API_KEY,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def _money(text: str) -> float:
    """$1,299.99 -> 1299.99. Anything unreadable sorts last."""
    digits = "".join(ch for ch in (text or "")
                     if ch.isdigit() or ch == ".")
    try:
        return float(digits)
    except ValueError:
        return 9e9


# "Epson ET-5850 printer from B&H": where to buy it is not part of its
# name. Counted as words of the title, "from" and "b&h" made the exact
# printer read as a near miss (call 65).
SHOP_PHRASE = _re_scrub.compile(
    r"(?i)\s+(?:from|at)\s+(?:the\s+)?([a-z0-9&'.\- ]{2,40}?)"
    r"(?:\s+(?:website|web site|site|store|online))?\s*$")
# A model number is the strongest thing a title can agree on: ET-5850,
# WH-1000XM5, 55UQ7590. Split on the hyphen, "et" was too short to count
# and the printer's own name did the least work of any word.
MODEL = _re_scrub.compile(r"(?i)\b(?=[a-z0-9-]*\d)(?=[a-z0-9-]*[a-z])"
                          r"[a-z0-9]+(?:-[a-z0-9]+)*\b")


def split_shop(item: str):
    """"Epson ET-5850 printer from B&H" -> ("Epson ET-5850 printer", "B&H")."""
    m = SHOP_PHRASE.search(item or "")
    if not m:
        return (item or "").strip(), ""
    return item[:m.start()].strip(), m.group(1).strip()


def _squash(text: str) -> str:
    return _re_scrub.sub(r"[^a-z0-9]", "", (text or "").lower())


def shopping_prices(item: str, limit: int = 8, shop: str = "") -> dict:
    """What the shops are asking, from the search provider's shopping
    results - the same block Google puts at the top of the page. No
    browser, so no shop can refuse us, and it takes about two seconds."""
    if is_blocked(item):
        return {"blocked": True, "answer": BLOCKED_REPLY, "offers": []}
    if not SERPER_API_KEY:
        return {"offers": [], "error": "shopping search isn't configured"}
    item, said_shop = split_shop(item)
    shop = (shop or said_shop).strip()
    try:
        d = _serper_shopping(item)
    except Exception as e:
        return {"offers": [], "error": str(e)[:200]}

    offers = []
    for x in d.get("shopping", []):
        price = (x.get("price") or "").strip()
        if not price:
            continue
        offers.append({
            "shop": (x.get("source") or "").strip(),
            "title": (x.get("title") or "").strip()[:120],
            "price": price,
            "amount": _money(price),
            "delivery": (x.get("delivery") or "").strip()[:60],
            "rating": x.get("rating"),
            "link": (x.get("link") or "")[:400],
        })
    # Shopping results match loosely: asking for an ECCO New Jersey
    # returns the Byway, the S Lite and the Move, and the cheapest of
    # those is the wrong shoe at the right price. Only compare listings
    # that are actually the thing they asked for.
    GENERIC = {"the", "and", "for", "with", "mens", "men", "womens", "women",
               "shoes", "shoe", "size", "pack", "inch", "inches", "new"}
    words = [w for w in _re_scrub.split(r"[^a-z0-9]+", item.lower())
             if len(w) > 2]
    wanted = [w for w in words if w not in GENERIC] or words

    models = [_squash(m) for m in MODEL.findall(item) if len(_squash(m)) >= 4]

    def _fits(title: str) -> float:
        low = (title or "").lower()
        if models and all(m in _squash(title) for m in models):
            return 1.0
        if not wanted:
            return 1.0
        return sum(1 for w in wanted if w in low) / len(wanted)

    offers_seen = len(offers)
    for o in offers:
        o["match"] = round(_fits(o["title"]), 2)
    exact = [o for o in offers if o["match"] >= 0.99]
    close = [o for o in offers if 0.6 <= o["match"] < 0.99]
    offers = exact or close
    same_thing = bool(exact)
    offers.sort(key=lambda o: o["amount"])
    # one line per shop: five listings from the same shop is not a choice
    seen, kept = set(), []
    for o in offers:
        key = (o["shop"] or o["title"]).lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(o)
        if len(kept) >= limit:
            break
    said = ""
    if kept:
        best = kept[0]
        said = (f"Cheapest is {best['shop'] or 'one shop'} at "
                f"{best['price']}")
        rest = [f"{o['shop']} {o['price']}" for o in kept[1:4] if o["shop"]]
        if rest:
            said += ", then " + ", ".join(rest)
        said += "."
        if not same_thing:
            said = ("I could not find that exact one, so these are the "
                    "closest: " + said)
    elif offers_seen:
        # listings came back, but for other things entirely. Saying
        # nothing here is how the wrong shoe gets priced as theirs.
        said = ("Nothing in the shopping listings is that exact item.")
    at_shop = []
    if shop:
        want_shop = _squash(shop.replace("&", "and"))
        at_shop = [o for o in kept
                   if want_shop and (want_shop in _squash(
                       (o["shop"] or "").replace("&", "and"))
                       or _squash(o["shop"]).startswith(_squash(shop)))]
    return {"offers": kept, "answer": said, "checked": len(offers),
            "exact": same_thing, "shop": shop, "at_shop": at_shop}


def tool_web_search(query: str, near: str = "") -> dict:
    """Google-backed web search with a content filter."""
    if is_blocked(query):
        return {"blocked": True,
                "answer": "I am not allowed to talk to you about this.",
                "results": []}

    q = f"{query} near {near}" if near else query
    try:
        if SERPER_API_KEY:
            out = _search_serper(q)
        elif TAVILY_API_KEY:
            out = _search_tavily(q)
        else:
            return {"answer": "Web search isn't configured.", "results": []}
    except Exception as e:
        return {"answer": f"Search failed: {e}", "results": []}

    # The direct answer is read out, so judge it as strictly as speech.
    if is_blocked(out.get("answer", "")):
        return {"blocked": True,
                "answer": "I am not allowed to talk to you about this.",
                "results": []}
    # Snippets are scraped web text and full of stray words. Refusing a
    # whole search because one result said "news" blocked a question about
    # security assessors. Two different forbidden words means the results
    # really are about something we don't discuss; one means nothing.
    snippets = " ".join(r.get("snippet", "") + " " + r.get("title", "")
                        for r in out.get("results", []))
    if len(blocked_terms_in(snippets)) >= 2:
        return {"blocked": True,
                "answer": "I am not allowed to talk to you about this.",
                "results": []}
    return out
