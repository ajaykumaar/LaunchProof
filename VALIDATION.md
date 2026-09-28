# Brainbase-heavy approach — validation notes

Validated against Brainbase docs (Universal Harness API, agent manifest, orchestration
manifest, MCP tools, external triggers) and against the vanilla CLI in
`../launchproof-v0_vanila`.

## What belongs on Brainbase (maximize credits)

| Capability | Implementation | Status |
|---|---|---|
| Tester agent (Claude Code + Daytona L) | `brainbase/agents/tester/` | Ready to push |
| MCP buyer tools | `launchproof/mcp_server.py` (+ `browser_close`) | Ready |
| Journey replay | `python -m launchproof run --journey` | Ready |
| Fix prompts by agent | `launchproof report --prompts` | Ready |
| Triage → Slack/Linear | `brainbase/agents/triage/` + `notify.py` | Ready |
| Janitor schedule | orchestration trigger `*/30` → janitor | Ready |
| Orchestration graph | `brainbase/brainbase-orchestration.yaml` | Ready (canvas fallback if CLI rejects) |
| API / chat handoff | `launchproof/brainbase.py` + `api/server.py` | Ready |
| Memory tables `runs` / `filed_issues` | Tester + Triage instructions | Configure in UI after push |
| Evals | declared on all three agent manifests | Ready |

## What stays off Brainbase (and why)

| Piece | Why |
|---|---|
| Multi-region load | Need create/destroy VMs in named regions; sandbox region/egress undocumented; 4 vCPU cap |
| Demo shop | Must stay awake; Brainbase preview URLs sleep when idle |
| $9 Launch Pass web | Thin Stripe + FastAPI; optionally on Fly; delegates runs to Brainbase |

Fallback: if Fly is unset, Tester omits `--regions` and load runs inside the sandbox (one region).

## Gaps closed in this finalize pass

- Janitor + Triage: real `entrypoint.sh` + `PYTHONPATH` + instructions/evals
- Orchestration file moved to `brainbase/brainbase-orchestration.yaml` (CLI expects `./agents/` siblings)
- Schedule trigger `node_id` UUID added
- `LP_REPO` required (no silent `YOUR_GITHUB_USER` clone)
- `task_message` no longer embeds `--token None`
- Web app hydrates `score`/`headline` from Brainbase `report.json`
- Slim `Dockerfile.api` (Playwright image kept as `Dockerfile.api.playwright`)
- Load-result endpoints require configured `LP_RESULT_TOKEN`
- README / RUNBOOK / secrets example aligned with Brainbase-first

## Still requires a live Brainbase account

These cannot be proven offline:

1. `brainbase agent create && push` for tester/triage/janitor
2. Model id acceptance (`claude-sonnet-5` — override in UI if rejected)
3. Playwright `--with-deps` inside Daytona (ask Brainbase table; e2b fallback in RUNBOOK)
4. Slack/Linear surface connect + orchestration handoff payload
5. File download path `launchproof/runs/<id>/report.html` from tasks API

## Local proof (no Brainbase)

```bash
pip install -r requirements.txt && python -m playwright install chromium
pytest -q
python -m launchproof run http://localhost:8100 --stage-seconds 8 --stages 10,25,50,100,200,400
```

Vanilla CLI behavior is preserved; Brainbase is the production orchestrator, not a rewrite of the checks.

## Fly status (hackathon)

**Default: Fly off.** Load runs locally or in the Brainbase Tester sandbox.
**Re-enable:** set `FLY_API_TOKEN` + `FLY_APP` (RUNBOOK Appendix A). `fly_configured()` gates
`--regions`; Janitor schedule stays `is_active: false` until you flip it. No code revert required.
