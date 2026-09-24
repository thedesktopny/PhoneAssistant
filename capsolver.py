"""
CapSolver integration.
Solves the CAPTCHAs signals.py can name, using the CapSolver API.
Needs CAPSOLVER_API_KEY in the environment (Railway).

Task types are CapSolver's own names:
  reCAPTCHA v2  -> ReCaptchaV2TaskProxyLess  (isEnterprise when needed)
  hCaptcha      -> AntiHcaptchaTaskProxyLess
  Turnstile     -> AntiTurnstileTaskProxyLess
  FunCaptcha    -> FunCaptchaTaskProxyLess
PerimeterX and DataDome need proxies on CapSolver's side, which we do
not have - they are refused here before any money is spent.
"""
import os
import re
import json
import time
import urllib.request

CAPSOLVER_API_KEY = os.environ.get("CAPSOLVER_API_KEY", "")
CAPSOLVER_API = "https://api.capsolver.com"


def _api(endpoint, payload):
    payload["clientKey"] = CAPSOLVER_API_KEY
    req = urllib.request.Request(
        f"{CAPSOLVER_API}/{endpoint}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _frame_htmls(page):
    """(url, html) for every frame, main page first. The captcha key
    usually hides in a frame, not in the page itself."""
    out = []
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    for fr in frames:
        try:
            out.append((fr.url or "", fr.content()))
        except Exception:
            continue
    if not out:
        try:
            out.append((page.url or "", page.content()))
        except Exception:
            out.append(("", ""))
    return out


def _find_recaptcha(page):
    """(sitekey, is_enterprise). Three hiding places: a data-sitekey
    attribute, a grecaptcha.render(...) call, or the anchor iframe's
    own URL - the last is the one most sites leave behind."""
    for url, html in _frame_htmls(page):
        ent = ("/recaptcha/enterprise" in url
               or "recaptcha/enterprise.js" in html
               or "grecaptcha.enterprise" in html)
        m = re.search(r'recaptcha/(?:api2|enterprise)/(?:anchor|bframe|frame)'
                      r'[^"\']*[?&]k=([0-9A-Za-z_-]{8,})', url)
        if m:
            return m.group(1), ent or "/enterprise/" in url
        for pat in (r'data-sitekey=["\']([0-9A-Za-z_-]{8,})["\']',
                    r'sitekey["\']?\s*:\s*["\']([0-9A-Za-z_-]{8,})["\']'):
            m = re.search(pat, html)
            if m:
                return m.group(1), ent
    for url, html in _frame_htmls(page):
        m = re.search(r'recaptcha/(?:api|enterprise)\.js[^"\']*[?&]render='
                      r'([0-9A-Za-z_-]{8,})', html)
        if m:
            return m.group(1), "enterprise" in m.group(0)
    return "", False


def _find_sitekey(page, pats):
    for url, html in _frame_htmls(page):
        for pat in pats:
            m = re.search(pat, html)
            if m:
                return m.group(1)
        m = re.search(r'[?&](?:sitekey|render)=([0-9A-Za-z_-]{8,})', url)
        if m:
            return m.group(1)
    return ""


def solve_captcha(page, vendor, url):
    if not CAPSOLVER_API_KEY:
        return {"ok": False, "error": "CAPSOLVER_API_KEY not set"}
    if vendor == "perimeterx":
        return {"ok": False,
                "error": "PerimeterX needs a proxy on CapSolver's side"}
    if vendor == "datadome":
        return {"ok": False,
                "error": "DataDome needs a proxy on CapSolver's side"}

    # --- AUTO-DETECT VENDOR IF UNKNOWN ---
    # If signals.py couldn't name the vendor from the text, look at the HTML.
    if not vendor:
        recaptcha_key, _ = _find_recaptcha(page)
        if recaptcha_key:
            vendor = "recaptcha"
        else:
            generic_key = _find_sitekey(page, (
                r'data-sitekey=["\']([0-9A-Za-z_-]{8,})["\']',
                r'sitekey["\']?\s*:\s*["\']([0-9A-Za-z_-]{8,})["\']'))
            if generic_key:
                # Cloudflare Turnstile keys almost always start with "0x"
                if generic_key.startswith("0x"):
                    vendor = "cloudflare"
                else:
                    vendor = "hcaptcha"
            else:
                return {"ok": False, "error": "Could not auto-detect captcha vendor"}
    # -------------------------------------

    task = {}
    if vendor == "recaptcha":
        key, ent = _find_recaptcha(page)
        if not key:
            return {"ok": False, "error": "reCAPTCHA sitekey not found"}
        task = {"type": "ReCaptchaV2TaskProxyLess", "websiteURL": url,
                "websiteKey": key, "isEnterprise": ent}
    elif vendor == "hcaptcha":
        key = _find_sitekey(page, (
            r'data-sitekey=["\']([0-9A-Za-z_-]{8,})["\']',
            r'sitekey["\']?\s*:\s*["\']([0-9A-Za-z_-]{8,})["\']'))
        if not key:
            return {"ok": False, "error": "hCaptcha sitekey not found"}
        task = {"type": "AntiHcaptchaTaskProxyLess", "websiteURL": url,
                "websiteKey": key}
    elif vendor == "cloudflare":
        key = _find_sitekey(page, (
            r'data-sitekey=["\']([0-9A-Za-z_-]{8,})["\']',
            r'sitekey["\']?\s*:\s*["\']([0-9A-Za-z_-]{8,})["\']'))
        if not key:
            return {"ok": False, "error": "Turnstile sitekey not found"}
        task = {"type": "AntiTurnstileTaskProxyLess", "websiteURL": url,
                "websiteKey": key}
    elif vendor == "arkose":
        key = _find_sitekey(page, (
            r'data-pkey=["\']([^"\']+)["\']',
            r'p?key["\']?\s*:\s*["\']([^"\']+)["\']'))
        if not key:
            return {"ok": False, "error": "FunCaptcha public key not found"}
        task = {"type": "FunCaptchaTaskProxyLess", "websiteURL": url,
                "websitePublicKey": key}
    else:
        return {"ok": False, "error": f"Unsupported vendor: {vendor}"}

    try:
        res = _api("createTask", {"task": task})
    except Exception as e:
        return {"ok": False, "error": f"CapSolver API: {str(e)[:100]}"}
    if res.get("errorId"):
        return {"ok": False,
                "error": f"CapSolver: {res.get('errorCode') or res.get('errorDescription')}"}
    task_id = res.get("taskId")

    for _ in range(40):
        time.sleep(3)
        try:
            res = _api("getTaskResult", {"taskId": task_id})
        except Exception as e:
            return {"ok": False, "error": f"CapSolver poll: {str(e)[:100]}"}
        if res.get("errorId"):
            return {"ok": False,
                    "error": f"CapSolver: {res.get('errorCode') or res.get('errorDescription')}"}
        status = res.get("status")
        if status == "ready":
            return {"ok": True, "solution": res.get("solution") or {}}
        if status != "processing":
            return {"ok": False, "error": f"CapSolver task: {status}"}
    return {"ok": False, "error": "CapSolver timeout"}


def inject_solution(page, vendor, solution):
    token = (solution.get("gRecaptchaResponse")
             or solution.get("token")
             or solution.get("response")
             or solution.get("captchaToken") or "")
    for name, value in (solution.get("cookies") or {}).items():
        try:
            page.context.add_cookies(
                [{"name": name, "value": value, "url": page.url}])
        except Exception:
            pass
    if not token:
        return

    if vendor == "recaptcha":
        js = """(t) => {
            document.querySelectorAll('textarea#g-recaptcha-response, [name="g-recaptcha-response"]')
                .forEach(el => { el.value = t; el.innerHTML = t; });
            const cfg = window.___grecaptcha_cfg;
            if (cfg) for (const id in cfg.clients) {
                const c = cfg.clients[id];
                if (c && c.callback) { try { c.callback(t); } catch (e) {} }
            }
        }"""
    elif vendor == "hcaptcha":
        js = """(t) => {
            document.querySelectorAll('textarea[name="h-captcha-response"], [name="h-captcha-response"]')
                .forEach(el => { el.value = t; el.innerHTML = t; });
        }"""
    elif vendor == "cloudflare":
        js = """(t) => {
            document.querySelectorAll('[name="cf-turnstile-response"], [name="cf_challenge_response"]')
                .forEach(el => { el.value = t; });
        }"""
    else:
        js = ""
        
    if js:
        try:
            page.evaluate(js, token)
        except Exception:
            pass
            
        # A v2 token only counts once the form carrying it is submitted.
        # We wait a second, then try to click the most likely submit button.
        time.sleep(1)
        for sel in ('button[type="submit"]', 'input[type="submit"]',
                    'button:has-text("Sign in")', 'button:has-text("Log in")',
                    'button:has-text("Continue")', 'button:has-text("Submit")'):
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click(timeout=3000)
                    break
            except Exception:
                continue