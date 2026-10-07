"""Live prices from the shops that publish them.

A search copy of a page can be days old (B&H's listed $849.99 was the
old price; the live page said $699.99 marked down). Some shops publish
their own product data for apps like this one: a live price, stock, no
wall, fully allowed. Each one here is a small adapter that returns the
same shape as a shopping listing, with live=True, and switches itself
on when its key is in the environment.

  Best Buy   BESTBUY_API_KEY   (free developer key, developer.bestbuy.com)

Adding a shop: one function returning offers, one line in FEEDS.
"""
from core import *                                   # noqa: F401,F403
from core import _re_scrub

BESTBUY_API_KEY = os.environ.get("BESTBUY_API_KEY", "")

# words that are not part of what the shop would search for
NOISE = {"a", "an", "the", "of", "for", "from", "at", "in", "on", "and",
         "with", "to", "price", "prices", "cost", "cheap", "cheapest",
         "buy", "new"}


def _words(item: str) -> list:
    out = []
    for w in _re_scrub.split(r"[^A-Za-z0-9&.-]+", item or ""):
        w = w.strip(".-").lower()
        if len(w) > 1 and w not in NOISE:
            out.append(w)
    return out[:6]


def _dollars(amount) -> str:
    try:
        return f"${float(amount):,.2f}"
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------- Best Buy
def _bestbuy_get(url: str) -> dict:
    """Its own function so a check can stand in for the network."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode())


def bestbuy_prices(item: str) -> list:
    """Best Buy's own catalogue: live price and whether it is in stock
    online, straight from the shop."""
    if not BESTBUY_API_KEY:
        return []
    words = _words(item)
    if not words:
        return []
    terms = "&".join("search=" + urllib.parse.quote(w) for w in words)
    url = ("https://api.bestbuy.com/v1/products((" + terms + "))"
           "?format=json&pageSize=5&sort=salePrice.asc"
           "&show=sku,name,salePrice,regularPrice,onlineAvailability,"
           "inStoreAvailability,url,freeShipping"
           "&apiKey=" + urllib.parse.quote(BESTBUY_API_KEY))
    try:
        d = _bestbuy_get(url)
    except Exception as e:
        emit("feed", "bestbuy", f"could not read: {str(e)[:120]}", "warn")
        return []
    out = []
    for p in d.get("products", [])[:5]:
        amount = p.get("salePrice")
        if amount is None:
            continue
        stock = ("in stock online" if p.get("onlineAvailability")
                 else "online: out of stock")
        if p.get("freeShipping"):
            stock += ", free delivery"
        was = p.get("regularPrice")
        out.append({
            "shop": "Best Buy",
            "title": (p.get("name") or "")[:120],
            "price": _dollars(amount),
            "amount": round(float(amount), 2),
            "was": _dollars(was) if was and float(was) > float(amount) else "",
            "delivery": stock,
            "link": (p.get("url") or "")[:400],
            "live": True,
        })
    return out


# Every live feed, by the shop name a caller says. The shop's own name is
# what the search listings call it too, so one line per shop replaces the
# listing for that shop.
FEEDS = {
    "best buy": bestbuy_prices,
}


def live_sources() -> list:
    """Which feeds are switched on - for the office, and for a check."""
    return [name for name, fn in FEEDS.items()
            if (name == "best buy" and BESTBUY_API_KEY)]


def live_prices(item: str, shop: str = "") -> list:
    """Live offers for an item from every switched-on feed, or from one
    shop's feed when the caller named the shop."""
    want = " ".join((shop or "").lower().replace("&", "and").split())
    out = []
    for name, fn in FEEDS.items():
        if want and name not in want and want not in name:
            continue
        try:
            out += fn(item)
        except Exception as e:
            emit("feed", name, f"failed: {str(e)[:120]}", "warn")
    return out
