"""Launchproof CLI.

  python -m launchproof token https://yoursite.com          # issue an ownership token + instructions
  python -m launchproof run https://yoursite.com --token lp_...  [options]

Order of a run: ownership check -> UI check (phone + desktop) -> payment check (Stripe test mode)
-> load test (local engine or Fly regions) -> score, fix prompts, report.html, share-card.png
-> optional Slack summary and Linear issues.
Unverified sites get the UI check only (robots.txt honored, 20 pages max).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from . import ownership
from .envfile import load_env_local

load_env_local()


import contextvars

# The web API sets this to a list per run so the status page can stream progress lines.
LOG_SINK: contextvars.ContextVar = contextvars.ContextVar("lp_log", default=None)


def _say(msg: str):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    sink = LOG_SINK.get()
    if sink is not None:
        sink.append(line)


def run_args(url: str, **kw) -> argparse.Namespace:
    """Build the same options the CLI would, for programmatic use (web API)."""
    defaults = dict(url=url, token=None, out="runs", run_id=None, max_pages=12, skip_ui=False, skip_pay=False,
                    skip_load=False, no_agent=False, unlock_selector=None, stripe_key=None, regions=None,
                    load_paths=None, stages="10,25,50,100,200,400,800", stage_seconds=30.0, think=(1.0, 3.0),
                    report_link=None, i_understand_costs=True, journey=None)
    defaults.update(kw)
    return argparse.Namespace(**defaults)


async def run(a) -> dict:
    run_id = a.run_id or f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    out = Path(a.out) / run_id
    out.mkdir(parents=True, exist_ok=True)
    url = a.url if "://" in a.url else "https://" + a.url
    record: dict = {"run_id": run_id, "url": url, "started_at": datetime.now().isoformat(timespec="seconds")}

    v = ownership.verify(url, a.token)
    record["ownership"] = v
    verified = v["verified"]
    _say(f"ownership: {'verified via ' + v['method'] if verified else 'NOT verified (' + v.get('reason', '') + ')'}")
    if not verified and not (a.skip_pay and a.skip_load):
        _say("payment and load tests need a verified site; running the UI check only")
        a.skip_pay = a.skip_load = True

    if not a.skip_ui:
        from .ui_check import run_ui_check
        _say("UI check: crawling on phone and desktop")
        ui = await run_ui_check(url, out, max_pages=a.max_pages if verified else min(a.max_pages, 20),
                                delay_s=0.3 if verified else 1.0, respect_robots=not verified)
        record["ui"] = ui.to_dict()
        _say(f"UI check: {len(ui.pages) // 2} pages, {len(ui.issues)} findings")

    if not a.skip_pay:
        from .payment import run_payment_check
        from .agent import agent_available
        recorded = None
        if getattr(a, "journey", None):
            data = json.loads(Path(a.journey).read_text())
            recorded = data.get("actions") if isinstance(data, dict) else data
            _say(f"payment check: replaying recorded journey ({len(recorded)} steps) from {a.journey}")
        elif a.no_agent:
            _say("payment check: heuristic journey (--no-agent)")
        elif agent_available():
            _say(f"payment check: Claude journey agent (model {os.getenv('LP_MODEL', 'claude-sonnet-5')})")
        else:
            _say("payment check: heuristic journey (set ANTHROPIC_API_KEY for Claude)")
        pay = await run_payment_check(url, out, run_id=run_id, use_agent=not a.no_agent,
                                      unlock_selector=a.unlock_selector, stripe_key=a.stripe_key or os.getenv("LP_TARGET_STRIPE_KEY"),
                                      journey_actions=recorded)
        record["payments"] = pay.to_dict()
        _say("payment check: " + ", ".join(f"{c.case}={c.outcome}" for c in pay.cases) + f" (driver: {pay.driver})")

    if not a.skip_load:
        paths = a.load_paths
        if not paths:  # pages we crawled + API calls the pages made, max 6, same site only
            host = urlparse(url).netloc
            seen: list[str] = []
            for p in (record.get("ui") or {}).get("pages", []):
                for u in [p["url"]] + p.get("api_calls", []):
                    pu = urlparse(u)
                    path = (pu.path or "/") + (f"?{pu.query}" if pu.query else "")
                    if pu.netloc == host and path not in seen and not any(w in path for w in ("checkout", "upgrade", "logout")):
                        seen.append(path)
            paths = seen[:6] or ["/"]
        stages = [int(x) for x in a.stages.split(",")]
        want_fly = bool(a.regions)
        if want_fly:
            from .load.fly import fly_configured
            if not fly_configured():
                _say("Fly not configured (need FLY_API_TOKEN + FLY_APP); running load from this machine instead")
                want_fly = False
        if want_fly:
            from .load.fly import combine, run_regions
            _say(f"load test on Fly: {', '.join(a.regions)} · paths {paths}")
            cfg = {"url": url, "paths": paths, "stages": stages, "stage_seconds": a.stage_seconds}
            res = await run_regions(run_id, cfg, a.regions)
            res["combined"] = combine(res["regions"])
            record["load"] = res
        else:
            from .load.engine import LoadConfig, run_load
            _say(f"load test from this machine · paths {paths}")
            cfg = LoadConfig(url=url, paths=paths, stages=stages, stage_seconds=a.stage_seconds, run_id=run_id,
                             think_min=a.think[0], think_max=a.think[1])
            r = await run_load(cfg, on_stage=lambda s: _say(
                f"  {s.users:>4} users  {s.rps:>6} rps  p95 {s.p95_ms} ms  errors {s.error_rate:.1%}"
                + (f"  BROKE: {s.reason}" if s.broke else "")))
            record["load"] = {"run_id": run_id, "regions": {"local": r}, "errors": {}}
        for region, e in record["load"].get("errors", {}).items():
            _say(f"  load error in {region}: {e}")

    record["finished_at"] = datetime.now().isoformat(timespec="seconds")
    from .report import build_report
    if os.getenv("ANTHROPIC_API_KEY"):
        _say(f"fix prompts: Claude rewrite (model {os.getenv('LP_MODEL', 'claude-sonnet-5')})")
    else:
        _say("fix prompts: templates (set ANTHROPIC_API_KEY for Claude rewrite)")
    rep = await build_report(record, out)
    (out / "run.json").write_text(json.dumps(record, indent=2, default=str))
    _say(f"score {rep['score']['total']}/100 · " + " · ".join(rep["headline"]))
    for i in rep["issues"][:8]:
        _say(f"  [{i['severity']}] {i['kind']}: {i['detail'][:110]}")
    _say(f"report: {out / 'report.html'}")

    from . import notify
    if notify.slack(rep, url, a.report_link):
        _say("posted summary to Slack")
    made = notify.linear(rep, url)
    if made:
        _say(f"created Linear issues: {', '.join(made)}")
    rep["run_id"] = run_id
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="launchproof", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("token", help="issue an ownership token for a site")
    t.add_argument("url")
    r = sub.add_parser("run", help="test a site")
    r.add_argument("url")
    r.add_argument("--token", help="ownership token from `launchproof token`")
    r.add_argument("--out", default="runs")
    r.add_argument("--run-id")
    r.add_argument("--max-pages", type=int, default=12)
    r.add_argument("--skip-ui", action="store_true")
    r.add_argument("--skip-pay", action="store_true")
    r.add_argument("--skip-load", action="store_true")
    r.add_argument("--no-agent", action="store_true", help="use the heuristic journey instead of Claude")
    r.add_argument("--unlock-selector", help="CSS selector that only exists once the user has paid, e.g. '#plan-status[data-plan=pro]'")
    r.add_argument("--stripe-key", help="restricted READ-ONLY key for the target's Stripe account (deep verification)")
    r.add_argument("--regions", nargs="*", help="Fly regions, e.g. sjc iad lhr sin. Omit to run the load test locally.")
    r.add_argument("--load-paths", nargs="*")
    r.add_argument("--stages", default="10,25,50,100,200,400,800")
    r.add_argument("--stage-seconds", type=float, default=30)
    r.add_argument("--think", type=lambda s: tuple(float(x) for x in s.split(",")), default=(1.0, 3.0))
    r.add_argument("--journey", help="journey.json recorded by the Brainbase agent's browser tools (skips discovery)")
    r.add_argument("--report-link", help="public URL of the report, for the Slack message")
    r.add_argument("--i-understand-costs", action="store_true",
                   help="acknowledge that load tests create real traffic and may cost the site owner bandwidth")
    rb = sub.add_parser("report", help="rebuild report.html/report.json from a run folder, optionally with new fix prompts")
    rb.add_argument("run_dir")
    rb.add_argument("--prompts", help="JSON list of fix prompts, same order as report.json issues")
    sv = sub.add_parser("serve", help="serve the runs folder over HTTP (for a sandbox preview URL)")
    sv.add_argument("--dir", default="runs")
    sv.add_argument("--port", type=int, default=8000)
    a = ap.parse_args(argv)
    if a.cmd == "report":
        from .report import build_report
        d = Path(a.run_dir)
        record = json.loads((d / "run.json").read_text())
        prompts = json.loads(Path(a.prompts).read_text()) if a.prompts else None
        rep = asyncio.run(build_report(record, d, prompts_override=prompts))
        print(json.dumps({"score": rep["score"], "headline": rep["headline"], "report": str(d / "report.html")}))
        return 0
    if a.cmd == "serve":
        import functools
        import http.server
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=a.dir)
        print(f"serving {a.dir} on 0.0.0.0:{a.port}")
        http.server.ThreadingHTTPServer(("0.0.0.0", a.port), handler).serve_forever()
        return 0
    if a.cmd == "token":
        tok = ownership.issue_token(a.url)
        print(json.dumps({"token": tok, **ownership.instructions(a.url, tok)}, indent=2))
        return 0
    host = ownership.host_of(a.url)
    if not a.skip_load and not ownership.DEV_HOSTS.match(host) and not a.i_understand_costs:
        print("Load tests send real traffic that can cost the site owner money (bandwidth, serverless invocations).\n"
              "Re-run with --i-understand-costs, or add --skip-load.")
        return 2
    t0 = time.monotonic()
    asyncio.run(run(a))
    _say(f"done in {time.monotonic() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
