"""Ramp load engine. Runs locally or inside a Fly Machine (one per region).

Closed model: N virtual users loop over the target paths with think time between requests.
Stages ramp N up; we stop at the first stage that breaks:
  error rate > 5%, or p95 > 3x the baseline stage p95, or p95 > 5 s.

Safety limits are enforced here as well as in the orchestrator, so a bad config can never exceed them:
  MAX_VUS=1000, MAX_DURATION_S=300, MAX_RPS=200 (per region / per engine process).

Every request carries User-Agent LaunchproofLoadTest/1.0 and X-Launchproof-Run so owners can filter it.

  python -m launchproof.load.engine --url https://site --paths / /pricing --stages 10,25,50 --stage-seconds 30
Inside Fly the same module reads its config from the LP_CONFIG env var (JSON) and POSTs the summary to
LP_RESULT_URL (with LP_RESULT_TOKEN) as well as printing it to stdout.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import urljoin

import httpx

MAX_VUS = 1000
MAX_DURATION_S = 300
MAX_RPS = 200
DEFAULT_STAGES = [10, 25, 50, 100, 200, 400, 800]
LAUNCH_STAGES = [25, 100, 100, 40]  # spike → plateau → drop
RESULT_FILE = "/tmp/lp_result.json"
UA = "LaunchproofLoadTest/1.0 (+https://launchproof.xyz/bot; run={run})"
UA_MOBILE = ("LaunchproofLoadTest/1.0 (Mobile; +https://launchproof.xyz/bot; run={run})")
REFERRERS = (
    "https://www.producthunt.com/",
    "https://news.ycombinator.com/",
)
SUCCESS_BODY = re.compile(
    r"\b(created|claimed|ok|success|welcome|accepted|unlocked|you.?re in)\b", re.I)
FAIL_BODY = re.compile(
    r"\b(already|exists|taken|conflict|duplicate|claimed|unavailable|denied)\b", re.I)


@dataclass
class LoadConfig:
    url: str
    paths: list[str] = field(default_factory=lambda: ["/"])
    stages: list[int] = field(default_factory=lambda: list(DEFAULT_STAGES))
    stage_seconds: float = 30
    think_min: float = 1.0
    think_max: float = 3.0
    timeout_s: float = 10.0
    max_rps: float = MAX_RPS
    max_duration_s: float = MAX_DURATION_S
    run_id: str = "local"
    region: str = "local"
    error_rate_limit: float = 0.05
    p95_multiplier: float = 3.0
    p95_ceiling_s: float = 5.0
    # Additive launch-day knobs (all default off / ramp-compatible).
    burst_users: int | None = None
    burst_path: str = "/"
    profile: str = "ramp"  # ramp | launch
    session_mix: bool = False

    def clamp(self) -> "LoadConfig":
        self.stages = [max(1, min(int(s), MAX_VUS)) for s in self.stages] or [10]
        self.max_rps = min(self.max_rps, MAX_RPS)
        self.max_duration_s = min(self.max_duration_s, MAX_DURATION_S)
        self.paths = [p if p.startswith("/") else "/" + p for p in self.paths] or ["/"]
        if self.burst_users is not None:
            self.burst_users = max(1, min(int(self.burst_users), MAX_VUS))
        bp = self.burst_path or "/"
        self.burst_path = bp if bp.startswith("/") else "/" + bp
        return self


def launch_stages() -> list[int]:
    return list(LAUNCH_STAGES)

@dataclass
class StageResult:
    users: int
    requests: int
    errors: int
    error_rate: float
    rps: float
    p50_ms: int
    p95_ms: int
    p99_ms: int
    status_counts: dict
    top_errors: dict
    cdn_blocked: int
    broke: bool = False
    reason: str = ""


class RateLimiter:
    """Token bucket shared by all virtual users in this process."""

    def __init__(self, rate: float):
        self.rate, self.tokens, self.last = rate, rate, time.monotonic()
        self.lock = asyncio.Lock()

    async def take(self):
        while True:
            async with self.lock:
                now = time.monotonic()
                self.tokens = min(self.rate, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            await asyncio.sleep(wait)


def _pct(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


def _is_cdn_block(resp: httpx.Response) -> bool:
    if resp.status_code not in (403, 429, 503):
        return False
    h = resp.headers
    server = h.get("server", "").lower()
    return ("cf-ray" in h and "cloudflare" in server) or "x-vercel-mitigated" in h or "akamai" in server


async def _run_stage(client: httpx.AsyncClient, cfg: LoadConfig, users: int, limiter: RateLimiter,
                     deadline: float) -> StageResult:
    lat: list[float] = []
    status: dict[str, int] = {}
    errs: dict[str, int] = {}
    cdn = 0
    stage_end = min(time.monotonic() + cfg.stage_seconds, deadline)
    paths = cfg.paths

    async def one_get(c: httpx.AsyncClient, path: str, headers: dict | None = None):
        nonlocal cdn
        await limiter.take()
        if time.monotonic() >= stage_end:
            return
        t0 = time.monotonic()
        try:
            r = await c.get(urljoin(cfg.url, path), headers=headers)
            dt = time.monotonic() - t0
            lat.append(dt)
            key = str(r.status_code)
            status[key] = status.get(key, 0) + 1
            if r.status_code >= 400:
                if _is_cdn_block(r):
                    cdn += 1
                msg = f"{r.status_code} {path}: {r.text[:80].strip()}"
                errs[msg] = errs.get(msg, 0) + 1
        except httpx.HTTPError as e:
            lat.append(time.monotonic() - t0)
            key = type(e).__name__
            status[key] = status.get(key, 0) + 1
            errs[f"{key} {path}"] = errs.get(f"{key} {path}", 0) + 1

    async def vu(n: int):
        rnd = random.Random(n)
        await asyncio.sleep(rnd.uniform(0, min(1.0, cfg.stage_seconds / 4)))  # spread the start
        mobile = cfg.session_mix and rnd.random() < 0.4
        ua = (UA_MOBILE if mobile else UA).format(run=cfg.run_id)
        # Per-VU client headers: base client already has UA; override when session_mix.
        extra = {"User-Agent": ua} if cfg.session_mix else None

        if not cfg.session_mix:
            while time.monotonic() < stage_end:
                path = paths[rnd.randrange(len(paths))]
                await one_get(client, path)
                if cfg.think_max > 0:
                    await asyncio.sleep(rnd.uniform(cfg.think_min, cfg.think_max))
            return

        # Weighted short sessions (browse / land / bounce / signup intent).
        roll = rnd.random()
        ref = {"Referer": REFERRERS[rnd.randrange(len(REFERRERS))]}
        headers = {**(extra or {}), **ref}
        if roll < 0.40:  # browse
            chain = [paths[0]]
            for p in paths[1:3]:
                chain.append(p)
            for p in chain:
                if time.monotonic() >= stage_end:
                    break
                await one_get(client, p, headers)
                await asyncio.sleep(min(30.0, rnd.expovariate(1 / 3)))
        elif roll < 0.65:  # land_home
            await one_get(client, "/", headers)
        elif roll < 0.85:  # bounce
            await one_get(client, paths[0], headers)
        else:  # signup_intent — GET only
            await one_get(client, "/", headers)
            signup = next((p for p in paths if "signup" in p or "register" in p), "/signup")
            if time.monotonic() < stage_end:
                await one_get(client, signup, headers)

    t_start = time.monotonic()
    await asyncio.gather(*(vu(i) for i in range(users)))
    elapsed = max(0.001, time.monotonic() - t_start)
    total = sum(status.values())
    bad = sum(v for k, v in status.items() if not k.isdigit() or int(k) >= 400)
    s = sorted(lat)
    return StageResult(
        users=users, requests=total, errors=bad, error_rate=round(bad / total, 4) if total else 1.0,
        rps=round(total / elapsed, 1), p50_ms=int(_pct(s, .5) * 1000), p95_ms=int(_pct(s, .95) * 1000),
        p99_ms=int(_pct(s, .99) * 1000), status_counts=status,
        top_errors=dict(sorted(errs.items(), key=lambda kv: -kv[1])[:5]), cdn_blocked=cdn)


async def _run_burst(client: httpx.AsyncClient, cfg: LoadConfig, users: int, path: str,
                     limiter: RateLimiter) -> StageResult:
    """All VUs fire one GET at the same path with no start stagger (thundering herd)."""
    lat: list[float] = []
    status: dict[str, int] = {}
    errs: dict[str, int] = {}
    cdn = 0

    async def vu(_n: int):
        nonlocal cdn
        await limiter.take()
        t0 = time.monotonic()
        try:
            r = await client.get(urljoin(cfg.url, path))
            lat.append(time.monotonic() - t0)
            key = str(r.status_code)
            status[key] = status.get(key, 0) + 1
            if r.status_code >= 400:
                if _is_cdn_block(r):
                    cdn += 1
                errs[f"{r.status_code} {path}"] = errs.get(f"{r.status_code} {path}", 0) + 1
        except httpx.HTTPError as e:
            lat.append(time.monotonic() - t0)
            key = type(e).__name__
            status[key] = status.get(key, 0) + 1
            errs[f"{key} {path}"] = errs.get(f"{key} {path}", 0) + 1

    t_start = time.monotonic()
    await asyncio.gather(*(vu(i) for i in range(users)))
    elapsed = max(0.001, time.monotonic() - t_start)
    total = sum(status.values())
    bad = sum(v for k, v in status.items() if not k.isdigit() or int(k) >= 400)
    s = sorted(lat)
    return StageResult(
        users=users, requests=total, errors=bad, error_rate=round(bad / total, 4) if total else 1.0,
        rps=round(total / elapsed, 1), p50_ms=int(_pct(s, .5) * 1000), p95_ms=int(_pct(s, .95) * 1000),
        p99_ms=int(_pct(s, .99) * 1000), status_counts=status,
        top_errors=dict(sorted(errs.items(), key=lambda kv: -kv[1])[:5]), cdn_blocked=cdn)


def classify_outcome(status_code: int, body: str) -> str:
    """Body-aware success/fail/ambiguous for race and signup-storm heuristics."""
    text = (body or "")[:2000]
    if status_code in (409, 422, 403):
        return "fail"
    if status_code >= 500 or status_code == 429:
        return "error"
    if 200 <= status_code < 300:
        if FAIL_BODY.search(text) and not SUCCESS_BODY.search(text):
            return "fail"
        if SUCCESS_BODY.search(text) or status_code in (201, 204):
            return "success"
        # Bare 200 with no signals — inconclusive, not a success claim.
        return "ambiguous"
    return "fail"


async def run_race(
    url: str,
    path: str,
    *,
    method: str = "POST",
    json_body: dict | None = None,
    form_body: dict | None = None,
    concurrency: int = 20,
    run_id: str = "local",
    timeout_s: float = 10.0,
) -> dict:
    """Fire K identical mutating requests; flag when >1 look success-shaped."""
    k = max(2, min(int(concurrency), 50))
    path = path if path.startswith("/") else "/" + path
    headers = {"User-Agent": UA.format(run=run_id), "X-Launchproof-Run": run_id,
               "Accept": "application/json, text/plain, */*"}
    results: list[dict] = []

    async with httpx.AsyncClient(headers=headers, timeout=timeout_s, follow_redirects=True) as client:
        async def one(i: int):
            t0 = time.monotonic()
            try:
                kw: dict = {}
                if json_body is not None:
                    kw["json"] = json_body
                elif form_body is not None:
                    kw["data"] = form_body
                r = await client.request(method.upper(), urljoin(url, path), **kw)
                body = r.text
                shape = classify_outcome(r.status_code, body)
                results.append({
                    "i": i, "status": r.status_code, "shape": shape,
                    "ms": int((time.monotonic() - t0) * 1000), "body": body[:200],
                })
            except httpx.HTTPError as e:
                results.append({
                    "i": i, "status": 0, "shape": "error",
                    "ms": int((time.monotonic() - t0) * 1000), "body": type(e).__name__,
                })

        await asyncio.gather(*(one(i) for i in range(k)))

    successes = [r for r in results if r["shape"] == "success"]
    fails = [r for r in results if r["shape"] == "fail"]
    ambiguous = [r for r in results if r["shape"] == "ambiguous"]
    errors = [r for r in results if r["shape"] == "error"]
    issues: list[dict] = []
    if len(successes) > 1:
        issues.append({
            "severity": "critical",
            "kind": "race_condition_exploit",
            "where": urljoin(url, path),
            "detail": (f"{len(successes)}/{k} concurrent {method} requests looked successful "
                       f"(statuses {[s['status'] for s in successes[:5]]}). "
                       f"Only one shared-resource claim should succeed."),
            "shot": None,
        })
    elif len(successes) <= 1 and ambiguous and not successes:
        issues.append({
            "severity": "low",
            "kind": "race_inconclusive",
            "where": urljoin(url, path),
            "detail": (f"Race probe got {len(ambiguous)} ambiguous 2xx bodies without clear "
                       f"created/claimed signals; not scoring as an exploit."),
            "shot": None,
        })

    return {
        "path": path, "method": method.upper(), "concurrency": k,
        "successes": len(successes), "fails": len(fails),
        "ambiguous": len(ambiguous), "errors": len(errors),
        "results": results, "issues": issues,
    }


def _judge(stage: StageResult, baseline_p95_ms: int | None, cfg: LoadConfig) -> str:
    if stage.requests == 0:
        return "no responses at all"
    if stage.cdn_blocked and stage.cdn_blocked >= 0.5 * stage.errors and stage.error_rate > cfg.error_rate_limit:
        return f"blocked by CDN/WAF ({stage.cdn_blocked} blocked responses)"
    if stage.error_rate > cfg.error_rate_limit:
        return f"error rate {stage.error_rate:.0%} > {cfg.error_rate_limit:.0%}"
    if stage.p95_ms > cfg.p95_ceiling_s * 1000:
        return f"p95 {stage.p95_ms} ms > {int(cfg.p95_ceiling_s * 1000)} ms"
    if baseline_p95_ms is not None:
        floor = max(baseline_p95_ms, 100)  # ignore noise on very fast baselines
        if stage.p95_ms > cfg.p95_multiplier * floor:
            return f"p95 {stage.p95_ms} ms > {cfg.p95_multiplier:g}x baseline ({baseline_p95_ms} ms)"
    return ""


async def run_load(cfg: LoadConfig, on_stage=None) -> dict:
    cfg.clamp()
    started = time.time()
    deadline = time.monotonic() + cfg.max_duration_s
    limiter = RateLimiter(cfg.max_rps)
    headers = {"User-Agent": UA.format(run=cfg.run_id), "X-Launchproof-Run": cfg.run_id,
               "Cache-Control": "no-cache"}
    max_conn = min(max(cfg.stages + ([cfg.burst_users] if cfg.burst_users else [])), 500)
    limits = httpx.Limits(max_connections=max_conn, max_keepalive_connections=100)
    stages: list[StageResult] = []
    issues: list[dict] = []
    burst_info = None
    stop_reason, break_point = "completed all stages", None
    async with httpx.AsyncClient(headers=headers, timeout=cfg.timeout_s, limits=limits,
                                 follow_redirects=True, http2=False) as client:
        baseline = None
        for users in cfg.stages:
            if time.monotonic() >= deadline:
                stop_reason = "hit max duration"
                break
            st = await _run_stage(client, cfg, users, limiter, deadline)
            reason = _judge(st, baseline, cfg)
            if baseline is None and not reason:
                baseline = st.p95_ms
            if reason:
                st.broke, st.reason = True, reason
            stages.append(st)
            if on_stage:
                on_stage(st)
            if reason:
                stop_reason, break_point = f"break point at {users} users: {reason}", users
                break

        if cfg.burst_users and time.monotonic() < deadline:
            burst = await _run_burst(client, cfg, cfg.burst_users, cfg.burst_path, limiter)
            # Compare to nearest ramp stage that did not break.
            comparable = None
            for st in stages:
                if st.broke:
                    break
                if comparable is None or abs(st.users - cfg.burst_users) <= abs(comparable.users - cfg.burst_users):
                    comparable = st
            burst_reason = _judge(burst, baseline, cfg)
            ramp_ok = comparable is not None and not comparable.broke and not _judge(comparable, baseline, cfg)
            herd = bool(burst_reason and ramp_ok)
            if herd:
                burst.broke, burst.reason = True, burst_reason
                issues.append({
                    "severity": "high",
                    "kind": "thundering_herd",
                    "where": urljoin(cfg.url, cfg.burst_path),
                    "detail": (f"Synchronized burst of {cfg.burst_users} users failed ({burst_reason}) "
                               f"while ramped stage at {comparable.users} users held. "
                               f"Burst p95={burst.p95_ms}ms err={burst.error_rate:.1%}; "
                               f"ramp p95={comparable.p95_ms}ms err={comparable.error_rate:.1%}."),
                    "shot": None,
                })
            burst_info = {
                "users": cfg.burst_users, "path": cfg.burst_path,
                "stage": asdict(burst), "thundering_herd": herd,
                "comparable_users": comparable.users if comparable else None,
            }
            if on_stage:
                on_stage(burst)

    survived = max((s.users for s in stages if not s.broke), default=0)
    rate_capped = any(s.rps >= 0.95 * cfg.max_rps for s in stages)
    return {
        "run_id": cfg.run_id, "region": cfg.region, "url": cfg.url, "paths": cfg.paths,
        "started_at": started, "duration_s": round(time.time() - started, 1),
        "survived_users": survived, "break_point_users": break_point, "stop_reason": stop_reason,
        "baseline_p95_ms": stages[0].p95_ms if stages else None, "rate_capped": rate_capped,
        "stages": [asdict(s) for s in stages], "config": asdict(cfg),
        "burst": burst_info, "issues": issues, "profile": cfg.profile,
        "session_mix": cfg.session_mix,
    }


def _post_result(summary: dict):
    url = os.getenv("LP_RESULT_URL")
    if not url:
        return
    try:
        httpx.post(url, json=summary, timeout=15,
                   headers={"Authorization": f"Bearer {os.getenv('LP_RESULT_TOKEN', '')}"})
    except Exception as e:  # never crash the machine on a callback failure; stdout still has the result
        print(f"result callback failed: {e}", file=sys.stderr)


def main(argv=None) -> int:
    env_cfg = os.getenv("LP_CONFIG")
    if env_cfg:
        cfg = LoadConfig(**json.loads(env_cfg))
        out = None
    else:
        ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
        ap.add_argument("--url", required=True)
        ap.add_argument("--paths", nargs="+", default=["/"])
        ap.add_argument("--stages", default=",".join(map(str, DEFAULT_STAGES)))
        ap.add_argument("--stage-seconds", type=float, default=30)
        ap.add_argument("--think", default="1,3", help="min,max seconds between a user's requests")
        ap.add_argument("--max-rps", type=float, default=MAX_RPS)
        ap.add_argument("--run-id", default="local")
        ap.add_argument("--region", default="local")
        ap.add_argument("--out")
        a = ap.parse_args(argv)
        tmin, tmax = (float(x) for x in a.think.split(","))
        cfg = LoadConfig(url=a.url, paths=a.paths, stages=[int(x) for x in a.stages.split(",")],
                         stage_seconds=a.stage_seconds, think_min=tmin, think_max=tmax, max_rps=a.max_rps,
                         run_id=a.run_id, region=a.region)
        out = a.out

    def log(st: StageResult):
        print(f"[{cfg.region}] {st.users:>4} users  {st.rps:>6} rps  p95 {st.p95_ms:>5} ms  "
              f"errors {st.error_rate:.1%}{'  BROKE: ' + st.reason if st.broke else ''}", file=sys.stderr, flush=True)

    summary = asyncio.run(run_load(cfg, on_stage=log))
    line = json.dumps(summary)
    print("LP_RESULT " + line, flush=True)  # the orchestrator can also scrape this from machine logs
    if out:
        with open(out, "w") as f:
            json.dump(summary, f, indent=2)
    _post_result(summary)
    # Hand-off without any public callback: write the result to a file and stay alive briefly so the
    # orchestrator can read it through the Fly Machines exec API, then it deletes this machine.
    # Bounded, so an orchestrator crash still ends the machine (and auto_destroy removes it).
    hold = min(float(os.getenv("LP_HOLD_S", "0") or 0), 180)
    if hold > 0:
        with open(RESULT_FILE, "w") as f:
            f.write(line)
        print(f"result ready at {RESULT_FILE}, holding up to {hold:.0f}s for collection", file=sys.stderr, flush=True)
        time.sleep(hold)
    return 0


if __name__ == "__main__":
    sys.exit(main())
