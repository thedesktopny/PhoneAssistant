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


def _serper_site(name: str) -> dict:
    """A plain search for one name, for official_site. Its own function
    so a test can stand in for the network."""
    payload = json.dumps({"q": name, "num": 8}).encode()
    req = urllib.request.Request(
        "https://google.serper.dev/search", data=payload,
        headers={"X-API-KEY": SERPER_API_KEY,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode())


# What comes up when you search a shop's name but is never the shop:
# directories, reviews, maps, social pages, and delivery apps that list
# it. Unless it IS what they asked for - "Instacart" means instacart.com.
NOT_THE_SHOP = _re_scrub.compile(
    r"(?i)(^|\.)(wikipedia\.org|yelp\.com|facebook\.com|instagram\.com|"
    r"linkedin\.com|twitter\.com|x\.com|youtube\.com|tiktok\.com|"
    r"pinterest\.com|reddit\.com|quora\.com|bbb\.org|tripadvisor\.com|"
    r"mapquest\.com|google\.com|apple\.com|trustpilot\.com|"
    r"sitejabber\.com|glassdoor\.com|indeed\.com|crunchbase\.com|"
    r"grocerydive\.com|instacart\.com|doordash\.com|ubereats\.com|"
    r"seamless\.com|grubhub\.com|nextdoor\.com|foursquare\.com|"
    r"allmenus\.com|yellowpages\.com|manta\.com|bloomberg\.com)$")

_SITE_CACHE = {}


def official_site(name: str) -> str:
    """The web address a person means by a shop's name, the way a search
    engine knows it: "B&H" is https://www.bhphotovideo.com, "Pomegranate
    Brooklyn" is https://thepompeople.com. Empty if nobody can say -
    never a guess. Remembered for a month, so each name costs one search.
    """
    key = " ".join((name or "").lower().split())
    if not key or not SERPER_API_KEY or is_blocked(key):
        return ""
    hit = _SITE_CACHE.get(key)
    if hit and time.time() - hit[0] < 30 * 86400:
        return hit[1]
    try:
        d = _serper_site(name)
    except Exception:
        return ""
    wanted = _squash(key.replace("&", "").replace(" and ", ""))
    links = []
    kg = d.get("knowledgeGraph") or {}
    if kg.get("website"):
        links.append(kg["website"])
    links += [x.get("link", "") for x in d.get("organic") or []]
    found = ""
    for link in links:
        bits = (link or "").split("/")
        if len(bits) < 3 or not bits[2]:
            continue
        host = bits[2].lower()
        if NOT_THE_SHOP.search(host) and wanted not in _squash(host):
            continue
        found = "https://" + host
        break
    if len(_SITE_CACHE) > 2000:
        _SITE_CACHE.clear()
    _SITE_CACHE[key] = (time.time(), found)
    return found


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


# Ink "for Epson ET-5850" names the printer's model and cost $10 (call
# 67). A listing that is FOR the thing they asked about is not the thing.
# Deliberately not a list of nouns: the real printer's own title says
# "Cartridge-Free", and printers come with "paper trays".
ACCESSORY_WORDS = _re_scrub.compile(
    r"(?i)\b(compatible|replacement|refills?|toner|dust cover|"
    r"screen protector|decals?|skins?)\b")
# They asked for the extra itself - "ink for my Epson" - so extras count.
EXTRA_ASKED = _re_scrub.compile(
    r"(?i)\b(ink|inks|cartridges?|toner|refills?|covers?|cases?|cables?|"
    r"chargers?|paper|parts?|batter(y|ies)|filters?|bags?)\b")


def for_the_item(title: str, models: list) -> bool:
    """"Ink bottles for Epson ET-5850" is for the printer, not it."""
    low = (title or "").lower()
    for m in _re_scrub.finditer(r"\b(for|fits|compatible with)\b", low):
        after = _squash(low[m.end():m.end() + 40])
        if any(md in after for md in models):
            return True
    return False


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

    extras = {m.group(0).lower().rstrip("s")
              for m in EXTRA_ASKED.finditer(item)}
    wants_extra = bool(extras or ACCESSORY_WORDS.search(item))

    def _fits(title: str) -> float:
        low = (title or "").lower()
        if not wants_extra and (ACCESSORY_WORDS.search(title or "")
                                or for_the_item(title, models)):
            return 0.0
        # They asked for the ink, not the printer: the listing has to be
        # the extra they named.
        if extras and not any(x in low for x in extras):
            return 0.0
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
