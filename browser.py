"""Driving a real browser on a customer's behalf.

Where they call from decides where the browser appears; the session
is kept so a shop does not ask them to sign in twice; and every
page read goes through the safe helpers, because pages navigate
underneath you and a raw call crashes the job.

The runners: signing in to Google for them, signing in to a shop,
searching one, reading their orders, pursuing a plain-English goal
on a site nobody set up, and taking a basket as far as the review
page. What they must never do is in code, not in a prompt - the
buttons that spend money are refused unless the job was started to
buy, a wall is named rather than fought, and a job dies with the
call that started it.
"""
from core import *                                   # noqa: F401,F403
from core import (_re_scrub, _tz, _clock, _when, _JOBS, _b64,
                  _make_link_token, _fmt_address)
from ai import _openai_chat, _summarise_page
from rules import is_blocked, BLOCKED_REPLY
from signals import (looks_like_bot_check, looks_signed_in,
                     looks_signed_out, code_destination,
                     classify_block, record_block, block_reason,
                     CODE_BAD, BOT_CHECK_MARKS, SIGNED_OUT_MARKS,
                     SIGNED_IN_MARKS, CODE_DEST, BLOCK_KINDS)
from search import tool_web_search
from google_tools import (code_from_email, gmail_client,
                          google_client, pick_connection,
                          list_mailboxes)


DIAL_MAP = {
    "1876": ("JM", ""), "1809": ("DO", ""), "1829": ("DO", ""),
    "1849": ("DO", ""), "1868": ("TT", ""), "1246": ("BB", ""),
    "1242": ("BS", ""), "1441": ("BM", ""), "1345": ("KY", ""),
    "1264": ("AI", ""), "1721": ("SX", ""), "1758": ("LC", ""),
    "1473": ("GD", ""), "1784": ("VC", ""), "1268": ("AG", ""),
    "1670": ("MP", ""), "1671": ("GU", ""), "1787": ("PR", ""),
    "1939": ("PR", ""), "1340": ("VI", ""), "1684": ("AS", ""),
    "1204": ("CA", ""), "1226": ("CA", ""), "1236": ("CA", ""),
    "1249": ("CA", ""), "1250": ("CA", ""), "1289": ("CA", ""),
    "1306": ("CA", ""), "1343": ("CA", ""), "1365": ("CA", ""),
    "1367": ("CA", ""), "1403": ("CA", ""), "1416": ("CA", ""),
    "1418": ("CA", ""), "1431": ("CA", ""), "1437": ("CA", ""),
    "1438": ("CA", ""), "1450": ("CA", ""), "1506": ("CA", ""),
    "1514": ("CA", ""), "1519": ("CA", ""), "1548": ("CA", ""),
    "1579": ("CA", ""), "1581": ("CA", ""), "1587": ("CA", ""),
    "1604": ("CA", ""), "1613": ("CA", ""), "1639": ("CA", ""),
    "1647": ("CA", ""), "1672": ("CA", ""), "1705": ("CA", ""),
    "1709": ("CA", ""), "1778": ("CA", ""), "1780": ("CA", ""),
    "1782": ("CA", ""), "1807": ("CA", ""), "1819": ("CA", ""),
    "1825": ("CA", ""), "1867": ("CA", ""), "1873": ("CA", ""),
    "1902": ("CA", ""), "1905": ("CA", ""),
    "44": ("GB", ""), "353": ("IE", ""), "972": ("IL", ""),
    "61": ("AU", ""), "64": ("NZ", ""), "27": ("ZA", ""),
    "33": ("FR", ""), "49": ("DE", ""), "34": ("ES", ""),
    "39": ("IT", ""), "31": ("NL", ""), "32": ("BE", ""),
    "41": ("CH", ""), "43": ("AT", ""), "351": ("PT", ""),
    "46": ("SE", ""), "47": ("NO", ""), "45": ("DK", ""),
    "358": ("FI", ""), "48": ("PL", ""), "420": ("CZ", ""),
    "36": ("HU", ""), "30": ("GR", ""), "40": ("RO", ""),
    "380": ("UA", ""), "52": ("MX", ""), "55": ("BR", ""),
    "54": ("AR", ""), "56": ("CL", ""), "57": ("CO", ""),
    "51": ("PE", ""), "91": ("IN", ""), "63": ("PH", ""),
    "65": ("SG", ""), "60": ("MY", ""), "66": ("TH", ""),
    "62": ("ID", ""), "84": ("VN", ""), "81": ("JP", ""),
    "82": ("KR", ""), "852": ("HK", ""), "886": ("TW", ""),
    "971": ("AE", ""), "966": ("SA", ""), "90": ("TR", ""),
    "20": ("EG", ""), "234": ("NG", ""), "254": ("KE", ""),
    "1": ("US", PROXY_STATE),
}

# US area code -> state, so a New York caller browses from New York.
US_AREA_STATE = {
    "212": "NY", "315": "NY", "332": "NY", "347": "NY", "516": "NY",
    "518": "NY", "585": "NY", "607": "NY", "631": "NY", "646": "NY",
    "680": "NY", "716": "NY", "718": "NY", "838": "NY", "845": "NY",
    "914": "NY", "917": "NY", "929": "NY", "934": "NY",
    "201": "NJ", "551": "NJ", "609": "NJ", "640": "NJ", "732": "NJ",
    "848": "NJ", "856": "NJ", "862": "NJ", "908": "NJ", "973": "NJ",
    "203": "CT", "475": "CT", "860": "CT", "959": "CT",
    "215": "PA", "267": "PA", "412": "PA", "445": "PA", "484": "PA",
    "570": "PA", "610": "PA", "717": "PA", "724": "PA", "814": "PA",
    "878": "PA",
    "305": "FL", "321": "FL", "352": "FL", "386": "FL", "407": "FL",
    "561": "FL", "689": "FL", "727": "FL", "754": "FL", "772": "FL",
    "786": "FL", "813": "FL", "850": "FL", "863": "FL", "904": "FL",
    "941": "FL", "954": "FL",
    "213": "CA", "310": "CA", "323": "CA", "408": "CA", "415": "CA",
    "424": "CA", "510": "CA", "530": "CA", "559": "CA", "562": "CA",
    "619": "CA", "626": "CA", "650": "CA", "657": "CA", "661": "CA",
    "707": "CA", "714": "CA", "747": "CA", "760": "CA", "805": "CA",
    "818": "CA", "831": "CA", "858": "CA", "909": "CA", "916": "CA",
    "925": "CA", "949": "CA", "951": "CA",
    "312": "IL", "224": "IL", "331": "IL", "630": "IL", "708": "IL",
    "773": "IL", "779": "IL", "815": "IL", "847": "IL", "872": "IL",
    "214": "TX", "210": "TX", "281": "TX", "409": "TX", "469": "TX",
    "512": "TX", "682": "TX", "713": "TX", "737": "TX", "817": "TX",
    "832": "TX", "915": "TX", "936": "TX", "972": "TX",
    "404": "GA", "470": "GA", "678": "GA", "770": "GA", "706": "GA",
    "202": "DC", "410": "MD", "240": "MD", "301": "MD", "443": "MD",
    "617": "MA", "339": "MA", "351": "MA", "508": "MA", "774": "MA",
    "781": "MA", "857": "MA", "978": "MA",
    "216": "OH", "234": "OH", "330": "OH", "419": "OH", "440": "OH",
    "513": "OH", "614": "OH", "740": "OH", "937": "OH",
    "206": "WA", "253": "WA", "360": "WA", "425": "WA", "509": "WA",
    "303": "CO", "720": "CO", "970": "CO",
    "602": "AZ", "480": "AZ", "520": "AZ", "623": "AZ", "928": "AZ",
    "702": "NV", "725": "NV", "775": "NV",
    "704": "NC", "336": "NC", "252": "NC", "743": "NC", "910": "NC",
    "919": "NC", "980": "NC", "984": "NC",
    "313": "MI", "248": "MI", "269": "MI", "517": "MI", "586": "MI",
    "616": "MI", "734": "MI", "810": "MI", "947": "MI", "989": "MI",
}



def _browser_error(e) -> str:
    """Say what a failure during a browser job actually means. Not every
    HTTP error comes from Browserbase - the thinking is OpenAI's."""
    t = str(e)
    if "openai" in t.lower() or getattr(e, "_from_openai", False):
        if "401" in t:
            return ("OpenAI rejected the API key (401). OPENAI_API_KEY is "
                    "missing or wrong on the BACKEND service in Railway - "
                    "the voice agent having one is not enough.")
        if "429" in t:
            return "OpenAI is rate limiting or the account is out of credit."
        return f"The thinking step failed: {t[:160]}"
    if "401" in t:
        return ("Browserbase rejected the API key (401). Check "
                "BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID in Railway.")
    if "402" in t:
        return ("Browserbase says payment required (402) - the account is "
                "out of sessions or minutes. Check the Browserbase "
                "dashboard; no code change will fix it.")
    if "500" in t and "connect.browserbase" in t:
        return ("Browserbase could not start a browser (500). This normally "
                "follows the account running out of sessions or minutes - "
                "check the Browserbase dashboard.")
    if "429" in t:
        return "Too many browser sessions at once. Try again in a minute."
    if "timeout" in t.lower():
        return "The site took too long to respond."
    return f"Browser error: {t[:200]}"

def _where_for_phone(number: str):
    """Which country and state a browser should appear to be in for this
    caller. Falls back to the configured default."""
    digits = "".join(ch for ch in (number or "") if ch.isdigit())
    if not digits:
        return PROXY_COUNTRY, PROXY_STATE, ""
    for length in (4, 3, 2, 1):
        pre = digits[:length]
        if pre in DIAL_MAP:
            country, state = DIAL_MAP[pre]
            if country == "US":
                area = digits[1:4]
                state = US_AREA_STATE.get(area, PROXY_STATE)
            return country, state, ""
    return PROXY_COUNTRY, PROXY_STATE, ""


def _where_for_account(account_id):
    """Use the account's own phone number to decide where to browse from."""
    if not account_id:
        return PROXY_COUNTRY, PROXY_STATE, ""
    try:
        db = Session()
        pn = db.query(PhoneNumber).filter_by(account_id=account_id).first()
        db.close()
        if pn and pn.number:
            return _where_for_phone(pn.number)
    except Exception:
        pass
    return PROXY_COUNTRY, PROXY_STATE, ""


_LAST_PROXY_FLAG = {"at": None}



def _shape(secret: str) -> str:
    """Describe a password without revealing it: length and the pattern of
    character types. Lets us see a misheard password without storing one."""
    if not secret:
        return "(empty)"
    out = []
    for ch in secret:
        if ch.isupper():
            out.append("A")
        elif ch.islower():
            out.append("a")
        elif ch.isdigit():
            out.append("9")
        elif ch.isspace():
            out.append("_")
        else:
            out.append("#")
    return f"{len(secret)} chars, pattern {''.join(out)}"


PROXY_STATUS = {"proxies_enabled": None, "checked": None, "note": ""}


def _flag_proxy_unavailable(wanted: str):
    """Browserbase proxies aren't on this plan. Sessions still run from
    Browserbase's own datacenter, which is in the US."""
    PROXY_STATUS.update({"proxies_enabled": False,
                         "checked": datetime.utcnow(),
                         "note": "Browserbase returned 402 Payment Required"})
    emit("browser", "proxy", f"Proxies not enabled on the Browserbase plan. "
                             f"Wanted {wanted}; running from Browserbase's "
                             f"own US datacenter instead.", "warn")
    if wanted.upper() == "US":
        return              # US customers are unaffected, no alert needed
    now = datetime.utcnow()
    last = _LAST_PROXY_FLAG.get("at")
    if last and (now - last).total_seconds() < 3600:
        return
    _LAST_PROXY_FLAG["at"] = now
    msg = (f"A customer in {wanted} was browsed from a US address, because "
           f"proxies are not enabled on the Browserbase plan. Their sign-ins "
           f"will look foreign to Google. Turn on proxies in Browserbase to "
           f"fix this. US customers are not affected.")
    try:
        db = Session()
        db.add(Followup(reason="proxy_not_enabled", note=msg,
                        channel="system"))
        db.commit()
        db.close()
    except Exception:
        pass


_LAST_LIMIT_FLAG = {"at": None}


def _flag_account_limit(detail: str):
    """Browserbase refused a plain browser. Out of sessions, out of minutes,
    or a billing problem - nothing in this code can fix it."""
    msg = ("Browserbase will not start a browser at all (402). The account "
           "is out of sessions or minutes, or billing needs attention. "
           "Every browser job - site sign-ins, order lookups, ordering - "
           "will fail until it is sorted out in the Browserbase dashboard. "
           "Email, calendar, texts and normal conversation are unaffected.")
    emit("browser", "ACCOUNT LIMIT", f"{msg} ({detail[:120]})", "error")
    now = datetime.utcnow()
    last = _LAST_LIMIT_FLAG.get("at")
    if last and (now - last).total_seconds() < 1800:
        return
    _LAST_LIMIT_FLAG["at"] = now
    try:
        db = Session()
        db.add(Followup(reason="browser_account_limit", note=msg,
                        channel="system"))
        db.commit()
        db.close()
    except Exception:
        pass


def _flag_proxy_fallback(reason: str):
    """Loud: browsers are running outside the US until this is fixed."""
    msg = (f"Browser is not in the expected country. Sign-ins may look "
           f"foreign to Google and get blocked. Reason: {reason}")
    emit("browser", "PROXY FALLBACK", msg, "error")
    now = datetime.utcnow()
    last = _LAST_PROXY_FLAG.get("at")
    if last and (now - last).total_seconds() < 1800:
        return                      # don't spam the to-do list
    _LAST_PROXY_FLAG["at"] = now
    try:
        db = Session()
        db.add(Followup(reason="proxy_fallback", note=msg, channel="system"))
        db.commit()
        db.close()
    except Exception:
        pass


def _bb_session(context_id: str = "", country: str = "",
                state: str = "", city: str = "") -> str:
    """Create a Browserbase session pinned to the caller's own country.
    Returns the session id, or "" to fall back to a plain connection."""
    if not BROWSERBASE_API_KEY:
        return ""
    country = country or PROXY_COUNTRY
    geo = {"country": country}
    if state and country == "US":
        geo["state"] = state
    if city:
        geo["city"] = city
    body = {"projectId": BROWSERBASE_PROJECT_ID}
    if context_id:
        body["browserSettings"] = {"context": {"id": context_id,
                                               "persist": True}}

    # The SAME call asks for the proxy and creates the session that keeps
    # the customer logged in. Asking for a proxy we don't have used to fail
    # the whole call, so we lost the session too - and every job started
    # logged out, making the site demand a fresh code every single time.
    # Proxies are a nice-to-have; staying signed in is not.
    with_proxy = dict(body)
    with_proxy["proxies"] = [{"type": "browserbase", "geolocation": geo}]
    attempts = [(True, with_proxy), (False, body)]
    if PROXY_STATUS.get("proxies_enabled") is False:
        attempts = [(False, body)]          # already known, don't waste a call

    for wants_proxy, payload in attempts:
        try:
            req = urllib.request.Request(
                "https://api.browserbase.com/v1/sessions",
                data=json.dumps(payload).encode(),
                headers={"X-BB-API-Key": BROWSERBASE_API_KEY,
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                sid = json.loads(r.read().decode()).get("id", "")
            if sid:
                PROXY_STATUS.update({"proxies_enabled": wants_proxy,
                                     "checked": datetime.utcnow(),
                                     "note": "" if wants_proxy else
                                     "running without a proxy so the "
                                     "signed-in session survives"})
                return sid
        except Exception as e:
            detail = str(e)[:300]
            # urllib throws the response body away, and the body is where
            # Browserbase explains itself. Read it.
            try:
                if isinstance(e, urllib.error.HTTPError):
                    said = e.read().decode("utf-8", "replace")[:300]
                    if said:
                        detail = f"{detail} - {said}"
            except Exception:
                pass
            paid = "402" in detail or "Payment Required" in detail
            if wants_proxy and paid:
                _flag_proxy_unavailable(country)
                continue        # keep the session, drop the proxy
            if paid:
                # We asked for a plain browser and were still refused. That
                # is the ACCOUNT, not the geography - saying "wrong country"
                # here sent someone looking in completely the wrong place.
                _flag_account_limit(detail)
                return ""
            _flag_proxy_fallback(f"{detail} (wanted {country})")
            return ""
    return ""


def _bb_connect_url(context_id: str = "", account_id=None,
                    phone: str = "") -> str:
    """Prefer a session pinned to the caller's country; fall back to a
    direct connection."""
    if phone:
        country, state, city = _where_for_phone(phone)
    else:
        country, state, city = _where_for_account(account_id)
    sid = _bb_session(context_id, country, state, city)
    if sid:
        # _bb_session records whether a proxy was actually granted - don't
        # overwrite it here, or the status page reports proxies that the
        # plan never gave us.
        emit("browser", "proxy",
             f"browsing from {country}{'/' + state if state else ''}"
             f"{'' if PROXY_STATUS.get('proxies_enabled') else ' (no proxy)'}"
             f"{', session kept' if context_id else ''}",
             "info", account_id)
        return (f"wss://connect.browserbase.com?apiKey={BROWSERBASE_API_KEY}"
                f"&sessionId={sid}")
    url = (f"wss://connect.browserbase.com?apiKey={BROWSERBASE_API_KEY}"
           f"&projectId={BROWSERBASE_PROJECT_ID}")
    if context_id:
        url += f"&contextId={context_id}&persist=true"
    return url


def signed_in(page, why_for: str = "") -> tuple:
    """Is this page showing the customer their own account? Returns
    (yes_or_no, reason).

    Deliberately not a list of phrases. Word lists only ever describe the
    shops someone has already added, in the language they added them in.
    This works in three stages, cheapest first:

      1. A password box on the page means we are still at the door.
      2. Wording that obviously settles it either way - no model call.
      3. Anything else: the model looks at the page, in any language.

    When it genuinely cannot tell, it answers YES. Wrongly claiming a
    session expired sends the customer through a sign-in they didn't need
    and files a job for the office; wrongly proceeding just reads a page
    that turns out to have nothing on it. The first mistake is worse."""
    text = page_text(page, 1500)
    if q(page, 'input[type="password"]'):
        return False, "there is still a password box on the page"
    if looks_signed_out(text):
        return False, "the page is asking them to sign in"
    if looks_signed_in(text):
        return True, "the page is showing their account"
    if not OPENAI_API_KEY or not text:
        return True, "no clear sign either way - carrying on"

    msg = (f"URL: {page_url(page)}\n\nPAGE TEXT:\n{text}\n\n"
           f"Is this person signed in to their own account on this site?\n"
           f"Seeing anything personal - their name, address, orders, "
           f"balance, saved details - means yes. A sign-in, registration "
           f"or password form means no. The page may be in any language, "
           f"and may be a site you have never seen.\n"
           f'Reply with JSON only: {{"signed_in": true, "why": "..."}}')
    try:
        d = _openai_chat(model=MODEL_BROWSER, cheap=False, messages=[
            _user_turn(msg, page_shot(page) if BROWSER_VISION else "")])
        got = _first_json(d["choices"][0]["message"].get("content") or "")
        if "signed_in" in got:
            return bool(got["signed_in"]), str(got.get("why", ""))[:160]
    except Exception as e:
        emit("browser", "signed_in", f"could not judge the page "
                                     f"({why_for}): {str(e)[:120]}", "warn")
    return True, "could not tell - carrying on rather than blocking them"


# ---------------------------------------------------- safe page operations
# Pages navigate under us constantly (Google, checkout flows). Every read of
# a live page goes through these so a navigation is a retry, not a crash.


# ---------------------------------------------------- safe page operations
# Pages navigate under us constantly (Google, checkout flows). Every read of
# a live page goes through these so a navigation is a retry, not a crash.

def _is_nav_error(e) -> bool:
    t = str(e).lower()
    return ("context was destroyed" in t or "navigation" in t
            or "target closed" in t or "frame was detached" in t)


def settle(page, ms: int = 1200):
    """Let any in-flight navigation finish."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    try:
        page.wait_for_timeout(ms)
    except Exception:
        pass


def q(page, selector):
    """query_selector that survives a navigation mid-check."""
    for _ in range(3):
        try:
            return page.query_selector(selector)
        except Exception as e:
            if _is_nav_error(e):
                settle(page)
                continue
            return None
    return None


def q_all(page, selector):
    for _ in range(3):
        try:
            return page.query_selector_all(selector)
        except Exception as e:
            if _is_nav_error(e):
                settle(page)
                continue
            return []
    return []


def page_text(page, limit: int = 4000) -> str:
    """The visible text, trimmed INSIDE the browser. Pulling a whole shop
    page across the network and then keeping the first 4000 characters was
    costing seconds per step.

    It takes the page's MAIN content where the page marks one. Otherwise
    the first thousand characters of every shop page are its menu -
    departments, sign-in, gift cards - and a model given that went hunting
    for a "details" section that had been in front of it all along."""
    js = (r"(n) => { const m = document.querySelector("
          r"'main, [role=main], #main-content, #content, #main, "
          r"[id*=product-detail], article, "
          r"#dp-container, #centerCol, #search') "
          r"|| document.body; "
          r"const t = (m && m.innerText ? m.innerText : "
          r"(document.body ? document.body.innerText : '')); "
          r"return t.replace(/\s+/g, ' ').slice(0, n); }")
    got = page_eval(page, js, limit)
    if got:
        return got
    for _ in range(2):
        try:
            return " ".join((page.inner_text("body") or "").split())[:limit]
        except Exception as e:
            if _is_nav_error(e):
                settle(page)
                continue
            return ""
    return ""


def page_url(page) -> str:
    try:
        return page.url
    except Exception:
        return ""


def do_click(page, el, wait_ms: int = 3500) -> bool:
    try:
        el.click()
    except Exception as e:
        if not _is_nav_error(e):
            return False
    settle(page, wait_ms)
    return True


def do_fill(page, el, value: str, press_enter: bool = False,
            wait_ms: int = 3500) -> bool:
    try:
        el.fill(value)
        if press_enter:
            page.keyboard.press("Enter")
    except Exception as e:
        if not _is_nav_error(e):
            return False
    settle(page, wait_ms)
    return True


def do_goto(page, url: str, wait_ms: int = 3500) -> bool:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        if not _is_nav_error(e):
            return False
    settle(page, wait_ms)
    return True


def page_eval(page, js, arg=None):
    """Run JavaScript inside the page, surviving a navigation.

    One call that does the work in the browser beats hundreds of calls that
    each cross the network to it."""
    for _ in range(3):
        try:
            return (page.evaluate(js, arg) if arg is not None
                    else page.evaluate(js))
        except Exception as e:
            if _is_nav_error(e):
                settle(page)
                continue
            return None
    return None


def page_shot(page) -> str:
    """A JPEG of what the page looks like right now, base64 encoded.
    Returns "" if it can't be taken - never raises, never blocks a job."""
    for _ in range(2):
        try:
            raw = page.screenshot(type="jpeg", quality=50, timeout=15000)
            return _b64.b64encode(raw).decode()
        except Exception as e:
            if _is_nav_error(e):
                settle(page)
                continue
            return ""
    return ""


def do_back(page, wait_ms: int = 3000) -> bool:
    """Back out of a dead end instead of getting stuck on it."""
    try:
        page.go_back(wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        if not _is_nav_error(e):
            return False
    settle(page, wait_ms)
    return True


# --------------------------------------------------- assisted Gmail sign-in
# The customer's Google password lives in memory for the length of one
# sign-in and is never written to the database or logged.

_PENDING = {}          # session_id -> {"password": str, "code": str|None}


def _ob_set(sid: int, state: str, message: str = "", reason: str = ""):
    emit("signin", f"signin {sid}", f"{state}: {message}",
         "error" if state == "failed" else "info")
    db = Session()
    row = db.query(Onboard).filter_by(id=sid).first()
    if row:
        row.state = state
        row.message = message[:2000]
        row.reason = reason[:40]
        stamp = datetime.utcnow().strftime("%H:%M:%S")
        line = f"[{stamp}] {state}: {message[:300]}"
        row.history = ((row.history or "") + line + "\n")[-6000:]
        db.commit()
    db.close()


def _run_signin(sid: int, account_id: int, email: str):
    """Drive a hosted browser through Google sign-in, any 2FA, and consent."""
    import re as _re
    from playwright.sync_api import sync_playwright

    creds = _PENDING.get(sid) or {}
    password = creds.get("password", "")
    if not password:
        _ob_set(sid, "failed", "No password supplied.")
        return

    ws = _bb_connect_url(account_id=account_id)

    EMAIL_SEL = ('input[type="email"], input#identifierId, '
                 'input[name="identifier"]')
    PW_SEL = ('input[type="password"], input[name="Passwd"], '
              'input[name="password"]')
    CODE_SEL = ('input[type="tel"], input[name="totpPin"], input#idvPin, '
                'input[name="Pin"], input[name="code"], '
                'input[autocomplete="one-time-code"], '
                'input[aria-label*="code" i]')

    def screen(page):
        return page_text(page, 6000)

    def where(page):
        return f"url={page_url(page)[:110]} | screen: {screen(page)[:260]}"

    def wants_tap(page):
        return q(page, 'text=/Check your device|Tap Yes on the notification|'
                       'Open the Gmail app|notification to your/i')

    def tap_number(page):
        """Google shows a two-digit number the caller must pick on their
        phone. It renders a moment after the screen appears, so look in a
        few places and don't give up on the first miss."""
        # The number is usually its own large element.
        for sel in ('div[jsname] span:text-matches("^[0-9]{1,3}$")',
                    'samp', 'strong:text-matches("^[0-9]{1,3}$")',
                    '*[aria-live] >> text=/^[0-9]{1,3}$/'):
            el = q(page, sel)
            if el:
                try:
                    t = (el.inner_text() or "").strip()
                    if t.isdigit() and 1 <= len(t) <= 3:
                        return t
                except Exception:
                    pass
        body = screen(page)
        for pat in (r"tap\s+(\d{1,3})\b",
                    r"select\s+(\d{1,3})\b",
                    r"number\s+(\d{1,3})\b",
                    r"\b(\d{1,3})\b\s*Check your device",
                    r"Check your device.{0,160}?\b(\d{1,3})\b",
                    r"tablet.{0,160}?\b(\d{1,3})\b"):
            m = _re.search(pat, body, _re.I)
            if m:
                return m.group(1)
        return ""

    def wait_for_tap_number(page, tries: int = 6):
        """The number can take a second or two to render."""
        for _ in range(tries):
            n = tap_number(page)
            if n:
                return n
            settle(page, 1200)
        return ""

    def describe_code_screen(page):
        """Say where the code went, so the caller knows what to look for."""
        body = screen(page)
        if _re.search(r"enter your (device |phone )?(pin|passcode)|"
                      r"screen lock|unlock your (phone|device)", body, _re.I):
            return ("Google wants the PIN or passcode they use to unlock "
                    "their own phone. If they don't want to give that, "
                    "offer try_another_way.")
        if _re.search(r"authenticator|Google Authenticator", body, _re.I):
            return ("Google wants the 6-digit code from their authenticator "
                    "app. Ask them to open it and read the current code.")
        if _re.search(r"backup code|recovery code", body, _re.I):
            return "Google wants one of their backup codes."
        if _re.search(r"security key|USB|tap your key", body, _re.I):
            return ("Google wants a physical security key, which we can't do. "
                    "Offer try_another_way.")
        tail = _re.search(r"(?:ending in|\u2022{2,}\s*)(\d{2,4})", body)
        if _re.search(r"call|voice", body, _re.I) and _re.search(
                r"code", body, _re.I):
            return ("Google is calling their phone with a spoken code. Ask "
                    "them to answer and read it out.")
        if tail:
            return (f"Google texted a code to the number ending {tail.group(1)}."
                    f" Ask them to read it out.")
        return "Google sent a code. Ask them to read it out."

    def pick_another_method(page):
        """Open 'Try another way' and choose a text/call option if offered."""
        try:
            alt = (q(page, 'text=/Try another way/i')
                   or q(page, 'text=/More ways to verify/i')
                   or q(page, 'text=/Try another method/i'))
            if not alt:
                return False
            do_click(page, alt, 3500)
            for sel in ('text=/Get a verification code at/i',
                        'text=/Text message/i',
                        'text=/Send code/i',
                        'text=/Get a code|verification code/i',
                        'text=/Phone call/i'):
                opt = q(page, sel)
                if opt:
                    do_click(page, opt, 3500)
                    return True
            return True          # menu is open; caller can be told options
        except Exception:
            return False

    def wait_for_code(page, sid, note):
        """Sit on a code screen until the caller supplies one."""
        _ob_set(sid, "needs_code", note)
        waited = 0
        while waited < 240:
            time.sleep(3)
            waited += 3
            st = _PENDING.get(sid) or {}
            if st.get("cancelled"):
                return "cancelled"
            if st.get("other_way"):
                _PENDING[sid]["other_way"] = False
                if pick_another_method(page):
                    page.wait_for_timeout(2000)
                    if wants_tap(page):
                        return "tap"
                    _ob_set(sid, "needs_code", describe_code_screen(page))
                continue
            code = st.get("code")
            if code:
                _PENDING[sid]["code"] = None
                code_el = q(page, CODE_SEL)
                if code_el:
                    do_fill(page, code_el, code, True, 5000)
                else:
                    settle(page, 4000)
                if q(page, 'text=/Wrong code|incorrect code|try again/i'):
                    _ob_set(sid, "needs_code",
                            "That code didn't work. Ask them to read it "
                            "again, or call try_another_way.")
                    continue
                return "ok"
        return "timeout"

    page = None
    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws)
            ctx = browser.contexts[0] if browser.contexts \
                else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(45000)

            db = Session()
            before_ids = {c.id for c in db.query(Connection)
                          .filter_by(account_id=account_id,
                                     provider="google").all()}
            db.close()

            _ob_set(sid, "signing_in", "Opening Google.")
            # trusted, server-side, so it mints its own ticket and skips
            # the confirmation page a person would see
            do_goto(page, f"{PUBLIC_URL}/link/start?go=1&t="
                          + urllib.parse.quote(_make_link_token(account_id)),
                    4000)

            other = q(page, 'text=/Use another account/i')
            if other:
                do_click(page, other, 2500)

            try:
                page.wait_for_selector(EMAIL_SEL, timeout=30000)
            except Exception:
                _ob_set(sid, "failed", "No email box. " + where(page))
                browser.close()
                return

            email_el = q(page, EMAIL_SEL)
            if not email_el:
                _ob_set(sid, "failed", "No email box. " + where(page))
                browser.close()
                return
            do_fill(page, email_el, email, True, 3500)

            try:
                page.wait_for_selector(PW_SEL, timeout=30000)
            except Exception:
                _ob_set(sid, "failed", "No password box. " + where(page))
                browser.close()
                return

            pw_el = q(page, PW_SEL)
            if not pw_el:
                _ob_set(sid, "failed", "No password box. " + where(page))
                browser.close()
                return
            emit("signin", f"signin {sid}",
                 f"typing password: {_shape(password)}")
            do_fill(page, pw_el, password, True, 5000)

            if q(page, 'text=/Wrong password/i'):
                emit("signin", f"signin {sid}",
                     f"Google rejected the password ({_shape(password)}). "
                     f"Compare that pattern with the real one - A is a "
                     f"capital, a is lowercase, 9 is a digit, # is a symbol.",
                     "warn")
                _ob_set(sid, "failed",
                        "Google says the password is wrong. Ask them to say "
                        "it again slowly, or have someone call them back.",
                        reason="bad_password")
                browser.close()
                return

            if q(page, 'text=/couldn.t sign you in|browser or app may not be '
                       'secure|unusual activity/i'):
                _ob_set(sid, "failed",
                        "Google blocked the automated sign-in. " + where(page))
                browser.close()
                return

            def on_challenge(page):
                """Still stuck on a Google verification screen?"""
                u = page_url(page)
                if "/challenge" in u or "/signin/v2/challenge" in u:
                    return True
                return bool(q(page, 'text=/Verify it.s you|Choose a way to '
                                    'verify|2-Step Verification/i'))

            def pick_from_selection(page):
                """On 'Choose a way to verify', pick something we can do:
                a texted code first, then a voice call, then anything."""
                for sel in ('text=/Get a verification code at/i',
                            'text=/Text message/i',
                            'text=/Send a text message/i',
                            'text=/Get a code.{0,40}(text|SMS)/i',
                            'text=/Phone call/i',
                            'text=/Call.{0,20}(instead|me)/i',
                            'text=/Google Authenticator/i',
                            'text=/backup code/i'):
                    el = q(page, sel)
                    if el:
                        emit("signin", f"signin {sid}",
                             f"Choosing verification method: {sel}")
                        do_click(page, el, 4000)
                        return True
                return False

            # ---- verification, whichever form it takes
            for _round in range(6):
                if "/link/callback" in page_url(page):
                    break
                if (q(page, 'text=/Choose a way to verify/i')
                        and not q(page, CODE_SEL)):
                    _ob_set(sid, "verifying",
                            "Google is asking how to verify. Picking a "
                            "texted code.")
                    if pick_from_selection(page):
                        settle(page, 3000)
                        continue
                    _ob_set(sid, "failed",
                            "Google offered no verification method we can "
                            "use. " + where(page))
                    browser.close()
                    return

                if wants_tap(page):
                    num = wait_for_tap_number(page)
                    if num:
                        _ob_set(sid, "needs_tap",
                                f"Google sent a prompt to their phone. Tell "
                                f"them to tap Yes and choose the number "
                                f"{num}.")
                    else:
                        emit("signin", f"signin {sid}",
                             f"No number found on the tap screen. Screen "
                             f"text: {screen(page)[:400]}", "warn")
                        _ob_set(sid, "needs_tap",
                                "Google sent a prompt to their phone. Tell "
                                "them to tap Yes, and to read out the number "
                                "shown on their own phone if it asks for one. "
                                "If they already missed it, use "
                                "try_another_way.")
                    waited = 0
                    switched = False
                    while waited < 200:
                        time.sleep(4)
                        waited += 4
                        if (_PENDING.get(sid) or {}).get("cancelled"):
                            _ob_set(sid, "failed", "The caller hung up.",
                                    reason="cancelled")
                            browser.close()
                            return
                        # the number sometimes renders after the first look
                        if not num:
                            num = tap_number(page)
                            if num:
                                _ob_set(sid, "needs_tap",
                                        f"The number is {num}. Tell them to "
                                        f"choose {num} on their phone.")
                        if (_PENDING.get(sid) or {}).get("other_way"):
                            _PENDING[sid]["other_way"] = False
                            if pick_another_method(page):
                                switched = True
                                break
                        # approving navigates the page — that's success
                        if "/link/callback" in page_url(page):
                            break
                        if not wants_tap(page):
                            settle(page, 2000)
                            if not wants_tap(page):
                                break
                    if switched:
                        continue
                    if wants_tap(page):
                        _ob_set(sid, "failed",
                                "They never approved the prompt.")
                        browser.close()
                        return
                    page.wait_for_timeout(3000)
                    continue

                if q(page, CODE_SEL):
                    res = wait_for_code(page, sid, describe_code_screen(page))
                    if res == "cancelled":
                        _ob_set(sid, "failed", "The caller hung up.",
                                reason="cancelled")
                        browser.close()
                        return
                    if res == "timeout":
                        _ob_set(sid, "failed", "Timed out waiting for a code.")
                        browser.close()
                        return
                    if res == "tap":
                        continue
                    page.wait_for_timeout(2000)
                    continue
                break

            _ob_set(sid, "consenting", "Approving access.")

            def consent_screen(page):
                """What Google is showing us right now."""
                t = screen(page)
                u = page_url(page)
                if "/link/callback" in u:
                    return "done", t
                if _re.search(r"hasn.t verified this app|being tested|"
                              r"unverified app", t, _re.I):
                    return "unverified", t
                if _re.search(r"wants access to your Google Account|"
                              r"Select what .* can access|"
                              r"See, edit, download", t, _re.I):
                    return "scopes", t
                if _re.search(r"Choose an account|Select an account", t,
                              _re.I):
                    return "chooser", t
                return "other", t

            approved = False
            for attempt in range(24):
                kind, text = consent_screen(page)
                emit("signin", f"signin {sid}",
                     f"consent screen [{kind}] {text[:160]}")
                if kind == "done":
                    approved = True
                    break

                if kind == "unverified":
                    # "Continue" is sometimes hidden behind "Advanced".
                    for sel in ('button:has-text("Continue")',
                                'span:has-text("Continue")',
                                'div[role="button"]:has-text("Continue")',
                                'text=/^Continue$/'):
                        el = q(page, sel)
                        if el:
                            do_click(page, el, 3000)
                            break
                    else:
                        adv = q(page, 'text=/^Advanced$/')
                        if adv:
                            do_click(page, adv, 2000)
                            unsafe = q(page, 'text=/Go to .*unsafe/i')
                            if unsafe:
                                do_click(page, unsafe, 3000)
                    continue

                if kind == "chooser":
                    acct = q(page, f'text=/{_re.escape(email)}/i')
                    if acct:
                        do_click(page, acct, 3000)
                    continue

                if kind == "scopes":
                    # tick "Select all" if the boxes aren't already on
                    for sel in ('text=/^Select all$/',
                                'input[type="checkbox"][aria-label*="all" i]'):
                        el = q(page, sel)
                        if el:
                            do_click(page, el, 1200)
                            break
                    for box in q_all(page, 'input[type="checkbox"]'):
                        try:
                            if not box.is_checked():
                                box.check(timeout=3000)
                        except Exception:
                            pass
                    for sel in ('button:has-text("Continue")',
                                'button:has-text("Allow")',
                                'span:has-text("Continue")',
                                'span:has-text("Allow")',
                                'div[role="button"]:has-text("Continue")'):
                        el = q(page, sel)
                        if el:
                            do_click(page, el, 3000)
                            break
                    continue

                # unknown screen: try the usual buttons, then wait
                clicked = False
                for sel in ('button:has-text("Continue")',
                            'button:has-text("Allow")',
                            'button:has-text("Next")',
                            'span:has-text("Continue")',
                            'span:has-text("Allow")',
                            'div[role="button"]:has-text("Continue")'):
                    el = q(page, sel)
                    if el:
                        do_click(page, el, 3000)
                        clicked = True
                        break
                if not clicked:
                    settle(page, 2500)

            db = Session()
            rows = (db.query(Connection)
                      .filter_by(account_id=account_id, provider="google")
                      .all())
            want = (email or "").strip().lower()
            match = next((c for c in rows
                          if (c.email or "").lower() == want), None)
            fresh = [c for c in rows if c.id not in before_ids]
            db.close()
            final = where(page)
            browser.close()

            if match:
                _ob_set(sid, "done", f"Connected {match.email}.")
            elif fresh:
                _ob_set(sid, "failed",
                        f"Signed in as {fresh[0].email}, not {email}. "
                        f"Google was already signed into another account. "
                        f"Try again.")
            elif _re.search(r"hasn.t verified this app|being tested", final,
                            _re.I):
                _ob_set(sid, "failed",
                        "Stuck on Google's 'app not verified' warning. The "
                        "Continue button could not be clicked. This account "
                        "may not be on the app's test-user list in Google "
                        "Cloud Console. " + final[:250])
            elif "/challenge" in final or "Verify it" in final:
                _ob_set(sid, "failed",
                        "Google is still asking to verify and we ran out of "
                        "attempts. The phone prompt expired. Try again and "
                        "tap Yes as soon as it appears. " + final[:300])
            else:
                _ob_set(sid, "failed", "Consent not completed. " + final)
    except Exception as e:
        detail = ""
        try:
            if page:
                detail = " " + where(page)
        except Exception:
            pass
        _ob_set(sid, "failed", f"Browser error: {str(e)[:150]}{detail}")
        try:
            if browser:
                browser.close()
        except Exception:
            pass
    finally:
        _PENDING.pop(sid, None)      # password gone from memory


def revoke_google(blob: str) -> bool:
    """Tell Google to invalidate the token, so access really ends."""
    try:
        tok = vault_get(blob)
        t = tok.get("refresh_token") or tok.get("token")
        if not t:
            return False
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/revoke",
            data=urllib.parse.urlencode({"token": t}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False


def disconnect_mailbox(account_id: int, which: str = "") -> dict:
    """Revoke and remove one mailbox."""
    db = Session()
    rows = (db.query(Connection)
              .filter_by(account_id=account_id, provider="google").all())
    if not rows:
        db.close()
        return {"removed": False, "reason": "nothing connected"}

    target = None
    if which:
        w = which.strip().lower()
        target = next((r for r in rows
                       if w == (r.email or "").lower()
                       or w == (r.label or "").lower()), None)
        if not target:
            target = next((r for r in rows
                           if w in (r.email or "").lower()
                           or w in (r.label or "").lower()), None)
        if not target:
            db.close()
            return {"removed": False, "reason": f"no mailbox like '{which}'"}
    else:
        if len(rows) > 1:
            db.close()
            return {"removed": False, "reason": "several mailboxes — ask which",
                    "mailboxes": [r.email for r in rows]}
        target = rows[0]

    email = target.email
    was_default = bool(target.is_default)
    revoked = revoke_google(target.secret_blob)
    db.delete(target)
    db.commit()

    left = (db.query(Connection)
              .filter_by(account_id=account_id, provider="google").all())
    if was_default and left:
        left[0].is_default = 1
        db.commit()
    db.close()
    return {"removed": True, "email": email, "revoked_at_google": revoked,
            "remaining": len(left)}


def delete_everything(account_id: int) -> dict:
    """Remove the customer and every trace of them."""
    db = Session()
    conns = db.query(Connection).filter_by(account_id=account_id).all()
    for c in conns:
        revoke_google(c.secret_blob)
        db.delete(c)

    for model in (Memory, Onboard, PhoneNumber, SiteLogin,
                  SiteSession, Job, Address, PaymentCard, Order):
        for row in db.query(model).filter_by(account_id=account_id).all():
            db.delete(row)

    calls = db.query(Call).filter_by(account_id=account_id).all()
    for call in calls:
        for t in db.query(CallTurn).filter_by(call_id=call.id).all():
            db.delete(t)
        db.delete(call)

    acct = db.query(Account).filter_by(id=account_id).first()
    if acct:
        db.delete(acct)
    db.commit()
    db.close()
    return {"deleted": True, "mailboxes": len(conns), "calls": len(calls)}



def save_site_login(account_id: int, site: str, username: str,
                    password: str) -> dict:
    """Store or replace one site login.

    A blank password NEVER overwrites a stored one. A caller asked to
    change only his username; the assistant called this with an empty
    password, and his real password was replaced with nothing - so the
    next sign-in reported 'no saved login' for an account that was there
    all along."""
    db = Session()
    row = (db.query(SiteLogin)
             .filter_by(account_id=account_id, site=site.lower()).first())

    if not (password or "").strip():
        if not row:
            db.close()
            raise HTTPException(
                400, "A new login needs a password as well as a username.")
        if username.strip():
            row.username = username.strip()
        row.at = datetime.utcnow()
        db.commit()
        out = {"saved": True, "site": site.lower(), "username": row.username,
               "password_unchanged": True}
        db.close()
        return out

    blob = vault_put({"password": password})
    if row:
        row.username = username
        row.secret_blob = blob
        row.at = datetime.utcnow()
    else:
        db.add(SiteLogin(account_id=account_id, site=site.lower(),
                         username=username, secret_blob=blob))
    db.commit()
    db.close()
    return {"saved": True, "site": site.lower(), "username": username}


def use_site_login(account_id: int, site: str, purpose: str = "") -> dict:
    """Decrypt for one use. Server-side only — never sent to a client."""
    db = Session()
    row = (db.query(SiteLogin)
             .filter_by(account_id=account_id, site=site.lower()).first())
    if not row:
        db.close()
        return {}
    data = vault_get(row.secret_blob)
    row.last_used = datetime.utcnow()
    row.use_count = (row.use_count or 0) + 1
    db.add(SecretAccess(account_id=account_id, site=site.lower(),
                        purpose=purpose[:120]))
    db.commit()
    out = {"username": row.username, "password": data.get("password", "")}
    db.close()
    return out


def list_site_logins(account_id: int) -> list:
    db = Session()
    rows = db.query(SiteLogin).filter_by(account_id=account_id).all()
    out = [{"site": r.site, "username": r.username,
            "saved": local_str(r.at, "day") if r.at else "",
            "used": r.use_count or 0} for r in rows]
    db.close()
    return out


def forget_site_login(account_id: int, site: str) -> dict:
    db = Session()
    rows = (db.query(SiteLogin)
              .filter_by(account_id=account_id, site=site.lower()).all())
    for r in rows:
        db.delete(r)
    db.commit()
    db.close()
    return {"removed": len(rows), "site": site.lower()}



# ------------------------------------------------------------ site logins
# Per-site hints. Selectors are deliberately loose; Google/Amazon change
# their pages often, so we try several and fail with the screen text.

SITES = {
    "amazon": {
        "login_url": "https://www.amazon.com/ap/signin?openid.mode=checkid_setup"
                     "&openid.identity=http://specs.openid.net/auth/2.0/"
                     "identifier_select&openid.claimed_id=http://specs.openid"
                     ".net/auth/2.0/identifier_select&openid.assoc_handle="
                     "usflex&openid.ns=http://specs.openid.net/auth/2.0"
                     "&openid.return_to=https://www.amazon.com/",
        "user_sel": 'input[type="email"], input#ap_email, input[name="email"]',
        "pass_sel": 'input[type="password"], input#ap_password',
        "next_sel": 'input#continue, input#signInSubmit',
        "ok_sel": '#nav-link-accountList, text=/Hello,/i',
        "otp_sel": 'input#auth-mfa-otpcode, input[name="otpCode"], '
                   'input[autocomplete="one-time-code"]',
    },
    "walmart": {
        "login_url": "https://www.walmart.com/account/login",
        "user_sel": 'input[type="email"], input#email',
        "pass_sel": 'input[type="password"], input#password',
        "next_sel": 'button[type="submit"]',
        "ok_sel": 'text=/Account|Sign out/i',
        "otp_sel": 'input[autocomplete="one-time-code"], input[name="code"]',
    },
    "temu": {
        "login_url": "https://www.temu.com/login.html",
        "user_sel": 'input[type="email"], input[name="email"], '
                    'input[placeholder*="mail" i]',
        "pass_sel": 'input[type="password"]',
        "next_sel": 'button[type="submit"], div[role="button"]:has-text("Continue")',
        "ok_sel": 'text=/Account|Sign out|Orders/i',
        "otp_sel": 'input[autocomplete="one-time-code"], input[name="code"]',
    },
}


# How many browsers may run at once. Keep at or below your Browserbase plan's
# concurrency limit; everything else waits in line.
MAX_BROWSERS = int(os.environ.get("MAX_BROWSERS", "5"))
_slots = threading.Semaphore(MAX_BROWSERS)
_queue_lock = threading.Lock()
_waiting = 0


_JOB_STARTED = {}


def _job_set(jid: int, state: str, message: str = "", reason: str = ""):
    if state in ("opening", "signing_in") and jid not in _JOB_STARTED:
        _JOB_STARTED[jid] = time.time()
    if state in ("done", "failed") and jid in _JOB_STARTED:
        secs = int(time.time() - _JOB_STARTED.pop(jid))
        try:
            db2 = Session()
            job = db2.query(Job).filter_by(id=jid).first()
            acc = job.account_id if job else None
            db2.close()
            record_usage(account_id=acc, kind="browser",
                         browser_seconds=secs)
        except Exception:
            pass
    emit("job", f"job {jid}", f"{state}: {message}",
         "error" if state == "failed" else "info")
    db = Session()
    row = db.query(Job).filter_by(id=jid).first()
    if row:
        row.state = state
        # 500 characters silently cut every answer that was a list: the
        # saved Amazon addresses stopped mid-word, and a list of cards
        # stopped at "Visa ending 6125, expi". The column is TEXT.
        row.message = message[:4000]
        row.reason = reason[:40]
        stamp = datetime.utcnow().strftime("%H:%M:%S")
        row.history = ((row.history or "") +
                       f"[{stamp}] {state}: {message[:300]}\n")[-6000:]
        if state in ("done", "failed"):
            row.done_at = datetime.utcnow()
        db.commit()
    db.close()


def _get_context(account_id: int, site: str):
    """Reuse a saved browser context so cookies persist between runs."""
    db = Session()
    row = (db.query(SiteSession)
             .filter_by(account_id=account_id, site=site).first())
    ctx_id = row.context_id if row else ""
    db.close()
    return ctx_id


def _forget_context(account_id: int, site: str) -> bool:
    """Throw away a saved browser identity so the next run starts clean.

    A context keeps cookies - including whatever the site decided about
    you at the time. Ours were created from a datacenter in Seattle, so
    Target kept showing a Seattle store to a New York customer long after
    the proxy was fixed."""
    db = Session()
    rows = (db.query(SiteSession)
              .filter_by(account_id=account_id, site=(site or "").lower())
              .all())
    for r in rows:
        db.delete(r)
    db.commit()
    db.close()
    return bool(rows)


def _save_context(account_id: int, site: str, ctx_id: str):
    db = Session()
    row = (db.query(SiteSession)
             .filter_by(account_id=account_id, site=site).first())
    if row:
        row.context_id = ctx_id
        row.last_ok = datetime.utcnow()
    else:
        db.add(SiteSession(account_id=account_id, site=site,
                           context_id=ctx_id, last_ok=datetime.utcnow()))
    db.commit()
    db.close()


def _new_browserbase_context() -> str:
    """Ask Browserbase for a persistent context id."""
    try:
        req = urllib.request.Request(
            "https://api.browserbase.com/v1/contexts",
            data=json.dumps({"projectId": BROWSERBASE_PROJECT_ID}).encode(),
            headers={"X-BB-API-Key": BROWSERBASE_API_KEY,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode()).get("id", "")
    except Exception:
        return ""


def looks_like_pdf(url: str) -> bool:
    u = (url or "").lower().split("?")[0]
    return u.endswith(".pdf") or "/pdf/" in u


def read_pdf(url: str, limit: int = 12000) -> str:
    """Pull the text out of a PDF.

    Appliance manuals, statements and bills are all PDFs, and a PDF has no
    readable text in a browser at all - document.innerText is empty. We
    were scraping videos for something the manufacturer's own manual says
    plainly."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; PhoneAssistant/1.0)"})
        with urllib.request.urlopen(req, timeout=40) as r:
            raw = r.read(8 * 1024 * 1024)
    except Exception as e:
        emit("browse", "pdf", f"could not fetch {url[:80]}: {str(e)[:90]}",
             "warn")
        return ""
    try:
        from pypdf import PdfReader
        import io as _io
        reader = PdfReader(_io.BytesIO(raw))
        out = []
        for page in reader.pages:
            out.append(page.extract_text() or "")
            if sum(len(x) for x in out) > limit:
                break
        return " ".join(" ".join(out).split())[:limit]
    except Exception as e:
        emit("browse", "pdf", f"could not read {url[:80]}: {str(e)[:90]}",
             "warn")
        return ""


def _agent_fallback(jid: int, account_id: int, site: str, goal: str,
                    url: str = ""):
    """No hand-written setup for this site - hand it to the general agent
    and let it work the page out. This is what stops every new site the
    customers ask for needing someone to configure it first."""
    emit("job", f"job {jid}",
         f"no saved setup for {site or 'this site'} - working it out")
    db = Session()
    row = db.query(Job).filter_by(id=jid).first()
    if row:
        payload = json.loads(row.payload or "{}")
        payload["goal"] = goal
        if url:
            payload["url"] = url
        row.payload = json.dumps(payload)
        db.commit()
    db.close()
    _run_browse(jid, account_id, site)


def _run_site_login(jid: int, account_id: int, site: str):
    """Log this customer in, handing over to the general agent if the
    hand-written setup doesn't fit the page any more.

    The handover happens HERE, after the browser work has finished and
    Playwright has closed. Starting a second Playwright inside the first
    one crashes with "Sync API inside the asyncio loop"."""
    try:
        handover = _do_site_login(jid, account_id, site)
        if handover:
            goal, url = handover
            _agent_fallback(jid, account_id, site, goal, url)
    finally:
        # only once EVERYTHING is finished. _do_site_login used to drop it
        # here, so a handed-over job could never be answered: the caller's
        # reply came back "that job is no longer running".
        _JOBS.pop(jid, None)


def _do_site_login(jid: int, account_id: int, site: str):
    """Returns (goal, url) if the agent should take over, else None."""
    from playwright.sync_api import sync_playwright

    creds = use_site_login(account_id, site, purpose=f"job {jid} login")
    if not creds or not creds.get("password"):
        _job_set(jid, "failed", "No saved login for that site.")
        return

    cfg = SITES.get(site.lower())
    if not cfg:
        # never seen this site - the agent signs in the way a person would
        return (f"sign in to {site} with the saved username and password, "
                f"then confirm you are signed in by naming what you can see "
                f"on the account page",
                f"https://www.{site}.com")

    ctx_id = _get_context(account_id, site.lower()) or \
        _new_browserbase_context()
    ws = _bb_connect_url(ctx_id, account_id)

    page = browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws)
            bctx = browser.contexts[0] if browser.contexts \
                else browser.new_context()
            page = bctx.pages[0] if bctx.pages else bctx.new_page()
            page.set_default_timeout(45000)

            _job_set(jid, "opening", f"Opening {site}.")
            do_goto(page, cfg["login_url"], 4000)

            # Already signed in from a previous session?
            if signed_in(page, f"{site} already open")[0]:
                if ctx_id:
                    _save_context(account_id, site.lower(), ctx_id)
                _job_set(jid, "done", f"Already signed in to {site}.")
                browser.close()
                return

            _job_set(jid, "signing_in", "Entering their details.")
            user_el = q(page, cfg["user_sel"])
            if not user_el:
                # The hand-written selectors are an optimisation, not the
                # plan. Walmart swapped its email box for a combined
                # phone-or-email one and this simply gave up; sites change
                # their pages and nobody should have to notice.
                browser.close()
                return (f"sign in to {site} with the saved username and "
                        f"password, then confirm you are signed in by "
                        f"naming what you can see on the account page",
                        cfg["login_url"])
            do_fill(page, user_el, creds["username"])
            nxt = q(page, cfg["next_sel"])
            if nxt:
                do_click(page, nxt)
            else:
                try:
                    page.keyboard.press("Enter")
                except Exception:
                    pass
                settle(page, 3500)

            pw_el = q(page, cfg["pass_sel"])
            if not pw_el:
                where = page_url(page) or cfg["login_url"]
                browser.close()
                return (f"finish signing in to {site} with the saved "
                        f"username and password, then confirm you are "
                        f"signed in", where)
            do_fill(page, pw_el, creds["password"])
            nxt = q(page, cfg["next_sel"])
            if nxt:
                do_click(page, nxt, 6000)
            else:
                try:
                    page.keyboard.press("Enter")
                except Exception:
                    pass
                settle(page, 6000)

            # One-time code? Up to three goes: a misheard digit is the
            # normal case, and one wrong code used to end the sign-in.
            code_since = int(time.time() * 1000)
            for code_try in range(3):
                if not q(page, cfg["otp_sel"]):
                    break
                seen = page_text(page, 1500)
                where = code_destination(seen)

                # If the site emailed the code, fetch it ourselves. The
                # caller has no screen; asking them to go and find it is
                # asking for the one thing they rang us to avoid.
                mailed = ""
                if "mail" in (where or "").lower() or not where:
                    _job_set(jid, "working",
                             f"{site.title()} wants a code - looking in "
                             f"their email for it.")
                    for _ in range(6):
                        time.sleep(5)
                        mailed = code_from_email(account_id, site, code_since)
                        if mailed:
                            break
                if mailed:
                    otp_el = q(page, cfg["otp_sel"])
                    if otp_el:
                        do_fill(page, otp_el, mailed, True, 6000)
                        settle(page, 4000)
                        emit("signin", site,
                             "read the sign-in code from their email",
                             "info", account_id)
                        if not CODE_BAD.search(page_text(page, 1500) or ""):
                            break
                    seen = page_text(page, 1500)
                    where = code_destination(seen)
                if CODE_BAD.search(seen or ""):
                    note = (f"That code wasn't accepted by {site.title()}. "
                            f"Ask them to read the newest code again, "
                            f"digit by digit.")
                elif where:
                    note = (f"{site.title()} sent a code - {where}. Ask them "
                            f"to read it out.")
                else:
                    note = (f"{site.title()} is asking for a code but does "
                            f"not say where it sent it. Tell them that, and "
                            f"ask them to check their phone and their email.")
                _job_set(jid, "needs_code", note)
                waited = 0
                code = None
                while waited < 240:
                    time.sleep(3)
                    waited += 3
                    if (_JOBS.get(jid) or {}).get("cancelled"):
                        break
                    code = (_JOBS.get(jid) or {}).get("code")
                    if code:
                        _JOBS[jid]["code"] = None
                        break
                if not code:
                    _job_set(jid, "failed", "Timed out waiting for the code.",
                             reason="no_code")
                    browser.close()
                    return
                otp_el = q(page, cfg["otp_sel"])
                if not otp_el:
                    break
                do_fill(page, otp_el, code, True, 6000)
                settle(page, 4000)
                if not CODE_BAD.search(page_text(page, 1500) or ""):
                    break
            else:
                _job_set(jid, "failed",
                         f"{site.title()} refused the code three times.",
                         reason="bad_code")
                browser.close()
                return

            settle(page, 3000)
            screen = page_text(page, 1500)
            if _re_scrub.search(r"(?i)(password is incorrect|wrong password|"
                                r"incorrect password|password you entered)",
                                screen or ""):
                _job_set(jid, "failed",
                         f"{site.title()} says the password is wrong.",
                         reason="bad_password")
                browser.close()
                return

            ok, why = signed_in(page, f"{site} sign-in")
            if ok:
                if ctx_id:
                    _save_context(account_id, site.lower(), ctx_id)
                _job_set(jid, "done",
                         f"Signed in to {site} and saved the session.")
                record_change(account_id, "login", "signed in",
                              f"signed in to {site} and saved the session "
                              f"so it isn't needed again")
            else:
                seen = page_text(page, 1500) or ""
                if q(page, cfg["otp_sel"]) or CODE_BAD.search(seen):
                    where = code_destination(seen)
                    _job_set(jid, "failed",
                             f"{site.title()} is still asking for a code"
                             + (f" - {where}" if where else "")
                             + ". The code we tried wasn't accepted.",
                             reason="bad_code")
                else:
                    _job_set(jid, "failed",
                             f"Sign-in didn't complete - {why}.",
                             reason="stuck")
            browser.close()
    except Exception as e:
        detail = " url=" + page_url(page)[:100] if page else ""
        _job_set(jid, "failed", f"Browser error: {str(e)[:150]}{detail}")
        try:
            if browser:
                browser.close()
        except Exception:
            pass
    # NB: the job is NOT removed from _JOBS here - the handover that may
    # follow still needs to receive the caller's answers.


def _open_with_session(p, account_id: int, site: str):
    """Connect to Browserbase reusing this customer's saved session."""
    ctx_id = _get_context(account_id, site) or _new_browserbase_context()
    browser = p.chromium.connect_over_cdp(
        _bb_connect_url(ctx_id, account_id))
    bctx = browser.contexts[0] if browser.contexts else browser.new_context()
    page = bctx.pages[0] if bctx.pages else bctx.new_page()
    page.set_default_timeout(45000)
    return browser, page, ctx_id


ORDER_PAGES = {
    "walmart": "https://www.walmart.com/orders",
    "amazon": "https://www.amazon.com/gp/css/order-history",
    "temu": "https://www.temu.com/orders.html",
}

SEARCH_PAGES = {
    "walmart": "https://www.walmart.com/search?q=",
    "amazon": "https://www.amazon.com/s?k=",
    "temu": "https://www.temu.com/search_result.html?search_key=",
}


# A shop page starts with a hundred menu items. Handing the first 1800
# characters to the model means handing it "Alexa Skills, Amazon Autos,
# Amazon Devices..." - so it reported, honestly enough, that it could not
# find anything. Read more of the page and let the summariser find the
# part that answers the question.
NAV_NOISE = _re_scrub.compile(
    r"(?i)(skip to main content|all departments|alexa skills|"
    r"customer service|registry|gift cards|sell on |your account|"
    r"hello, sign in|deliver to|shop by category)")


def page_answer(page, question: str, limit: int = 9000) -> tuple:
    """(answer, raw) for one loaded page. The answer is empty when the page
    genuinely doesn't say - never menu text dressed up as a result."""
    raw = page_text(page, limit)
    if not (raw or "").strip():
        return "", ""
    answer = _summarise_page(raw, question)
    if not answer or "NOTHING_RELEVANT" in answer:
        return "", raw
    # a "summary" that is only chrome is not an answer
    words = [w for w in answer.split() if len(w) > 2]
    if len(words) < 6 or len(NAV_NOISE.findall(answer)) >= 2:
        return "", raw
    return answer, raw


def _run_site_orders(jid: int, account_id: int, site: str):
    """Read the customer's recent orders from a site they're signed into."""
    from playwright.sync_api import sync_playwright
    url = ORDER_PAGES.get(site)
    if not url:
        _agent_fallback(
            jid, account_id, site,
            "find my recent orders and read back what was ordered, the "
            "status of each, and the date",
            f"https://www.{site}.com")
        return

    browser = page = None
    try:
        with sync_playwright() as p:
            browser, page, ctx_id = _open_with_session(p, account_id, site)
            _job_set(jid, "opening", f"Opening {site} orders.")
            do_goto(page, url, 5000)

            ok, why = signed_in(page, f"{site} orders")
            if not ok:
                emit("job", f"job {jid}", f"{site} is signed out - {why}",
                     "warn")
                _job_set(jid, "failed",
                         f"Not signed in to {site} - {why}. The saved "
                         f"session has expired. Sign in again first.",
                         reason="signed_out")
                browser.close()
                return
            settle(page, 2500)          # let the list actually render
            answer, raw = page_answer(page, "their recent orders: what was "
                                            "ordered, when, and the status")
            if answer:
                _job_set(jid, "done", answer)
            else:
                _job_set(jid, "failed",
                         f"The {site} orders page opened but didn't show any "
                         f"orders we could read. Say that, rather than that "
                         f"they have no orders.",
                         reason="no_results")
            if ctx_id:
                _save_context(account_id, site, ctx_id)
            browser.close()
    except Exception as e:
        _job_set(jid, "failed", _browser_error(e))
        try:
            if browser:
                browser.close()
        except Exception:
            pass
    finally:
        _JOBS.pop(jid, None)


def _run_site_search(jid: int, account_id: int, site: str):
    """Search a site for a product, using the customer's session."""
    from playwright.sync_api import sync_playwright
    db = Session()
    row = db.query(Job).filter_by(id=jid).first()
    payload = json.loads(row.payload or "{}") if row else {}
    db.close()
    query = (payload.get("query") or "").strip()
    if not query:
        _job_set(jid, "failed", "Nothing to search for.")
        return
    base = SEARCH_PAGES.get(site)
    if not base:
        _agent_fallback(
            jid, account_id, site,
            f"search this site for {query} and read back the best few "
            f"matches with their prices",
            f"https://www.{site}.com")
        return

    browser = page = None
    try:
        with sync_playwright() as p:
            browser, page, ctx_id = _open_with_session(p, account_id, site)
            _job_set(jid, "opening", f"Searching {site} for {query}.")
            do_goto(page, base + urllib.parse.quote_plus(query), 5000)
            settle(page, 2500)          # results load after the shell
            answer, raw = page_answer(
                page, f"the best few matches for '{query}', with prices")
            if looks_signed_out(raw or ""):
                _job_set(jid, "failed",
                         f"{site} wants them signed in before it will "
                         f"search. Sign in to {site} first.",
                         reason="signed_out")
                browser.close()
                return
            if looks_like_bot_check(raw or ""):
                wall = record_block(account_id, site, raw or "",
                                    page_url(page), jid)
                _job_set(jid, "failed",
                         f"{site} refused us: {wall['what']}. "
                         f"{wall['advice']}",
                         reason=block_reason(wall["kind"]))
                browser.close()
                return
            if not answer:
                _job_set(jid, "failed",
                         f"The {site} search page opened but no results came "
                         f"back that we could read. Say that, rather than "
                         f"that the item doesn't exist.",
                         reason="no_results")
                browser.close()
                return
            _job_set(jid, "done", answer)
            if ctx_id:
                _save_context(account_id, site, ctx_id)
            browser.close()
    except Exception as e:
        _job_set(jid, "failed", _browser_error(e))
        try:
            if browser:
                browser.close()
        except Exception:
            pass
    finally:
        _JOBS.pop(jid, None)



# --------------------------------------------------- general browser agent
# Give it a goal and a starting URL. It reads the page, decides the next
# action, and repeats. No per-site configuration.

BROWSE_SYSTEM = """You are operating a web browser for someone on a phone
call. You get the page's text, a numbered list of things you can interact
with, and usually a picture of the page as it looks right now. Use the
picture to see the layout - which box is the search box, where the total
sits, what a button actually says. Act only on the numbered list; the
picture is for understanding, the numbers are for clicking.
Reply with ONE action as JSON and nothing else.

Actions:
{"action":"click","index":N,"why":"..."}
{"action":"type","index":N,"text":"...","enter":true,"why":"..."}
{"action":"goto","url":"https://...","why":"..."}
{"action":"back","why":"..."}                         the last step led
                                                      nowhere - go back and
                                                      try a different way
{"action":"scroll","why":"..."}
{"action":"wait","why":"..."}
{"action":"ask_user","question":"...","why":"..."}   when you need a code,
                                                      a choice, or anything
                                                      only they can answer
{"action":"done","answer":"what to say out loud","why":"..."}
Any action may also carry "found":"..." - a fact this page told you that
the goal needs: a price, a size, what something is made of. It is kept and
given back to you on every later step, so once you have written something
down you never need to open that page again.
{"action":"give_up","answer":"why it can't be done","why":"..."}

Rules:
- Work towards the goal in as few steps as possible.
- Never buy, pay, submit an order, or send anything irreversible. If the goal
  needs that, stop with ask_user and describe exactly what you would do.
- If the page wants a login and there are saved details, use them; if it
  wants a one-time code, use ask_user. Signing in is a normal step towards
  the goal on any site - work it out from the page in front of you.
- If a click led somewhere useless, use back rather than repeating it. If
  the same approach has failed twice, try a different route to the goal.
- If you can already answer the goal from the page, use done. PAGE TEXT is
  what the page says: read it before clicking anything to "see details".
- Write down what you read, with "found", BEFORE you leave a page. To
  compare two things: open the first, note what matters with "found", go
  back, open the second, note that too, then answer from your notes. Never
  open a page you have already noted.
- The answer field is read aloud, so keep it to two or three sentences with
  plain names, prices and dates. Never include a URL."""


_SNAPSHOT_JS = r"""
(args) => {
  const limit = args.limit, want = (args.want || '').toLowerCase();
  const words = want.split(/[^a-z0-9]+/).filter(w => w.length > 3);
  const sel = 'a, button, input, textarea, select, [role=button], ' +
              '[role=link], [role=combobox], [contenteditable="true"]';
  const typed = ['input', 'textarea', 'select'];
  // Clear the markers left by the last look at this page. A page that
  // only partly redraws - or a page we came BACK to - kept its old
  // numbers, so [12] could still match something from the previous
  // screen: the click landed on the wrong thing, or on nothing, and the
  // model concluded the product page "wasn't opening properly".
  for (const old of document.querySelectorAll('[data-pa-idx]'))
    old.removeAttribute('data-pa-idx');
  const cand = [];
  const dupes = new Set();
  let seen = 0;
  for (const el of document.querySelectorAll(sel)) {
    // Amazon keeps thousands of hidden menu links at the TOP of the page.
    // A budget counted over everything scanned was spent entirely on those,
    // and the visible page was never reached: "the page offers 7 things you
    // can use" on a shop full of products. Count what we can actually use.
    if (cand.length >= 600 || ++seen > 12000) break;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    const tag = el.tagName.toLowerCase();
    let label = el.getAttribute('aria-label') || el.getAttribute('placeholder')
      || (el.innerText || '').trim() || el.getAttribute('name')
      || el.getAttribute('value') || el.getAttribute('title') || '';
    label = label.replace(/\s+/g, ' ').slice(0, 70);
    if (!label && !typed.includes(tag)) continue;
    const type = (el.getAttribute('type') || '').toLowerCase();

    // What matters on a shop page is buried under a hundred menu links,
    // so rank rather than take the first ones in the page's own order.
    let score = 0;
    if (tag === 'button' || el.getAttribute('role') === 'button'
        || type === 'submit' || type === 'button') score += 4;
    if (typed.includes(tag)) score += 3;
    const low = label.toLowerCase();
    if (/add to (cart|basket|bag)|buy now|check ?out|place .*order|continue|proceed|sign in|log in|search|save|next|submit|apply|pay/
        .test(low)) score += 4;
    if (words.some(w => low.includes(w))) score += 3;
    if (el.closest('nav, header, footer, [role=navigation], [role=banner], ' +
                   '[role=contentinfo]')) score -= 4;
    if (el.closest('main, [role=main], form, [id*=cart], [id*=checkout]'))
      score += 2;
    if (r.top >= 0 && r.top < 1400) score += 1;
    // One "Add to cart" button per product means sixty identical entries.
    // They outranked the product names, filled every slot, and were then
    // collapsed into one - leaving ten things on a page of hundreds.
    const key = tag + '|' + type + '|' + label.toLowerCase();
    if (dupes.has(key)) continue;
    dupes.add(key);
    cand.push({el: el, order: cand.length, score: score,
               tag: tag, type: type, label: label});
  }
  cand.sort((a, b) => b.score - a.score || a.order - b.order);
  const keep = cand.slice(0, limit).sort((a, b) => a.order - b.order);
  const out = [];
  for (const c of keep) {
    c.el.setAttribute('data-pa-idx', String(out.length));
    out.push({tag: c.tag, type: c.type, label: c.label});
  }
  return out;
}
"""


def _body_mark(page) -> str:
    """A fingerprint of what a page is showing. Deliberately ignores the
    first part: every page on a shop starts with the same menu, and
    comparing that told us nothing had happened when the whole page had
    just changed."""
    import hashlib
    body = page_text(page, 6000) or ""
    return hashlib.md5(body[600:4000].encode("utf-8",
                                             "ignore")).hexdigest()


def _page_snapshot(page, limit: int = 80, want: str = ""):
    """Page text plus a numbered list of things you can interact with.

    This used to ask the browser about each element one at a time - is it
    visible, what tag, what label - which is seven network round trips per
    element. On a big shop that took over two minutes for a single step,
    and often timed out with an empty list, so the model was choosing
    numbers for elements that weren't there. Now the browser does the whole
    job once and hands back the finished list.

    It used to hand back the first 60 things in the page's own order. On
    Amazon that is the menu - Alexa Skills, Amazon Autos, Amazon Fresh -
    so "Add to Cart" never appeared and the model pressed things at random
    until it was declared stuck. The page is ranked now: buttons and boxes
    first, words from the goal next, menus and footers last."""
    raw = page_eval(page, _SNAPSHOT_JS, {"limit": limit, "want": want}) or []
    items, seen = [], set()
    for i, it in enumerate(raw):
        tag = it.get("tag", "")
        typ = it.get("type", "")
        desc = f"{tag}{'/' + typ if typ else ''}: {it.get('label', '')}"
        if desc in seen:
            continue
        seen.add(desc)
        items.append({"idx": i, "desc": desc})
    return items, page_text(page, 4000)


def _handle(page, item):
    """The live element for a snapshot entry, found by the marker the
    snapshot left on it."""
    if not item:
        return None
    return q(page, f'[data-pa-idx="{item["idx"]}"]')


def _first_json(raw: str) -> dict:
    """Pull the first JSON object out of a model's reply.

    The old code demanded the whole reply be nothing but JSON. Newer models
    often add a sentence before or after it, and that used to be read as
    'I have no idea what to do' - the agent gave up on a good answer."""
    if not raw:
        return {}
    s = raw.replace("```json", " ").replace("```", " ")
    start = s.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except Exception:
                        break
        start = s.find("{", start + 1)
    return {}


def _user_turn(msg: str, shot: str = ""):
    """A user message, with a picture of the page attached when we have one."""
    if not shot:
        return {"role": "user", "content": msg}
    return {"role": "user", "content": [
        {"type": "text", "text": msg},
        {"type": "image_url",
         "image_url": {"url": f"data:image/jpeg;base64,{shot}"}}]}


# The model sometimes answers a shopping page in prose - "I'm unable to
# perform the checkout" - instead of an action. It is not being blocked by
# the site: it has lost track of whose browser this is. One plain reminder
# gets it going again; a second refusal is reported honestly rather than
# dressed up as "I couldn't work out what to do next".
REFUSAL_HINT = """You are not browsing on your own behalf and you are not
being asked to buy anything. You are operating the customer's OWN browser,
already signed into their own account, at their spoken request, on a phone
call where they cannot see the screen. Working through a cart and a
checkout page is the job. Follow the GOAL exactly, including any limit it
sets on what you must not click. If the goal says to stop before placing
the order, stop there and report what the page shows. Reply with ONE action
as JSON and nothing else - never prose, never an explanation."""


def _decide(goal: str, url: str, text: str, items: list, history: list,
            answer_hint: str = "", shot: str = "", account_id=None,
            call_id=None, findings=None, model: str = ""):
    listing = "\n".join(f"[{i}] {it['desc']}" for i, it in enumerate(items))
    steps = "\n".join(history[-8:]) or "(none yet)"
    # What it has already read. Without this it walked into a product page,
    # walked out again, and had nothing - so it walked back in.
    notes = "\n".join(f"- {n}" for n in (findings or [])[-12:])
    msg = (f"GOAL: {goal}\n"
           f"URL: {url}\n"
           + (f"WHAT YOU HAVE WRITTEN DOWN SO FAR:\n{notes}\n"
              if notes else "")
           + f"STEPS SO FAR:\n{steps}\n"
           f"{answer_hint}\n"
           f"ELEMENTS:\n{listing}\n\n"
           f"PAGE TEXT:\n{text}")
    turns = [{"role": "system", "content": BROWSE_SYSTEM},
             _user_turn(msg, shot)]
    use = model or MODEL_BROWSER
    d = _openai_chat(turns, model=use, account_id=account_id,
                     call_id=call_id, cheap=False)
    raw = (d["choices"][0]["message"].get("content") or "").strip()
    act = _first_json(raw)
    if act.get("action"):
        return act

    emit("browse", "decide", f"{MODEL_BROWSER} answered in words instead of "
                             f"an action: {raw[:160]} - reminding it",
         "warn", account_id)
    try:
        d = _openai_chat(turns + [{"role": "assistant", "content": raw[:400]},
                                  {"role": "system", "content": REFUSAL_HINT}],
                         model=use, account_id=account_id,
                         call_id=call_id, cheap=False)
        again = (d["choices"][0]["message"].get("content") or "").strip()
        act = _first_json(again)
        if act.get("action"):
            return act
        raw = again or raw
    except Exception:
        pass

    emit("browse", "decide", f"{MODEL_BROWSER} would not act: {raw[:200]}",
         "error", account_id)
    return {"action": "give_up", "refused": True,
            "answer": (f"The part that reads web pages wouldn't carry on "
                       f"with this. It said: {raw[:200]}")}


_TASK_CACHE = {}


def _action_index(act: dict) -> int:
    """Which numbered element the model chose, or -1 if it didn't say.

    This used to read the index with an `or -1` fallback, and in Python
    `0 or -1` is -1 - so element [0], the first link on every page, could
    never be clicked. On Kohl's that was the sign-in link, and the agent
    spent seven steps being told "there is no [-1]"."""
    raw = act.get("index")
    if isinstance(raw, bool):
        return -1
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


def _action_sig(act: dict) -> str:
    """A short fingerprint of what the model just decided to do, so the
    same decision can be recognised when it comes round again."""
    a = act.get("action", "")
    if a == "goto":
        return f"goto:{act.get('url', '')}"
    return f"{a}:{_action_index(act)}:{str(act.get('text', ''))[:20]}"


def _going_in_circles(sigs: list, window: int = 6, times: int = 3) -> bool:
    """Is it doing the same thing over and over?

    Comparing the page before and after an action misses a loop that
    alternates - Home Depot went search, error, refresh, search, error,
    refresh six times, and the page 'changed' on every single step."""
    if len(sigs) < times:
        return False
    recent = sigs[-window:]
    return any(recent.count(s) >= times for s in set(recent))


def _stuck_note(n: int) -> str:
    """What to tell the agent when the page hasn't reacted.

    Without this it repeated 'fill in the username, fill in the password'
    twenty-four times on Target and called it a day. It has no memory that
    an action did nothing, so it has to be told."""
    if n == 1:
        return "That changed nothing on the page."
    if n == 2:
        return ("That changed nothing again. Do not click the same thing a "
                "third time. If you were trying to reach a sign-in page, "
                "use goto with the site's own sign-in address instead of "
                "hunting for the link.")
    return ("Nothing you have tried has changed the page. Stop repeating "
            "these steps. Either go back, try a completely different route, "
            "or give_up and say what you could see.")


STUCK_LIMIT = int(os.environ.get("BROWSE_STUCK_LIMIT", "4"))


def _task_shape(goal: str) -> dict:
    """Split a goal into the KIND of task and its SUBJECT, in one call.

    The kind keys the recipe, so 'where's my order' and 'check my order
    status' share one. The subject is the part that changes between
    callers - 'paper towels', 'milk' - and is what gets typed. Recording
    the subject as a placeholder is what lets a search recipe be reused."""
    key = goal.strip().lower()
    if key in _TASK_CACHE:
        return _TASK_CACHE[key]
    out = {"label": "misc", "subject": ""}
    if OPENAI_API_KEY:
        try:
            d = _openai_chat(model=MODEL_SUMMARY, messages=[
                {"role": "system",
                 "content": ('Split the task into its kind and its subject. '
                             'Reply with JSON only: {"label": "...", '
                             '"subject": "..."}. label is a snake_case kind '
                             'of 1-3 words describing the type of task, not '
                             'the specifics: order_status, product_price, '
                             'account_balance, store_hours, track_package. '
                             'subject is the specific thing being looked '
                             'for, or "" when the task has no variable '
                             'subject. Examples: "find snow blowers and '
                             'their prices" -> {"label": "product_price", '
                             '"subject": "snow blowers"}. "where is my '
                             'order" -> {"label": "order_status", '
                             '"subject": ""}.')},
                {"role": "user", "content": goal[:300]}])
            raw = (d["choices"][0]["message"].get("content") or "").strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            got = json.loads(raw)
            label = "".join(ch if ch.isalnum() or ch == "_" else "_"
                            for ch in str(got.get("label", "")).lower())
            label = label.strip("_")[:60]
            if label:
                out = {"label": label,
                       "subject": str(got.get("subject", ""))[:120].strip()}
        except Exception:
            pass
    _TASK_CACHE[key] = out
    return out


def _task_label(goal: str) -> str:
    return _task_shape(goal)["label"]


def _recipe_value(val: str, creds: dict, subject: str) -> str:
    """Turn a recorded placeholder back into a real value at replay time."""
    if val == "SAVED_PASSWORD":
        return creds.get("password", "")
    if val == "SAVED_USERNAME":
        return creds.get("username", "")
    if val == "TASK_SUBJECT":
        return subject
    return val


def _as_placeholder(typed: str, subject: str) -> str:
    """When the agent typed the subject of the task, record it as a
    placeholder so the next caller's subject goes in instead."""
    a, b = (typed or "").strip().lower(), (subject or "").strip().lower()
    if len(a) >= 2 and b and (a == b or a in b or b in a):
        return "TASK_SUBJECT"
    return typed


def _record_request(site, task, goal, path, outcome, seconds, jid):
    db = Session()
    db.add(SiteRequest(site=site or "generic", task=task, goal=goal[:500],
                       path=path, outcome=outcome, seconds=int(seconds),
                       job_id=jid))
    db.commit()
    db.close()


# Goals that require DOING something. A saved shortcut replays a few
# steps and then summarises whatever page it lands on - fine for "what
# does it cost", never right for "put it in the basket". Replaying one
# for an action goal is how a Best Buy search page became "the item
# successfully went into the cart".
DOING_GOAL = _re_scrub.compile(
    r"(?i)(add .{0,20}to (the |your )?(cart|basket|bag)|"
    r"put .{0,20}in (the |your )?(cart|basket)|"
    r"check ?out|proceed to|place .{0,12}order|sign in|log in|"
    r"send|book|cancel|change|update|remove|delete|reply|pay)")


def claims_action(answer: str) -> bool:
    """Does this answer say something was DONE, rather than seen?"""
    return bool(_re_scrub.search(
        r"(?i)(added to (the )?(cart|basket)|went into the (cart|basket)|"
        r"is now in (the |your )?(cart|basket)|signed (you )?in|"
        r"order (was )?placed|has been (sent|placed|ordered|added)|"
        r"i (have |'ve )?(added|sent|placed|ordered|signed))", answer or ""))


def _find_recipe(site: str, task: str):
    db = Session()
    row = (db.query(Recipe)
             .filter_by(site=site or "generic", task=task, retired=0)
             .order_by(Recipe.times_ok.desc()).first())
    out = None
    if row:
        out = {"id": row.id, "steps": json.loads(row.steps or "[]")}
    db.close()
    return out


def _save_recipe(site: str, task: str, goal: str, steps: list):
    """Store the steps that worked. Existing recipe -> refresh it."""
    db = Session()
    row = (db.query(Recipe)
             .filter_by(site=site or "generic", task=task).first())
    if row:
        row.steps = json.dumps(steps)
        row.retired = 0
        row.times_ok = (row.times_ok or 0) + 1
        row.last_ok = datetime.utcnow()
    else:
        db.add(Recipe(site=site or "generic", task=task,
                      example_goal=goal[:500], steps=json.dumps(steps),
                      times_ok=1, last_ok=datetime.utcnow()))
    db.commit()
    db.close()


def _recipe_result(rid: int, ok: bool):
    db = Session()
    row = db.query(Recipe).filter_by(id=rid).first()
    if row:
        if ok:
            row.times_ok = (row.times_ok or 0) + 1
            row.last_ok = datetime.utcnow()
        else:
            row.times_failed = (row.times_failed or 0) + 1
            # three failures in a row with few successes -> retire it
            if (row.times_failed or 0) >= 3 and \
                    (row.times_failed or 0) > (row.times_ok or 0):
                row.retired = 1
        db.commit()
    db.close()


def _match_element(items: list, desc: str):
    """Find today's version of an element recorded by description."""
    if not desc:
        return None
    want = desc.lower()
    for it in items:
        if it["desc"].lower() == want:
            return it
    tail = want.split(":", 1)[-1].strip()
    if tail:
        for it in items:
            if tail in it["desc"].lower():
                return it
    return None


def _replay_recipe(page, steps: list, creds: dict, log_fn,
                   subject: str = ""):
    """Run recorded steps without the model. Returns (ok, answer_text)."""
    needs = any(st.get("text") == "TASK_SUBJECT" for st in steps)
    if needs and not subject:
        log_fn("saved steps need a subject and this task has none")
        return False, ""
    for i, st in enumerate(steps):
        a = st.get("action")
        try:
            if a in ("click", "type"):
                items, _ = _page_snapshot(page)
                el = _handle(page, _match_element(items, st.get("desc", "")))
                if not el:
                    log_fn(f"replay step {i + 1}: couldn't find "
                           f"'{st.get('desc', '')}'")
                    return False, ""
                if a == "click":
                    do_click(page, el)
                else:
                    val = _recipe_value(st.get("text", ""), creds, subject)
                    do_fill(page, el, val, bool(st.get("enter")))
            elif a == "goto":
                do_goto(page, st["url"])
            elif a == "back":
                do_back(page)
            elif a == "scroll":
                page.mouse.wheel(0, 1400)
                settle(page, 2000)
            elif a == "wait":
                settle(page, 3000)
            log_fn(f"replay step {i + 1}: {a} ok")
        except Exception as e:
            log_fn(f"replay step {i + 1}: {a} failed — {str(e)[:80]}")
            return False, ""
    return True, page_text(page, 5000)


# Pressing this is spending someone else's money. A prompt saying "do not
# buy" is a wish; this is a rule. A browsing job may never press it, and an
# order may only press it once the caller has said yes out loud and the job
# was started with may_buy set.
BUY_BUTTONS = _re_scrub.compile(
    r"(?i)(place (your )?order|buy now|complete (the )?(purchase|order)|"
    r"confirm (and )?(pay|purchase|order)|submit (my |your )?order|"
    r"pay now|place order)")


def _run_browse(jid: int, account_id: int, site: str):
    """Pursue a goal on any site. Try the learned recipe first; fall back to
    the step-by-step agent; record what worked."""
    from playwright.sync_api import sync_playwright

    db = Session()
    row = db.query(Job).filter_by(id=jid).first()
    payload = json.loads(row.payload or "{}") if row else {}
    call_id = row.call_id if row else None
    db.close()
    goal = payload.get("goal", "")
    start = payload.get("url") or ""
    if not start and not site:
        # "Look this up properly": do the search here and start on the best
        # result. Asking the model to search and then decide to read a page
        # never worked - it searched, narrated, and answered from thin air.
        try:
            found = tool_web_search(goal)
            hits = [r.get("url") for r in found.get("results", [])
                    if r.get("url")]
            # the manual beats a video, and both beat a shop listing -
            # a product page for the thing they already own tells them
            # nothing and is usually the first result
            def _rank(u: str) -> int:
                low = (u or "").lower()
                if looks_like_pdf(low):
                    return 0                     # the manual itself
                if "manual" in low or "use-and-care" in low:
                    return 1                     # a page hosting one
                if "/p/" in low or "/shop" in low or "buy" in low:
                    return 3                     # somewhere to buy it
                return 2
            hits.sort(key=_rank)
            if hits:
                start = hits[0]
                payload.setdefault("urls", [])
                payload["urls"] = hits[1:4] + list(payload["urls"])
                _job_set(jid, "opening", f"Looking up: {goal[:90]}")
        except Exception as e:
            emit("browse", f"job {jid}", f"search first failed: {e}", "warn")
    if not start:
        start = (f"https://www.{site}.com" if site
                 else "https://www.google.com")
    # Other pages to try if this one turns out to refuse robots. A
    # manufacturer's own support page is usually the first search result
    # and usually the one that blocks, so going back to the agent to pick
    # again costs the caller half a minute of silence.
    spares = [u for u in (payload.get("urls") or []) if u and u != start]
    # a sign-in plus a lookup does not fit in twelve
    max_steps = int(payload.get("max_steps", 24))
    site_key = site or "generic"
    shape = _task_shape(goal)
    task = shape["label"]
    # the part that changes between callers, e.g. "paper towels"
    subject = (payload.get("query") or shape["subject"] or "").strip()
    t0 = time.time()

    creds = use_site_login(account_id, site, purpose=f"job {jid} browse") \
        if site else {}
    hint = ""
    if creds.get("username"):
        hint = (f"Saved login for this site: username {creds['username']}. "
                f"To fill the username field type SAVED_USERNAME; to fill "
                f"the password field type SAVED_PASSWORD. Both get replaced "
                f"with the real values.")

    def log_fn(msg):
        _job_set(jid, "working", msg)

    # A PDF has nothing for a browser to read - fetch and extract it
    # instead. No browser session, no bot check, and it is the manual.
    if looks_like_pdf(start):
        text = read_pdf(start)
        if text:
            answer = _summarise_page(text, goal)
            if answer and "NOTHING_RELEVANT" not in answer:
                _job_set(jid, "done", answer[:1500])
                _record_request(site_key, task, goal, "pdf", "ok",
                                time.time() - t0, jid)
                return
        nxt = [u for u in (payload.get("urls") or []) if u]
        if nxt:
            start, payload["urls"] = nxt[0], nxt[1:]
        else:
            _job_set(jid, "failed", "That document could not be read.")
            return

    browser = page = None
    recorded = []          # steps with element descriptions, for the recipe
    history = []
    path_used = "agent"
    stuck = 0              # actions in a row that changed nothing
    sigs = []              # what it has been trying, to spot a loop
    try:
        with sync_playwright() as p:
            browser, page, ctx_id = _open_with_session(p, account_id, site_key)
            _job_set(jid, "opening", f"Opening {start}")
            do_goto(page, start, 4000)

            # ---- 1. learned recipe first, but never for a goal that
            # asks for something to be DONE
            recipe = (None if DOING_GOAL.search(goal)
                      else _find_recipe(site_key, task))
            if recipe and recipe["steps"]:
                _job_set(jid, "working",
                         f"Using what worked before for {task}.")
                ok, text = _replay_recipe(page, recipe["steps"], creds,
                                          log_fn, subject)
                if ok and text:
                    answer = _summarise_page(text, goal)
                    if is_blocked(answer):
                        _job_set(jid, "done", BLOCKED_REPLY)
                        browser.close()
                        return
                    if answer and "NOTHING_RELEVANT" not in answer:
                        _recipe_result(recipe["id"], True)
                        _job_set(jid, "done", answer[:1500])
                        _record_request(site_key, task, goal, "recipe", "ok",
                                        time.time() - t0, jid)
                        if ctx_id:
                            _save_context(account_id, site_key, ctx_id)
                        browser.close()
                        return
                _recipe_result(recipe["id"], False)
                path_used = "fallback"
                _job_set(jid, "working",
                         "The saved steps didn't work — working it out fresh.")
                do_goto(page, start, 4000)

            # ---- 2. step-by-step agent
            outcome = "failed"
            findings = []
            for step in range(max_steps):
                if (_JOBS.get(jid) or {}).get("cancelled"):
                    _job_set(jid, "failed", "The caller hung up.",
                             reason="cancelled")
                    break
                # A shop draws its results after the shell, and we were
                # reading the page in between: "the page offers 9 things
                # you can use" on a search results page full of products.
                items, text = _page_snapshot(page, want=goal)
                for _ in range(3):
                    if len(items) >= 15 and len(text) >= 800:
                        break
                    settle(page, 1500)
                    items, text = _page_snapshot(page, want=goal)
                if looks_like_bot_check(text):
                    wall = record_block(account_id, site_key, text,
                                        page_url(page), jid)
                    if spares:
                        nxt = spares.pop(0)
                        _job_set(jid, "working",
                                 "that page wants a human check - trying "
                                 "another source")
                        do_goto(page, nxt, 4000)
                        continue
                    _job_set(jid, "failed",
                             f"{site_key} refused us: {wall['what']}"
                             + (f" ({wall['vendor']})" if wall["vendor"]
                                else "") + f". {wall['advice']}",
                             reason=block_reason(wall["kind"]))
                    break
                shot = page_shot(page) if BROWSER_VISION else ""
                url_before, body_before = page_url(page), _body_mark(page)
                act = _decide(goal, page_url(page), text, items, history,
                              hint, shot, account_id, call_id, findings,
                              payload.get("model", ""))
                noted = (act.get("found") or "").strip()
                if noted and noted not in findings:
                    findings.append(noted[:300])
                    _job_set(jid, "working", f"Noted: {noted[:120]}")
                a = act.get("action")
                why = act.get("why", "")[:120]

                if a == "done":
                    answer = act.get("answer", "")[:1500]
                    # A job can finish by REPORTING a wall - the probe does
                    # exactly that - and the wall still needs naming, or
                    # the record shows nothing was ever blocked.
                    if (looks_like_bot_check(text)
                            or answer.strip().upper().startswith("BLOCKED")
                            or classify_block(text)["kind"] not in
                            ("unknown", "site_error")):
                        record_block(account_id, site_key, text,
                                     page_url(page), jid)
                    wall = classify_block(text)
                    if wall["kind"] == "login_wall" and not creds.get(
                            "username"):
                        _job_set(jid, "failed",
                                 f"{site_key} will not go further without "
                                 f"an account, and we have no login saved "
                                 f"for it.", reason="login_needed")
                        break
                    if is_blocked(answer):
                        _job_set(jid, "done", BLOCKED_REPLY)
                        break
                    # Saying it was done does not make it done. If the
                    # answer claims an action and nothing in this job
                    # clicked anything, it is being imagined.
                    if claims_action(answer) and not any(
                            r.get("action") == "click" for r in recorded):
                        note = ("You said something had been done, but "
                                "nothing on this page has been clicked in "
                                "this job. Do it, or say only what the page "
                                "shows.")
                        emit("browse", f"job {jid}",
                             "refused an answer claiming an action that "
                             "never happened", "warn", account_id)
                        history.append(note)
                        _job_set(jid, "working", "checking that before "
                                                 "saying it")
                        continue
                    _job_set(jid, "done", answer)
                    outcome = "ok"
                    if recorded:
                        _save_recipe(site_key, task, goal, recorded)
                    break
                if a == "give_up":
                    if classify_block(text)["kind"] != "unknown":
                        record_block(account_id, site_key, text,
                                     page_url(page), jid)
                    _job_set(jid, "failed", act.get("answer", "")[:600],
                             reason="model_refused" if act.get("refused")
                             else "gave_up")
                    break
                if a == "ask_user":
                    # never name this 'q' - that shadows the page helper q()
                    question = act.get("question", "")[:300]
                    if looks_like_bot_check(question):
                        wall = record_block(account_id, site_key,
                                            question + " " + text,
                                            page_url(page), jid)
                        _job_set(jid, "failed",
                                 f"{site_key} wants a human to complete a "
                                 f"check by hand, which a caller on the "
                                 f"phone cannot do for us: {wall['what']}"
                                 + (f" ({wall['vendor']})"
                                    if wall["vendor"] else ""),
                                 reason=block_reason(wall["kind"]))
                        break
                    _job_set(jid, "needs_input", question)
                    waited, reply = 0, None
                    while waited < 240:
                        time.sleep(3)
                        waited += 3
                        reply = (_JOBS.get(jid) or {}).get("code")
                        if reply:
                            _JOBS[jid]["code"] = None
                            break
                    if not reply:
                        _job_set(jid, "failed", "No answer from the caller.")
                        break
                    history.append(f"asked: {question} -> they said: {reply}")
                    _job_set(jid, "working", "Carrying on.")
                    continue

                if a in ("click", "type"):
                    idx = _action_index(act)
                    if idx < 0 or idx >= len(items):
                        note = (f"There is no [{idx}] - the page offers "
                                f"{len(items)} things you can use."
                                + (" Nothing was found on the page at all; "
                                   "it may still be loading, so wait or "
                                   "scroll before choosing again."
                                   if not items else ""))
                        history.append(note)
                        _job_set(jid, "working", note)
                        settle(page, 2500)
                        continue

                try:
                    if a == "click":
                        it = items[int(act["index"])]
                        if (BUY_BUTTONS.search(it["desc"])
                                and not payload.get("may_buy")):
                            note = (f"Refused to press '{it['desc'][:60]}'. "
                                    f"Nothing here may complete a purchase. "
                                    f"Report what the page shows instead.")
                            emit("browse", f"job {jid}",
                                 f"refused to press {it['desc'][:60]}",
                                 "warn", account_id)
                            history.append(note)
                            _job_set(jid, "working", "stopped short of "
                                                     "buying anything")
                            continue
                        do_click(page, _handle(page, it))
                        recorded.append({"action": "click",
                                         "desc": it["desc"]})
                    elif a == "type":
                        it = items[int(act["index"])]
                        val = act.get("text", "")
                        real = _recipe_value(val, creds, subject)
                        do_fill(page, _handle(page, it), real,
                                bool(act.get("enter")))
                        # store what it meant, not what it said
                        recorded.append({"action": "type", "desc": it["desc"],
                                         "text": _as_placeholder(val, subject),
                                         "enter": bool(act.get("enter"))})
                    elif a == "goto":
                        where = (act.get("url") or "").lower()
                        # Search engines answer a robot with a puzzle. The
                        # model kept navigating to Google when it wanted
                        # more shops, and the job died there having priced
                        # nothing. Searching happens before the browser
                        # opens, through the search API.
                        if _re_scrub.search(
                                r"(?i)://(www\.)?(google|bing|duckduckgo|"
                                r"search\.yahoo)\.", where):
                            note = ("Search engines refuse a browser. Use "
                                    "the pages you were given, or answer "
                                    "with what you have.")
                            history.append(note)
                            _job_set(jid, "working", "kept off the search "
                                                     "engines")
                            continue
                        do_goto(page, act["url"])
                        recorded.append({"action": "goto", "url": act["url"]})
                    elif a == "back":
                        do_back(page)
                        # a dead end is not worth recording as a step
                    elif a == "scroll":
                        page.mouse.wheel(0, 1400)
                        recorded.append({"action": "scroll"})
                        settle(page, 2000)
                    else:
                        settle(page, 3000)
                except Exception as e:
                    history.append(f"{a} failed: {str(e)[:90]}")
                    _job_set(jid, "working", f"Retrying after: {str(e)[:80]}")
                    continue

                sigs.append(_action_sig(act))
                if _going_in_circles(sigs):
                    stuck += 1
                    history.append(
                        "You have done that same thing several times now "
                        "and are going round in circles. Try a completely "
                        "different route, or give_up and say what you saw.")
                    _job_set(jid, "working", "going round in circles")
                    if stuck >= STUCK_LIMIT:
                        _job_set(jid, "failed",
                                 f"It kept repeating the same steps without "
                                 f"getting anywhere. Last screen: "
                                 f"{text[:200]}", reason="stuck")
                        break
                    sigs.clear()
                    continue

                # Did any of that actually do something? The first 200
                # characters of a shop page are its menu and never change,
                # so "nothing happened" was reported after a search, after
                # opening a product, and after going back - three real
                # steps in a row, and the job was declared stuck.
                was_url, was_body = url_before, body_before
                now_url = page_url(page)
                now_body = _body_mark(page)
                if now_url == was_url and now_body == was_body:
                    stuck += 1
                    history.append(_stuck_note(stuck))
                    if stuck >= STUCK_LIMIT:
                        # A stale identity is one reason a site ignores
                        # everything - but so is a button we never saw.
                        # Throwing the session away costs the customer
                        # another sign-in and another code read out over
                        # the phone, so only do it when the page itself
                        # says they are signed out.
                        fresh = (_forget_context(account_id, site_key)
                                 if looks_signed_out(text) else False)
                        record_block(account_id, site_key, text,
                                     page_url(page), jid)
                        _job_set(jid, "failed",
                                 f"The page stopped responding to anything "
                                 f"it tried"
                                 + (" - the saved browser session has been "
                                    "cleared, so trying again starts fresh"
                                    if fresh else "")
                                 + f". Last screen: {text[:200]}",
                                 reason="stuck")
                        break
                else:
                    stuck = 0

                shown = act.get("text", "")
                if shown in ("SAVED_PASSWORD",):
                    shown = "(password)"
                history.append(f"{a} {act.get('index', act.get('url', ''))}"
                               f" {shown} — {why}")
                _job_set(jid, "working", f"Step {step + 1}: {why}")
            else:
                # Notes taken along the way are worth more than nothing,
                # and the caller is waiting.
                if findings:
                    _job_set(jid, "done",
                             "It didn't get all the way, but here is what "
                             "it read: " + " ".join(findings)[:1200])
                else:
                    _job_set(jid, "failed",
                             "Ran out of steps before finishing.")

            _record_request(site_key, task, goal, path_used, outcome,
                            time.time() - t0, jid)
            if ctx_id:
                _save_context(account_id, site_key, ctx_id)
            browser.close()
    except Exception as e:
        _job_set(jid, "failed", _browser_error(e))
        _record_request(site_key, task, goal, path_used, "failed",
                        time.time() - t0, jid)
        try:
            if browser:
                browser.close()
        except Exception:
            pass
    finally:
        _JOBS.pop(jid, None)



CHECKOUT_SYSTEM = """You are placing an order on a website for a customer
who has already confirmed every detail on the phone. You get the page text
and numbered interactive elements. Reply with ONE JSON action and nothing
else.

Actions:
{"action":"click","index":N,"why":"..."}
{"action":"type","index":N,"text":"...","enter":false,"why":"..."}
{"action":"goto","url":"https://...","why":"..."}
{"action":"scroll","why":"..."}
{"action":"wait","why":"..."}
{"action":"ask_user","question":"...","why":"..."}
{"action":"place_order","index":N,"total":"12.34","why":"..."}
{"action":"done","confirmation":"...","total":"12.34","answer":"...","why":"..."}
{"action":"give_up","answer":"...","why":"..."}

THE ORDER (already confirmed by the customer — use exactly these):
{spec}

Placeholders you may type: SHIP_LINE1, SHIP_LINE2, SHIP_CITY, SHIP_STATE,
SHIP_ZIP, SHIP_NAME, CARD_NUMBER, CARD_EXP_MM, CARD_EXP_YY, CARD_CVV,
CARD_NAME, SAVED_USERNAME, SAVED_PASSWORD. They are swapped for real values.

Rules, in order of importance:
1. Add ONLY the item described, in the quantity given. If you cannot find a
   product that clearly matches, give_up — do not substitute.
2. If the site already has a saved address or card that matches the
   customer's, use it. Otherwise enter the customer's details.
3. Before the final purchase, you must be on a review/summary screen. Read
   the item, quantity, shipping address, and total. Then use place_order
   with the index of the final purchase button and the total shown.
   If the total is more than 20% above the expected price, use ask_user
   instead, stating the total.
4. After purchasing, find the confirmation/order number and use done.
5. If anything asks for a code or a choice only the customer can make,
   use ask_user.
6. Never buy anything else, never add extras, never change quantity."""



def _order_set(oid: int, state: str, message: str = "", **fields):
    emit("order", f"order {oid}", f"{state}: {message}",
         "error" if state == "failed" else "info")
    db = Session()
    row = db.query(Order).filter_by(id=oid).first()
    if row:
        row.state = state
        row.message = message[:600]
        for k, v in fields.items():
            setattr(row, k, v)
        stamp = datetime.utcnow().strftime("%H:%M:%S")
        row.history = ((row.history or "") +
                       f"[{stamp}] {state}: {message[:300]}\n")[-6000:]
        if state == "placed":
            row.placed_at = datetime.utcnow()
        db.commit()
    db.close()

def _run_checkout(jid: int, account_id: int, site: str):
    """Place a confirmed order. Every value comes from the confirmed spec."""
    from playwright.sync_api import sync_playwright

    db = Session()
    job = db.query(Job).filter_by(id=jid).first()
    payload = json.loads(job.payload or "{}") if job else {}
    oid = payload.get("order_id")
    order = db.query(Order).filter_by(id=oid).first() if oid else None
    if not order:
        db.close()
        _job_set(jid, "failed", "No order attached.")
        return
    addr = (db.query(Address).filter_by(id=order.address_id).first()
            if order.address_id else None)
    card = (db.query(PaymentCard).filter_by(id=order.card_id).first()
            if order.card_id else None)
    acct = db.query(Account).filter_by(id=account_id).first()
    order_call_id = order.call_id
    spec = {
        "site": order.site, "item": order.item, "quantity": order.quantity,
        "expected_price": order.expected_price,
        "ship_to": _fmt_address(addr) if addr else "(use the site's saved address)",
        "pay_with": (f"{card.brand} ending {card.last4}" if card
                     else "(use the site's saved payment method)"),
    }
    values = {
        "SHIP_LINE1": addr.line1 if addr else "",
        "SHIP_LINE2": addr.line2 if addr else "",
        "SHIP_CITY": addr.city if addr else "",
        "SHIP_STATE": addr.state if addr else "",
        "SHIP_ZIP": addr.zip if addr else "",
        "SHIP_NAME": (card.name_on_card if card and card.name_on_card
                      else (acct.name if acct else "")),
        "CARD_NAME": (card.name_on_card if card and card.name_on_card
                      else (acct.name if acct else "")),
    }
    db.close()

    if card:
        secret = vault_get(card.secret_blob)
        values["CARD_NUMBER"] = secret.get("number", "")
        values["CARD_CVV"] = secret.get("cvv", "")
        if secret.get("stripe_pm") and not values["CARD_NUMBER"]:
            # Held by Stripe, so there are no digits to type into a form.
            # Say so plainly rather than silently typing nothing.
            spec["pay_with"] = (
                f"{card.brand} ending {card.last4}, held securely and NOT "
                f"available to type in. Use a card the site already has "
                f"saved for them. If the site has none, stop with ask_user "
                f"and say the card cannot be entered on this site.")
        mm, _, yy = (card.exp or "").partition("/")
        values["CARD_EXP_MM"] = mm.strip()
        values["CARD_EXP_YY"] = yy.strip()[-2:]
        db = Session()
        db.add(SecretAccess(account_id=account_id, site="card",
                            purpose=f"order {oid} checkout"))
        db.commit()
        db.close()

    creds = use_site_login(account_id, site, purpose=f"order {oid} checkout") \
        if site else {}
    values["SAVED_USERNAME"] = creds.get("username", "")
    values["SAVED_PASSWORD"] = creds.get("password", "")

    system = CHECKOUT_SYSTEM.replace("{spec}", json.dumps(spec, indent=1))

    def decide(url, text, items, history, shot=""):
        listing = "\n".join(f"[{i}] {it['desc']}"
                             for i, it in enumerate(items))
        msg = (f"URL: {url}\nSTEPS SO FAR:\n" +
               ("\n".join(history[-10:]) or "(none)") +
               f"\n\nELEMENTS:\n{listing}\n\nPAGE TEXT:\n{text}")
        d = _openai_chat([{"role": "system", "content": system},
                          _user_turn(msg, shot)],
                         model=MODEL_BROWSER, account_id=account_id,
                         call_id=order_call_id, cheap=False)
        raw = (d["choices"][0]["message"].get("content") or "").strip()
        act = _first_json(raw)
        if act.get("action"):
            return act
        emit("order", f"order {oid}", f"{MODEL_BROWSER} gave no usable "
                                      f"action: {raw[:200]}", "warn")
        return {"action": "give_up", "answer": "Lost track of the page."}

    browser = page = None
    history = []
    try:
        with sync_playwright() as p:
            browser, page, ctx_id = _open_with_session(p, account_id, site)
            _job_set(jid, "opening", f"Opening {site}.")
            _order_set(oid, "placing", f"Opening {site}.")
            do_goto(page, f"https://www.{site}.com", 4000)

            for step in range(30):
                if (_JOBS.get(jid) or {}).get("cancelled"):
                    _job_set(jid, "failed", "The caller hung up.",
                             reason="cancelled")
                    break
                items, text = _page_snapshot(
                    page, limit=80,
                    want=f"{spec.get('item', '')} add to cart checkout")
                shot = page_shot(page) if BROWSER_VISION else ""
                act = decide(page_url(page), text, items, history, shot)
                a = act.get("action")
                why = act.get("why", "")[:120]

                if a == "done":
                    conf = act.get("confirmation", "")[:120]
                    total = act.get("total", "")[:20]
                    _order_set(oid, "placed",
                               act.get("answer", "Order placed.")[:400],
                               confirmation=conf, final_total=total)
                    _job_set(jid, "done", f"Placed. Confirmation {conf}, "
                                          f"total {total}.")
                    break
                if a == "give_up":
                    _order_set(oid, "failed", act.get("answer", "")[:400])
                    _job_set(jid, "failed", act.get("answer", "")[:400])
                    break
                if a == "ask_user":
                    # never name this 'q' - that shadows the page helper q()
                    question = act.get("question", "")[:300]
                    _job_set(jid, "needs_input", question)
                    _order_set(oid, "placing",
                               f"Needs the customer: {question}")
                    waited, reply = 0, None
                    while waited < 240:
                        time.sleep(3)
                        waited += 3
                        reply = (_JOBS.get(jid) or {}).get("code")
                        if reply:
                            _JOBS[jid]["code"] = None
                            break
                    if not reply:
                        _order_set(oid, "failed", "No answer from the customer.")
                        _job_set(jid, "failed", "No answer from the caller.")
                        break
                    history.append(f"asked: {question} -> they said: {reply}")
                    continue
                if a in ("click", "type", "place_order"):
                    idx = _action_index(act)
                    if idx < 0 or idx >= len(items):
                        note = (f"There is no [{idx}] - the page offers "
                                f"{len(items)} things you can use.")
                        history.append(note)
                        _job_set(jid, "working", note)
                        settle(page, 2500)
                        continue

                if a == "place_order":
                    total = str(act.get("total", ""))[:20]
                    _order_set(oid, "placing",
                               f"On the review screen, total {total}. "
                               f"Placing now.")
                    history.append(f"place_order total {total} — {why}")
                    ok = do_click(page, _handle(page, items[int(act["index"])]),
                                  8000)
                    if not ok:
                        history.append("place_order click failed")
                    continue

                try:
                    if a == "click":
                        do_click(page, _handle(page, items[int(act["index"])]))
                    elif a == "type":
                        val = act.get("text", "")
                        real = values.get(val, val)
                        do_fill(page, _handle(page, items[int(act["index"])]),
                                real, bool(act.get("enter")))
                    elif a == "goto":
                        do_goto(page, act["url"])
                    elif a == "scroll":
                        page.mouse.wheel(0, 1400)
                        settle(page, 2000)
                    else:
                        settle(page, 3000)
                except Exception as e:
                    history.append(f"{a} failed: {str(e)[:90]}")
                    continue

                shown = act.get("text", "")
                if shown in values and shown.startswith(("CARD", "SAVED_P")):
                    shown = f"({shown.lower()})"
                history.append(f"{a} {act.get('index', act.get('url', ''))}"
                               f" {shown} — {why}")
                _job_set(jid, "working", f"Step {step + 1}: {why}")
                _order_set(oid, "placing", f"Step {step + 1}: {why}")
            else:
                _order_set(oid, "failed", "Ran out of steps.")
                _job_set(jid, "failed", "Ran out of steps.")

            if ctx_id:
                _save_context(account_id, site, ctx_id)
            browser.close()
    except Exception as e:
        _order_set(oid, "failed", f"Browser error: {str(e)[:200]}")
        _job_set(jid, "failed", f"Browser error: {str(e)[:200]}")
        try:
            if browser:
                browser.close()
        except Exception:
            pass
    finally:
        values.clear()
        _JOBS.pop(jid, None)
