# PhoneAssistant — read this first

## What this is
An AI assistant customers reach **by phone only**. They have no internet,
often no text. It reads their Gmail, checks calendars, signs in to shopping
sites, places orders, and schedules — all by voice. Owner: David, a
non-technical founder. Explain things in plain language. Give him one
PowerShell block per change, never two.

## Live system
- Backend: FastAPI, Railway, `https://web-production-13961.up.railway.app`
- Voice agent: LiveKit + OpenAI Realtime, Railway service `PhoneAssistant`
  (start command `python agent.py start`)
- Phone number: +1 484 518 2072
- Admin panel: `/admin` (password in Railway `ADMIN_PASSWORD`)
- Database: Railway Postgres
- Browsers: Browserbase (proxies NOT enabled on the plan — 402 is expected)

## Reading the live log (do this before guessing at any bug)
The `/events` endpoint is the live log. Needs `Authorization: Bearer
<SERVICE_TOKEN>` — the token is in Railway env for both services. Ask David
for it if it isn't in the environment.

    Invoke-RestMethod -Uri "https://web-production-13961.up.railway.app/events?limit=100" -Headers @{Authorization="Bearer $env:SERVICE_TOKEN"} | Format-Table at,ref,level,text -Wrap

Other useful reads: `/usage/summary?days=7`, `/browser/where`,
`/browser/proxy_status`, `/followups`, `/jobs/status?job_id=N`,
`/onboard/status?session_id=N`.

## Files
- `main.py` — backend, admin panel, Gmail/Calendar, browser runners,
  ordering, costs, live log. Big. Read the section you need.
- `agent.py` — the LiveKit voice agent and its ~45 tools.
- `check.py` — pre-push checks. **Run before every push. Never push on a
  failure.**
- `requirements.txt`, `Procfile`.

## Rules that exist because something broke
1. **Run `python check.py` before every `git push`.** It catches the classes
   of bug we've already hit. When a new bug appears, add a check for it.
2. **Never write a password, PIN or code anywhere** — not logs, not notes,
   not tool results. `scrub()` in main.py runs on every stored line; keep
   it that way. Passwords are only ever held in memory during a sign-in.
3. **All browser page reads go through the safe helpers** in main.py:
   `q`, `q_all`, `page_text`, `page_url`, `do_click`, `do_fill`, `do_goto`,
   `settle`. Never call `page.query_selector`, `page.goto`, `el.click()` or
   `page.inner_text` directly — pages navigate mid-check and raw calls
   crash. `check.py` enforces this.
4. **Never assign to a LiveKit `Agent` property** (`session`, `tools`,
   `instructions`, …). They are read-only; assigning crashed the entrypoint
   and the phone rang with no one there.
5. **`session.start()` must run before any watchdog or background task in
   the entrypoint.** Nothing may prevent the call being answered.
6. **After editing with a string replacement, verify it landed.** Two edits
   silently failed to apply because the anchor didn't match. Grep for the
   new text afterwards.
7. Tool names in agent.py are exact: `sign_in_to_site` (not `site_login`),
   `connect_email` (Gmail only), `check_email`, `mark_read`, `end_call`.
   Instructions that name a tool that doesn't exist confuse the model.
8. **Proactive updates, not polling.** Background jobs are watched by
   `_start_watch` / `_watch_job` in agent.py, which make the agent speak
   when state changes. Do not add "check again in a few seconds" chatter.
9. **Admin panel is for visibility only.** Customers control everything by
   voice; staff never set things up for them.
10. Blocked topics (news, sports, gossip, jokes, dating, religion, etc.)
    get exactly: "I am not allowed to talk to you about this." Enforced in
    prompt and server-side.

## Deploy
One block, checks first, push only on pass:

    cd C:\Users\Admin\PhoneAssistant; python check.py; if ($LASTEXITCODE -eq 0) { git add .; git commit -m "MESSAGE"; git push } else { Write-Host "NOT PUSHED - checks failed" -ForegroundColor Red }

Railway auto-deploys both services from `main` on push.

## Known state (Sep 2026)
- Gmail sign-in by voice works end to end (spelled password, tap prompt
  with number, unverified-app consent screen).
- Site logins (Amazon/Walmart) save and encrypt fine; a login session must
  succeed once before order lookups work. Amazon fights automation —
  prefer Walmart for testing.
- SMS is blocked on 10DLC campaign registration (BulkVS / Telnyx). Not a
  code problem.
- Real cost is ~$0.44/min, ~94% of it the OpenAI Realtime model. Rates
  are Railway vars `RATE_*`; verify against invoices.
- Google OAuth app is in testing mode — accounts must be on the test-user
  list in Google Cloud Console.

## When David reports a failed call
1. Pull `/events` for the window he describes.
2. Find the first `error`/`failed` line — the later ones are consequences.
3. Fix the cause, add a check to `check.py` if it's a new class of bug,
   run `check.py`, then give him the one-block deploy command.
