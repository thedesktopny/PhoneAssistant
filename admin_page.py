"""The admin panel, as one page of HTML.

Markup and the script that drives it, nothing else. Staff see what
the system did; they never act on a customer's behalf from here.
Kept out of main.py so nine hundred lines of HTML do not sit in the
middle of the backend.
"""


ADMIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Phone Assistant &mdash; Admin</title>
<style>
 *{box-sizing:border-box}
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;
      color:#e6e6e6;margin:0;padding:0;}
 header{display:flex;align-items:center;gap:18px;padding:16px 24px;
        border-bottom:1px solid #262b36;background:#141821;
        position:sticky;top:0;z-index:5;}
 header h1{font-size:17px;margin:0;}
 nav{display:flex;gap:4px;margin-left:auto;flex-wrap:wrap;}
 nav a{padding:8px 14px;border-radius:6px;color:#8b94a7;text-decoration:none;
       font-size:14px;cursor:pointer;}
 nav a.on{background:#232936;color:#fff;}
 main{padding:22px 24px;max-width:1150px;}
 .card{background:#171a21;border:1px solid #262b36;border-radius:10px;
       padding:18px;margin-bottom:18px;}
 .card h2{font-size:15px;margin:0 0 4px;}
 .hint{color:#8b94a7;font-size:13px;margin-bottom:10px;}
 label{display:block;font-size:12px;color:#8b94a7;margin:10px 0 4px;}
 input{width:100%;padding:9px 10px;background:#0f1115;border:1px solid #2c3240;
       border-radius:6px;color:#e6e6e6;font-size:14px;}
 .search{max-width:340px;display:inline-block;margin-right:8px;}
 button{padding:9px 15px;background:#3b82f6;border:0;border-radius:6px;
        color:#fff;font-size:14px;cursor:pointer;}
 button.sec{background:#2c3240;padding:6px 12px;}
 table{width:100%;border-collapse:collapse;margin-top:12px;font-size:14px;}
 th{text-align:left;color:#8b94a7;font-weight:500;font-size:12px;
    padding:8px 6px;border-bottom:1px solid #262b36;}
 td{padding:10px 6px;border-bottom:1px solid #1c212b;vertical-align:top;}
 tr.det td{background:#12151c;}
 .ok{color:#4ade80;} .no{color:#f87171;} .warn{color:#fbbf24;}
 .tag{display:inline-block;background:#232936;border-radius:4px;
      padding:2px 7px;margin:2px 3px 2px 0;font-size:12px;color:#c3cad8;}
 .err{color:#f87171;font-size:13px;display:block;margin-top:5px;
      white-space:pre-wrap;line-height:1.5;}
 a.btn{display:inline-block;padding:6px 12px;background:#2c3240;color:#e6e6e6;
       border-radius:6px;text-decoration:none;font-size:13px;}
 .msg{margin-top:10px;font-size:13px;color:#8b94a7;}
 .nums{display:flex;gap:26px;flex-wrap:wrap;}
 .num b{display:block;font-size:24px;color:#fff;}
 .num span{font-size:12px;color:#8b94a7;}
 pre{white-space:pre-wrap;background:#0f1115;padding:14px;border-radius:6px;
     font-size:13px;line-height:1.65;max-height:460px;overflow:auto;
     margin:10px 0 0;}
 .page{display:none;} .page.on{display:block;}
</style></head><body>
<header>
  <h1>Phone Assistant</h1>
  <nav>
    <a data-p="live">Live</a>
    <a data-p="changes">What it did</a>
    <a data-p="reviews">Call checks</a>
    <a data-p="know">Who they are</a>
    <a data-p="blocks">Blocked by</a>
    <a data-p="costs">Costs</a>
    <a data-p="overview" class="on">Overview</a>
    <a data-p="calls">Calls</a>
    <a data-p="customers">Customers</a>
    <a data-p="followups">To do</a>
    <a data-p="signins">Sign-ins</a>
    <a data-p="orders">Orders</a>
    <a data-p="sites">Websites</a>
    <a data-p="jobs">Site logins</a>
    <a data-p="texts">Texts</a>
  </nav>
  <button class="sec" onclick="fetch('/admin/logout',{method:'POST'})
    .then(()=>location.reload())">Sign out</button>
</header>
<main>

<section class="page" id="p-changes">
  <div class="card"><h2>What the assistant did for customers</h2>
    <div class="hint">Every email sent, file changed, card charged, contact
      saved. In plain words, newest first. Passwords and card numbers never
      appear here.</div>
    <label style="display:inline-block;margin:8px 14px 8px 0">Show
      <select id="charea" style="width:auto;margin-left:6px"
              onchange="loadChanges()">
        <option value="">everything</option>
        <option value="email">email</option>
        <option value="drive">documents</option>
        <option value="calendar">calendar</option>
        <option value="contacts">contacts</option>
        <option value="to-do">to-do list</option>
        <option value="payment">payments</option>
        <option value="card">cards</option>
        <option value="login">logins</option>
        <option value="mailbox">mailboxes</option>
      </select></label>
    <button class="sec" onclick="loadChanges()">Refresh</button>
    <table><thead><tr><th>When</th><th>Who</th><th>What</th>
    <th>Details</th><th>Call</th><th>Can it be undone?</th></tr></thead>
    <tbody id="chrows"><tr><td colspan="6" class="hint">Loading&hellip;</td></tr>
    </tbody></table>
  </div>
</section>

<section class="page" id="p-know">
  <div class="card"><h2>What we know about each customer</h2>
    <div class="hint">Built up after every call: how they need to be spoken
      to, who their people are, what they order. The assistant reads this
      before it speaks to them. Passwords and PINs are never kept here.
      Anything you write in "the office says" is yours - the system never
      overwrites it, and it is read first.</div>
    <button class="sec" onclick="loadKnow()">Refresh</button>
    <div id="knowrows" class="hint">Loading&hellip;</div>
  </div>
</section>

<section class="page" id="p-blocks">
  <div class="card"><h2>Which sites refuse us, and why</h2>
    <div class="hint">Every refusal, named. A puzzle or a fingerprint wall
      will not open for anyone - those shops need a sanctioned route or a
      person. An address refusal, a rate limit or a login wall might open,
      and the advice column says what would change it.</div>
    <button class="sec" onclick="loadBlocks()">Refresh</button>
    <table><thead><tr><th>Site</th><th>Kind</th><th>Who blocks</th>
    <th>Times</th><th>Worth retrying?</th><th>What would change it</th>
    </tr></thead>
    <tbody id="blkrows"><tr><td colspan="6" class="hint">Loading&hellip;</td></tr>
    </tbody></table>
    <h2 style="font-size:16px;margin-top:22px">Most recent</h2>
    <table><thead><tr><th>When</th><th>Site</th><th>Kind</th>
    <th>What the page said</th></tr></thead>
    <tbody id="blkrecent"></tbody></table>
  </div>
</section>

<section class="page" id="p-reviews">
  <div class="card"><h2>Calls the system checked itself</h2>
    <div class="hint">After every call, the assistant's own words are read
      back against what actually happened. Anything it said that the record
      doesn't support is listed here, so nobody has to ring in to report
      it.</div>
    <button class="sec" onclick="loadReviews()">Refresh</button>
    <table><thead><tr><th>When</th><th>Call</th><th>Who</th>
    <th>What it found</th></tr></thead>
    <tbody id="rvrows"><tr><td colspan="4" class="hint">Loading&hellip;</td></tr>
    </tbody></table>
  </div>
</section>

<section class="page" id="p-costs">
  <div class="card"><h2>What calls cost</h2>
    <div class="hint">Real token counts from each call, priced with the
      rates in Railway. Verify the rates against your own invoices before
      you price customers.</div>
    <label style="display:inline-block;margin:8px 14px 8px 0">Period
      <select id="costdays" style="width:auto;margin-left:6px">
        <option value="1">today</option>
        <option value="7">7 days</option>
        <option value="30" selected>30 days</option>
        <option value="90">90 days</option>
      </select></label>
    <div id="costtop" style="margin:14px 0"></div>
    <h2 class="no">Where the money goes</h2>
    <table><tbody id="costparts"></tbody></table>
    <h2 class="no" style="margin-top:18px">By customer</h2>
    <table><thead><tr><th>Customer</th><th>Calls</th><th>Minutes</th>
      <th>Cost</th></tr></thead><tbody id="costcust"></tbody></table>
  </div>
</section>

<section class="page" id="p-live">
  <div class="card"><h2>Live log</h2>
    <div class="hint">Everything as it happens: sign-in steps, what Google
      says, browser jobs, orders, tool errors. Updates every 2 seconds.
      Errors in red, notes for the office in amber.</div>
    <label style="display:inline-block;margin-right:14px">
      <input type="checkbox" id="live_pause" style="width:auto"> pause</label>
    <label style="display:inline-block;margin-right:14px">
      <input type="checkbox" id="live_err" style="width:auto"> errors only</label>
    <button class="sec" onclick="liveClear()">Clear view</button>
    <pre id="livelog" style="max-height:70vh;min-height:300px;margin-top:12px">
Waiting for events…</pre>
  </div>
</section>

<section class="page on" id="p-overview">
  <div class="card" id="alertcard" style="display:none;
       border-color:#7f1d1d;background:#1b1113">
    <h2 class="no">Needs attention</h2>
    <div id="alerts"></div>
  </div>
  <div class="card"><h2>Last 7 days</h2>
    <div class="nums" id="stats"><span class="hint">Loading&hellip;</span></div>
  </div>
  <div class="card"><h2>Latest calls</h2>
    <table><thead><tr><th>#</th><th>Who</th><th>When</th><th>Length</th>
    <th>What they wanted</th></tr></thead>
    <tbody id="mini"><tr><td colspan="5" class="hint">Loading&hellip;</td></tr>
    </tbody></table>
  </div>
</section>

<section class="page" id="p-calls">
  <div class="card"><h2>Calls</h2>
    <div class="hint">Search by name, number, call id, or what they asked for.</div>
    <div class="search"><input id="q_calls" placeholder="e.g. David, 3476, send_email"
      onkeydown="if(event.key==='Enter')loadCalls()"></div>
    <button onclick="loadCalls()">Search</button>
    <button class="sec" onclick="document.getElementById('q_calls').value='';loadCalls()">
      Clear</button>
    <table><thead><tr><th>#</th><th>Who</th><th>When</th><th>Length</th>
    <th>PIN</th><th>What they wanted</th><th></th></tr></thead>
    <tbody id="calls"><tr><td colspan="7" class="hint">Loading&hellip;</td></tr>
    </tbody></table>
  </div>
</section>

<section class="page" id="p-customers">
  <div class="card"><h2>Customers</h2>
    <div class="search"><input id="q_cust" placeholder="name, number or email"
      onkeydown="if(event.key==='Enter')load()"></div>
    <button onclick="load()">Search</button>
    <button class="sec" onclick="document.getElementById('q_cust').value='';load()">
      Clear</button>
    <table><thead><tr><th>ID</th><th>Name</th><th>Phone</th>
    <th>Mailboxes</th><th></th></tr></thead>
    <tbody id="rows"><tr><td colspan="5" class="hint">Loading&hellip;</td></tr>
    </tbody></table>
  </div>
  <div class="card"><h2>Invite someone</h2>
    <div class="hint">Make a code and give it to them. They call
      +1 484 518 2072 from their own phone, say the code, say their name and
      choose their own PIN &mdash; and they are set up. Each code works once,
      for 14 days. It is shown only now, so write it down or send it.
      Customers can add a second phone themselves: they ask for it by voice
      and get their own code. You can also use "+ Phone" below.</div>
    <label>Who it's for (only you see this)</label>
    <input id="inv_note" placeholder="e.g. Yossi, freelancer">
    <button onclick="makeInvite()">Make a code</button>
    <div id="inv_new" style="font-size:30px;font-weight:bold;
      letter-spacing:6px;margin:10px 0"></div>
    <table><thead><tr><th>For</th><th>Kind</th><th>Made</th><th>Status</th>
    <th>Signed up as</th><th></th></tr></thead>
    <tbody id="inv_rows"><tr><td colspan="5" class="hint">Loading&hellip;</td>
    </tr></tbody></table>
  </div>
  <div class="card"><h2>Add a customer by hand</h2>
    <div class="hint">For when someone can't sign up by phone. The PIN
      below is made fresh each time &mdash; tell it to them, or type the one
      they want.</div>
    <label>Name</label><input id="n">
    <label>Their phone number</label><input id="p" placeholder="+18455551234">
    <label>PIN</label><input id="k" value="">
    <button onclick="add()">Create</button>
    <div class="msg" id="msg"></div>
  </div>
</section>

<section class="page" id="p-followups">
  <div class="card"><h2>Needs attention</h2>
    <div class="hint">Anything the assistant couldn't finish, or that a
      caller asked to be passed on.</div>
    <button class="sec" onclick="loadFu()">Refresh</button>
    <button class="sec" onclick="fuAll=!fuAll;loadFu()">Show/hide done</button>
    <table><thead><tr><th>When</th><th>Who</th><th>Why</th><th>Note</th>
    <th>Call</th><th></th></tr></thead>
    <tbody id="furows"><tr><td colspan="6" class="hint">Loading&hellip;</td></tr>
    </tbody></table>
  </div>
</section>

<section class="page" id="p-signins">
  <div class="card"><h2>Email sign-in attempts</h2>
    <div class="hint">Every step of each attempt, with the reason it stopped.</div>
    <button class="sec" onclick="loadOb()">Refresh</button>
    <table><thead><tr><th>#</th><th>Who</th><th>Address</th><th>When</th>
    <th>Result</th><th></th></tr></thead>
    <tbody id="obrows"><tr><td colspan="6" class="hint">Loading&hellip;</td></tr>
    </tbody></table>
  </div>
</section>

<section class="page" id="p-orders">
  <div class="card"><h2>Orders</h2>
    <div class="hint">Every order a customer confirmed on the phone, and what
      happened to it. Card numbers never appear here.</div>
    <button class="sec" onclick="loadOrders()">Refresh</button>
    <table><thead><tr><th>#</th><th>Who</th><th>Site</th><th>Item</th>
    <th>Expected</th><th>Status</th><th>Confirmation</th><th>When</th>
    <th></th></tr></thead>
    <tbody id="orderrows"><tr><td colspan="9" class="hint">Loading&hellip;</td>
    </tr></tbody></table>
  </div>
</section>

<section class="page" id="p-sites">
  <div class="card"><h2>What customers ask for, by site</h2>
    <div class="hint">Last 30 days. "Learned" means it ran from saved steps
      with no thinking; "fell back" means the saved steps broke and it
      worked it out fresh. Nothing here needs you to do anything.</div>
    <button class="sec" onclick="loadSites()">Refresh</button>
    <table><thead><tr><th>Site</th><th>Requests</th><th>Worked</th>
    <th>Learned</th><th>Fell back</th><th>Avg time</th>
    <th>Most asked</th></tr></thead>
    <tbody id="siterows"><tr><td colspan="7" class="hint">Loading&hellip;</td>
    </tr></tbody></table>
  </div>
  <div class="card"><h2>What it has learned</h2>
    <div class="hint">Steps it recorded after succeeding once. It retires a
      recipe by itself after repeated failures and re-learns.</div>
    <table><thead><tr><th>Site</th><th>Task</th><th>Example</th>
    <th>Worked</th><th>Failed</th><th>Last ok</th><th></th></tr></thead>
    <tbody id="reciperows"><tr><td colspan="7" class="hint">Loading&hellip;</td>
    </tr></tbody></table>
  </div>
  <div class="card"><h2>Recent website activity</h2>
    <div class="hint">Every request, including fallbacks and failures.</div>
    <table><thead><tr><th>When</th><th>Site</th><th>Task</th>
    <th>What they asked</th><th>How</th><th>Result</th><th>Time</th>
    <th></th></tr></thead>
    <tbody id="siteevents"><tr><td colspan="8" class="hint">Loading&hellip;</td>
    </tr></tbody></table>
  </div>
</section>

<section class="page" id="p-jobs">
  <div class="card"><h2>Browser queue</h2>
    <div class="nums" id="qhealth"><span class="hint">Loading&hellip;</span></div>
  </div>
  <div class="card"><h2>Site sign-ins</h2>
    <div class="hint">Amazon, Walmart, Temu. Sessions are kept so customers
      aren't asked to sign in again each time.</div>
    <button class="sec" onclick="loadJobs()">Refresh</button>
    <table><thead><tr><th>#</th><th>Who</th><th>Site</th><th>When</th>
    <th>Result</th><th></th></tr></thead>
    <tbody id="jobrows"><tr><td colspan="6" class="hint">Loading&hellip;</td></tr>
    </tbody></table>
  </div>
</section>

<section class="page" id="p-texts">
  <div class="card"><h2>Text delivery</h2>
    <div class="hint">What the carrier reported for each message.</div>
    <button class="sec" onclick="loadDlr()">Refresh</button>
    <table><thead><tr><th>When</th><th>To</th><th>Status</th>
    <th>Detail</th></tr></thead>
    <tbody id="dlr"><tr><td colspan="4" class="hint">Loading&hellip;</td></tr>
    </tbody></table>
  </div>
</section>

</main>
<script>
function esc(x){ return String(x==null?'':x)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

document.querySelectorAll('nav a').forEach(function(a){
  a.onclick = function(){
    document.querySelectorAll('nav a').forEach(function(b){
      b.classList.remove('on'); });
    a.classList.add('on');
    document.querySelectorAll('.page').forEach(function(s){
      s.classList.remove('on'); });
    document.getElementById('p-'+a.dataset.p).classList.add('on');
    if(a.dataset.p === 'changes') loadChanges();
    if(a.dataset.p === 'reviews') loadReviews();
    if(a.dataset.p === 'know') loadKnow();
    if(a.dataset.p === 'blocks') loadBlocks();
  };
});

function taskCell(c){
  var t = (c.tasks||[]).map(function(x){
    return '<span class="tag">'+esc(x)+'</span>'; }).join('');
  if(!t) t = '<span class="hint">just talking</span>';
  if(c.slowest_ms > 2500) t += '<span class="warn"> slow '+c.slowest_ms+'ms</span>';
  (c.problems||[]).forEach(function(p){
    t += '<span class="err">'+esc(p)+'</span>'; });
  return t;
}

async function loadStats(){
  try{
    const d = await (await fetch('/stats?days=7')).json();
    const tasks = (d.top_tasks||[]).map(function(t){
      return '<span class="tag">'+esc(t[0])+' '+t[1]+'</span>'; }).join('')
      || '&mdash;';
    document.getElementById('stats').innerHTML =
      '<div class="num"><b>'+d.calls+'</b><span>calls</span></div>'+
      '<div class="num"><b>'+d.verified+'</b><span>passed PIN</span></div>'+
      '<div class="num"><b>'+d.avg_seconds+'s</b><span>avg length</span></div>'+
      '<div class="num"><b>'+d.avg_tool_ms+'ms</b><span>avg lookup</span></div>'+
      '<div class="num" style="flex:1"><span>most asked for</span>'+
      '<div style="margin-top:6px">'+tasks+'</div></div>';
  }catch(e){
    document.getElementById('stats').innerHTML =
      '<span class="no">'+esc(e.message)+'</span>'; }
}

async function loadCalls(){
  const q = (document.getElementById('q_calls')||{}).value || '';
  const tb = document.getElementById('calls');
  const mini = document.getElementById('mini');
  try{
    const d = await (await fetch('/calls?limit=50&q='+
      encodeURIComponent(q))).json();
    if(!d.length){
      tb.innerHTML = '<tr><td colspan="7" class="hint">No calls found.</td></tr>';
      mini.innerHTML = '<tr><td colspan="5" class="hint">No calls yet.</td></tr>';
      return;
    }
    tb.innerHTML = d.map(function(c){
      return '<tr><td>'+c.call_id+'</td>'+
        '<td>'+esc(c.who)+'<br><span class="hint">'+esc(c.from)+'</span></td>'+
        '<td>'+esc(c.started)+'</td>'+
        '<td>'+c.seconds+'s<br><span class="hint">'+c.turns+' turns</span></td>'+
        '<td>'+(c.verified?'<span class="ok">ok</span>'
                          :'<span class="no">no</span>')+'</td>'+
        '<td>'+taskCell(c)+'</td>'+
        '<td><button class="sec" onclick="showTx('+c.call_id+',this)">'+
        'Transcript</button></td></tr>'+
        '<tr class="det" id="det'+c.call_id+'" style="display:none">'+
        '<td colspan="7"><pre id="tx'+c.call_id+'"></pre></td></tr>';
    }).join('');
    mini.innerHTML = d.slice(0,6).map(function(c){
      return '<tr><td>'+c.call_id+'</td><td>'+esc(c.who)+'</td>'+
        '<td>'+esc(c.started)+'</td><td>'+c.seconds+'s</td>'+
        '<td>'+taskCell(c)+'</td></tr>'; }).join('');
  }catch(e){
    tb.innerHTML = '<tr><td colspan="7" class="no">'+esc(e.message)+'</td></tr>'; }
}

async function showTx(id, btn){
  const row = document.getElementById('det'+id);
  const pre = document.getElementById('tx'+id);
  if(row.style.display === 'table-row'){
    row.style.display = 'none'; btn.textContent = 'Transcript'; return; }
  row.style.display = 'table-row';
  btn.textContent = 'Hide';
  pre.textContent = 'Loading…';
  try{
    const r = await fetch('/calls/'+id);
    if(!r.ok){ pre.innerHTML = '<span class="no">Server said '+r.status+
      '. '+esc(await r.text())+'</span>'; return; }
    const d = await r.json();
    if(!d.length){
      pre.innerHTML = '<span class="warn">Nothing was recorded for this '+
        'call. If this keeps happening, SERVICE_TOKEN may be missing on '+
        'the agent service.</span>'; return; }
    pre.textContent = d.map(function(t){
      return '['+t.at+'] '+t.who+(t.tool?' ('+t.tool+')':'')+
             (t.latency_ms?' '+t.latency_ms+'ms':'')+': '+t.text;
    }).join(String.fromCharCode(10));
  }catch(e){ pre.innerHTML = '<span class="no">'+esc(e.message)+'</span>'; }
}

function mboxes(a){
  var m = a.mailboxes || [];
  if(!m.length) return '<span class="no">none connected</span>';
  return m.map(function(b){
    return '<div style="margin-bottom:4px"><span class="ok">'+
      esc(b.email)+'</span>'+
      (b.label?' <span class="tag">'+esc(b.label)+'</span>':'')+
      (b.default?' <span class="tag">main</span>':'')+
      ' <span class="hint">'+b.used+' uses</span></div>'; }).join('');
}

async function load(){
  const q = (document.getElementById('q_cust')||{}).value || '';
  const tb = document.getElementById('rows');
  try{
    const d = await (await fetch('/accounts?q='+
      encodeURIComponent(q))).json();
    if(!d.length){
      tb.innerHTML='<tr><td colspan="5" class="hint">None found.</td></tr>';
      return; }
    tb.innerHTML = d.map(function(a){
      return '<tr><td>'+a.account_id+'</td><td>'+esc(a.name)+'</td>'+
        '<td>'+esc((a.phones||[]).join(', '))+'</td>'+
        '<td>'+mboxes(a)+'</td>'+
        '<td><button class="sec" onclick="copyLink('+a.account_id+
        ')">Link</button> '+
        '<button class="sec" onclick="copyLink('+a.account_id+')">Copy</button> '+
        '<button class="sec" onclick="textLink('+a.account_id+')">Text</button> '+
        '<button class="sec" onclick="addPhone('+a.account_id+')">+ Phone</button> '+
        '<button class="sec" onclick="removePhone('+a.account_id+')">- Phone</button>'+
        '</td></tr>'; }).join('');
  }catch(e){
    tb.innerHTML='<tr><td colspan="5" class="no">'+esc(e.message)+'</td></tr>'; }
}

var fuAll = false;
async function loadCosts(){
  try{
    const days = document.getElementById('costdays').value;
    const d = await (await fetch('/usage/summary?days='+days)).json();
    document.getElementById('costtop').innerHTML =
      '<b style="font-size:26px">$'+d.total_cost_usd.toFixed(2)+'</b>'+
      '<span class="hint"> over '+d.calls+' calls, '+
      d.total_minutes+' minutes</span><br>'+
      '<span class="hint">$'+d.cost_per_call_usd.toFixed(3)+
      ' per call &middot; $'+d.cost_per_minute_usd.toFixed(3)+
      ' per minute</span>';
    document.getElementById('costparts').innerHTML =
      Object.entries(d.by_component_usd).map(function(e){
        return '<tr><td>'+esc(e[0])+'</td><td>$'+e[1].toFixed(4)+
               '</td></tr>'; }).join('') ||
      '<tr><td class="hint">Nothing recorded yet.</td></tr>';
    document.getElementById('costcust').innerHTML =
      Object.entries(d.by_customer).map(function(e){
        return '<tr><td>'+esc(e[0])+'</td><td>'+e[1].calls+'</td><td>'+
               e[1].minutes.toFixed(1)+'</td><td>$'+
               e[1].cost_usd.toFixed(3)+'</td></tr>'; }).join('') ||
      '<tr><td class="hint">Nothing recorded yet.</td></tr>';
  }catch(e){}
}
document.getElementById('costdays').addEventListener('change', loadCosts);
async function loadAlerts(){
  try{
    const d = await (await fetch('/followups?include_done=0')).json();
    const urgent = d.filter(function(f){
      return f.reason==='proxy_fallback' || f.channel==='system'; });
    const card = document.getElementById('alertcard');
    if(!urgent.length){ card.style.display='none'; return; }
    card.style.display='block';
    document.getElementById('alerts').innerHTML = urgent.map(function(f){
      return '<div class="err" style="margin-bottom:8px">'+esc(f.at)+
        ' — '+esc(f.note)+
        ' <button class="sec" onclick="fuDone('+f.id+',0)">Fixed</button>'+
        '</div>'; }).join('');
  }catch(e){}
}
async var chArea = "";
function loadChanges(){
  var sel = document.getElementById('charea');
  chArea = sel ? sel.value : "";
  fetch('/changes?limit=200' + (chArea ? '&area=' + chArea : ''))
    .then(function(r){ return r.json(); })
    .then(function(rows){
      var b = document.getElementById('chrows');
      if(!rows.length){ b.innerHTML =
        '<tr><td colspan="6" class="hint">Nothing yet.</td></tr>'; return; }
      b.innerHTML = rows.map(function(c){
        return '<tr><td>' + esc(c.at) + '</td>' +
          '<td>' + esc(c.who || ('#' + (c.account_id||''))) + '</td>' +
          '<td><span class="tag">' + esc(c.area) + '</span> ' +
            esc(c.what) + '</td>' +
          '<td>' + esc(c.detail) + '</td>' +
          '<td>' + (c.call_id ? esc(c.call_id) : '') + '</td>' +
          '<td class="hint">' + esc(c.undo || '') + '</td></tr>';
      }).join('');
    });
}
function loadKnow(){
  fetch('/profiles?limit=100').then(function(r){ return r.json(); })
    .then(function(rows){
      var b = document.getElementById('knowrows');
      if(!rows.length){ b.innerHTML =
        'Nothing learned yet. It fills in after calls.'; return; }
      b.innerHTML = rows.map(function(p){
        return '<div class="card" style="margin-top:14px">' +
          '<h2 style="font-size:16px">' + esc(p.who || ('#'+p.account_id)) +
          ' <span class="hint" style="font-weight:400">updated ' +
          esc(p.updated) + '</span></h2>' +
          '<div class="hint">From earlier calls</div>' +
          '<pre style="max-height:200px">' +
          esc(p.notes || '(nothing yet)') + '</pre>' +
          '<div class="hint" style="margin-top:10px">The office says' +
          ' &mdash; the assistant reads this first</div>' +
          '<textarea id="byhand' + p.account_id + '" rows="4" ' +
          'style="width:100%;background:#0f1115;color:#e6e6e6;border:' +
          '1px solid #262b36;border-radius:6px;padding:10px;' +
          'font-size:14px">' + esc(p.by_hand) + '</textarea>' +
          '<button class="sec" style="margin-top:8px" onclick="saveKnow(' +
          p.account_id + ')">Save</button>' +
          '<span class="msg" id="knowmsg' + p.account_id + '"></span>' +
          '</div>';
      }).join('');
    });
}
function saveKnow(id){
  var box = document.getElementById('byhand' + id);
  var msg = document.getElementById('knowmsg' + id);
  fetch('/profile', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({account_id:id, by_hand:box.value})})
    .then(function(r){ msg.textContent = r.ok ? ' Saved.' : ' Did not save.';
                       setTimeout(function(){ msg.textContent=''; }, 2500); });
}
function loadBlocks(){
  fetch('/blocks?days=60').then(function(r){ return r.json(); })
    .then(function(d){
      var b = document.getElementById('blkrows');
      var rows = d.by_site || [];
      b.innerHTML = rows.length ? rows.map(function(x){
        return '<tr><td>' + esc(x.site) + '</td>' +
          '<td><span class="tag">' + esc(x.kind) + '</span></td>' +
          '<td>' + esc(x.vendor || '') + '</td>' +
          '<td>' + esc(x.times) + '</td>' +
          '<td class="' + (x.worth_retrying ? 'ok' : 'no') + '">' +
            (x.worth_retrying ? 'maybe' : 'no') + '</td>' +
          '<td class="hint">' + esc(x.advice) + '</td></tr>';
      }).join('') :
        '<tr><td colspan="6" class="hint">No refusals recorded.</td></tr>';
      var r2 = document.getElementById('blkrecent');
      r2.innerHTML = (d.recent || []).slice(0, 25).map(function(x){
        return '<tr><td>' + esc(x.at) + '</td><td>' + esc(x.site) +
          '</td><td>' + esc(x.kind) + '</td><td class="hint">' +
          esc(x.saw) + '</td></tr>';
      }).join('');
    });
}
function loadReviews(){
  fetch('/reviews?limit=50').then(function(r){ return r.json(); })
    .then(function(rows){
      var b = document.getElementById('rvrows');
      if(!rows.length){ b.innerHTML =
        '<tr><td colspan="4" class="hint">No calls flagged.</td></tr>';
        return; }
      b.innerHTML = rows.map(function(r){
        return '<tr><td>' + esc(r.at) + '</td>' +
          '<td>' + esc(r.call_id || '') + '</td>' +
          '<td>' + esc(r.account_id || '') + '</td>' +
          '<td><pre style="margin:0;max-height:200px">' +
            esc(r.note) + '</pre></td></tr>';
      }).join('');
    });
}
function loadFu(){
  const tb = document.getElementById('furows');
  try{
    const d = await (await fetch('/followups?include_done='+
      (fuAll?1:0))).json();
    if(!d.length){
      tb.innerHTML='<tr><td colspan="6" class="hint">Nothing outstanding.'+
        '</td></tr>'; return; }
    tb.innerHTML = d.map(function(f){
      return '<tr'+(f.done?' style="opacity:.45"':'')+'>'+
        '<td>'+esc(f.at)+'</td>'+
        '<td>'+esc(f.who)+'<br><span class="hint">'+esc(f.phone)+'</span></td>'+
        '<td><span class="tag">'+esc(f.reason)+'</span><br>'+
        '<span class="hint">'+esc(f.channel)+'</span></td>'+
        '<td>'+esc(f.note)+'</td>'+
        '<td>'+(f.call_id?('#'+f.call_id):'&mdash;')+'</td>'+
        '<td><button class="sec" onclick="fuDone('+f.id+','+
        (f.done?1:0)+')">'+(f.done?'Reopen':'Done')+'</button></td></tr>';
    }).join('');
  }catch(e){
    tb.innerHTML='<tr><td colspan="6" class="no">'+esc(e.message)+'</td></tr>'; }
}
async function fuDone(id, isDone){
  await fetch('/followups/done?followup_id='+id+'&undo='+(isDone?1:0),
              {method:'POST'});
  loadFu();
}
async function loadOb(){
  const tb = document.getElementById('obrows');
  try{
    const d = await (await fetch('/onboard/sessions?limit=20')).json();
    if(!d.length){
      tb.innerHTML='<tr><td colspan="6" class="hint">None yet.</td></tr>';
      return; }
    tb.innerHTML = d.map(function(o){
      var cls = o.state==='done'?'ok':(o.state==='failed'?'no':'warn');
      return '<tr><td>'+o.session_id+'</td><td>'+esc(o.who)+'</td>'+
        '<td>'+esc(o.email)+'</td><td>'+esc(o.at)+'</td>'+
        '<td class="'+cls+'">'+esc(o.state)+
        (o.message?'<span class="'+(cls==='no'?'err':'hint')+'">'+
          esc(o.message)+'</span>':'')+'</td>'+
        '<td><button class="sec" onclick="showOb('+o.session_id+',this)">'+
        'Steps</button></td></tr>'+
        '<tr class="det" id="ob'+o.session_id+'" style="display:none">'+
        '<td colspan="6"><pre>'+esc(o.history||'No steps recorded.')+
        '</pre></td></tr>'; }).join('');
  }catch(e){
    tb.innerHTML='<tr><td colspan="6" class="no">'+esc(e.message)+'</td></tr>'; }
}
function showOb(id, btn){
  const row = document.getElementById('ob'+id);
  const open = row.style.display === 'table-row';
  row.style.display = open ? 'none' : 'table-row';
  btn.textContent = open ? 'Steps' : 'Hide';
}

var liveLast = 0, liveLines = [];
function liveClear(){ liveLines = []; render(); }
function render(){
  const errOnly = document.getElementById('live_err').checked;
  const el = document.getElementById('livelog');
  const shown = liveLines.filter(function(l){ return !errOnly || l.level!=='info'; });
  el.innerHTML = shown.length ? shown.map(function(l){
    var color = l.level==='error' ? '#f87171' : (l.level==='warn' ? '#fbbf24' : '#c3cad8');
    return '<span style="color:#8b94a7">'+esc(l.at)+'</span> '+
      '<span style="color:#60a5fa">'+esc(l.ref)+'</span>'+
      (l.who?' <span style="color:#8b94a7">'+esc(l.who)+'</span>':'')+
      ' <span style="color:'+color+'">'+esc(l.text)+'</span>';
  }).join(String.fromCharCode(10)) : 'Nothing yet.';
  el.scrollTop = el.scrollHeight;
}
async function pollLive(){
  if(document.getElementById('live_pause').checked) return;
  try{
    const d = await (await fetch('/events?after_id='+liveLast+'&limit=200')).json();
    if(d.length){
      const seen = {};
      liveLines.forEach(function(l){ seen[l.id] = 1; });
      const fresh = d.filter(function(l){
        if(seen[l.id]) return false; seen[l.id] = 1; return true; });
      if(fresh.length){
        liveLines = liveLines.concat(fresh).slice(-800);
        liveLast = Math.max(liveLast, fresh[fresh.length-1].id);
        render();
      }
    }
  }catch(e){}
}
document.getElementById('live_err').addEventListener('change', render);
async function loadOrders(){
  const tb = document.getElementById('orderrows');
  try{
    const d = await (await fetch('/orders?limit=50')).json();
    if(!d.length){ tb.innerHTML='<tr><td colspan="9" class="hint">'+
      'No orders yet.</td></tr>'; return; }
    tb.innerHTML = d.map(function(o){
      var cls = o.state==='placed'?'ok':(o.state==='failed'?'no':
                (o.state==='cancelled'?'hint':'warn'));
      return '<tr><td>'+o.order_id+'</td><td>'+esc(o.who)+'</td>'+
        '<td><span class="tag">'+esc(o.site)+'</span></td>'+
        '<td>'+o.quantity+' x '+esc(o.item)+'</td>'+
        '<td>'+(o.expected_price?'$'+esc(o.expected_price):'')+'</td>'+
        '<td class="'+cls+'">'+esc(o.state)+
        (o.message?'<span class="'+(cls==='no'?'err':'hint')+'">'+
          esc(o.message)+'</span>':'')+'</td>'+
        '<td>'+esc(o.confirmation||'')+(o.final_total?'<br><span class="hint">$'+
          esc(o.final_total)+'</span>':'')+'</td>'+
        '<td>'+esc(o.at)+'</td>'+
        '<td><button class="sec" onclick="showOrd('+o.order_id+',this)">'+
        'Steps</button></td></tr>'+
        '<tr class="det" id="od'+o.order_id+'" style="display:none">'+
        '<td colspan="9"><pre>'+esc(o.history||'No steps.')+'</pre></td></tr>';
    }).join('');
  }catch(e){ tb.innerHTML='<tr><td colspan="9" class="no">'+esc(e.message)+
    '</td></tr>'; }
}
function showOrd(id, btn){
  const row = document.getElementById('od'+id);
  const open = row.style.display === 'table-row';
  row.style.display = open ? 'none' : 'table-row';
  btn.textContent = open ? 'Steps' : 'Hide';
}
async function loadSites(){
  const tb = document.getElementById('siterows');
  const rb = document.getElementById('reciperows');
  const eb = document.getElementById('siteevents');
  try{
    const d = await (await fetch('/sites/report?days=30')).json();
    tb.innerHTML = d.sites.length ? d.sites.map(function(x){
      var tasks = (x.tasks||[]).map(function(t){
        return '<span class="tag">'+esc(t[0])+' '+t[1]+'</span>'; }).join('');
      return '<tr><td><b>'+esc(x.site)+'</b></td><td>'+x.requests+'</td>'+
        '<td class="ok">'+x.ok+'</td><td>'+x.via_recipe+'</td>'+
        '<td class="'+(x.fallbacks?'warn':'')+'">'+x.fallbacks+'</td>'+
        '<td>'+x.avg_seconds+'s</td><td>'+tasks+'</td></tr>'; }).join('')
      : '<tr><td colspan="7" class="hint">No website requests yet.</td></tr>';
    rb.innerHTML = d.recipes.length ? d.recipes.map(function(r){
      var steps = (r.steps||[]).map(function(st, i){
        var t = (i+1)+'. '+st.action;
        if(st.desc) t += ' → '+st.desc;
        if(st.url) t += ' → '+st.url;
        if(st.text) t += ' ["'+st.text+'"]';
        return t; }).join(String.fromCharCode(10));
      return '<tr'+(r.retired?' style="opacity:.45"':'')+'>'+
        '<td>'+esc(r.site)+'</td><td><span class="tag">'+esc(r.task)+
        '</span>'+(r.retired?' <span class="no">retired</span>':'')+'</td>'+
        '<td class="hint">'+esc(r.example)+'</td>'+
        '<td class="ok">'+r.ok+'</td><td class="'+(r.failed?'no':'')+'">'+
        r.failed+'</td><td>'+esc(r.last_ok)+'</td>'+
        '<td><button class="sec" onclick="showRec('+r.id+',this)">Steps'+
        '</button></td></tr>'+
        '<tr class="det" id="rc'+r.id+'" style="display:none"><td colspan="7">'+
        '<pre>'+esc(steps||'No steps.')+'</pre></td></tr>'; }).join('')
      : '<tr><td colspan="7" class="hint">Nothing learned yet.</td></tr>';
  }catch(e){ tb.innerHTML='<tr><td colspan="7" class="no">'+esc(e.message)+
    '</td></tr>'; }
  try{
    const ev = await (await fetch('/sites/events?limit=40')).json();
    eb.innerHTML = ev.length ? ev.map(function(x){
      var how = x.path==='recipe' ? '<span class="ok">learned</span>'
        : x.path==='fallback' ? '<span class="warn">fell back</span>'
        : 'worked it out';
      var res = x.outcome==='ok' ? '<span class="ok">ok</span>'
        : '<span class="no">failed</span>';
      return '<tr><td>'+esc(x.at)+'</td><td>'+esc(x.site)+'</td>'+
        '<td><span class="tag">'+esc(x.task)+'</span></td>'+
        '<td class="hint">'+esc(x.goal)+'</td><td>'+how+'</td>'+
        '<td>'+res+'</td><td>'+x.seconds+'s</td>'+
        '<td>'+(x.job_id?'<button class="sec" onclick="jumpJob('+x.job_id+
        ')">Details</button>':'')+'</td></tr>'; }).join('')
      : '<tr><td colspan="8" class="hint">Nothing yet.</td></tr>';
  }catch(e){ eb.innerHTML='<tr><td colspan="8" class="no">'+esc(e.message)+
    '</td></tr>'; }
}
function showRec(id, btn){
  const row = document.getElementById('rc'+id);
  const open = row.style.display === 'table-row';
  row.style.display = open ? 'none' : 'table-row';
  btn.textContent = open ? 'Steps' : 'Hide';
}
function jumpJob(id){
  document.querySelector('nav a[data-p="jobs"]').click();
  setTimeout(function(){
    var row = document.getElementById('jb'+id);
    if(row){ row.style.display='table-row'; row.scrollIntoView(); }
  }, 300);
}
async function loadHealth(){
  try{
    const d = await (await fetch('/jobs/health')).json();
    document.getElementById('qhealth').innerHTML =
      '<div class="num"><b>'+d.running+'</b><span>running now</span></div>'+
      '<div class="num"><b>'+d.waiting+'</b><span>waiting</span></div>'+
      '<div class="num"><b>'+d.max_browsers+'</b><span>max at once</span></div>'+
      '<div class="num"><b>'+d.saved_sessions+'</b><span>saved sessions</span></div>'+
      (d.stuck_over_20min ? '<div class="num"><b class="no">'+
        d.stuck_over_20min+'</b><span>stuck 20min+</span></div>' : '');
  }catch(e){ document.getElementById('qhealth').innerHTML =
    '<span class="no">'+esc(e.message)+'</span>'; }
}
async function loadJobs(){
  const tb = document.getElementById('jobrows');
  try{
    const d = await (await fetch('/jobs?limit=25')).json();
    if(!d.length){
      tb.innerHTML='<tr><td colspan="6" class="hint">None yet.</td></tr>';
      return; }
    tb.innerHTML = d.map(function(j){
      var cls = j.state==='done'?'ok':(j.state==='failed'?'no':'warn');
      return '<tr><td>'+j.job_id+'</td><td>'+esc(j.who)+'</td>'+
        '<td><span class="tag">'+esc(j.site)+'</span> '+
        '<span class="hint">'+esc(j.kind)+'</span></td>'+
        '<td>'+esc(j.at)+'</td>'+
        '<td class="'+cls+'">'+esc(j.state)+
        (j.message?'<span class="'+(cls==='no'?'err':'hint')+'">'+
          esc(j.message)+'</span>':'')+'</td>'+
        '<td><button class="sec" onclick="showJob('+j.job_id+',this)">'+
        'Steps</button></td></tr>'+
        '<tr class="det" id="jb'+j.job_id+'" style="display:none">'+
        '<td colspan="6"><pre>'+esc(j.history||'No steps.')+'</pre></td></tr>';
    }).join('');
  }catch(e){
    tb.innerHTML='<tr><td colspan="6" class="no">'+esc(e.message)+'</td></tr>'; }
}
function showJob(id, btn){
  const row = document.getElementById('jb'+id);
  const open = row.style.display === 'table-row';
  row.style.display = open ? 'none' : 'table-row';
  btn.textContent = open ? 'Steps' : 'Hide';
}
async function loadDlr(){
  const tb = document.getElementById('dlr');
  try{
    const d = await (await fetch('/sms/dlr?limit=25')).json();
    if(!d.length){
      tb.innerHTML='<tr><td colspan="4" class="hint">'+
        'No delivery receipts yet.</td></tr>'; return; }
    tb.innerHTML = d.map(function(x){
      var good = /deliver|success|ok/i.test(x.status||'');
      return '<tr><td>'+esc(x.at)+'</td><td>'+esc(x.to)+'</td>'+
        '<td class="'+(good?'ok':'no')+'">'+esc(x.status||'?')+'</td>'+
        '<td class="hint">'+esc(x.raw||'')+'</td></tr>'; }).join('');
  }catch(e){
    tb.innerHTML='<tr><td colspan="4" class="no">'+esc(e.message)+'</td></tr>'; }
}

function copyLink(id){
  fetch('/link/new?account_id=' + id).then(function(r){ return r.json(); })
   .then(function(d){
     navigator.clipboard.writeText(d.url);
     document.getElementById('msg').textContent =
       'Copied a link for ' + d.for + ', good for ' + d.valid_minutes +
       ' minutes.';
   }).catch(function(e){
     document.getElementById('msg').textContent = 'Could not make a link.';
   });
}
async function textLink(id){
  const m = document.getElementById('msg');
  m.textContent = 'Sending…';
  try{
    const d = await (await fetch('/sms/link?account_id='+id,
                                 {method:'POST'})).json();
    m.textContent = d.sent ? 'Text sent.'
      : ('Not sent: ' + (d.detail || d.error || 'check SMS settings'));
  }catch(e){ m.textContent = 'Not sent: ' + e.message; }
}
function freshPin(){
  // not 1234, not one digit four times: the same rule as phone sign-up
  var p = '';
  while(true){
    p = String(1000 + Math.floor(Math.random() * 9000));
    var same = p.split('').every(function(c){ return c === p[0]; });
    var run = '01234567890'.indexOf(p) >= 0 || '09876543210'.indexOf(p) >= 0;
    if(!same && !run) return p;
  }
}
async function addPhone(id){
  const n = prompt('Their other phone number, e.g. +18455551234');
  if(!n) return;
  const d = await (await fetch('/accounts/phone', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({account_id: id, phone: n})})).json();
  document.getElementById('msg').textContent = d.ok ? 'Phone added.' :
    ({already_customer: 'That number is already on a customer.',
      no_number: 'That is not a full phone number.'}[d.reason] || 'Not added.');
  load();
}
async function removePhone(id){
  const n = prompt('Which number should come off?');
  if(!n) return;
  const d = await (await fetch('/accounts/phone/remove', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({account_id: id, phone: n})})).json();
  document.getElementById('msg').textContent = d.ok ? 'Phone removed.' :
    ({last_number: 'That is their only number - it stays.',
      not_found: 'That number is not on this customer.'}[d.reason] ||
     'Not removed.');
  load();
}
async function makeInvite(){
  const box = document.getElementById('inv_new');
  box.textContent = 'Making...';
  try{
    const r = await fetch('/invites', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({note: document.getElementById('inv_note').value})});
    const d = await r.json();
    box.textContent = d.code ? (d.code.slice(0,3) + ' ' + d.code.slice(3)) : 'Failed';
    document.getElementById('inv_note').value = '';
    loadInvites();
  }catch(e){ box.textContent = 'Failed: ' + e.message; }
}
async function cancelInvite(id){
  await fetch('/invites/cancel?invite_id=' + id, {method:'POST'});
  loadInvites();
}
async function loadInvites(){
  const tb = document.getElementById('inv_rows');
  if(!tb) return;
  try{
    const d = await (await fetch('/invites')).json();
    if(!d.length){
      tb.innerHTML = '<tr><td colspan="6" class="hint">No codes yet.</td></tr>';
      return; }
    tb.innerHTML = d.map(function(i){
      var who = i.name ? (esc(i.name) + ' (' + esc(i.phone.slice(-4)) +
        ', ' + esc(i.used) + ')') : '';
      var btn = i.state === 'waiting' ? '<button class="sec" ' +
        'onclick="cancelInvite(' + i.id + ')">Cancel</button>' : '';
      return '<tr><td>' + esc(i.note || '-') + '</td><td>' +
        esc(i.kind || '') + '</td><td>' + esc(i.made) +
        '</td><td>' + esc(i.state) + (i.state === 'waiting' ?
        ' until ' + esc(i.expires) : '') + '</td><td>' + who + '</td><td>' +
        btn + '</td></tr>'; }).join('');
  }catch(e){
    tb.innerHTML = '<tr><td colspan="5" class="no">' + esc(e.message) +
      '</td></tr>'; }
}
async function add(){
  const m = document.getElementById('msg');
  const r = await fetch('/accounts',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name:document.getElementById('n').value,
      phone:document.getElementById('p').value,
      pin:document.getElementById('k').value})});
  if(r.ok){ m.textContent='Created.';
    document.getElementById('n').value='';
    document.getElementById('p').value='';
    document.getElementById('k').value = freshPin(); load(); }
  else { m.textContent='Failed — that number may already exist.'; }
}

document.getElementById('k').value = freshPin();
loadInvites();
load(); loadCalls(); loadStats(); loadDlr(); loadOb(); loadFu(); loadJobs(); loadHealth(); loadSites(); loadOrders(); loadAlerts(); loadCosts();
if(!window._livePoller){
  pollLive(); window._livePoller = setInterval(pollLive, 2000);
}
setInterval(function(){ loadCalls(); loadStats(); loadDlr(); loadOb();
                        loadFu(); loadJobs(); loadHealth(); loadSites();
                        loadOrders(); loadAlerts(); loadCosts(); }, 25000);
</script></body></html>"""
