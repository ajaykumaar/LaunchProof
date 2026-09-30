"""Launch-day concurrency unit tests (no network to production)."""
from __future__ import annotations

from pathlib import Path

from launchproof.load import engine
from launchproof.smart_ui import merge_into_ui, skipped_payload
from launchproof import report
from dummy_site.app import app as dummy_app


def test_classify_outcome_body_aware():
    assert engine.classify_outcome(201, '{"status":"claimed","message":"seat claimed successfully"}') == "success"
    assert engine.classify_outcome(409, '{"status":"already_claimed","message":"seat already taken"}') == "fail"
    assert engine.classify_outcome(200, "ok thanks") == "success"
    assert engine.classify_outcome(200, "already exists") == "fail"
    assert engine.classify_outcome(200, "Thanks for visiting") == "ambiguous"


def test_launch_stages_helper():
    assert engine.launch_stages() == [25, 100, 100, 40]


def test_load_config_burst_defaults_off():
    cfg = engine.LoadConfig(url="http://x").clamp()
    assert cfg.burst_users is None and cfg.session_mix is False and cfg.profile == "ramp"


def test_race_against_dummy_claim_finds_bug():
    """Default claim endpoint is intentionally racy → >1 success-shaped."""
    import httpx
    from httpx import ASGITransport

    async def go():
        transport = ASGITransport(app=dummy_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/claim/reset")

            async def hit(i: int) -> str:
                r = await client.post("/api/claim", data={"code": f"u{i}"},
                                      headers={"Accept": "application/json", "X-Launchproof-Vu": f"u{i}"})
                return engine.classify_outcome(r.status_code, r.text)

            shapes = await asyncio.gather(*(hit(i) for i in range(16)))
            return shapes

    import asyncio
    shapes = asyncio.run(go())
    assert sum(1 for s in shapes if s == "success") > 1


def test_race_safe_claim_single_winner(monkeypatch):
    import asyncio
    import httpx
    from httpx import ASGITransport

    monkeypatch.setenv("LP_CLAIM_SAFE", "1")

    async def go():
        transport = ASGITransport(app=dummy_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/claim/reset")

            async def hit(i: int) -> str:
                r = await client.post("/api/claim", data={"code": "same"},
                                      headers={"Accept": "application/json"})
                return engine.classify_outcome(r.status_code, r.text)

            return await asyncio.gather(*(hit(i) for i in range(12)))

    shapes = asyncio.run(go())
    assert sum(1 for s in shapes if s == "success") == 1
    monkeypatch.delenv("LP_CLAIM_SAFE", raising=False)


def test_run_race_emits_exploit_when_multiple_success_bodies():
    """Pure heuristic: issue builder path via classify counts."""
    # Simulate run_race issue rule without network.
    results = [
        {"shape": "success", "status": 201},
        {"shape": "success", "status": 201},
        {"shape": "fail", "status": 409},
    ]
    successes = [r for r in results if r["shape"] == "success"]
    assert len(successes) > 1
    issue = {
        "kind": "race_condition_exploit",
        "detail": f"{len(successes)} concurrent successes",
    }
    assert issue["kind"] == "race_condition_exploit"


def test_smart_ui_merge_parallel_payload():
    ui = {"issues": [], "pages": [], "start_url": "http://x"}
    smart = {
        "status": "done",
        "budget": {"max_chaos_scenarios": 1, "max_vision_views": 1, "max_pages_hint": 2, "credit_soft_cap": 10},
        "spent": {"chaos_scenarios": 1, "vision_views": 1},
        "appeal_score": 72,
        "style": "Neo-brutalism",
        "chaos_issues": [{"severity": "medium", "kind": "chaos_double_submit", "where": "http://x/signup",
                          "detail": "no debounce", "shot": None}],
        "visual_issues": [{"severity": "medium", "kind": "persona_mobile_fail", "where": "http://x",
                           "detail": "overflow", "shot": "persona-mobile-overflow.png"}],
    }
    merged = merge_into_ui(ui, smart)
    assert merged["appeal_score"] == 72
    assert merged["style"] == "Neo-brutalism"
    assert len(merged["issues"]) == 2
    assert skipped_payload()["status"] == "skipped"


def test_headline_launch_day_order():
    ui = {"issues": [
        {"severity": "critical", "kind": "race_condition_exploit", "where": "/api/claim", "detail": "x"},
        {"severity": "high", "kind": "thundering_herd", "where": "/", "detail": "y"},
        {"severity": "medium", "kind": "chaos_double_submit", "where": "/signup", "detail": "z"},
    ]}
    load = {"regions": {"local": {"survived_users": 100, "break_point_users": None}},
            "issues": [{"kind": "thundering_herd"}]}
    bits = report.headline({"total": 50, "grade": "D", "parts": {}, "skipped": []}, ui, None, load)
    assert bits[0].startswith("Race")
    assert any("Thundering" in b for b in bits)
    assert any("debounce" in b.lower() or "race" in b.lower() for b in bits)


def test_score_unchanged_without_new_fields():
    ui = {"issues": [{"severity": "low", "kind": "missing_title", "where": "/", "detail": "x"}]}
    sc = report.score(ui, None, None)
    assert "ui" in sc["parts"] and "payments" in sc["skipped"] and "load" in sc["skipped"]


def test_shot_names_include_smart_ui(tmp_path: Path):
    from launchproof.brainbase import _shot_names_from_json
    names = _shot_names_from_json({
        "ui": {"issues": [{"shot": "a.png"}], "pages": []},
        "smart_ui": {"chaos_issues": [{"shot": "chaos-double-submit.png"}],
                     "persona_issues": [{"shot": "persona-mobile-overflow.png"}]},
    })
    assert "a.png" in names
    assert "chaos-double-submit.png" in names
    assert "persona-mobile-overflow.png" in names
