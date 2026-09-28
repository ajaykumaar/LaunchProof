# Launchproof (Brainbase-first)

Paste a URL. A **Brainbase agent** uses your site like a first-time customer on phone and desktop,
buys with a Stripe test card, then load-tests it (local / sandbox today; multi-region Fly when billing
works). You get a score, what broke, paste-ready fix prompts, and a share card.

Built for the Startup Speedrun Hackathon (Sep 28, 2026).

| Track | How we hit it |
|---|---|
| Agentic Payments | Tester records checkout via MCP; code replays success / decline / paywall-bypass |
| Agents That Deploy Infrastructure | Fly Machines optional (enable with `FLY_*`); until then load runs in Brainbase sandbox / laptop. Janitor ready. |
| Autonomous Organizations | Tester → Chaos → Critic → Triage; Janitor schedule off until Fly is on |

**Disclosure:** head-start code existed before kickoff. Tell the organizers.

**Day-of setup: [RUNBOOK.md](RUNBOOK.md)** (Fly is Appendix A). Agents: [`brainbase/`](brainbase/).

---

## What runs where

| Piece | Where | Notes |
|---|---|---|
| Tester (UI, journey, payment, report, fix prompts, **load**) | Brainbase sandbox | Default path |
| Chaos (messy-human probes) | Brainbase | Budget-capped; handoff from Tester |
| Critic (visual appeal) | Brainbase browser | Budget-capped; no Anthropic vision |
| Triage | Brainbase | Slack / Linear |
| Janitor | Brainbase schedule | **Disabled** until Fly credentials exist |
| Multi-region load | Fly Machines | **Opt-in**: set `FLY_API_TOKEN` + `FLY_APP` (+ optional `LP_REGIONS`) |
| Demo shop | Laptop + Cloudflare Tunnel | Or Fly when card works |
| Landing + $9 pass | Thin API → Brainbase | Last step |

`fly_configured()` turns Fly on automatically when credentials are present — **no code revert**.

---

## Laptop check

```powershell
cd launchproof-v1_brainbase
pip install -r requirements.txt
python -m playwright install chromium
uvicorn demo_shop.app:app --port 8100
python -m launchproof run http://localhost:8100 --stage-seconds 8 --stages 10,25,50,100,200,400
pytest -q
```

Public URL without Fly: `cloudflared tunnel --url http://localhost:8100`

---

## Environment

| Variable | Needed for |
|---|---|
| `BRAINBASE_API_KEY`, `BRAINBASE_AGENT_ID` | Hand runs to Brainbase |
| `LP_SECRET`, `LP_REPO`, `GITHUB_TOKEN` | Ownership + sandbox clone |
| `FLY_API_TOKEN`, `FLY_APP` | **Optional** multi-region load + Janitor |
| `LP_REGIONS` | e.g. `sjc iad` when Fly is on |
| `LP_USE_FLY=0` | Force local load even if Fly creds exist |
| `SLACK_*` / `LINEAR_*` | Triage fallback |

---

## Safety

- Ownership required for payment + load (localhost exempt).
- Load caps + labeled traffic unchanged.
- Fly teardown code remains; unused until credentials are set.

Legal: `legal/`. Fly re-enable: RUNBOOK Appendix A.
