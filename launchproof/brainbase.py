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


def task_message(url: str, run_id: str, token: str | None, full: bool, regions: str | None = None,
                 budget: dict | None = None) -> str:
    """The exact instruction we send; the agent's instructions (brainbase/agents/tester) do the rest.

    Multi-region Fly is opt-in: pass regions only when Fly is configured (FLY_API_TOKEN + FLY_APP),
    or set LP_REGIONS. Otherwise the agent runs load in its sandbox / on this machine.
    Smart UI budget is always embedded so Chaos/Critic stop at hard caps (no Anthropic vision).
    """
    from .smart_ui import budget_from_env, format_budget_line, smart_ui_enabled

    b = budget_from_env(budget)
    budget_line = format_budget_line(b)
    smart_line = (
        f" After the heuristic UI check, run Smart UI within budget ({budget_line}): "
        f"hand off to Chaos then Critic (prefer Chaos interesting_states for vision). "
        f"Merge chaos_issues + visual_issues into the report, record smart_ui.budget/spent and "
        f"appeal_score in report.json. Stop when caps hit. Do not use Anthropic for vision."
        if smart_ui_enabled(b)
        else f" Smart UI is disabled ({budget_line}); skip Chaos/Critic."
    )
    if not full:
        return (f"Run a UI-only Launchproof check on {url}. Use run id {run_id}: "
                f"`python3 -m launchproof run {url} --skip-pay --skip-load --run-id {run_id}`. "
                f"{smart_line} Then reply with the summary.")
    if not token:
        # Never embed the string "None" as --token; fall back to UI-only so ownership stays enforced.
        return task_message(url, run_id, None, full=False, regions=regions, budget=b)
    from .load.fly import fly_configured
    region_arg = (regions if regions is not None else os.getenv("LP_REGIONS", "")).strip()
    use_fly = bool(region_arg) and fly_configured()
    fly_flags = f" --regions {region_arg}" if use_fly else ""
    return (f"Run a full Launchproof test on {url} with ownership token {token}. Use run id {run_id}. "
            f"First record the journey to checkout with your launchproof browser tools and save it as journey.json, "
            f"then call browser_close, then run `python3 -m launchproof run {url} --token {token} "
            f"--journey journey.json{fly_flags} --i-understand-costs --run-id {run_id}`"
            f"{'' if use_fly else ' (omit --regions: load runs in this sandbox until Fly credentials are set)'}. "
            f"{smart_line} Rewrite the fix prompts, rebuild the report, hand off to triage, and reply with the summary.")

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


def list_files(thread_id: str, path: str = "") -> list[dict]:
    """List sandbox files. path='' is workspace root; use launchproof/runs/<id> for a run folder."""
    with _client() as c:
        params = {"path": path} if path else None
        r = c.get(f"/v2/tasks/{thread_id}/files", params=params)
        if r.status_code != 200:
            return []
        return list(r.json().get("items") or [])


def _shot_names_from_json(data: dict) -> set[str]:
    names: set[str] = set()
    for i in data.get("issues") or []:
        if isinstance(i, dict) and i.get("shot"):
            names.add(str(i["shot"]))
    ui = data.get("ui") or {}
    for p in ui.get("pages") or []:
        if isinstance(p, dict) and p.get("screenshot"):
            names.add(str(p["screenshot"]))
    for i in ui.get("issues") or []:
        if isinstance(i, dict) and i.get("shot"):
            names.add(str(i["shot"]))
    for s in (data.get("signup") or {}).get("screenshots") or []:
        names.add(str(s))
    return names


def download_run(thread_id: str, run_id: str, dest_dir: Path, prefixes: tuple[str, ...] = (
        "launchproof/runs", "runs")) -> dict:
    """Download a full run folder (report + every screenshot) from a Brainbase task.

    Prefers directory listing via the files API; falls back to known names + JSON shot refs.
    Returns {"ok": [...], "missing": [...], "prefix": str|None}.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    remote_prefix = None
    names: list[str] = []

    for prefix in prefixes:
        items = list_files(thread_id, f"{prefix}/{run_id}")
        files = [i for i in items if not i.get("is_dir") and i.get("name")]
        if files:
            remote_prefix = prefix
            names = [str(i["name"]) for i in files]
            break

    # Always ensure core artifacts are attempted even if listing failed.
    core = ["report.html", "report.json", "run.json", "share-card.png", "smart_ui.json", "fix_prompts.json"]
    for n in core:
        if n not in names:
            names.append(n)

    # If listing failed, try prefixes for report.json first then expand shot refs.
    if remote_prefix is None:
        for prefix in prefixes:
            if download(thread_id, f"{prefix}/{run_id}/report.json", dest_dir / "report.json"):
                remote_prefix = prefix
                break
        if (dest_dir / "report.json").is_file():
            try:
                names = list(dict.fromkeys(names + sorted(_shot_names_from_json(
                    json.loads((dest_dir / "report.json").read_text(encoding="utf-8"))))))
            except Exception:
                pass
        if download(thread_id, f"{remote_prefix or prefixes[0]}/{run_id}/run.json", dest_dir / "run.json"):
            try:
                names = list(dict.fromkeys(names + sorted(_shot_names_from_json(
                    json.loads((dest_dir / "run.json").read_text(encoding="utf-8"))))))
            except Exception:
                pass
        # Heuristic UI shot names if JSON missing
        for i in range(20):
            for vp in ("phone", "desktop"):
                names.append(f"{i:02d}-{vp}.png")
        names += ["signup-form.png", "signup-after.png", "signup-nofields.png"]
        names = list(dict.fromkeys(names))

    prefix = remote_prefix or prefixes[0]
    ok, missing = [], []
    for name in names:
        if "/" in name or name in (".", ".."):
            continue
        dest = dest_dir / name
        if dest.is_file() and dest.stat().st_size > 0 and name.endswith((".png", ".json", ".html")):
            # Still refresh html/json; keep existing pngs if present to save time
            if name.endswith(".png"):
                ok.append(name)
                continue
        remote = f"{prefix}/{run_id}/{name}"
        if download(thread_id, remote, dest):
            ok.append(name)
        else:
            # Don't count speculative numbered shots as missing if listing worked
            if remote_prefix is None and name[:2].isdigit() and name.endswith(".png"):
                continue
            if name in core or name.endswith(".png"):
                missing.append(name)
    return {"ok": ok, "missing": missing, "prefix": remote_prefix}


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
    r.add_argument("--max-chaos", type=int, default=None, help="Smart UI: max Chaos scenarios")
    r.add_argument("--max-vision", type=int, default=None, help="Smart UI: max Critic vision views")
    r.add_argument("--smart-ui-credits", type=int, default=None, help="Smart UI soft credit cap")
    f = sub.add_parser("fetch", help="download a full run folder (report + screenshots) from a thread/task id")
    f.add_argument("thread_id")
    f.add_argument("run_id")
    f.add_argument("--out", default="runs")
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        result = download_run(a.thread_id, a.run_id, Path(a.out) / a.run_id)
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] and (Path(a.out) / a.run_id / "report.html").is_file() else 1
    ov = {}
    if a.max_chaos is not None:
        ov["max_chaos_scenarios"] = a.max_chaos
    if a.max_vision is not None:
        ov["max_vision_views"] = a.max_vision
    if a.smart_ui_credits is not None:
        ov["credit_soft_cap"] = a.smart_ui_credits
    from .smart_ui import budget_from_env
    tid = start(task_message(a.url, a.run_id, a.token, a.full, budget=budget_from_env(ov or None)))
    print(f"thread {tid} started")
    st = wait(tid, on_message=lambda m: print(m[:2000]))
    print(f"status: {st}")
    result = download_run(tid, a.run_id, Path(a.out) / a.run_id)
    print(f"downloaded {len(result['ok'])} files" + (f"; missing {result['missing']}" if result["missing"] else ""))
    return 0 if st == "success" and (Path(a.out) / a.run_id / "report.html").is_file() else 1


if __name__ == "__main__":
    sys.exit(main())
