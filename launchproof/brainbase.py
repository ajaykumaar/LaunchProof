"""Tiny client for the Brainbase API, so anything (our web app, CI, a laptop) can hand a run to the
Launchproof agent on Brainbase and get the report back.

  export BRAINBASE_API_KEY=... BRAINBASE_AGENT_ID=...
  python -m launchproof.brainbase run https://site --token lp_... [--full]

Endpoints (docs.brainbaselabs.com/api): POST /v2/threads, GET /v2/threads/{id},
GET /v2/threads/{id}/messages, GET /v2/tasks/{id}/files/download?path=...
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

from .envfile import load_env_local

load_env_local()

BASE = os.getenv("BRAINBASE_API_BASE", "https://api.brainbaselabs.com")
DONE = {"success", "fail", "need_more_info", "idle"}


def _client() -> httpx.Client:
    key = os.getenv("BRAINBASE_API_KEY")
    if not key:
        raise SystemExit("BRAINBASE_API_KEY is not set")
    return httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {key}"}, timeout=30)


def task_message(url: str, run_id: str, token: str | None, full: bool, regions: str | None = None) -> str:
    """The exact instruction we send; the agent's instructions (brainbase/agents/tester) do the rest.

    Multi-region Fly is opt-in: pass regions only when Fly is configured (FLY_API_TOKEN + FLY_APP),
    or set LP_REGIONS. Otherwise the agent runs load in its sandbox / on this machine.
    """
    if not full:
        return (f"Run a UI-only Launchproof check on {url}. Use run id {run_id}: "
                f"`python3 -m launchproof run {url} --skip-pay --skip-load --run-id {run_id}`. Then reply with the summary.")
    if not token:
        # Never embed the string "None" as --token; fall back to UI-only so ownership stays enforced.
        return task_message(url, run_id, None, full=False)
    from .load.fly import fly_configured
    region_arg = (regions if regions is not None else os.getenv("LP_REGIONS", "")).strip()
    use_fly = bool(region_arg) and fly_configured()
    fly_flags = f" --regions {region_arg}" if use_fly else ""
    return (f"Run a full Launchproof test on {url} with ownership token {token}. Use run id {run_id}. "
            f"First record the journey to checkout with your launchproof browser tools and save it as journey.json, "
            f"then call browser_close, then run `python3 -m launchproof run {url} --token {token} "
            f"--journey journey.json{fly_flags} --i-understand-costs --run-id {run_id}`"
            f"{'' if use_fly else ' (omit --regions: load runs in this sandbox until Fly credentials are set)'}. "
            f"Rewrite the fix prompts, rebuild the report, hand off to triage, and reply with the summary.")

def start(message: str, agent_id: str | None = None, title: str = "Launchproof run") -> str:
    with _client() as c:
        r = c.post("/v2/threads", json={"agent_id": agent_id or os.environ["BRAINBASE_AGENT_ID"],
                                        "input": message, "title": title})
        r.raise_for_status()
        return r.json()["thread_id"]


def status(thread_id: str) -> str:
    with _client() as c:
        r = c.get(f"/v2/threads/{thread_id}")
        r.raise_for_status()
        return r.json().get("status", "unknown")


def messages(thread_id: str) -> list[str]:
    with _client() as c:
        r = c.get(f"/v2/threads/{thread_id}/messages")
        if r.status_code != 200:
            return []
        return [m.get("content", "") if isinstance(m.get("content"), str) else json.dumps(m.get("content"))
                for m in r.json().get("items", []) if m.get("role") == "assistant"]


def download(thread_id: str, path: str, dest: Path, tries: int = 5) -> bool:
    """Files live under /v2/tasks/{id}/files (a thread id works as the task id)."""
    with _client() as c:
        for _ in range(tries):
            r = c.get(f"/v2/tasks/{thread_id}/files/download", params={"path": path})
            if r.status_code == 200:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(r.content)
                return True
            if r.status_code == 409 and "terminal" not in r.text:
                time.sleep(3)  # machine_not_ready / task_run_busy: retry
                continue
            return False
    return False


def wait(thread_id: str, timeout_s: int = 1200, on_message=None) -> str:
    seen, t0 = 0, time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        st = status(thread_id)
        msgs = messages(thread_id)
        for m in msgs[seen:]:
            if on_message:
                on_message(m)
        seen = len(msgs)
        if st in DONE:
            return st
        time.sleep(5)
    return "timeout"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("url")
    r.add_argument("--token")
    r.add_argument("--full", action="store_true")
    r.add_argument("--run-id", default=f"bb{int(time.time())}")
    r.add_argument("--out", default="runs")
    a = ap.parse_args(argv)
    tid = start(task_message(a.url, a.run_id, a.token, a.full))
    print(f"thread {tid} started")
    st = wait(tid, on_message=lambda m: print(m[:2000]))
    print(f"status: {st}")
    for name in ("report.html", "share-card.png", "report.json"):
        ok = download(tid, f"launchproof/runs/{a.run_id}/{name}", Path(a.out) / a.run_id / name)
        print(f"{'downloaded' if ok else 'missing'}: {name}")
    return 0 if st == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
