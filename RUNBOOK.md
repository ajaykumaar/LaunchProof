# Launchproof runbook (Brainbase-first, Fly optional)

Do the steps in order. **Fly is optional** until billing works: load runs on your laptop or in the
Brainbase Tester sandbox. Fly code stays in the repo — set `FLY_API_TOKEN` + `FLY_APP` later to
turn multi-region load back on (no code revert). Hacking ends 3:30. Our $9 checkout is last.

## What runs where

| Piece | Runs on | Paid by |
|---|---|---|
| **Tester agent**: UI, MCP journey, Stripe test payments, bypass probe, report, fix prompts, **local/sandbox load** | Brainbase sandbox | Brainbase credits |
| **Chaos agent**: messy-human probes (rage click, double submit) | Brainbase | Budget-capped credits |
| **Critic agent**: visual appeal score | Brainbase browser | Budget-capped credits (no Anthropic) |
| **Triage agent**: Slack + Linear | Brainbase | Brainbase credits |
| **Janitor agent**: Fly reaper | Off until Fly credentials exist (`is_active: false`) | — |
| **Front doors**: chat, Slack, Linear, API | Brainbase | Brainbase credits |
| **Demo target**: Brightboard | Laptop + Cloudflare Tunnel (or Fly when card works) | Free tunnel / Fly |
| **Multi-region load** | Fly Machines — **optional**, enable later | Fly cents |
| **Landing + $9 Launch Pass** | Thin web app (later) | Stripe test |

No Anthropic key required on Brainbase (Tester uses Brainbase credits). For **local** CLI runs,
`.env.local` is auto-loaded: with `ANTHROPIC_API_KEY` set you get Claude for journey discovery and
fix-prompt rewrite (`LP_MODEL=claude-sonnet-5`). Use `--no-agent` to force heuristics. Do **not**
put your Anthropic key in Brainbase agent secrets unless you want the sandbox CLI fallback to bill Anthropic.

---

## Step 0. Shared secret (1 min)
```powershell
python -c "import secrets; print('LP_SECRET=' + secrets.token_urlsafe(32))"
```
Add `LP_SECRET=...` to `.env.local` (no space after `=`). Same value everywhere later.
Also set `ANTHROPIC_API_KEY` and optional `LP_MODEL=claude-sonnet-5` for local Claude. The CLI/API
load `.env.local` automatically — you no longer need the PowerShell `Get-Content` loop for those runs.

## Step 1. Accounts (parallel)
1. **GitHub**: private repo + fine-grained PAT (Contents read) → `GITHUB_TOKEN` in `.env.local`.
2. **Brainbase**: app.brainbaselabs.com → API key → `BRAINBASE_API_KEY` / `BRAINBASE_TOKEN`. CLI: `brainbase login`.
3. **Fly.io**: skip until card works. (Appendix A when ready.)
4. **Stripe** (optional for real Checkout): test-mode `sk_test_...`.
5. **Slack / Linear** (optional): for Triage.

Load `.env.local` each new terminal:
```powershell
Get-Content .env.local | ForEach-Object {
  if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
    Set-Item -Path ("env:" + $matches[1].Trim()) -Value $matches[2].Trim()
  }
}
```

## Step 2. Laptop check + push (15 min)
```powershell
cd launchproof-v1_brainbase
# venv already active
pip install -r requirements.txt
python -m playwright install chromium
pytest -q

# terminal A
uvicorn demo_shop.app:app --port 8100

# terminal B — full run, local load (no Fly)
python -m launchproof run http://localhost:8100 --stages 10,25,50,100,200,400 --stage-seconds 8
# open runs\*\report.html  — expect ~46/100
```
Push the repo to GitHub, then set `LP_REPO=github.com/<you>/launchproof.git` in `.env.local` and agent secrets.

## Step 3. Public demo URL without Fly (Cloudflare Tunnel)
```powershell
# install cloudflared once, then:
cloudflared tunnel --url http://localhost:8100
```
Use the `https://….trycloudflare.com` URL as the site under test for Brainbase (sandboxes cannot hit localhost).

Ownership token against that URL:
```powershell
python -m launchproof token https://YOUR-TUNNEL.trycloudflare.com
```
Put the token in the demo via meta / file if you want full payment+load from Brainbase (or use UI-only).

## Step 4. Brainbase agents (was Step 5 — do this now)
Skip old Fly load steps. Go straight to agents:

**4a. Tester** (from repo root: `LaunchProof\` or `launchproof-v1_brainbase\`)
```powershell
cd brainbase\agents\tester
mkdir .brainbase
copy ..\..\secrets.env.example .brainbase\secrets.env
# edit secrets: GITHUB_TOKEN, LP_REPO, LP_SECRET — leave FLY_* blank
brainbase agent create
brainbase agent push
```

**4b. Triage + Janitor + Chaos + Critic** (from repo root each time — Janitor is a no-op without Fly)
```powershell
# start from LaunchProof\ (or launchproof-v1_brainbase\), not from tester/
cd brainbase\agents\triage
mkdir .brainbase; copy ..\..\secrets.env.example .brainbase\secrets.env
# edit secrets same as Tester, then:
brainbase agent create; brainbase agent push

cd ..\janitor
mkdir .brainbase; copy ..\..\secrets.env.example .brainbase\secrets.env
brainbase agent create; brainbase agent push

cd ..\chaos
mkdir .brainbase; copy ..\..\secrets.env.example .brainbase\secrets.env
brainbase agent create; brainbase agent push

cd ..\critic
mkdir .brainbase; copy ..\..\secrets.env.example .brainbase\secrets.env
brainbase agent create; brainbase agent push
```

**4c. Orchestration** (must run from `brainbase\`, where `agents\` and `brainbase-orchestration.yaml` live)
```powershell
cd ..\..   # if you are in brainbase\agents\critic → lands in brainbase\
# or from repo root:  cd brainbase
brainbase orchestration create
brainbase orchestration push
```
Janitor schedule is `is_active: false` until Fly is back. Tester hands off to Chaos then Critic within the Smart UI budget, then Triage.

**Smart UI budget** (Chaos messy-human + Critic visual appeal; Brainbase credits only — no Anthropic vision):
| Knob | Default | Meaning |
|---|---|---|
| `LP_MAX_CHAOS` / `max_chaos_scenarios` | 3 | Max messy-human scenarios per run |
| `LP_MAX_VISION_VIEWS` / `max_vision_views` | 4 | Max Critic page views (URL×viewport) |
| `LP_SMART_UI_CREDITS` / `credit_soft_cap` | 40 | Soft credit stop for the smart-UI phase |
| `LP_SMART_UI_BUDGET` | JSON | Override all four keys at once |

Webapp start-run form has the same fields under “Smart UI budget”. Tiny smoke: set both chaos and vision to `1`. Local CLI marks `smart_ui: skipped` (agents only run on Brainbase).

**4d. Test in Brainbase chat**
1. UI-only on your tunnel URL (heuristic UI + optional Chaos/Critic).
2. Full test with token — agent must **omit `--regions`**; load runs in the sandbox.

**4e. API kickoff**
```powershell
$env:BRAINBASE_AGENT_ID = "<from brainbase agent status>"
python -m launchproof.brainbase run https://YOUR-TUNNEL.trycloudflare.com --token lp_... --full --max-chaos 1 --max-vision 1
```

## Step 5. Real teams / video / $9 pass
Same as before: opt-in runs, backup video, Stripe Launch Pass last if time.

---

## Appendix A — Re-enable Fly (when card works)

No code revert. Credentials auto-enable multi-region load.

1. `fly auth login`, create demo + load apps (see `fly.demo.toml`, `fly.load.toml` — rename CHANGEME).
2. Deploy demo; push load image with `--image-label engine`.
3. Set in `.env.local` and Brainbase secrets:
   ```
   FLY_API_TOKEN=FlyV1 ...
   FLY_APP=launchproof-load-YOU
   LP_REGIONS=sjc iad
   ```
4. Set Janitor trigger `is_active: true` in `brainbase-orchestration.yaml` and `brainbase orchestration push`.
5. Smoke: `python -m launchproof.load.fly run --url https://demo... --paths / --regions sjc --stages 10,25 --stage-seconds 10`
6. Full Brainbase run will include `--regions` automatically when `fly_configured()` is true.

Force local even with Fly creds: `LP_USE_FLY=0`.

---

## If something breaks
| Symptom | Fix |
|---|---|
| Agent passes `--regions` and Fly errors | Secrets must not set FLY_*; instructions say omit regions |
| Brainbase can't reach localhost | Use Cloudflare Tunnel (step 3) |
| `npm` / `python3` missing on Windows | Use `python`; install Node LTS via winget for Brainbase CLI |
| Fly card declined | Stay on Appendix-A path later; product works without it |
