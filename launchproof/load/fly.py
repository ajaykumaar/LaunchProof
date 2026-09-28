"""Run the load engine on Fly Machines in several regions, then make sure every machine is gone.

Teardown is layered so nothing is ever left running (or billing):
  1. The engine exits on its own (stage plan done, break point hit, or 300 s hard cap).
  2. auto_destroy=true: Fly destroys the machine when the process exits. restart policy "no".
  3. run_regions() force-deletes every machine it created in a finally block, even on Ctrl-C or errors.
  4. `python -m launchproof.load.fly reap` kills any machine in the app older than REAP_AFTER_S.
     Run it from cron every 5 minutes, and once at the end of the day.

Setup (once):
  fly auth login
  fly apps create launchproof-load
  fly deploy --build-only --push -a launchproof-load --image-label engine -c fly.load.toml
  export FLY_API_TOKEN=$(fly tokens create deploy -a launchproof-load) FLY_APP=launchproof-load

Usage:
  python -m launchproof.load.fly run --url https://site --paths / /pricing --regions sjc iad lhr sin
  python -m launchproof.load.fly reap
  python -m launchproof.load.fly list

Results come back one of three ways (first that works wins):
  - default: the engine writes /tmp/lp_result.json and waits up to 150 s; we read it with the Machines
    exec API, then delete the machine. Needs nothing public, so it works from a Brainbase sandbox.
  - LP_RESULT_BASE set (e.g. https://launchproof-api.fly.dev): each engine POSTs its summary to
    {base}/api/load-results/{run_id}/{region} and we poll {base}/api/load-results/{run_id}.
    This is the production path and works from anywhere (laptop, Brainbase sandbox).
  - last resort: the "LP_RESULT {...}" line from machine logs via `flyctl logs --no-tail`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

import httpx

API = "https://api.machines.dev/v1"
DEFAULT_REGIONS = ["sjc", "iad", "lhr", "sin"]
REAP_AFTER_S = 600
MAX_REGIONS = 6


def fly_configured() -> bool:
    """True when multi-region Fly load is usable. Set FLY_API_TOKEN + FLY_APP to re-enable after card setup.
    LP_USE_FLY=0 forces local/sandbox load even if credentials exist."""
    flag = os.getenv("LP_USE_FLY", "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if flag in ("1", "true", "yes", "on"):
        return bool(os.getenv("FLY_API_TOKEN") and os.getenv("FLY_APP"))
    return bool(os.getenv("FLY_API_TOKEN") and os.getenv("FLY_APP"))


def _app() -> str:
    return os.getenv("FLY_APP", "launchproof-load")


def _image() -> str:
    return os.getenv("LP_LOAD_IMAGE", f"registry.fly.io/{_app()}:engine")


def _client() -> httpx.AsyncClient:
    token = os.getenv("FLY_API_TOKEN")
    if not token:
        raise SystemExit("FLY_API_TOKEN is not set (fly tokens create deploy -a <app>)")
    if not token.startswith(("FlyV1 ", "Bearer ")):
        token = ("FlyV1 " if token.startswith("fm2_") else "Bearer ") + token
    return httpx.AsyncClient(base_url=API, headers={"Authorization": token}, timeout=30)


def machine_body(region: str, run_id: str, load_cfg: dict) -> dict:
    env = {"LP_CONFIG": json.dumps({**load_cfg, "region": region, "run_id": run_id}),
           "LP_HOLD_S": "150"}  # engine waits (max 150 s) for us to read its result via exec
    base = os.getenv("LP_RESULT_BASE", "").rstrip("/")
    if base:
        env["LP_RESULT_URL"] = f"{base}/api/load-results/{run_id}/{region}"
        env["LP_RESULT_TOKEN"] = os.getenv("LP_RESULT_TOKEN", "")
    return {
        "name": f"lp-{run_id[:12]}-{region}",
        "region": region,
        "config": {
            "image": _image(),
            "env": env,
            "auto_destroy": True,
            "restart": {"policy": "no"},
            "guest": {"cpu_kind": "shared", "cpus": 1, "memory_mb": 512},
            "metadata": {"launchproof_run": run_id},
            "stop_config": {"signal": "SIGINT", "timeout": 5_000_000_000},
        },
    }


async def _create(c: httpx.AsyncClient, body: dict) -> dict:
    r = await c.post(f"/apps/{_app()}/machines", json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"create {body['region']} failed: {r.status_code} {r.text[:300]}")
    return r.json()


async def _state(c: httpx.AsyncClient, mid: str) -> str:
    r = await c.get(f"/apps/{_app()}/machines/{mid}")
    if r.status_code == 404:
        return "destroyed"
    return r.json().get("state", "unknown")


async def destroy(c: httpx.AsyncClient, mid: str) -> bool:
    r = await c.delete(f"/apps/{_app()}/machines/{mid}", params={"force": "true"})
    return r.status_code in (200, 404)


async def _exec_result(c: httpx.AsyncClient, mid: str) -> dict | None:
    """Read the engine's result file through the Machines exec API. No public URL needed, so this works
    from a laptop, a CI job, or a Brainbase sandbox."""
    try:
        r = await c.post(f"/apps/{_app()}/machines/{mid}/exec",
                         json={"command": ["cat", "/tmp/lp_result.json"], "timeout": 10})
        if r.status_code != 200:
            return None
        body = r.json()
        if body.get("exit_code") == 0 and body.get("stdout", "").strip():
            return json.loads(body["stdout"])
    except Exception:
        return None
    return None


async def _remote_results(run_id: str) -> dict:
    """Results the engines POSTed to our API (LP_RESULT_BASE)."""
    base = os.getenv("LP_RESULT_BASE", "").rstrip("/")
    if not base:
        return {}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{base}/api/load-results/{run_id}",
                            headers={"Authorization": f"Bearer {os.getenv('LP_RESULT_TOKEN', '')}"})
            return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def _logs_result(mid: str) -> dict | None:
    """Fallback: scrape the engine's LP_RESULT line from logs. Needs flyctl logged in."""
    fly = shutil.which("fly") or shutil.which("flyctl")
    if not fly:
        return None
    try:
        out = subprocess.run([fly, "logs", "-a", _app(), "--machine", mid, "--no-tail"],
                             capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return None
    for line in reversed(out.splitlines()):
        if "LP_RESULT " in line:
            try:
                return json.loads(line.split("LP_RESULT ", 1)[1])
            except json.JSONDecodeError:
                continue
    return None


async def run_regions(run_id: str, load_cfg: dict, regions: list[str], results_store: dict | None = None,
                      poll_s: float = 5.0) -> dict:
    """Start one engine per region, wait for all to finish, collect results, destroy everything.
    results_store: dict filled by our API's result callback (keyed by region) when LP_RESULT_URL is used."""
    regions = regions[:MAX_REGIONS]
    budget_s = min(load_cfg.get("max_duration_s", 300), 300) + 120  # engine cap + boot + slack
    created: dict[str, str] = {}
    results: dict[str, dict] = {}
    errors: dict[str, str] = {}
    async with _client() as c:
        try:
            made = await asyncio.gather(*(_create(c, machine_body(r, run_id, load_cfg)) for r in regions),
                                        return_exceptions=True)
            for region, m in zip(regions, made):
                if isinstance(m, Exception):
                    errors[region] = str(m)
                else:
                    created[region] = m["id"]
            deadline = time.monotonic() + budget_s
            pending = set(created)
            while pending and time.monotonic() < deadline:
                await asyncio.sleep(poll_s)
                remote = await _remote_results(run_id)
                if remote:
                    results_store = {**(results_store or {}), **remote}
                for region in list(pending):
                    if results_store and region in results_store:
                        results[region] = results_store[region]
                        pending.discard(region)
                        continue
                    state = await _state(c, created[region])
                    if state == "started":
                        got = await _exec_result(c, created[region])
                        if got:
                            results[region] = got
                            pending.discard(region)
                            await destroy(c, created[region])  # done with it: delete now
                    elif state in ("stopped", "destroyed", "failed"):
                        pending.discard(region)
            for region in pending:
                errors[region] = f"timed out after {budget_s}s, force-destroyed"
        finally:
            for region, mid in created.items():  # layer 3: always clean up
                try:
                    await destroy(c, mid)
                except Exception as e:
                    errors[region] = f"{errors.get(region, '')} destroy failed: {e} (reaper will retry)".strip()
    if created and len(results) < len(created):
        await asyncio.sleep(3)  # an engine may POST just after its machine stops
        results_store = {**(results_store or {}), **(await _remote_results(run_id))}
    for region, mid in created.items():
        if region not in results:
            r = (results_store or {}).get(region) or _logs_result(mid)
            if r:
                results[region] = r
            elif region not in errors:
                errors[region] = "no result received"
    return {"run_id": run_id, "regions": results, "errors": errors, "machines": created}


def combine(region_results: dict[str, dict]) -> dict:
    """Merge per-region summaries into one headline number."""
    if not region_results:
        return {"survived_users_total": 0, "break_point_users_total": None, "regions": 0}
    survived = sum(r.get("survived_users", 0) for r in region_results.values())
    broke = [r for r in region_results.values() if r.get("break_point_users")]
    return {
        "regions": len(region_results),
        "survived_users_total": survived,
        "break_point_users_total": sum(r["break_point_users"] for r in broke) if broke else None,
        "any_break": bool(broke),
        "worst_region": max(region_results.values(), key=lambda r: (r.get("stages") or [{}])[-1].get("p95_ms", 0))["region"],
        "stop_reasons": {k: v.get("stop_reason") for k, v in region_results.items()},
    }


async def list_machines() -> list[dict]:
    async with _client() as c:
        r = await c.get(f"/apps/{_app()}/machines")
        r.raise_for_status()
        return r.json()


async def reap(max_age_s: int = REAP_AFTER_S) -> list[str]:
    """Layer 4: destroy any machine older than max_age_s. Safe to run any time."""
    killed = []
    now = datetime.now(timezone.utc)
    async with _client() as c:
        r = await c.get(f"/apps/{_app()}/machines")
        r.raise_for_status()
        for m in r.json():
            created = datetime.fromisoformat(m["created_at"].replace("Z", "+00:00"))
            if (now - created).total_seconds() > max_age_s and m.get("state") != "destroyed":
                if await destroy(c, m["id"]):
                    killed.append(f"{m['id']} ({m.get('region')}, {m.get('state')})")
    return killed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--url", required=True)
    run.add_argument("--paths", nargs="+", default=["/"])
    run.add_argument("--regions", nargs="+", default=DEFAULT_REGIONS)
    run.add_argument("--stages", default="10,25,50,100,200,400,800")
    run.add_argument("--stage-seconds", type=float, default=30)
    run.add_argument("--run-id", default=f"cli{int(time.time())}")
    rp = sub.add_parser("reap")
    rp.add_argument("--max-age", type=int, default=REAP_AFTER_S)
    sub.add_parser("list")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        cfg = {"url": a.url, "paths": a.paths, "stages": [int(x) for x in a.stages.split(",")],
               "stage_seconds": a.stage_seconds}
        out = asyncio.run(run_regions(a.run_id, cfg, a.regions))
        out["combined"] = combine(out["regions"])
        print(json.dumps(out, indent=2))
    elif a.cmd == "reap":
        killed = asyncio.run(reap(a.max_age))
        print("reaped:", ", ".join(killed) or "nothing")
    else:
        for m in asyncio.run(list_machines()):
            print(m["id"], m.get("region"), m.get("state"), m.get("created_at"), m.get("name"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
