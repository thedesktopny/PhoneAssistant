"""
Which shops will let the assistant in?  Ask them, don't guess.

    python probe.py macys bestbuy cvs            probe these
    python probe.py                              probe the default list

For each site it runs a SHORT browse job (a handful of steps) whose only
goal is to find out how the site treats an automated visitor:

    BLOCKED     a "prove you're human" check - the site refuses robots;
                use a sanctioned rail (ACP / partner API) or a concierge
                account instead
    GUEST_OK    reached a cart or checkout without signing in - ordering
                may work today with no login at all
    SIGN_IN     found a normal sign-in form and nothing hostile - a login
                is needed but the site is not fighting us
    UNCLEAR     ran out of steps or something else - look at the job

Needs SERVICE_TOKEN in the environment (setx SERVICE_TOKEN "...").
Runs several sites at once, up to MAX_BROWSERS on the backend. Each probe
costs a few cents of browser time and model calls.
"""
import os
import sys
import json
import time
import urllib.request
import urllib.parse
import urllib.error

BACKEND = os.environ.get(
    "BACKEND_URL", "https://web-production-13961.up.railway.app").rstrip("/")
TOKEN = os.environ.get("SERVICE_TOKEN", "")
ACCOUNT = int(os.environ.get("TEST_ACCOUNT_ID", "1"))

DEFAULT = ["macys", "bestbuy", "homedepot", "cvs", "walgreens", "kohls",
           "costco", "wayfair", "etsy", "lowes"]

GOAL_GUEST = (
    "Do NOT sign in and do NOT go anywhere near a sign-in page. Pretend you "
    "are a shopper who has never had an account here. Search for a cheap "
    "everyday item, open it, add it to the basket, then open the basket and "
    "try to start checkout as a guest. Stop as soon as you know one of these "
    "and reply with done, answer being EXACTLY one word: BLOCKED if a robot "
    "or human-verification check appears at any point; GUEST_OK if you reach "
    "a checkout page that asks for a delivery address or payment without "
    "demanding an account; LOGIN_WALL if it will not let you check out "
    "without signing in or creating an account. Never buy anything and never "
    "enter payment details.")

GOAL = ("Find out how this site treats you. Try to reach the sign-in page, "
        "then try to put any one item in the cart and get to checkout WITHOUT "
        "signing in. Stop as soon as you know one of these and reply with "
        "done, answer being EXACTLY one word: BLOCKED if the site asks you "
        "to prove you are human or a robot check appears; GUEST_OK if you "
        "reach a cart or checkout without signing in; SIGN_IN if you find a "
        "normal sign-in form and are not blocked. Never buy anything.")

STEPS = int(os.environ.get('PROBE_STEPS', '12'))


def call(path, method="GET", **params):
    url = BACKEND + path + ("?" + urllib.parse.urlencode(params) if params
                            else "")
    req = urllib.request.Request(url, method=method, headers={
        "Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode() or "{}")


def verdict(job: dict) -> str:
    reason = job.get("reason") or ""
    msg = (job.get("message") or "").strip()
    if reason == "bot_check" or "BLOCKED" in msg.upper():
        return "BLOCKED"
    if job.get("state") == "done":
        up = msg.upper()
        if "GUEST_OK" in up:
            return "GUEST_OK"
        if "SIGN_IN" in up:
            return "SIGN_IN"
        if "LOGIN_WALL" in up:
            return "LOGIN_WALL"
    return "UNCLEAR"


def main():
    if not TOKEN or "<" in TOKEN:
        print("SERVICE_TOKEN is not set to a real token.")
        return 2
    args = [a for a in sys.argv[1:]]
    guest = "--guest" in args
    args = [a for a in args if not a.startswith("--")]
    sites = [s.lower().strip() for s in args] or DEFAULT
    goal = GOAL_GUEST if guest else GOAL
    if guest:
        print("GUEST-ONLY probe: never touches a sign-in page")
    print(f"probing {len(sites)} sites, {STEPS} steps each, via {BACKEND}\n")

    started = {}
    for s in sites:
        d = call("/jobs/browse", "POST", account_id=ACCOUNT, goal=goal,
                 site=s, max_steps=STEPS)
        if d.get("blocked"):
            print(f"  {s:<12} refused by our own topic filter")
            continue
        started[s] = d["job_id"]
        print(f"  {s:<12} job {d['job_id']}")
    print()

    results = {}
    deadline = time.time() + 15 * 60
    while started and time.time() < deadline:
        time.sleep(15)
        for s, jid in list(started.items()):
            try:
                j = call("/jobs/status", job_id=jid)
            except Exception:
                continue
            if j.get("state") in ("done", "failed"):
                results[s] = (verdict(j), j)
                del started[s]
                v, _ = results[s]
                print(f"  {s:<12} {v:<9} ({j.get('state')}: "
                      f"{(j.get('message') or '')[:70]})")
    for s in started:
        results[s] = ("UNCLEAR", {"message": "still running at timeout"})

    print("\n" + "-" * 60)
    print(f"{'site':<12} {'verdict':<9} what it means")
    print("-" * 60)
    meaning = {
        "BLOCKED": "refuses robots - needs ACP / partner API / concierge",
        "GUEST_OK": "ordering may work today with no login",
        "SIGN_IN": "login needed, but the site isn't fighting us",
        "LOGIN_WALL": "guest checkout not allowed - an account is required",
        "UNCLEAR": "look at the job in the admin panel",
    }
    for s in sites:
        v = results.get(s, ("UNCLEAR", {}))[0]
        print(f"{s:<12} {v:<9} {meaning[v]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
