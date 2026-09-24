"""
CapSolver integration.
Uses the CapSolver API to solve CAPTCHAs detected by signals.py.
Requires CAPSOLVER_API_KEY in environment (Railway).
"""
import os
import json
import time
import urllib.request
import urllib.parse
from core import emit, _re_scrub

CAPSOLVER_API_KEY = os.environ.get("CAPSOLVER_API_KEY", "")
CAPSOLVER_API = "https://api.capsolver.com"

def _api(endpoint: str, payload: dict) -> dict:
    payload["clientKey"] = CAPSOLVER_API_KEY
    req = urllib.request.Request(
        f"{CAPSOLVER_API}/{endpoint}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

def _find_sitekey(page, patterns: list) -> str:
    try:
        html = page.content()
    except Exception:
        html = ""
    for pat in patterns:
        m = _re_scrub.search(pat, html, _re_scrub.I)
        if m:
            return m.group(1)
    for sel in ('[data-sitekey]', '[data-pkey]', '[data-key]'):
        try:
            el = page.query_selector(sel)
            if el:
                return (el.get_attribute('data-sitekey') or 
                        el.get_attribute('data-pkey') or 
                        el.get_attribute('data-key'))
        except Exception:
            pass
    return ""

def solve_captcha(page, vendor: str, url: str) -> dict:
    # Early exit if the key is missing (e.g., in test environments)
    if not CAPSOLVER_API_KEY:
        return {"ok": False, "error": "CAPSOLVER_API_KEY not set"}
        
    task_type = ""
    extra = {}
    
    if vendor == "recaptcha":
        sitekey = _find_sitekey(page, [
            r'data-sitekey=["\']([^"\']+)["\']',
            r'sitekey["\']?\s*:\s*["\']([^"\']+)["\']',
            r'render\?[^"]*render=([^&"\']+)'
        ])
        if not sitekey:
            return {"ok": False, "error": "reCAPTCHA sitekey not found"}
        task_type = "reCaptchaTaskProxyLess"
        extra = {"websiteURL": url, "websiteKey": sitekey}
        
    elif vendor == "hcaptcha":
        sitekey = _find_sitekey(page, [
            r'data-sitekey=["\']([^"\']+)["\']',
            r'sitekey["\']?\s*:\s*["\']([^"\']+)["\']'
        ])
        if not sitekey:
            return {"ok": False, "error": "hCaptcha sitekey not found"}
        task_type = "hcaptchaTaskProxyLess"
        extra = {"websiteURL": url, "websiteKey": sitekey}
        
    elif vendor == "cloudflare":
        sitekey = _find_sitekey(page, [
            r'data-sitekey=["\']([^"\']+)["\']',
            r'sitekey["\']?\s*:\s*["\']([^"\']+)["\']'
        ])
        task_type = "antiCloudflareTask"
        extra = {"websiteURL": url}
        if sitekey:
            extra["websiteKey"] = sitekey
            
    elif vendor == "perimeterx":
        task_type = "antiPerimeterxTask"
        extra = {"websiteURL": url}
        
    elif vendor == "datadome":
        task_type = "antiDatadomeTask"
        extra = {"websiteURL": url}
        
    elif vendor == "arkose":
        sitekey = _find_sitekey(page, [
            r'data-pkey=["\']([^"\']+)["\']',
            r'pk["\']?\s*:\s*["\']([^"\']+)["\']',
            r'pkey["\']?\s*:\s*["\']([^"\']+)["\']'
        ])
        task_type = "antiArkoseLabsTask"
        extra = {"websiteURL": url}
        if sitekey:
            extra["websitePublicKey"] = sitekey
            
    else:
        return {"ok": False, "error": f"Unsupported vendor: {vendor}"}

    try:
        res = _api("createTask", {"task": {"type": task_type, **extra}})
        if res.get("errorId") != 0:
            return {"ok": False, "error": f"CapSolver createTask: {res.get('errorDescription')}"}
        task_id = res.get("taskId")
    except Exception as e:
        return {"ok": False, "error": f"CapSolver API: {str(e)[:100]}"}

    for _ in range(40):
        time.sleep(3)
        try:
            res = _api("getTaskResult", {"taskId": task_id})
            status = res.get("status")
            if status == "ready":
                return {"ok": True, "solution": res.get("solution", {})}
            elif status == "processing":
                continue
            else:
                return {"ok": False, "error": f"CapSolver task: {status}"}
        except Exception as e:
            return {"ok": False, "error": f"CapSolver poll: {str(e)[:100]}"}
            
    return {"ok": False, "error": "CapSolver timeout"}

def inject_solution(page, vendor: str, solution: dict):
    token = (solution.get("token") or solution.get("gRecaptchaResponse") or 
             solution.get("response") or solution.get("captchaToken") or "")
             
    cookies = solution.get("cookies", {})
    if cookies:
        for k, v in cookies.items():
            try:
                page.context.add_cookies([{"name": k, "value": v, "url": page.url}])
            except Exception:
                pass
                
    if not token:
        return
        
    js = ""
    if vendor == "recaptcha":
        js = """
        () => {
            let t = '%s';
            document.querySelectorAll('[name="g-recaptcha-response"], #g-recaptcha-response')
                .forEach(el => el.innerHTML = t);
            if (window.___grecaptcha_cfg) {
                for (let id in ___grecaptcha_cfg.clients) {
                    let c = ___grecaptcha_cfg.clients[id];
                    if (c && c.callback) c.callback(t);
                }
            }
        }
        """ % token
    elif vendor == "hcaptcha":
        js = """
        () => {
            let t = '%s';
            document.querySelectorAll('[name="h-captcha-response"], #h-captcha-response')
                .forEach(el => el.innerHTML = t);
        }
        """ % token
    elif vendor == "cloudflare":
        js = """
        () => {
            let t = '%s';
            document.querySelectorAll('[name="cf-turnstile-response"], [name="cf_challenge_response"]')
                .forEach(el => el.value = t);
        }
        """ % token
    elif vendor == "perimeterx":
        if token:
            js = """
            () => {
                let t = '%s';
                if (window._pxAppId) localStorage.setItem('_pxToken', t);
            }
            """ % token
    elif vendor == "arkose":
        js = """
        () => {
            let t = '%s';
            document.querySelectorAll('[name="fc-token"], [name="arkoseToken"]')
                .forEach(el => el.value = t);
        }
        """ % token
        
    if js:
        try:
            page.evaluate(js)
        except Exception:
            pass