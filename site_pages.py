"""
The public website: the pages a customer, a family member, or a Google
reviewer sees.

Kept out of main.py deliberately - that file is already too big to hold in
your head, and this is the part most likely to be edited for wording rather
than behaviour.

The privacy policy is written to meet Google's requirements for restricted
scopes, including the Limited Use disclosure they specifically look for.
It is a solid starting draft, not legal advice - have someone qualified
read it before you rely on it.
"""

BRAND = "Phone Assistant"
PHONE = "+1 484 518 2072"
SUPPORT_EMAIL = "support@hellobuziness.com"

CSS = """
 *{box-sizing:border-box}
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;
      color:#1b2430;background:#fff;line-height:1.65;}
 header{padding:18px 24px;border-bottom:1px solid #e6e9ef;display:flex;
        align-items:center;gap:20px;flex-wrap:wrap;}
 header b{font-size:18px;}
 header nav{margin-left:auto;display:flex;gap:18px;}
 header nav a{color:#4a5568;text-decoration:none;font-size:15px;}
 main{max-width:720px;margin:0 auto;padding:38px 24px 70px;}
 h1{font-size:30px;line-height:1.25;margin:0 0 16px;}
 h2{font-size:20px;margin:34px 0 10px;}
 p,li{font-size:17px;color:#2f3a49;}
 .lead{font-size:19px;color:#3d4757;}
 .call{background:#eef3ff;border-radius:12px;padding:20px 22px;margin:26px 0;}
 .call b{font-size:24px;display:block;margin-top:4px;}
 .box{border:1px solid #e6e9ef;border-radius:12px;padding:20px 22px;
      margin:22px 0;}
 label{display:block;font-size:14px;color:#55607048;margin:14px 0 5px;
       color:#556070;}
 input,textarea{width:100%;padding:11px 12px;border:1px solid #cdd4de;
                border-radius:8px;font-size:16px;font-family:inherit;}
 button{margin-top:18px;padding:13px 24px;background:#2563eb;color:#fff;
        border:0;border-radius:8px;font-size:16px;cursor:pointer;}
 footer{border-top:1px solid #e6e9ef;padding:24px;text-align:center;
        color:#77808f;font-size:14px;}
 footer a{color:#77808f;}
 .msg{margin-top:14px;font-size:15px;color:#2f6b3a;}
 .updated{color:#77808f;font-size:14px;}
"""


def page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} &mdash; {BRAND}</title><style>{CSS}</style></head><body>
<header><b>{BRAND}</b><nav>
  <a href="/">Home</a><a href="/signup">Sign up</a>
  <a href="/privacy">Privacy</a><a href="/terms">Terms</a>
</nav></header><main>{body}</main>
<footer>{BRAND} &middot; <a href="/privacy">Privacy</a> &middot;
<a href="/terms">Terms</a> &middot; <a href="/signup">Sign up</a></footer>
</body></html>"""


HOME = page("An assistant you reach by phone", f"""
<h1>An assistant you reach by telephone.</h1>
<p class="lead">For people who do not use the internet. No app, no website
to learn, no screen. You ring a number and ask for what you need.</p>

<div class="call">Call the assistant<b>{PHONE}</b></div>

<h2>What it does</h2>
<ul>
  <li>Reads your email to you, and replies when you tell it what to say</li>
  <li>Tells you what is on your calendar, and books things in</li>
  <li>Looks things up &mdash; a shop's opening hours, a phone number,
      how something works</li>
  <li>Places orders for everyday things</li>
</ul>

<h2>Who it is for</h2>
<p>Mostly older people, and people who are blind or partially sighted:
anyone who has email but no comfortable way to reach it. Family members
usually set it up, and then the person uses it entirely by voice.</p>

<h2>Setting it up</h2>
<p>Connecting an email account is the one step that needs a screen, and it
happens once. A family member can do it, or our office can help with the
customer on the line. After that, everything is done by telephone.</p>
<p><a href="/signup">Register someone for the service</a></p>

<h2>What we can and cannot see</h2>
<p>With permission, the assistant can read and send email, and read and
change the calendar, for the account that was connected. It cannot see
Google Drive, photos or contacts, and it cannot sign in to a Google
account. Permission can be withdrawn at any time, by asking us or from
the customer's own Google account settings.</p>
""")


SIGNUP = page("Register someone", f"""
<h1>Register someone for the service</h1>
<p class="lead">Fill this in and we will telephone to finish setting it up.
If you are arranging this for a parent or relative, put their details in
and yours underneath.</p>

<form class="box" onsubmit="return send(event)">
  <label>Name of the person who will use the assistant</label>
  <input id="n" required>
  <label>The telephone number they will call from</label>
  <input id="p" required placeholder="(845) 555 0123">
  <label>Your name and number, if you are arranging this for them</label>
  <input id="h" placeholder="optional">
  <label>Anything we should know</label>
  <textarea id="m" rows="3" placeholder="optional"></textarea>
  <button type="submit">Send</button>
  <div class="msg" id="msg"></div>
</form>

<p class="updated">We will only use these details to contact you about
setting up the service.</p>

<script>
async function send(e){{
  e.preventDefault();
  const msg = document.getElementById('msg');
  msg.textContent = 'Sending...';
  try{{
    const r = await fetch('/signup', {{method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{
        name: document.getElementById('n').value,
        phone: document.getElementById('p').value,
        helper: document.getElementById('h').value,
        note: document.getElementById('m').value}})}});
    msg.textContent = r.ok
      ? 'Thank you. We will telephone you to finish setting it up.'
      : 'Something went wrong. Please ring {PHONE} instead.';
  }}catch(err){{
    msg.textContent = 'Something went wrong. Please ring {PHONE} instead.';
  }}
  return false;
}}
</script>
""")


PRIVACY = page("Privacy", f"""
<h1>Privacy</h1>
<p class="updated">Last updated 14 September 2026</p>

<p>This explains what {BRAND} holds, why, and how to make us delete it.
It is written to be read aloud, because many of our customers cannot read
it themselves.</p>

<h2>What we hold</h2>
<ul>
  <li><b>Your name and telephone number</b>, so we know who is calling.</li>
  <li><b>Permission to reach your email and calendar</b>, if you granted
      it. We hold a token from Google, not your password.</li>
  <li><b>A record of your calls</b> &mdash; what was asked and what was
      done &mdash; so we can fix problems and improve the service.</li>
  <li><b>Delivery addresses and payment cards</b>, if you asked us to keep
      them for ordering.</li>
</ul>

<h2>What we never hold</h2>
<ul>
  <li>Your Google password. Passwords, PINs and security codes are removed
      from every record before it is stored.</li>
  <li>Anything in Google Drive, your photos, or your contacts. The
      permission we are granted does not reach them.</li>
</ul>

<h2>How Google data is used</h2>
<p>{BRAND}'s use of information received from Google APIs follows the
<a href="https://developers.google.com/terms/api-services-user-data-policy">
Google API Services User Data Policy</a>, including the Limited Use
requirements.</p>
<p>In plain words: your email and calendar information is used only to
provide the features you asked for on the telephone. It is not sold, not
shared with anyone else, not used for advertising, and not used to train
artificial intelligence models. Only the people needed to run and support
the service can reach it, and only when there is a reason to.</p>

<h2>Where it is kept</h2>
<p>On servers operated by Railway in the United States. Tokens, addresses
and card details are encrypted. Cards are held by our payment provider
rather than by us wherever possible.</p>

<h2>How long we keep it</h2>
<p>Call records are kept while you are a customer, so we can answer
questions about what was done. Everything is deleted when you ask.</p>

<h2>Getting it deleted</h2>
<p>Ring {PHONE} and say you want your account deleted. It removes your
email connection, your call history and your account, and we tell Google
to cancel the permission. You can also cancel the permission yourself at
any time in your Google account under Security, Third-party apps.</p>

<h2>Children</h2>
<p>This service is for adults. We do not knowingly register anyone under
eighteen.</p>

<h2>Changes and contact</h2>
<p>If this changes, we will tell customers on a call. Questions: ring
{PHONE} or write to {SUPPORT_EMAIL}.</p>
""")


TERMS = page("Terms", f"""
<h1>Terms of service</h1>
<p class="updated">Last updated 14 September 2026</p>

<h2>What this is</h2>
<p>{BRAND} is a telephone assistant. You ring, and it helps with email,
calendar, looking things up, and ordering. It works on your behalf, with
your permission, on the accounts you have connected.</p>

<h2>Your account</h2>
<p>We identify you by the telephone number you call from and a PIN. Keep
the PIN to yourself &mdash; anyone with it, calling from your number, can
reach your email.</p>

<h2>What the assistant will and will not do</h2>
<ul>
  <li>It will not send an email or place an order without reading it back
      and hearing you agree.</li>
  <li>It will not delete anything permanently. Deleted email goes to the
      bin and can be recovered.</li>
  <li>It will not discuss certain subjects, and will say so plainly.</li>
  <li>It can be wrong. It will tell you when it is unsure. For anything
      that matters &mdash; money, health, legal &mdash; check for yourself.</li>
</ul>

<h2>Orders and payment</h2>
<p>When we place an order for you, we are acting on your instruction. The
goods, the price, the delivery and any returns are between you and the
shop. We read the total back before placing anything.</p>

<h2>Stopping</h2>
<p>Ring and say you want to stop. There is no notice period. Ask us to
delete your information and we will.</p>

<h2>Limits</h2>
<p>We work to keep the service running but cannot promise it will always
be available. We are not responsible for losses caused by an order being
delayed, an email being missed, or the service being unavailable. Nothing
here removes rights you have under law.</p>

<h2>Contact</h2>
<p>{PHONE} &middot; {SUPPORT_EMAIL}</p>
""")
