"""Money: Stripe, cards, and charging one.

Card numbers are never typed into this system when it can be
avoided - the card page hands the customer to Stripe, and only a
reference comes back. A charge carries a key so a retry can never
take the money twice, a ceiling, and reason codes instead of
sentences, so the assistant can say what actually happened.
"""
from core import *                                   # noqa: F401,F403


class StripeError(Exception):
    def __init__(self, code: str, message: str, decline: str = ""):
        super().__init__(message)
        self.code, self.message, self.decline = code, message, decline


def _stripe_call(method: str, path: str, fields: dict | None = None,
                 idem: str = "") -> dict:
    """One call to Stripe, plain urllib like every other service here.
    Failures keep Stripe's own error code, so nothing reads the message."""
    data = urllib.parse.urlencode(fields or {}, doseq=True)
    url = f"https://api.stripe.com/v1/{path}"
    headers = {"Authorization": f"Bearer {STRIPE_SECRET_KEY}"}
    body = None
    if method == "GET":
        url += ("?" + data) if data else ""
    else:
        body = data.encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if idem:
        headers["Idempotency-Key"] = idem[:255]
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        err = {}
        try:
            err = json.loads(e.read().decode()).get("error", {}) or {}
        except Exception:
            pass
        raise StripeError(err.get("code") or err.get("type") or str(e.code),
                          err.get("message") or f"Stripe said {e.code}.",
                          err.get("decline_code", "")) from None


def _stripe(path: str, fields: dict) -> dict:
    try:
        return _stripe_call("POST", path, fields)
    except StripeError as e:
        raise HTTPException(400, e.message)


def stripe_customer_for(account_id: int) -> str:
    """Each customer gets one Stripe customer, made the first time."""
    db = Session()
    acct = db.query(Account).filter_by(id=account_id).first()
    if not acct:
        db.close()
        raise HTTPException(404, "No such customer.")
    if acct.stripe_customer:
        cus = acct.stripe_customer
        db.close()
        return cus
    name = acct.name or ""
    db.close()
    made = _stripe_call("POST", "customers", {
        "name": name, "metadata[account_id]": str(account_id)},
        idem=f"customer-{account_id}")
    db = Session()
    acct = db.query(Account).filter_by(id=account_id).first()
    acct.stripe_customer = made["id"]
    db.commit()
    db.close()
    return made["id"]


def stripe_card_page_url(account_id: int) -> str:
    """Stripe's own hosted page, set up to SAVE a card, not charge it. The
    card is typed into Stripe, never into us."""
    cus = stripe_customer_for(account_id)
    session = _stripe_call("POST", "checkout/sessions", {
        "mode": "setup",
        "customer": cus,
        "payment_method_types[0]": "card",
        "client_reference_id": str(account_id),
        "metadata[account_id]": str(account_id),
        "setup_intent_data[metadata][account_id]": str(account_id),
        "success_url": f"{PUBLIC_URL}/card/done?session_id="
                       "{CHECKOUT_SESSION_ID}",
        "cancel_url": f"{PUBLIC_URL}/card?cancelled=1",
    })
    return session["url"]


def stripe_save_finished(session_id: str) -> dict:
    """Stripe says the card page was completed: record the card. Asks
    Stripe, never trusts the address bar, and is safe to run twice."""
    sess = _stripe_call("GET", f"checkout/sessions/{session_id}",
                        {"expand[]": "setup_intent.payment_method"})
    if sess.get("status") != "complete" or sess.get("mode") != "setup":
        raise HTTPException(400, "not_finished")
    try:
        account_id = int(sess.get("client_reference_id") or 0)
    except ValueError:
        account_id = 0
    db = Session()
    acct = db.query(Account).filter_by(id=account_id).first()
    if not acct or not acct.stripe_customer \
            or acct.stripe_customer != sess.get("customer"):
        db.close()
        raise HTTPException(400, "not_finished")
    pm = ((sess.get("setup_intent") or {}).get("payment_method") or {})
    card = pm.get("card") or {}
    pm_id = pm.get("id", "")
    for c in db.query(PaymentCard).filter_by(account_id=account_id).all():
        try:
            if vault_get(c.secret_blob).get("stripe_pm") == pm_id:
                out = {"brand": c.brand, "last4": c.last4, "new": False}
                db.close()
                return out
        except Exception:
            continue
    for c in db.query(PaymentCard).filter_by(account_id=account_id).all():
        c.is_default = 0
    brand = (card.get("brand") or "card").title()
    row = PaymentCard(
        account_id=account_id, brand=brand, last4=card.get("last4", ""),
        exp=f"{card.get('exp_month', '')}/{str(card.get('exp_year', ''))[-2:]}",
        name_on_card=((pm.get("billing_details") or {}).get("name") or "")[:120],
        secret_blob=vault_put({"stripe_pm": pm_id,
                               "stripe_customer": acct.stripe_customer}),
        is_default=1)
    db.add(row)
    db.commit()
    out = {"brand": brand, "last4": row.last4, "new": True,
           "account_id": account_id}
    db.close()
    record_change(account_id, "card", "saved",
                  f"added a {brand} ending {out['last4']} on the card page")
    emit("cards", "saved", f"card saved at Stripe ({brand} {out['last4']})",
         "info", account_id)
    return out


CHARGE_LIMIT_CENTS = int(os.environ.get("CHARGE_LIMIT_CENTS", "50000"))


def stripe_charge(account_id: int, card_id: int, cents: int,
                  what_for: str, key: str) -> dict:
    """Charge a card saved at Stripe. key makes it safe to retry: the same
    key never charges twice. Outcomes are reason codes, not sentences."""
    if cents < 50:
        raise HTTPException(400, "too_small")
    if cents > CHARGE_LIMIT_CENTS:
        raise HTTPException(400, "over_limit")
    db = Session()
    card = db.query(PaymentCard).filter_by(id=card_id,
                                           account_id=account_id).first()
    db.close()
    if not card:
        raise HTTPException(404, "no_card")
    secret = vault_get(card.secret_blob)
    pm, cus = secret.get("stripe_pm"), secret.get("stripe_customer")
    if not pm or not cus:
        raise HTTPException(400, "card_not_at_stripe")
    try:
        pi = _stripe_call("POST", "payment_intents", {
            "amount": str(int(cents)), "currency": "usd",
            "customer": cus, "payment_method": pm,
            "off_session": "true", "confirm": "true",
            "description": (what_for or "")[:300],
            "metadata[account_id]": str(account_id)}, idem=key)
    except StripeError as e:
        reason = {"authentication_required": "needs_authentication",
                  "card_declined": "declined",
                  "expired_card": "card_expired",
                  "insufficient_funds": "declined"}.get(e.code, "stripe_error")
        if e.decline == "authentication_required":
            reason = "needs_authentication"
        emit("cards", "charge", f"charge of ${cents / 100:.2f} failed: "
                                f"{reason}", "warn", account_id)
        return {"charged": False, "reason": reason, "message": e.message}
    ok = pi.get("status") == "succeeded"
    if ok:
        record_change(account_id, "payment", "charged",
                      f"charged ${cents / 100:.2f} to {card.brand} ending "
                      f"{card.last4} for {what_for[:120]}",
                      undo="refundable from the Stripe dashboard")
    emit("cards", "charge",
         f"{'charged' if ok else 'charge ' + pi.get('status', '')} "
         f"${cents / 100:.2f} on {card.brand} {card.last4}: {what_for[:80]}",
         "info" if ok else "warn", account_id)
    return {"charged": ok, "reason": "" if ok else pi.get("status", ""),
            "id": pi.get("id"), "amount": f"${cents / 100:.2f}",
            "card": f"{card.brand} ending {card.last4}"}


class StripeNeedsRawCardAccess(Exception):
    """Stripe will not take card digits from a server until the account is
    approved for it. Phone orders have no browser to collect a card in, so
    this has to be requested - until then we keep storing cards ourselves."""


def stripe_hold_card(number: str, exp: str, cvv: str, name: str) -> dict:
    """Give the card to Stripe, get back a token. We never store the digits.

    Stripe is also the authority on the brand and last four, so we stop
    guessing those ourselves."""
    mm, _, yy = (exp or "").partition("/")
    yy = yy.strip()
    year = int(yy) + 2000 if len(yy) == 2 else int(yy or 0)
    fields = {"type": "card", "card[number]": number,
              "card[exp_month]": (mm.strip() or "0"), "card[exp_year]": year}
    if cvv:
        fields["card[cvc]"] = cvv
    if name:
        fields["billing_details[name]"] = name
    try:
        pm = _stripe("payment_methods", fields)
    except HTTPException as e:
        said = str(getattr(e, "detail", ""))
        if "raw card" in said.lower() or "directly to the stripe api"                 in said.lower():
            raise StripeNeedsRawCardAccess(said) from None
        raise
    card = pm.get("card", {})
    return {"id": pm.get("id", ""), "brand": (card.get("brand") or "").title(),
            "last4": card.get("last4", ""),
            "exp": f"{card.get('exp_month', '')}/"
                   f"{str(card.get('exp_year', ''))[-2:]}"}


def _luhn_ok(num: str) -> bool:
    d = [int(c) for c in num if c.isdigit()]
    if len(d) < 13:
        return False
    total, alt = 0, False
    for x in reversed(d):
        if alt:
            x *= 2
            if x > 9:
                x -= 9
        total += x
        alt = not alt
    return total % 10 == 0


def _card_brand(num: str) -> str:
    if num.startswith("4"):
        return "Visa"
    if num[:2] in ("51", "52", "53", "54", "55") or 2221 <= int(num[:4] or 0) <= 2720:
        return "Mastercard"
    if num[:2] in ("34", "37"):
        return "Amex"
    if num.startswith("6"):
        return "Discover"
    return "Card"


# ------------------------------------------------------- text brain (SMS)


# Which model does which job. All three are Railway variables, so you can
# change the browser's brain without a code change or a deploy.
#   MODEL_BROWSER  decides every click on a website - the one that matters
#   MODEL_SUMMARY  turns a finished page into a spoken sentence - easy work
#   MODEL_TEXT     answers incoming text messages
# GET /models lists what your OpenAI account can actually use.
