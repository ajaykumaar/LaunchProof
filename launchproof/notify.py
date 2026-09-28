"""Hand results to the team: a Slack summary and one Linear issue per critical/high finding.

  SLACK_WEBHOOK_URL   Slack incoming webhook (api.slack.com/messaging/webhooks)
  LINEAR_API_KEY      Linear personal API key (Settings > API)
  LINEAR_TEAM_ID      team UUID; find it with `python -m launchproof.notify teams`

Both are optional; missing config is skipped silently. Nothing here ever blocks the run.
"""
from __future__ import annotations

import os
import sys

import httpx

LINEAR_URL = "https://api.linear.app/graphql"


def slack(report: dict, url: str, report_link: str | None = None) -> bool:
    hook = os.getenv("SLACK_WEBHOOK_URL")
    if not hook:
        return False
    sc = report["score"]
    top = report["issues"][:5]
    lines = [f"*Launchproof: {url} scored {sc['total']}/100 ({sc['grade']})*", " · ".join(report["headline"])]
    lines += [f"• [{i['severity']}] {i['kind'].replace('_', ' ')}: {i['detail'][:140]}" for i in top]
    if report_link:
        lines.append(f"<{report_link}|Full report>")
    try:
        return httpx.post(hook, json={"text": "\n".join(lines)}, timeout=10).status_code == 200
    except httpx.HTTPError:
        return False


def _gql(query: str, variables: dict) -> dict:
    r = httpx.post(LINEAR_URL, json={"query": query, "variables": variables}, timeout=15,
                   headers={"Authorization": os.environ["LINEAR_API_KEY"], "Content-Type": "application/json"})
    r.raise_for_status()
    return r.json()


def linear(report: dict, url: str, min_severity: str = "high") -> list[str]:
    if not (os.getenv("LINEAR_API_KEY") and os.getenv("LINEAR_TEAM_ID")):
        return []
    rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    prio = {"critical": 1, "high": 2, "medium": 3, "low": 4}  # Linear: 1 urgent .. 4 low
    made = []
    q = """mutation($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { identifier url } } }"""
    for i in report["issues"]:
        if rank[i["severity"]] > rank[min_severity]:
            continue
        desc = (f"**Where:** {i['where']}\n\n**Evidence:** {i['detail']}\n\n**Fix prompt:**\n\n```\n{i['fix_prompt']}\n```\n\n"
                f"Found by Launchproof on {url}.")
        try:
            out = _gql(q, {"input": {"teamId": os.environ["LINEAR_TEAM_ID"], "priority": prio[i["severity"]],
                                     "title": f"[Launchproof] {i['kind'].replace('_', ' ')}: {i['where'][:80]}",
                                     "description": desc}})
            issue = out.get("data", {}).get("issueCreate", {}).get("issue")
            if issue:
                made.append(issue["identifier"])
        except Exception as e:
            print(f"linear: {e}", file=sys.stderr)
    return made


def _normalize(payload: dict) -> dict:
    """Accept either report.json or the triage hand-off payload."""
    rep = dict(payload)
    if isinstance(rep.get("score"), (int, float)):
        rep["score"] = {"total": int(rep["score"]), "grade": rep.get("grade", "")}
    rep.setdefault("headline", [])
    rep["issues"] = [{"where": "", "detail": "", "fix_prompt": "", **i} for i in rep.get("issues", [])]
    return rep


if __name__ == "__main__":
    if sys.argv[1:] == ["teams"]:
        print(_gql("{ teams { nodes { id key name } } }", {}))
    elif len(sys.argv) >= 4 and sys.argv[1] == "send":
        import json
        rep = _normalize(json.loads(open(sys.argv[2]).read()))
        print("slack:", slack(rep, sys.argv[3], rep.get("report_link")))
        print("linear:", linear(rep, sys.argv[3]))
    else:
        print("usage: python -m launchproof.notify send <report.json> <url> | teams")
