"""What a page is telling us, read from its own words.

Are we signed in or signed out; is this a wall, and which kind -
a puzzle nobody may solve for a phone caller, a fingerprint, an
address refusal, a rate limit, a plain login wall; and where a site
says it sent a one-time code.

Pure text in, a word out. Nothing here touches a browser, so it can
be read and tested on its own - which is how "Walmart blocked us"
became five different problems with five different answers.
"""
from core import *                                   # noqa: F401,F403
from core import _re_scrub


SIGNED_OUT_MARKS = _re_scrub.compile(
    r"(?i)(sign in or create account|sign in to do more|"
    r"create your account|please sign in|log in to your account|"
    r"you.re signed out|track your order status)")


SIGNED_IN_MARKS = _re_scrub.compile(
    r"(?i)(deliver to|hello,\s*\w|your orders|account & lists|sign out|"
    r"my account|order history)")


def looks_signed_in(text: str) -> bool:
    """Fast path only: obvious English wording that needs no thinking.
    Never the last word - see signed_in()."""
    if not text:
        return False
    return bool(SIGNED_IN_MARKS.search(text)) and not looks_signed_out(text)


BOT_CHECK_MARKS = _re_scrub.compile(
    r"(?i)(press(ing)?\s*(and|&)\s*hold|activat\w*\s+and\s+hold|"
    r"hold\w*\s+(the\s+)?button|prove you.re (not a robot|human)|"
    r"(confirm|verify) (that )?you.?re (a )?human|"
    r"verify you are (a )?human|i.m not a robot|captcha|recaptcha|"
    r"unusual traffic from your|are you a robot|human verification)")


def looks_like_bot_check(text: str) -> bool:
    """The site is asking for a human. We do not try to get past these -
    we stop and say so. Recognising it early also saves burning every
    remaining step on a wall that will not move."""
    return bool(text and BOT_CHECK_MARKS.search(text))


CODE_DEST = _re_scrub.compile(
    r"(?i)(sent (?:the |a |your )?(?:code|otp)[^.<]{0,60}|"
    r"code (?:was )?sent to[^.<]{0,40}|"
    r"(?:emailed|texted|messaged|called) (?:it )?to[^.<]{0,40}|"
    r"(?:to|on) your (?:phone|email|mobile|number)[^.<]{0,40}|"
    r"(?:phone|email|number|address) (?:ending|ending in)[^.<]{0,20}|"
    r"\*{2,}[-\s]*\d{2,4})")

CODE_BAD = _re_scrub.compile(
    r"(?i)(code (?:you entered )?is not valid|invalid code|"
    r"incorrect code|wrong code|code (?:is )?expired|"
    r"couldn.t verify the code|enter a valid code)")


def code_destination(text: str) -> str:
    """Where a site says it sent its one-time code. A caller who is told
    only "they sent a code" has nowhere to look; the page nearly always
    says "to your phone ***-**96" and we were throwing that away."""
    m = CODE_DEST.search(text or "")
    return " ".join(m.group(0).split())[:120] if m else ""


def looks_signed_out(text: str) -> bool:
    """A page that shows sign-in prompts and no account name."""
    if not text:
        return False
    if SIGNED_OUT_MARKS.search(text):
        return True
    return False


# ---------------------------------------------------------------- blocks
# Which wall is it? "Walmart blocked us" is not actionable. A different
# address, a slower pace, a saved login and "this door does not open"
# are four different answers, and only the measurement tells them apart.


BLOCK_VENDORS = (
    ("perimeterx", r"(?i)(perimeterx|px-captcha|human security|"
                   r"press (and|&) hold)"),
    ("cloudflare", r"(?i)(cloudflare|cf-ray|checking your browser|"
                   r"attention required|error 10\d\d|"
                   r"enable javascript and cookies to continue)"),
    ("akamai", r"(?i)(akamai|reference #\d|access denied.{0,40}"
               r"reference)"),
    ("datadome", r"(?i)(datadome|geo\.captcha-delivery\.com)"),
    ("imperva", r"(?i)(incapsula|imperva|request unsuccessful|"
                r"pardon our interruption)"),
    ("recaptcha", r"(?i)(recaptcha|g-recaptcha|i.m not a robot)"),
    ("hcaptcha", r"(?i)hcaptcha"),
    ("arkose", r"(?i)(arkose|funcaptcha)"),
    ("aws_waf", r"(?i)(aws waf|awswaf)"),
    ("queue_it", r"(?i)queue-?it"),
)

# kind -> (what it is, can anything legitimate change it, what to do)
BLOCK_KINDS = {
    "puzzle": ("a puzzle for a human: press and hold, tick a box, pick "
               "pictures", False,
               "Nobody can do this for a caller with no screen. Use a "
               "sanctioned route (partner API, ACP) or have staff place "
               "the order."),
    "fingerprint": ("the site decided we are a robot from the browser "
                    "itself, with no puzzle offered", False,
                    "A different address will not help. This needs a "
                    "sanctioned route, or a person."),
    "ip_block": ("the address we came from is refused", True,
                 "Worth retrying from the caller's own region, or a "
                 "residential address. Check /browser/proxy_status."),
    "rate_limit": ("too many requests too quickly", True,
                   "Wait and try again more slowly. Nothing is wrong "
                   "with the account."),
    "geo_block": ("the site does not serve this country", True,
                  "Try the caller's own country."),
    "login_wall": ("it will not go further without an account", True,
                   "Save the customer's login for this site, then try "
                   "again."),
    "site_error": ("the site's own error page, not a block", True,
                   "Worth trying again shortly."),
    "unknown": ("refused, and it does not say why", False,
                "Look at the stored page text and name it properly."),
}

BLOCK_MARKS = (
    # order matters: a page can say several of these at once, and the
    # strongest signal must win. Amazon's silent refusal says "something
    # went wrong" AND "to discuss automated access" - it is not an outage.
    ("fingerprint", r"(?i)(to discuss automated access|automated queries|"
                    r"unusual activity from your|suspicious activity|"
                    r"bot detected|request looks automated|"
                    r"enable javascript and cookies|"
                    r"checking your browser)"),
    ("rate_limit", r"(?i)(too many requests|rate limit|"
                   r"slow down|try again in a (few|moment)|"
                   r"you have exceeded)"),
    ("geo_block", r"(?i)(not available in your (country|region)|"
                  r"geo.?restricted|unavailable in your country)"),
    ("login_wall", r"(?i)(sign in to (your account|continue|see)|"
                   r"please sign in|create an account to|"
                   r"log in to continue|members only)"),
    ("ip_block", r"(?i)(access denied|you don.t have permission to access|"
                 r"your ip address|blocked your ip|403 forbidden)"),
    ("site_error", r"(?i)(something went wrong|oops|please refresh|"
                   r"temporarily unavailable|internal server error|"
                   r"service unavailable)"),
)


def classify_block(text: str, url: str = "") -> dict:
    """What kind of wall this is, in a word, plus what would change it.

    The whole text is searched for a human check. A check inside a frame
    is added after the main page's words, and on B&H the menu alone fills
    the first 4,000 characters: the runner saw the check, this looked only
    at the menu, and the wall was filed as "unknown". What is kept is the
    part around the check, not the first lines of the page."""
    full = " ".join((text or "").split())
    body = full[:4000]
    check = BOT_CHECK_MARKS.search(full)
    near = full[max(0, check.start() - 200):check.end() + 300] if check \
        else body
    vendor = ""
    for name, pattern in BLOCK_VENDORS:
        if _re_scrub.search(pattern, near) or _re_scrub.search(pattern, body):
            vendor = name
            break
    kind = ""
    if check:
        kind = "puzzle"
        body = near
    if not kind:
        for name, pattern in BLOCK_MARKS:
            if _re_scrub.search(pattern, body):
                kind = name
                break
    if not kind and looks_signed_out(body):
        kind = "login_wall"
    if not kind:
        kind = "fingerprint" if vendor else "unknown"
    what, retry, advice = BLOCK_KINDS[kind]
    return {"kind": kind, "vendor": vendor, "what": what,
            "worth_retrying": retry, "advice": advice,
            "url": (url or "")[:300], "saw": body[:400]}


# The wall's kind decides the reason code the caller's side acts on. A
# login wall used to come back as "this site refuses robots", so nobody
# ever asked the customer for the login that would have opened it.
BLOCK_REASONS = {
    "puzzle": "bot_check",
    "fingerprint": "bot_check",
    "ip_block": "site_refused",
    "geo_block": "site_refused",
    "rate_limit": "rate_limited",
    "login_wall": "login_needed",
    "site_error": "site_error",
    "unknown": "bot_check",
}


def block_reason(kind: str) -> str:
    return BLOCK_REASONS.get(kind, "bot_check")


def record_block(account_id, site: str, text: str, url: str = "",
                 job_id=None) -> dict:
    """Name it, write it down, and say it once in the live log. Knowing
    Walmart is a fingerprint wall and Lowe's an address refusal is what
    decides where the ordering work goes."""
    got = classify_block(text, url)
    try:
        db = Session()
        db.add(Block(account_id=account_id or None, site=(site or "?")[:80],
                     job_id=job_id, kind=got["kind"],
                     vendor=got["vendor"], url=got["url"],
                     saw=scrub(got["saw"])))
        db.commit()
        db.close()
    except Exception:
        pass
    emit("block", site or "?",
         f"{got['kind']}"
         + (f" ({got['vendor']})" if got["vendor"] else "")
         + f": {got['what']}", "warn", account_id)
    return got
