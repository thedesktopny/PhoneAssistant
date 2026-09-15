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

Everything on these pages must be true of how the system actually works.
If the system changes, change the words here in the same commit.
"""
import html

BRAND = "Phone Assistant"
PHONE = "+1 484 518 2072"
PHONE_TEL = "+14845182072"
SUPPORT_EMAIL = "support@hellobuziness.com"

CSS = """
 :root{--ink:#1d2433;--soft:#4a5365;--mute:#7a8292;--line:#e4ddd0;
   --paper:#fbf8f2;--card:#ffffff;--accent:#1f5f5b;--accent-2:#e7f0ee;
   --warm:#b4652a;--warm-2:#f6ebdf;--bad:#9b2c2c;--good:#236041;}
 *{box-sizing:border-box}
 html{-webkit-text-size-adjust:100%}
 body{margin:0;background:var(--paper);color:var(--ink);
   font:18px/1.65 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
 a{color:var(--accent)}
 .serif,h1,h2,h3{font-family:"Iowan Old Style","Palatino Linotype",
   Palatino,Georgia,serif;font-weight:600;letter-spacing:-.01em}
 .wrap{max-width:1040px;margin:0 auto;padding-inline:22px}
 header{background:var(--paper);border-bottom:1px solid var(--line);
   position:sticky;top:0;z-index:5}
 header .wrap{display:flex;align-items:center;gap:18px;flex-wrap:wrap;
   padding-block:14px}
 .logo{display:flex;align-items:center;gap:10px;text-decoration:none;
   color:var(--ink);font-weight:700;font-size:19px}
 .logo i{display:grid;place-items:center;width:34px;height:34px;
   border-radius:10px;background:var(--accent);color:#fff;font-style:normal;
   font-size:17px}
 nav{margin-left:auto;display:flex;gap:6px 18px;flex-wrap:wrap}
 nav a{color:var(--soft);text-decoration:none;font-size:15px;
   padding:4px 0;border-bottom:2px solid transparent}
 nav a:hover,nav a.on{color:var(--ink);border-color:var(--warm)}
 main{padding-block:0 60px}
 section{padding-block:56px}
 section + section{border-top:1px solid var(--line)}
 h1{font-size:clamp(34px,6vw,54px);line-height:1.1;margin:0 0 18px}
 h2{font-size:clamp(26px,4vw,34px);line-height:1.2;margin:0 0 14px}
 h3{font-size:21px;margin:0 0 6px}
 p{margin:0 0 14px;color:var(--soft)}
 .lead{font-size:clamp(19px,2.4vw,22px);color:var(--soft);max-width:36em}
 .eyebrow{font-size:14px;letter-spacing:.08em;text-transform:uppercase;
   color:var(--warm);font-weight:700;margin-bottom:10px}
 .hero{display:grid;grid-template-columns:1.25fr .9fr;gap:40px;
   align-items:center;padding-block:64px}
 .dial{background:var(--card);border:1px solid var(--line);border-radius:22px;
   padding:28px;box-shadow:0 18px 40px -24px rgba(29,36,51,.35)}
 .dial small{display:block;color:var(--mute);font-size:15px}
 .dial a.num{display:block;font-size:clamp(28px,4vw,36px);font-weight:700;
   color:var(--ink);text-decoration:none;margin:4px 0 16px;
   font-variant-numeric:tabular-nums}
 .said{background:var(--accent-2);border-radius:14px;padding:14px 16px;
   margin-top:10px;font-size:16px;color:var(--ink)}
 .said b{display:block;font-size:13px;color:var(--accent);
   letter-spacing:.05em;text-transform:uppercase;margin-bottom:2px}
 .btns{display:flex;gap:12px;flex-wrap:wrap;margin-top:24px}
 .btn{display:inline-block;padding:14px 22px;border-radius:12px;
   font-size:17px;font-weight:600;text-decoration:none;border:0;
   cursor:pointer;font-family:inherit}
 .btn.main{background:var(--accent);color:#fff}
 .btn.main:hover{background:#174b48}
 .btn.alt{background:transparent;color:var(--ink);
   box-shadow:inset 0 0 0 2px var(--line)}
 .btn.alt:hover{box-shadow:inset 0 0 0 2px var(--warm)}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));
   gap:16px;margin-top:26px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:18px;
   padding:22px}
 .card .q{font-family:"Iowan Old Style",Georgia,serif;font-size:20px;
   color:var(--ink);margin-bottom:8px}
 .card p{font-size:16px;margin:0}
 .steps{counter-reset:s;display:grid;gap:14px;margin-top:26px}
 .step{display:grid;grid-template-columns:52px 1fr;gap:16px;
   align-items:start;background:var(--card);border:1px solid var(--line);
   border-radius:18px;padding:20px}
 .step:before{counter-increment:s;content:counter(s);display:grid;
   place-items:center;width:44px;height:44px;border-radius:50%;
   background:var(--warm-2);color:var(--warm);font-weight:700;font-size:19px}
 .step p{margin:0;font-size:16px}
 .two{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:22px}
 ul.ticks{list-style:none;padding:0;margin:0}
 ul.ticks li{position:relative;padding-left:30px;margin:0 0 10px;
   color:var(--soft);font-size:17px}
 ul.ticks li:before{content:"";position:absolute;left:4px;top:.55em;
   width:12px;height:7px;border-left:2.5px solid var(--good);
   border-bottom:2.5px solid var(--good);transform:rotate(-45deg)}
 ul.no li:before{border:0;width:12px;height:2.5px;background:var(--bad);
   transform:none;top:.8em}
 .narrow{max-width:720px}
 .doc h2{font-size:25px;margin-top:38px}
 .doc li{color:var(--soft);margin-bottom:8px}
 .updated{color:var(--mute);font-size:15px}
 form.box{background:var(--card);border:1px solid var(--line);
   border-radius:20px;padding:26px;margin-top:24px}
 label{display:block;font-size:15px;font-weight:600;color:var(--ink);
   margin:18px 0 6px}
 label span{font-weight:400;color:var(--mute)}
 input,textarea{width:100%;padding:13px 14px;border:1.5px solid #d3cbbd;
   border-radius:11px;font-size:18px;font-family:inherit;background:#fff;
   color:var(--ink)}
 input:focus,textarea:focus{outline:none;border-color:var(--accent);
   box-shadow:0 0 0 3px var(--accent-2)}
 input.code{font-size:28px;letter-spacing:.35em;text-align:center;
   font-variant-numeric:tabular-nums}
 form.box .btn{margin-top:22px}
 .agree{font-size:15px;color:var(--mute);margin:16px 0 0}
 .msg{margin-top:14px;font-size:16px;min-height:1.4em}
 .msg.ok{color:var(--good)} .msg.err{color:var(--bad)}
 .note{background:var(--warm-2);border-radius:14px;padding:16px 18px;
   color:var(--ink);font-size:16px;margin-top:18px}
 .centre{text-align:center;max-width:560px;margin:0 auto;padding-block:70px}
 .big{font-size:56px;line-height:1;margin-bottom:10px}
 footer{border-top:1px solid var(--line);padding-block:30px;
   color:var(--mute);font-size:15px}
 footer .wrap{display:flex;gap:10px 22px;flex-wrap:wrap;align-items:center}
 footer a{color:var(--mute)}
 footer .r{margin-left:auto}
 @media (max-width:760px){
   .hero,.two{grid-template-columns:1fr}
   .hero{padding-block:40px;gap:28px}
   section{padding-block:42px}
   nav{margin-left:0;width:100%}
   footer .r{margin-left:0}
 }
"""

NAV = [("/", "Home"), ("/#how", "How it works"), ("/signup", "Sign up"),
       ("/connect", "Connect account"), ("/privacy", "Privacy")]


def page(title: str, body: str, here: str = "") -> str:
    links = "".join(
        f'<a href="{href}"{" class=on" if href == here else ""}>{name}</a>'
        for href, name in NAV)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} &mdash; {BRAND}</title>
<meta name="description" content="A telephone assistant for people who do
not use the internet: email, calendar, looking things up and ordering, all
by voice.">
<style>{CSS}</style></head><body>
<header><div class="wrap">
  <a class="logo" href="/"><i>&#9742;</i>{BRAND}</a>
  <nav>{links}</nav>
</div></header>
<main>{body}</main>
<footer><div class="wrap">
  <span>&copy; {BRAND}</span>
  <a href="/privacy">Privacy</a><a href="/terms">Terms</a>
  <a href="/signup">Sign up</a><a href="/connect">Connect account</a>
  <span class="r"><a href="tel:{PHONE_TEL}">{PHONE}</a> &middot;
  <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a></span>
</div></footer>
</body></html>"""


HOME = page("A telephone assistant", f"""
<div class="wrap">
<div class="hero">
  <div>
    <div class="eyebrow">No internet needed</div>
    <h1>Your email, your diary and your errands, by telephone.</h1>
    <p class="lead">For people who don't use the internet, or would rather not.
    There's no app, no website to learn and no screen. You ring one number
    and ask for what you need.</p>
    <div class="btns">
      <a class="btn main" href="/signup">Get started</a>
      <a class="btn alt" href="#how">How it works</a>
    </div>
  </div>
  <div class="dial">
    <small>Call the assistant</small>
    <a class="num" href="tel:{PHONE_TEL}">{PHONE}</a>
    <div class="said"><b>You say</b>&ldquo;Read me my new emails.&rdquo;</div>
    <div class="said"><b>It says</b>&ldquo;You have two new messages. The first
    is from the dentist, confirming Tuesday at ten&hellip;&rdquo;</div>
  </div>
</div>
</div>

<section><div class="wrap">
  <div class="eyebrow">What you can ask</div>
  <h2>Just say it the way you would to a person.</h2>
  <div class="grid">
    <div class="card"><div class="q">&ldquo;Anything new in my email?&rdquo;</div>
      <p>It reads your messages out, replies with the words you give it, and
      reads a reply back to you before it sends anything.</p></div>
    <div class="card"><div class="q">&ldquo;What do I have on Thursday?&rdquo;</div>
      <p>It tells you what's in your calendar, adds appointments and keeps
      your to-do list.</p></div>
    <div class="card"><div class="q">&ldquo;What's my son's number?&rdquo;</div>
      <p>It looks people up in your contacts and adds new ones.</p></div>
    <div class="card"><div class="q">&ldquo;Read me the letter from the school.&rdquo;</div>
      <p>It finds documents in your Google Drive, reads them to you, and
      writes letters, lists and spreadsheets for you.</p></div>
    <div class="card"><div class="q">&ldquo;When does the pharmacy close?&rdquo;</div>
      <p>It finds opening hours, phone numbers and how-to instructions, and
      tells you when it isn't sure.</p></div>
    <div class="card"><div class="q">&ldquo;Order more of the usual.&rdquo;</div>
      <p>It places orders for everyday things. It always reads the total to
      you and waits for your yes.</p></div>
  </div>
</div></section>

<section id="how"><div class="wrap">
  <div class="eyebrow">How it works</div>
  <h2>Set it up once. After that, just call.</h2>
  <div class="steps">
    <div class="step"><div><h3>Sign up</h3>
      <p>Fill in <a href="/signup">the short form</a> or call us, and we'll
      call you back.</p></div></div>
    <div class="step"><div><h3>We set up your account</h3>
      <p>We register the phone number you'll call from, and you choose a PIN
      so that only you can reach your account.</p></div></div>
    <div class="step"><div><h3>Connect your Google account</h3>
      <p>You only do this once. The assistant gives you a short code. Enter
      it on <a href="/connect">our connect page</a> and approve access on
      Google's own screen. If you don't have a computer or smartphone, call
      us and we'll help.</p></div></div>
    <div class="step"><div><h3>Just call</h3>
      <p>From then on, everything is done by voice.</p></div></div>
  </div>
</div></section>

<section><div class="wrap">
  <div class="eyebrow">Made for our community</div>
  <h2>Careful by design.</h2>
  <div class="two">
    <div class="card"><h3>It always checks first</h3>
      <ul class="ticks">
        <li>It reads an email back before sending it</li>
        <li>It reads an order total back before placing the order</li>
        <li>Nothing is deleted for good. Deleted mail goes to the bin</li>
        <li>It tells you when it isn't sure</li>
      </ul></div>
    <div class="card"><h3>What it stays away from</h3>
      <ul class="ticks no">
        <li>News, sports, gossip and entertainment</li>
        <li>Subjects that aren't appropriate, which it declines plainly</li>
        <li>Rulings on halacha. It will tell you to ask your rav</li>
      </ul></div>
  </div>
</div></section>

<section><div class="wrap"><div style="max-width:720px">
  <div class="eyebrow">Your information</div>
  <h2>What we can and cannot see.</h2>
  <p>With your permission, the assistant can read and send your email,
  manage your calendar and to-do list, look up and add contacts, and read,
  create and edit documents and spreadsheets in your Google Drive. It
  always reads a change back before making it, it never deletes or shares
  your files, and it can't see your photos. Google shows you each of these permissions
  before you agree.</p>
  <p>Email and calendar information is used only to do what you asked for
  on the phone. It is never sold, never used for advertising and never used
  to train AI. Permission can be withdrawn at any time, either by asking us
  or in your Google account settings.</p>
  <p><a href="/privacy">Read the full privacy policy</a></p>
</div></div></section>
""", "/")


SIGNUP = page("Get started", f"""
<div class="wrap narrow"><section>
<div class="eyebrow">Get started</div>
<h1>Sign up for {BRAND}</h1>
<p class="lead">Leave your details and we'll call you to set up your
account. It takes a few minutes, all by phone.</p>

<form class="box" id="f">
  <label for="n">Your name</label>
  <input id="n" required autocomplete="name">
  <label for="p">The phone number you'll call from</label>
  <input id="p" required inputmode="tel" placeholder="(845) 555 0123">
  <label for="h">Another contact <span>optional, if someone else should
  hear from us about setup</span></label>
  <input id="h" placeholder="Name and phone number">
  <label for="m">Anything we should know <span>optional</span></label>
  <textarea id="m" rows="3"></textarea>
  <p class="agree">By sending this you agree to our <a href="/terms">Terms</a>
  and <a href="/privacy">Privacy Policy</a>. We'll only use these details to
  set up your service.</p>
  <button class="btn main" type="submit">Send</button>
  <div class="msg" id="msg" role="status"></div>
</form>

<div class="note">Your Google account is connected separately, by you, on
Google's own screen. You'll see exactly what you're allowing before you
agree.</div>
</section></div>
""" + """<script>
document.getElementById('f').addEventListener('submit', async function(e){
  e.preventDefault();
  var msg = document.getElementById('msg');
  var v = function(id){ return document.getElementById(id).value; };
  msg.className = 'msg'; msg.textContent = 'Sending...';
  try {
    var r = await fetch('/signup', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name:v('n'), phone:v('p'), helper:v('h'),
                            note:v('m')})});
    if (!r.ok) throw 0;
    msg.className = 'msg ok';
    msg.textContent = "Thank you. We'll call you shortly to set up your account.";
    this.reset();
  } catch (err) {
    msg.className = 'msg err';
    msg.textContent = 'Something went wrong. Please call """ + PHONE + """ instead.';
  }
});
</script>
""", "/signup")


CONNECT = page("Connect an email account", f"""
<div class="wrap narrow"><section>
<div class="eyebrow">Connect email</div>
<h1>Connect an email account</h1>
<p class="lead">You only need to do this once. Call the assistant, ask for a
<b>connect code</b>, and enter it below with the phone number you call
from.</p>

<form class="box" id="f">
  <label for="p">The phone number you call the assistant from</label>
  <input id="p" required inputmode="tel" autocomplete="tel"
         placeholder="(845) 555 0123">
  <label for="c">Your six-digit code</label>
  <input id="c" class="code" required inputmode="numeric" maxlength="7"
         autocomplete="one-time-code" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;">
  <button class="btn main" type="submit">Continue</button>
  <div class="msg" id="msg" role="status"></div>
</form>

<div class="note">Next, Google shows you exactly what the assistant will be
allowed to do, and you decide whether to agree. Only the owner of the
Google account should approve. You can remove access at any time.</div>

<p class="updated" style="margin-top:20px">Codes last about an hour. If
yours has stopped working, ring {PHONE} and ask for a new one.</p>
</section></div>
""" + """<script>
document.getElementById('f').addEventListener('submit', async function(e){
  e.preventDefault();
  var msg = document.getElementById('msg');
  msg.className = 'msg'; msg.textContent = 'Checking...';
  try {
    var r = await fetch('/connect', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({phone: document.getElementById('p').value,
                            code: document.getElementById('c').value})});
    var d = await r.json().catch(function(){ return {}; });
    if (r.ok && d.url) { msg.textContent = 'One moment...';
                         location.href = d.url; return; }
    msg.className = 'msg err';
    msg.textContent = d.detail || 'That did not work. Please try again.';
  } catch (err) {
    msg.className = 'msg err';
    msg.textContent = 'Something went wrong. Please try again.';
  }
});
</script>
""", "/connect")


def _centre(title: str, icon: str, heading: str, body: str) -> str:
    return page(title, f"""<div class="wrap"><div class="centre">
<div class="big">{icon}</div><h1>{heading}</h1>{body}</div></div>""")


def connect_confirm(name: str, go_url: str) -> str:
    """Shown before Google, so they can see whose account they're joining."""
    return _centre("Connect your account", "&#9993;",
                   "Connect your Google account", f"""
<p class="lead">This connects your Google account to your phone
assistant.</p>
<div class="said" style="text-align:left;margin:22px 0"><b>Account
holder</b>{html.escape(name)}</div>
<p>If that isn't you, close this page.</p>
<p>Google will ask you to sign in and show you exactly what you're
allowing.</p>
<div class="btns" style="justify-content:center">
  <a class="btn main" href="{html.escape(go_url)}">Continue to Google</a></div>
""")


LINK_EXPIRED = _centre("This link has expired", "&#8987;",
                       "This link has expired", f"""
<p class="lead">For your security, connect links only work for a short
time.</p>
<p>Call {PHONE}, ask the assistant for a new connect code, and enter it on
<a href="/connect">the connect page</a>.</p>""")


def connected(email: str) -> str:
    who = f" <b>{html.escape(email)}</b>" if email else ""
    return _centre("Connected", "&#10003;", "You're connected", f"""
<p class="lead">Your assistant can now reach{who or " your account"}.</p>
<p>You can close this page. Next time you call, just ask.</p>""")


NOT_CONNECTED = _centre("Not connected", "&#10005;", "Nothing was connected",
                        f"""
<p class="lead">Access wasn't approved, so nothing has been connected or
changed.</p>
<p>If that was a mistake, <a href="/connect">start again</a>. Codes last
about an hour.</p>""")


PRIVACY = page("Privacy", f"""
<div class="wrap narrow doc"><section>
<div class="eyebrow">Privacy</div>
<h1>Privacy policy</h1>
<p class="updated">Last updated 14 September 2026</p>

<p>This explains what {BRAND} holds, why, and how to have it deleted. It is
written to be read aloud, because many of our customers can't read it
themselves.</p>

<h2>What we hold</h2>
<ul>
  <li><b>Your name and telephone number</b>, so we know who is calling.</li>
  <li><b>Permission to reach your Google account</b>, if you granted it:
      your email, calendar, contacts, to-do list and Google Drive. This is held as an encrypted token from Google.</li>
  <li><b>A record of your calls</b>: what was asked and what was done.
      We keep it so we can fix problems and improve the service.</li>
  <li><b>Delivery addresses and payment cards</b>, if you asked us to keep
      them for ordering.</li>
</ul>

<h2>What we never keep</h2>
<ul>
  <li>Passwords, PINs and security codes. These are removed from every
      record before it is stored.</li>
  <li>Copies of your Drive files or contacts. They're read from Google
      when you ask and aren't stored by us. Files are only created or
      changed when you ask, after the change is read back to you, and are
      never deleted or shared by us.</li>
  <li>Your photos. The permission we are granted doesn't reach them.</li>
</ul>

<h2>How Google data is used</h2>
<p>{BRAND}'s use of information received from Google APIs follows the
<a href="https://developers.google.com/terms/api-services-user-data-policy">
Google API Services User Data Policy</a>, including the Limited Use
requirements.</p>
<p>In plain words, information from your Google account is used only to
provide the features you asked for on the telephone. It is not sold, not
shared with anyone else, not used for advertising, and not used to train
artificial intelligence models. Only the people needed to run and support
the service can reach it, and only when there is a reason to.</p>

<h2>Where it is kept</h2>
<p>On servers operated by Railway in the United States. Tokens, addresses
and card details are encrypted. Wherever possible, cards are held by our
payment provider rather than by us.</p>

<h2>How long we keep it</h2>
<p>Call records are kept while you are a customer, so we can answer
questions about what was done. Everything is deleted when you ask.</p>

<h2>Getting it deleted</h2>
<p>Ring {PHONE} and say you want your account deleted. This removes your
email connection, your call history and your account, and we tell Google
to cancel the permission. You can also cancel the permission yourself at
any time in your Google account, under Security, Third-party apps.</p>

<h2>Children</h2>
<p>This service is for adults. We do not knowingly register anyone under
eighteen.</p>

<h2>Changes and contact</h2>
<p>If this policy changes, we'll tell customers on a call. For questions,
ring {PHONE} or write to <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>.</p>
</section></div>
""", "/privacy")


TERMS = page("Terms", f"""
<div class="wrap narrow doc"><section>
<div class="eyebrow">Terms</div>
<h1>Terms of service</h1>
<p class="updated">Last updated 14 September 2026</p>

<h2>What this is</h2>
<p>{BRAND} is a telephone assistant. You ring it, and it helps with email,
your calendar, looking things up and ordering. It works on your behalf,
with your permission, on the accounts you have connected.</p>

<h2>Your account</h2>
<p>We identify you by the telephone number you call from and a PIN. Keep
the PIN to yourself: anyone who has it and calls from your number can reach
your email.</p>

<h2>What the assistant will and will not do</h2>
<ul>
  <li>It will not send an email or place an order without reading it back
      and hearing you agree.</li>
  <li>It will not delete anything permanently. Deleted email goes to the
      bin and can be recovered.</li>
  <li>It will not discuss certain subjects, and will say so plainly.</li>
  <li>It can be wrong. It will tell you when it's unsure. For anything
      that matters, such as money, health or legal questions, check for
      yourself.</li>
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
<p>{PHONE} &middot; <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a></p>
</section></div>
""")
