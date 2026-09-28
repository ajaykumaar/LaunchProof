# Launchproof: build plan

Working name. One URL in, one report out: does this site survive launch day?
Hackathon: Startup Speedrun, Cloudflare HQ, Mon Sep 28 2026. Hacking 9:30 to 3:30, judging 3:30 to 5:00.

---

## 1. The product in one paragraph

A founder pastes their site URL. An agent (1) walks the site like a real user on phone and desktop and flags broken pages and layouts, (2) buys the product with a test card and confirms the payment actually unlocked something, then refunds, and (3) sends real traffic from several regions, ramping until the site breaks, then tears every machine down. The founder gets a score, a list of what broke with fix prompts to paste into Lovable or Cursor, and a share card ("Survived 800 users from 4 regions").

## 2. Track fit (say this in the pitch)

| Track | What the agent does |
|---|---|
| Agents That Deploy Infrastructure | The Tester agent (on Brainbase) provisions load machines in several regions on Fly, collects results through the exec API, and destroys them; the Janitor agent guarantees teardown on a schedule. |
| Agentic Payments | The agent shops like a customer, pays with Stripe test cards, checks the unlock, the decline path and the paywall-bypass hole, and (optional) verifies webhooks with a read-only key. Our own customers pay through Stripe Checkout. |
| Autonomous Organizations | Three agents on a Brainbase orchestration: Tester hands off to Triage (Slack + Linear), Janitor runs on a schedule. Work arrives from Slack mentions and Linear issues delegated to the agent. |

## 3. Scope for today (6 hours) and what to cut

Must have, in this order:
1. Ownership check (without it the product is an attack tool).
2. UI and visual check (fastest to impress, always works).
3. Payment check in Stripe test mode.
4. Load test from at least 2 regions with automatic teardown.
5. Report page + share card.
6. Stripe Checkout for our own $9 launch pass.

Cut first if behind: Linear/Slack, more than 2 regions, LLM vision review of screenshots, live-mode payments, subscription plan.

Never cut: ownership check, load caps, machine TTL, teardown.

## 4. Architecture (Brainbase-first, updated 11:30)

```
Slack mention / Linear issue / Brainbase chat / Brainbase API / (last) our landing page
                 │
                 ▼
Brainbase orchestration "Launchproof Team"
 ├─ Tester agent  (Claude Code harness, Daytona sandbox L, Brainbase credits)
 │    entrypoint: clone repo, pip install, Playwright Chromium
 │    MCP tools (launchproof.mcp_server): browser_open/click/fill/check, save_journey, tokens
 │    shell:      python -m launchproof run <url> --journey journey.json --regions sjc iad
 │                  ├─ ownership.py   DNS TXT / file / meta token
 │                  ├─ ui_check.py    Playwright crawl on phone + desktop
 │                  ├─ payment.py     replays the agent's journey: success, decline, paywall bypass
 │                  ├─ load/fly.py ─────────────► Fly Machines in sjc/iad/lhr/sin (engine.py), exec to collect, delete
 │                  └─ report.py      score, share card; agent rewrites fix prompts, `launchproof report` rebuilds
 │    memory: runs table (history per URL)      evals: verified-before-load, evidence, no machines left
 ├─ edge (JSON payload) ─► Triage agent: Slack summary + Linear issues (dedupe in memory)
 └─ schedule */30 ──────► Janitor agent: reap Fly machines older than 10 min
```

What moved to Brainbase: the orchestrator, browsers, the buyer agent's model (no separate Anthropic key),
fix prompts, Slack and Linear, scheduling, run history, and agent quality checks.
What stays elsewhere and why:
- Load generators on Fly: needs VMs in several regions created and destroyed by API; Brainbase does not document sandbox regions or egress, and caps a sandbox at 4 vCPU / 8 GiB. Fallback: the load test runs from the tester's own sandbox.
- Demo target on Fly: must be always on; Brainbase preview URLs sleep when idle.
- Landing page + $9 checkout (built last): tiny web app that hands every run to the Tester through the Brainbase API; always-on for the Stripe webhook.

## 5. Accounts and keys (full commands in RUNBOOK.md)

| Service | What to do | Env / secret |
|---|---|---|
| Brainbase | Log in, confirm credits, API key, `npm i -g @brainbase-labs/cli`, `brainbase login` | `BRAINBASE_API_KEY`, `BRAINBASE_AGENT_ID` |
| GitHub | Private repo + read-only fine-grained token (the sandboxes clone it) | `GITHUB_TOKEN`, `LP_REPO` |
| Fly.io | Card, `fly auth login`, load app + deploy token, demo app | `FLY_API_TOKEN`, `FLY_APP` |
| Stripe | Test mode key for the demo shop; Launch Pass price + webhook only in the last step | `STRIPE_SECRET_KEY`, later `STRIPE_PRICE_ID`, `STRIPE_WEBHOOK_SECRET` |
| Slack / Linear | Connect as Brainbase surfaces (Tester) and integrations (Triage) | fallbacks: `SLACK_WEBHOOK_URL`, `LINEAR_API_KEY`, `LINEAR_TEAM_ID` |
| Shared | One random value on laptop and in Brainbase | `LP_SECRET` |
| Anthropic | Optional now (fallback buyer loop inside the CLI) | `ANTHROPIC_API_KEY` |

Ask the organizers at kickoff: (a) is prior code allowed if disclosed, (b) any objection to running load tests against teams that opt in.

## 6. Ownership verification (non-negotiable)

Before any load test or payment test, the user proves control of the domain. Accept any one:
- DNS TXT record: `_launchproof.<domain>` = `launchproof-verify=<token>`
- File: `https://<domain>/.well-known/launchproof.txt` containing the token
- Meta tag on the homepage: `<meta name="launchproof-verify" content="<token>">`

Rules:
- Token is random per user+domain, expires in 24 hours.
- Verified domain only covers that exact host (and subdomains only if verified by DNS).
- UI crawl of public pages can run before verification (it is just visiting a public site), capped at 20 pages. Load and payment tests cannot.
- Re-verify for every run older than 24 hours.

## 7. UI and visual check

Per page, at 390x844 (phone) and 1440x900 (desktop):
- Full-page screenshot.
- Console errors and uncaught exceptions.
- Failed requests (status ≥ 400 or network error), mixed content.
- Blank or near-blank page: visible text under 40 characters, or a single full-screen element, or the classic white/black screen.
- Horizontal overflow: `scrollWidth > innerWidth + 1` (the most common mobile bug).
- Broken images (`naturalWidth == 0`).
- Tap targets smaller than 24px on phone (count only).
- Slow page: load event over 5 seconds.
- Missing title / meta description / favicon (launch polish).
- Security headers present: HSTS, CSP, X-Content-Type-Options (report only).

Crawl: same-origin links from the homepage, breadth-first, max 20 pages, skip logout/delete/unsubscribe links, 1 request/second.

LLM review (stretch): send the phone + desktop screenshots of the top 5 pages to Claude with "list visual defects a first-time visitor would notice". Keep it as advice, not a failing check.

## 8. User journey agent (signup + checkout)

Primary (Brainbase): the Tester agent's own model drives our MCP tools `browser_open`, `browser_goto`, `browser_click`, `browser_fill`, `browser_check`, `save_journey`. Every step is recorded with a stable selector; `launchproof run --journey journey.json` replays it for each payment case. Fallbacks: the CLI's own Claude loop (`agent.py`, needs ANTHROPIC_API_KEY), then heuristics.
Goal prompt: "You are a first-time customer. Sign up with the email given, then buy the cheapest paid plan. Report each step and anything that blocks you."
- Test identity: `lp+<runid>@<our inbox domain>` so receipts land in our inbox (Cloudflare Email Routing → Email Worker, or AgentMail/Inkbox).
- Max 40 steps, 5 minutes. Never enter real personal data. Never click anything labeled delete, cancel account, or unsubscribe.
- Deterministic fallback when no API key: find a link/button matching /pricing|buy|upgrade|subscribe|checkout|get started/.

## 9. Payment check

Test mode (default, what the demo uses):
1. The agent reaches Stripe Checkout (checkout.stripe.com) or an embedded Payment Element.
2. Fills card `4242 4242 4242 4242`, any future expiry, any CVC, ZIP 94107, test email.
3. Also runs one decline case `4000 0000 0000 0002` to confirm the site shows a useful error.
4. Optional 3DS case `4000 0025 0000 3155` to confirm the challenge flow works.
5. After success: checks the page says success, the paid feature unlocked (agent looks for the upgraded state), and the receipt email arrived (inbox check, 2-minute timeout).
6. Deep verification (optional, strongly recommended): user pastes a **restricted read-only Stripe key** (read: Checkout Sessions, Charges, Events, Webhook Endpoints). We confirm the session is `complete`, the charge `succeeded`, and every webhook endpoint delivered the event (`pending_webhooks == 0`, recent deliveries without failures).
7. Refund: in test mode nothing to refund. With a key that has refund permission, refund automatically.

Live mode (post-hackathon, opt-in only):
- One real purchase of the cheapest item, then refund. Stripe keeps its fee (about 2.9% + 30¢), so tell the user and charge for it.
- Use a dedicated virtual card (Stripe Issuing or a Link agent wallet) with a per-run spend limit. One attempt per run, never retry declines: repeated automated attempts look like card testing to Stripe and to the target's Radar.
- Only on verified domains, only with explicit consent checkbox.

## 10. Load test

Flow to replay: the agent records the real requests for homepage → pricing → signup page (GETs only by default). POSTs only when the user explicitly allows them, never payment endpoints.

Stages (default "Launch pass"): 10 → 25 → 50 → 100 → 200 → 400 → 800 virtual users, 30 seconds each, 1–3 second think time. Hard caps: 1,000 VUs total, 5 minutes total, 200 requests/second per region.

Break point = the first stage where error rate > 5% **or** p95 latency > 3× the baseline stage **or** p95 > 5 seconds. Stop the test immediately when the break point is hit (do not keep hammering a dead site).

Traffic identity (always): `User-Agent: LaunchproofLoadTest/1.0 (+https://launchproof.xyz/bot; run=<id>)` and header `X-Launchproof-Run: <id>` so owners can see and filter it.

Where it runs:
- Today: Fly Machines, 2–4 regions (sjc, iad, lhr, sin), started by the Tester agent from its Brainbase sandbox. One machine per region, image = our Python engine, `auto_destroy: true`, restart policy `no`. The engine writes its result to a file and waits up to 150 s; the orchestrator reads it through the Machines exec API and deletes the machine, so no public callback URL is needed.
- Fallback: the same engine runs inside the Brainbase sandbox (or a laptop) if Fly is not ready: one region, same caps.

Teardown (layered, so nothing is ever left running):
1. The engine exits when the stage plan ends or the break point is hit (hard `max_duration`).
2. `auto_destroy: true` destroys the machine when the process exits.
3. The orchestrator calls `DELETE /machines/{id}?force=true` for every machine it created, in a `finally` block.
4. A reaper (`python -m launchproof.load.fly reap`) lists machines in the app and kills any older than 10 minutes. Run it on a 5-minute cron and once at the end of the day.
5. Fly spend alert in the dashboard. Per-run cost at these sizes: shared-cpu-1x is about $0.008/hour, so a 4-region, 5-minute run costs well under a cent in compute.

Sites behind Cloudflare: Cloudflare's DDoS protection may block the test. The report must say so plainly ("blocked by your CDN at 180 users: that is your protection working, not your app failing") and give the user the option to allowlist our run header in a WAF skip rule.

Their bill: warn before running. Traffic costs them money on usage-priced hosts (Vercel bandwidth, Supabase egress). Show an estimate (requests × average response size) and require a checkbox.

## 11. Score and report

Score out of 100: UI 35 (errors, blank pages, overflow, broken images), Payments 35 (checkout completes, unlock confirmed, decline handled, webhook delivered), Load 30 (break point relative to target: 800 users = full marks).
Report sections: headline + share card; what broke (ranked, with screenshot and the exact error); fix prompts (one paste-ready prompt per issue, written by Claude with the evidence); raw numbers.
Share card: 1200×630 PNG, "Survived 800 users from 4 regions · Checkout works · 2 layout bugs", site name, date. OG tags on the public report link.

## 12. Our own payments (Stripe)

- Free: 1 run, UI check only (no verification needed for the crawl), 100-user load cap after verification.
- Launch pass $9 one-time: full load (1,000 users, 4 regions), payment check, fix prompts, share card.
- Pro $29/month (after the hackathon): run on every deploy via a GitHub Action or deploy webhook, history, Slack/Linear.
- Implementation today: Stripe Checkout Session (mode=payment) created by the API with `client_reference_id=<user or run id>`; webhook `checkout.session.completed` grants the pass. Test mode for the demo.
- Stripe activation needs on the site: what you sell and the price in USD, contact email, refund policy, terms, privacy policy (templates in `legal/`).
- Refund policy: full refund within 14 days if a run failed to complete for reasons on our side.

## 13. Legal and safety

- **Authorization is the whole game.** Load testing a site you do not control can be a denial-of-service attack under the Computer Fraud and Abuse Act and similar laws. Ownership verification + terms representation + caps + labeled traffic are the defenses.
- Terms of Service must include: user represents they own or are authorized to test the target and have permission from their hosting providers; user is responsible for costs their providers charge; no guarantee the site will not slow or go down during the test; we may refuse or stop any run; indemnity; limitation of liability; 18+.
- Acceptable Use Policy: no testing third-party sites, no bypassing caps, no testing payment endpoints with real cards except through our live-payment flow, no using results to attack.
- Hosting provider policies: AWS allows ordinary load tests without a form (DDoS simulation needs approval and pre-approved partners); Cloudflare blocks load-test traffic it sees as DDoS and has a separate DDoS-simulation process; check Vercel/Netlify/Supabase fair-use terms and warn users about bandwidth bills.
- Payments: test mode by default. Live mode needs explicit consent, one attempt, auto refund, spend-limited card. Never store card numbers. Never submit real personal data into target forms.
- Privacy: screenshots can contain personal data shown on the target site. Store them privately, delete after 30 days, let users delete a report. Privacy policy lists processors (Anthropic, Fly.io, Cloudflare, Stripe).
- Robots and politeness for the crawl: honor robots.txt for crawling unverified sites, 1 request/second, 20-page cap.
- Email: our test signups use our own inbox domain, never a real person's address.

## 14. Hackathon-day schedule (rewritten 11:30, Brainbase-first, checkout last)

| Time | Do (RUNBOOK step) |
|---|---|
| 11:30–11:45 | Accounts, Brainbase questions at their table (1) |
| 11:45–12:00 | Laptop test run, push to GitHub (2) |
| 12:00–12:20 | Demo shop on Fly + ownership token (3) |
| 12:20–12:30 | Fly load image + one-region smoke test (4) |
| 12:30–1:45 | Lunch + panel. During it: Tester agent on Brainbase, UI-only test in chat (5a) |
| 1:45–2:15 | Full test from Brainbase chat, then Triage + Janitor + orchestration + Slack/Linear surfaces (5a–5d) |
| 2:15–3:00 | Real teams through Slack (6) |
| 3:00–3:10 | Backup video, reap, `fly list` empty (7) |
| 3:10–3:25 | Last: $9 Launch Pass + landing page (8). Skip if behind. |
| 3:25–3:30 | Submit with disclosure |

## 15. Demo script (3 minutes)

1. "Every team here deploys today. Mine checks that yours survives launch." (Slack #launches on screen.)
2. `@Launchproof Tester full test https://brightboard-demo-<you>.fly.dev` with the token. Ownership verified.
3. Live: the agent, running on Brainbase, browses the site as a new customer and reaches Stripe Checkout, then saves the path.
4. Live: test card pays, decline shows an error, and the paywall-bypass probe unlocks Pro without paying. Critical.
5. Live: load machines appear in sjc and iad, users ramp, the site breaks at 200 users, machines are deleted (Fly dashboard empty; the Janitor agent guarantees it every 30 minutes).
6. Triage agent posts the summary and opens Linear issues with paste-ready fix prompts. Share card.
7. Flip `DEMO_TRUST_REDIRECT=0`, re-run: "bypass fixed since your last run" (memory).
8. Close: three agents on Brainbase coordinating through Slack and Linear, an agent that buys, and an agent that provisions and tears down infrastructure: all three tracks. Then the $9 pass if it is built.

## 16. Risks and answers for judges

- "Isn't this just k6 + Playwright?" The value is the agent that finds the real user journey, completes a payment and verifies it, decides the break point, and writes fixes, in one run, safely, for people who have never written a test.
- "Can it be abused?" Verification, caps, labeled traffic, hard teardown, terms.
- "Why will people pay?" One launch-day outage costs more than $9; Pro sells the every-deploy habit.
- Competitors: QA.tech, Octomind, Shiplight, Scout QA (functional); k6, LoadFocus (load); Percy, Chromatic (visual). None combine the three with a payment check.

## 17. After the hackathon

Product Hunt launch with share cards from real users; post in r/lovable, r/vibecoding, r/SideProject; GitHub Action for every-deploy runs; live-mode payment check with a spend-limited card; Cloudflare Browser Rendering for UI runs; public leaderboard (opt-in).

## Head start status (written before kickoff, disclose it)

Done and tested (60 tests, including real-browser end to end against the demo shop):
- `python -m launchproof run <url>`: ownership → UI check → payment check → load → report → Slack/Linear.
- UI check catches the planted overflow, broken image and console error, and records the site's API calls to reuse as load paths.
- Payment check: success, decline and a new **paywall-bypass probe** (opens the success URL with a fake session id as an unpaid user). This is the strongest demo moment: it is a real, common vibe-coded bug and a one-line explanation.
- Load engine: ramp, caps, break point, CDN-block detection. Demo shop survives 100 users and breaks at 200 (db pool exhausted).
- Fly orchestrator with layered teardown and reaper (not run against real Fly: needs your token).
- Claude journey agent (loop tested with a scripted fake model; needs your key for the real run).
- Report HTML, share card PNG, fix prompts (templates now, Claude when the key is set).
- Legal templates in `legal/`.

Not done (today's work): public deployment, web front end, our own $9 checkout, real Stripe Checkout selectors verified live, Fly image pushed, Cloudflare Browser Rendering (optional).

## 18. Where each piece runs, Brainbase credits, Stripe agent payments

Superseded by section 4 (Brainbase-first) and RUNBOOK.md. Still true from the earlier version:
- Jetson Nano: not used (JetPack 4 = Ubuntu 18.04 + Python 3.6; Playwright Chromium unsupported there).
- Stripe agent payments: test cards today. Demo upgrade if Issuing is enabled on the test account: one virtual card per run, limited to the product price at that merchant, cancelled after. Live mode later: the same design, one purchase, automatic refund; Link wallet for agents when the user wants to approve on their phone. Our own billing stays on Stripe Checkout.
- Why Fly for load and not Cloudflare Workers or AWS: Cloudflare IPs get blocked by Cloudflare-protected sites and Workers cap subrequests; AWS multi-region setup takes hours; Fly is one API call per region with per-second billing and delete by API.

