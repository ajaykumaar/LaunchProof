"""Concurrent signup storm against a discovered form action (zero-config race/load probe).

Uses body-aware success heuristics from load.engine.classify_outcome — never treat bare 200
as a duplicate collision. Ownership gating is the caller's responsibility.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

import httpx

from .load.engine import UA, classify_outcome
from .journey import Identity

DEFAULT_N = 10
MAX_N = 30


@dataclass
class StormReport:
    action_url: str
    n: int = 0
    unique_ok: int = 0
    unique_errors: int = 0
    duplicate_successes: int = 0
    rate_limited: bool = False
    issues: list[dict] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


async def run_signup_storm(
    action_url: str,
    *,
    n: int = DEFAULT_N,
    run_id: str = "storm",
    timeout_s: float = 15.0,
) -> StormReport:
    n = max(0, min(int(n), MAX_N))
    res = StormReport(action_url=action_url, n=n)
    if n <= 0 or not action_url:
        res.log.append("skipped")
        return res

    host = urlparse(action_url).netloc
    headers = {
        "User-Agent": UA.format(run=run_id),
        "X-Launchproof-Run": run_id,
        "Accept": "application/json, text/html;q=0.9",
    }

    async with httpx.AsyncClient(headers=headers, timeout=timeout_s, follow_redirects=True) as client:
        async def unique_one(i: int) -> dict:
            ident = Identity.new(f"{run_id}-{i}", "storm")
            try:
                r = await client.post(action_url, data={
                    "email": ident.email, "password": ident.password, "name": ident.name,
                })
                shape = classify_outcome(r.status_code, r.text)
                return {"status": r.status_code, "shape": shape, "body": r.text[:200]}
            except httpx.HTTPError as e:
                return {"status": 0, "shape": "error", "body": type(e).__name__}

        unique_results = await asyncio.gather(*(unique_one(i) for i in range(n)))
        res.unique_ok = sum(1 for r in unique_results if r["shape"] == "success")
        res.unique_errors = sum(1 for r in unique_results if r["shape"] == "error" or (
            isinstance(r["status"], int) and r["status"] >= 500))
        res.rate_limited = any(r["status"] == 429 for r in unique_results)
        if any("rate" in (r.get("body") or "").lower() and r["status"] in (429, 503) for r in unique_results):
            res.rate_limited = True

        # Same-email collision probe (exactly two concurrent POSTs).
        dup_ident = Identity.new(f"{run_id}-dup", "storm")
        form = {"email": dup_ident.email, "password": dup_ident.password, "name": dup_ident.name}

        async def dup_one():
            try:
                r = await client.post(action_url, data=form)
                return {"status": r.status_code, "shape": classify_outcome(r.status_code, r.text),
                        "body": r.text[:200]}
            except httpx.HTTPError as e:
                return {"status": 0, "shape": "error", "body": type(e).__name__}

        d0, d1 = await asyncio.gather(dup_one(), dup_one())
        dup_successes = [d for d in (d0, d1) if d["shape"] == "success"]
        res.duplicate_successes = len(dup_successes)

    if res.unique_errors >= max(2, n // 3):
        res.issues.append({
            "severity": "high", "kind": "signup_storm_errors", "where": action_url,
            "detail": f"{res.unique_errors}/{n} concurrent unique signups errored (5xx/network).",
            "shot": None,
        })
    if res.rate_limited:
        res.issues.append({
            "severity": "medium", "kind": "signup_rate_limit", "where": action_url,
            "detail": "Signup storm observed 429/rate-limit responses under concurrent creates.",
            "shot": None,
        })
    if res.duplicate_successes > 1:
        res.issues.append({
            "severity": "critical", "kind": "signup_duplicate_collision", "where": action_url,
            "detail": (f"Two concurrent signups with the same email both looked successful "
                       f"(body-aware). Host={host}."),
            "shot": None,
        })

    res.log.append(
        f"unique ok={res.unique_ok}/{n} errors={res.unique_errors} "
        f"dup_successes={res.duplicate_successes} rate_limited={res.rate_limited}"
    )
    return res
