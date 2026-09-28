"""Smart UI budget + merge helpers for Chaos (messy human) and Critic (visual appeal) agents.

Heuristic UI stays in ui_check.py. Chaos/Critic run on Brainbase only (no Anthropic vision).
Budget caps limit screenshots / scenarios / soft credits per run; the webapp can override later.
"""
from __future__ import annotations

import json
import os
from typing import Any


DEFAULT_BUDGET = {
    "max_chaos_scenarios": 3,
    "max_vision_views": 4,
    "max_pages_hint": 5,
    "credit_soft_cap": 40,
}


def budget_from_env(overrides: dict[str, Any] | None = None) -> dict[str, int]:
    """Resolve per-run smart UI budget from env and optional request overrides."""
    b = dict(DEFAULT_BUDGET)
    raw = (os.getenv("LP_SMART_UI_BUDGET") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                for k in DEFAULT_BUDGET:
                    if k in parsed:
                        b[k] = int(parsed[k])
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    discrete = {
        "max_chaos_scenarios": "LP_MAX_CHAOS",
        "max_vision_views": "LP_MAX_VISION_VIEWS",
        "max_pages_hint": "LP_MAX_PAGES_HINT",
        "credit_soft_cap": "LP_SMART_UI_CREDITS",
    }
    for key, env_name in discrete.items():
        v = (os.getenv(env_name) or "").strip()
        if v.isdigit():
            b[key] = int(v)
    if overrides:
        for k in DEFAULT_BUDGET:
            if k in overrides and overrides[k] is not None:
                try:
                    b[k] = int(overrides[k])
                except (TypeError, ValueError):
                    pass
    # Hard floors / ceilings so a bad form cannot burn the org or disable everything silently.
    b["max_chaos_scenarios"] = max(0, min(b["max_chaos_scenarios"], 8))
    b["max_vision_views"] = max(0, min(b["max_vision_views"], 12))
    b["max_pages_hint"] = max(1, min(b["max_pages_hint"], 20))
    b["credit_soft_cap"] = max(0, min(b["credit_soft_cap"], 200))
    return b


def smart_ui_enabled(budget: dict[str, int] | None = None) -> bool:
    """Smart UI is on when either chaos scenarios or vision views are allowed."""
    b = budget or budget_from_env()
    return b["max_chaos_scenarios"] > 0 or b["max_vision_views"] > 0


def format_budget_line(budget: dict[str, int]) -> str:
    return (
        f"smart_ui budget: max_chaos_scenarios={budget['max_chaos_scenarios']}, "
        f"max_vision_views={budget['max_vision_views']}, "
        f"max_pages_hint={budget['max_pages_hint']}, "
        f"credit_soft_cap={budget['credit_soft_cap']}"
    )


def skipped_payload(reason: str = "Brainbase Chaos/Critic not available on local CLI") -> dict:
    return {
        "status": "skipped",
        "reason": reason,
        "budget": budget_from_env(),
        "spent": {"chaos_scenarios": 0, "vision_views": 0},
        "appeal_score": None,
        "chaos_issues": [],
        "visual_issues": [],
        "interesting_states": [],
    }


def merge_into_ui(ui: dict | None, smart: dict | None) -> dict | None:
    """Append chaos/visual issues onto the UI report and attach appeal_score / smart_ui meta."""
    if ui is None:
        return None
    if not smart or smart.get("status") == "skipped":
        ui = dict(ui)
        ui["smart_ui"] = smart or skipped_payload()
        return ui
    ui = dict(ui)
    issues = list(ui.get("issues") or [])
    for i in (smart.get("chaos_issues") or []) + (smart.get("visual_issues") or []):
        if isinstance(i, dict) and i.get("kind") and i.get("severity"):
            issues.append(i)
    ui["issues"] = issues
    if smart.get("appeal_score") is not None:
        try:
            ui["appeal_score"] = int(smart["appeal_score"])
        except (TypeError, ValueError):
            ui["appeal_score"] = None
    for key in ("style", "style_secondary", "style_coherence", "trend_alignment", "uniqueness", "notes"):
        if smart.get(key) is not None:
            ui[key] = smart[key]
    ui["smart_ui"] = {
        "status": smart.get("status", "done"),
        "budget": smart.get("budget"),
        "spent": smart.get("spent"),
        "appeal_score": ui.get("appeal_score"),
        "style": ui.get("style"),
        "style_secondary": ui.get("style_secondary"),
        "style_coherence": ui.get("style_coherence"),
        "trend_alignment": ui.get("trend_alignment"),
        "uniqueness": ui.get("uniqueness"),
        "scenarios_run": smart.get("scenarios_run"),
        "views_used": smart.get("views_used"),
        "notes": smart.get("notes"),
    }
    return ui


def parse_form_budget(
    max_chaos_scenarios: str | None = None,
    max_vision_views: str | None = None,
    max_pages_hint: str | None = None,
    credit_soft_cap: str | None = None,
) -> dict[str, int]:
    """Webapp form fields (strings) → budget overrides; empty means use env/default."""
    raw: dict[str, Any] = {}
    mapping = {
        "max_chaos_scenarios": max_chaos_scenarios,
        "max_vision_views": max_vision_views,
        "max_pages_hint": max_pages_hint,
        "credit_soft_cap": credit_soft_cap,
    }
    for k, v in mapping.items():
        if v is not None and str(v).strip() != "":
            raw[k] = v
    return budget_from_env(raw or None)
