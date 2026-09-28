"""Unit tests (no network, no browser)."""
import os
import time
from pathlib import Path

import pytest

from launchproof import ownership, report
from launchproof.load import engine, fly, k6gen
from launchproof.payment import CaseResult, PaymentReport, _fake_success_url, _pick_success_url, summarize


# ---------- ownership ----------

def test_token_roundtrip():
    t = ownership.issue_token("https://shop.example.com/pricing")
    assert ownership.token_valid("https://shop.example.com", t) == (True, "")


def test_token_bound_to_host():
    t = ownership.issue_token("https://a.example.com")
    ok, why = ownership.token_valid("https://b.example.com", t)
    assert not ok and "different site" in why


def test_token_expires():
    t = ownership.issue_token("https://a.example.com", now=time.time() - 25 * 3600)
    ok, why = ownership.token_valid("https://a.example.com", t)
    assert not ok and "expired" in why


def test_tampered_token_rejected():
    t = ownership.issue_token("https://a.example.com")
    ts = int(t.split("_")[1])
    forged = t.replace(str(ts), str(ts + 3600), 1)
    assert not ownership.token_valid("https://a.example.com", forged)[0]


def test_dev_hosts_skip_verification():
    assert ownership.verify("http://localhost:8100", None)["verified"]
    assert not ownership.verify("https://example.com", None, allow_dev=True)["verified"]


# ---------- load engine ----------

def _stage(**kw):
    base = dict(users=50, requests=1000, errors=0, error_rate=0.0, rps=50, p50_ms=40, p95_ms=80, p99_ms=100,
                status_counts={"200": 1000}, top_errors={}, cdn_blocked=0)
    base.update(kw)
    return engine.StageResult(**base)


def test_break_on_error_rate():
    cfg = engine.LoadConfig(url="http://x")
    assert "error rate" in engine._judge(_stage(errors=60, error_rate=0.06), 80, cfg)


def test_break_on_latency_multiple_with_noise_floor():
    cfg = engine.LoadConfig(url="http://x")
    assert engine._judge(_stage(p95_ms=250), 20, cfg) == ""  # 20 ms baseline floored to 100 ms
    assert "baseline" in engine._judge(_stage(p95_ms=301), 20, cfg)


def test_break_on_absolute_ceiling():
    cfg = engine.LoadConfig(url="http://x")
    assert "5000" in engine._judge(_stage(p95_ms=5200), None, cfg)


def test_cdn_block_is_named():
    cfg = engine.LoadConfig(url="http://x")
    r = engine._judge(_stage(errors=500, error_rate=0.5, cdn_blocked=480), 80, cfg)
    assert "CDN" in r


def test_caps_cannot_be_exceeded():
    cfg = engine.LoadConfig(url="http://x", stages=[5000], max_rps=10_000, max_duration_s=9999, paths=["api"]).clamp()
    assert cfg.stages == [engine.MAX_VUS] and cfg.max_rps == engine.MAX_RPS
    assert cfg.max_duration_s == engine.MAX_DURATION_S and cfg.paths == ["/api"]


def test_percentile():
    assert engine._pct(sorted(range(101)), 0.95) == 95
    assert engine._pct([], 0.95) == 0


# ---------- fly ----------

def test_machine_body_is_self_destructing(monkeypatch):
    monkeypatch.setenv("FLY_APP", "lp-test")
    b = fly.machine_body("sjc", "run1", {"url": "http://x", "paths": ["/"]})
    assert b["region"] == "sjc"
    assert b["config"]["auto_destroy"] is True and b["config"]["restart"] == {"policy": "no"}
    assert b["config"]["metadata"] == {"launchproof_run": "run1"}
    assert '"region": "sjc"' in b["config"]["env"]["LP_CONFIG"]


def test_machine_body_sets_result_callback(monkeypatch):
    monkeypatch.setenv("LP_RESULT_BASE", "https://api.example.com/")
    monkeypatch.setenv("LP_RESULT_TOKEN", "t")
    env = fly.machine_body("lhr", "run9", {"url": "http://x"})["config"]["env"]
    assert env["LP_RESULT_URL"] == "https://api.example.com/api/load-results/run9/lhr" and env["LP_RESULT_TOKEN"] == "t"


def test_combine():
    c = fly.combine({"sjc": {"region": "sjc", "survived_users": 200, "break_point_users": 400, "stages": [{"p95_ms": 900}]},
                     "iad": {"region": "iad", "survived_users": 800, "break_point_users": None, "stages": [{"p95_ms": 90}]}})
    assert c["survived_users_total"] == 1000 and c["any_break"] and c["worst_region"] == "sjc"


def test_k6_script_has_abort_thresholds():
    s = k6gen.generate("https://x.com/", ["/"], [10, 2000])
    assert "abortOnFail: true" in s and '"target": 1000' in s and "X-Launchproof-Run" in s


# ---------- payments ----------

def test_fake_success_url_replaces_session_id():
    u = _fake_success_url("https://a.com/success?session_id=cs_test_123&x=1")
    assert "session_id=cs_test_launchproof_fake" in u and "x=1" in u


def test_pick_success_url_prefers_hop_with_session_id():
    navs = ["https://a.com/checkout", "https://a.com/checkout/success?session_id=abc", "https://a.com/dashboard"]
    assert _pick_success_url(navs, "https://a.com/checkout") == "https://a.com/checkout/success?session_id=abc"


def test_payment_summary_flags_bypass_and_decline():
    rpt = PaymentReport(start_url="https://a.com", cases=[
        CaseResult(case="success", reached_checkout=True, outcome="paid", unlocked=True),
        CaseResult(case="decline", reached_checkout=True, outcome="declined_silent", unlocked=False),
        CaseResult(case="bypass", outcome="unlocked", unlocked=True, error_text="opened https://a.com/s?session_id=x without paying"),
    ])
    kinds = [i["kind"] for i in summarize(rpt)]
    assert kinds[0] == "paywall_bypass" and "decline_silent" in kinds


def test_payment_summary_unreachable():
    assert summarize(PaymentReport(start_url="https://a.com"))[0]["kind"] == "checkout_unreachable"


# ---------- report ----------

def test_dedupe_merges_viewports():
    issues = [{"severity": "medium", "kind": "broken_images", "where": "https://a.com/about (phone)", "detail": "x"},
              {"severity": "medium", "kind": "broken_images", "where": "https://a.com/about (desktop)", "detail": "x"}]
    d = report.dedupe_ui(issues)
    assert len(d) == 1 and d[0]["viewports"] == ["phone", "desktop"]


def test_score_rescales_when_parts_skipped():
    sc = report.score({"issues": []}, None, None)
    assert sc["total"] == 100 and sc["skipped"] == ["payments", "load"]


def test_score_caps_payments_on_bypass():
    pay = {"cases": [{"case": "success", "reached_checkout": True, "outcome": "paid", "unlocked": True},
                     {"case": "decline", "outcome": "declined_shown", "unlocked": False},
                     {"case": "bypass", "outcome": "unlocked", "unlocked": True}]}
    assert report.score(None, pay, None)["parts"]["payments"]["got"] == 15


def test_load_score_steps():
    load = {"regions": {"sjc": {"survived_users": 200}, "iad": {"survived_users": 800}}}
    assert report.score(None, None, load)["parts"]["load"]["got"] == 20  # weakest region counts


@pytest.mark.parametrize("kind", list(report.TEMPLATES))
def test_every_fix_template_renders(kind):
    p = report.fix_prompt({"kind": kind, "where": "https://a.com/x", "detail": "Something: broke here", "paths": ["/"]})
    assert "{" not in p and len(p) > 30


# ---------- fly orchestration with a fake Machines API ----------

def test_run_regions_collects_via_exec_and_always_deletes(monkeypatch):
    import asyncio
    import json as _json

    import httpx
    calls = []
    result = {"region": "sjc", "survived_users": 100, "break_point_users": 200, "stages": [{"p95_ms": 900}]}

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path))
        if req.method == "POST" and req.url.path.endswith("/machines"):
            region = _json.loads(req.content)["region"]
            return httpx.Response(200, json={"id": f"m-{region}"})
        if req.method == "GET" and "/machines/m-" in req.url.path:
            return httpx.Response(200, json={"state": "started"})
        if req.method == "POST" and req.url.path.endswith("/exec"):
            return httpx.Response(200, json={"exit_code": 0, "stdout": _json.dumps(result)})
        if req.method == "DELETE":
            return httpx.Response(200, json={})
        return httpx.Response(404)

    monkeypatch.setattr(fly, "_client", lambda: httpx.AsyncClient(base_url=fly.API, transport=httpx.MockTransport(handler)))
    monkeypatch.delenv("LP_RESULT_BASE", raising=False)
    out = asyncio.run(fly.run_regions("r1", {"url": "http://x", "paths": ["/"]}, ["sjc", "iad"], poll_s=0.01))
    assert set(out["regions"]) == {"sjc", "iad"} and out["regions"]["sjc"]["survived_users"] == 100
    deletes = [p for m, p in calls if m == "DELETE"]
    assert any(p.endswith("m-sjc") for p in deletes) and any(p.endswith("m-iad") for p in deletes)


def test_notify_normalizes_handoff_payload():
    from launchproof.notify import _normalize
    rep = _normalize({"url": "u", "score": 46, "grade": "D", "headline": ["x"], "issues": [{"severity": "critical", "kind": "k", "detail": "d"}]})
    assert rep["score"]["total"] == 46 and rep["issues"][0]["fix_prompt"] == ""


def test_brainbase_task_message_full_uses_recorded_journey(monkeypatch):
    from launchproof.brainbase import task_message
    monkeypatch.delenv("FLY_API_TOKEN", raising=False)
    monkeypatch.delenv("FLY_APP", raising=False)
    monkeypatch.delenv("LP_REGIONS", raising=False)
    m = task_message("https://a.com", "run1", "lp_tok", True)
    assert "--journey journey.json" in m and "--run-id run1" in m and "lp_tok" in m
    assert "browser_close" in m
    assert "--regions sjc" not in m and "--regions iad" not in m  # no Fly CLI flags
    assert "omit --regions" in m or "sandbox" in m
    assert "--skip-pay --skip-load" in task_message("https://a.com", "run1", None, False)


def test_brainbase_task_message_includes_regions_when_fly_configured(monkeypatch):
    from launchproof.brainbase import task_message
    monkeypatch.setenv("FLY_API_TOKEN", "FlyV1 test")
    monkeypatch.setenv("FLY_APP", "launchproof-load-test")
    monkeypatch.setenv("LP_REGIONS", "sjc iad")
    m = task_message("https://a.com", "run1", "lp_tok", True)
    assert "--regions sjc iad" in m


def test_fly_configured_helper(monkeypatch):
    from launchproof.load.fly import fly_configured
    monkeypatch.delenv("FLY_API_TOKEN", raising=False)
    monkeypatch.delenv("FLY_APP", raising=False)
    monkeypatch.delenv("LP_USE_FLY", raising=False)
    assert not fly_configured()
    monkeypatch.setenv("FLY_API_TOKEN", "FlyV1 x")
    monkeypatch.setenv("FLY_APP", "app")
    assert fly_configured()
    monkeypatch.setenv("LP_USE_FLY", "0")
    assert not fly_configured()


def test_load_env_local_sets_missing_keys(tmp_path, monkeypatch):
    from launchproof import envfile
    envfile._LOADED = False
    root = Path(envfile.__file__).resolve().parents[1]
    # write beside package; restore after
    target = root / ".env.local"
    backup = target.read_text(encoding="utf-8") if target.exists() else None
    try:
        target.write_text("LP_TEST_ENVFILE_ONLY=from_file\n", encoding="utf-8")
        monkeypatch.delenv("LP_TEST_ENVFILE_ONLY", raising=False)
        envfile._LOADED = False
        assert envfile.load_env_local() == target
        assert os.environ["LP_TEST_ENVFILE_ONLY"] == "from_file"
        monkeypatch.setenv("LP_TEST_ENVFILE_ONLY", "from_shell")
        envfile._LOADED = False
        envfile.load_env_local()  # should not override non-empty existing
        assert os.environ["LP_TEST_ENVFILE_ONLY"] == "from_shell"
    finally:
        if backup is None:
            target.unlink(missing_ok=True)
        else:
            target.write_text(backup, encoding="utf-8")
        envfile._LOADED = False
        monkeypatch.delenv("LP_TEST_ENVFILE_ONLY", raising=False)


def test_brainbase_task_message_full_without_token_falls_back_to_ui_only():
    from launchproof.brainbase import task_message
    m = task_message("https://a.com", "run1", None, True)
    assert "--skip-pay --skip-load" in m and "--token None" not in m and "lp_" not in m


def test_smart_ui_budget_defaults_and_clamps(monkeypatch):
    from launchproof.smart_ui import budget_from_env, merge_into_ui, smart_ui_enabled
    monkeypatch.delenv("LP_SMART_UI_BUDGET", raising=False)
    monkeypatch.delenv("LP_MAX_CHAOS", raising=False)
    monkeypatch.delenv("LP_MAX_VISION_VIEWS", raising=False)
    monkeypatch.delenv("LP_SMART_UI_CREDITS", raising=False)
    b = budget_from_env()
    assert b["max_chaos_scenarios"] == 3 and b["max_vision_views"] == 4
    assert smart_ui_enabled(b)
    monkeypatch.setenv("LP_MAX_CHAOS", "99")
    assert budget_from_env()["max_chaos_scenarios"] == 8  # clamp
    assert not smart_ui_enabled({"max_chaos_scenarios": 0, "max_vision_views": 0,
                                 "max_pages_hint": 5, "credit_soft_cap": 0})
    ui = {"issues": [{"severity": "low", "kind": "missing_title", "where": "https://a.com", "detail": "x"}]}
    smart = {
        "status": "done",
        "appeal_score": 72,
        "budget": b,
        "spent": {"chaos_scenarios": 1, "vision_views": 1},
        "chaos_issues": [{"severity": "high", "kind": "chaos_double_submit", "where": "https://a.com", "detail": "dup"}],
        "visual_issues": [{"severity": "medium", "kind": "visual_cta", "where": "https://a.com", "detail": "weak"}],
    }
    merged = merge_into_ui(ui, smart)
    assert merged["appeal_score"] == 72
    assert len(merged["issues"]) == 3
    assert merged["smart_ui"]["status"] == "done"


def test_brainbase_task_message_embeds_smart_ui_budget(monkeypatch):
    from launchproof.brainbase import task_message
    monkeypatch.delenv("LP_SMART_UI_BUDGET", raising=False)
    m = task_message("https://a.com", "run1", None, False, budget={"max_chaos_scenarios": 1, "max_vision_views": 1,
                                                                    "max_pages_hint": 2, "credit_soft_cap": 10})
    assert "max_chaos_scenarios=1" in m and "Chaos" in m and "Critic" in m
    m0 = task_message("https://a.com", "run1", None, False, budget={"max_chaos_scenarios": 0, "max_vision_views": 0,
                                                                     "max_pages_hint": 5, "credit_soft_cap": 0})
    assert "Smart UI is disabled" in m0


def test_headline_includes_appeal_score():
    bits = report.headline({"total": 50, "grade": "D", "parts": {}, "skipped": []},
                           {"issues": [], "appeal_score": 72}, None, None)
    assert any("Visual appeal 72/100" == b for b in bits)


def test_chaos_visual_fix_prompt_templates():
    p = report.fix_prompt({"kind": "chaos_double_submit", "where": "https://a.com/signup", "detail": "two POSTs"})
    assert "Debounce" in p and "signup" in p
    p2 = report.fix_prompt({"kind": "visual_cta", "where": "https://a.com/", "detail": "no primary"})
    assert "call to action" in p2.lower() or "CTA" in p2 or "primary button" in p2


def test_prompts_override_length_mismatch_falls_back(capsys):
    import asyncio
    from pathlib import Path
    import tempfile
    run = {"url": "https://a.com", "run_id": "t1", "ui": {"issues": [
        {"severity": "high", "kind": "console_errors", "where": "https://a.com/", "detail": "x", "shot": None},
    ]}, "payments": None, "load": None}
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        # length mismatch → warning + templates
        asyncio.run(report.build_report(run, out, prompts_override=["only one but wait we need match... actually wrong len", "extra"]))
        err = capsys.readouterr().out
        assert "prompts_override length" in err
        assert (out / "report.json").exists()
