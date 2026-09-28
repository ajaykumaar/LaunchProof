"""Web app smoke tests (no browser, no Stripe)."""
from fastapi.testclient import TestClient

from api import server

client = TestClient(server.app)


def test_home_and_verify_page():
    assert "survive launch day" in client.get("/").text
    r = client.post("/verify", data={"url": "https://shop.example.com"})
    assert "_launchproof.shop.example.com" in r.text and "launchproof-verify=lp_" in r.text


def test_run_requires_consent():
    r = client.post("/runs", data={"url": "https://shop.example.com"}, follow_redirects=False)
    assert r.status_code == 400


def test_full_run_on_unpaid_site_redirects_to_buy(monkeypatch):
    monkeypatch.setattr(server, "_do_run", lambda *a, **k: None)
    r = client.post("/runs", data={"url": "https://shop.example.com", "consent": "1", "full": "1"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/buy")


def test_report_files_are_path_safe():
    assert client.get("/r/abc/..%2Frun.json").status_code == 404
    assert client.get("/r/abc/report.html").status_code == 404


def test_webhook_rejects_bad_signature():
    assert client.post("/api/stripe/webhook", content=b"{}", headers={"stripe-signature": "x"}).status_code == 400


def test_legal_pages():
    assert "Terms of Service" in client.get("/legal/terms").text
    assert client.get("/legal/../../etc/passwd").status_code == 404


def test_load_result_callback_requires_token(monkeypatch):
    monkeypatch.setenv("LP_RESULT_TOKEN", "t0k")
    assert client.post("/api/load-results/r1/sjc", json={"x": 1}).status_code == 401
    ok = client.post("/api/load-results/r1/sjc", json={"survived_users": 100}, headers={"Authorization": "Bearer t0k"})
    assert ok.status_code == 200
    got = client.get("/api/load-results/r1", headers={"Authorization": "Bearer t0k"}).json()
    assert got == {"sjc": {"survived_users": 100}}


def test_load_result_callback_refuses_when_token_unset(monkeypatch):
    monkeypatch.delenv("LP_RESULT_TOKEN", raising=False)
    assert client.post("/api/load-results/r1/sjc", json={"x": 1}).status_code == 503


def test_brainbase_run_hydrates_score_from_report_json(tmp_path, monkeypatch):
    """When BRAINBASE_* is set, status API must expose score/headline from downloaded report.json."""
    import json

    monkeypatch.setenv("BRAINBASE_API_KEY", "k")
    monkeypatch.setenv("BRAINBASE_AGENT_ID", "agent-1")
    monkeypatch.setattr(server, "RUNS_DIR", tmp_path)

    def fake_start(msg):
        return "thread-1"

    def fake_wait(tid, timeout_s=1500, on_message=None):
        if on_message:
            on_message("running checks")
        return "success"

    def fake_download(tid, path, dest, tries=5):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if path.endswith("report.json"):
            dest.write_text(json.dumps({"score": {"total": 46}, "headline": ["paywall bypass"]}))
        elif path.endswith("report.html"):
            dest.write_text("<html>ok</html>")
        else:
            dest.write_bytes(b"png")
        return True

    monkeypatch.setattr("launchproof.brainbase.start", fake_start)
    monkeypatch.setattr("launchproof.brainbase.wait", fake_wait)
    monkeypatch.setattr("launchproof.brainbase.download", fake_download)
    monkeypatch.setattr("launchproof.brainbase.task_message", lambda *a, **k: "msg")

    import asyncio
    rid = "abc12"
    server.RUNS[rid] = {"status": "queued", "log": [], "url": "https://x.com", "result": None}
    asyncio.run(server._do_run_brainbase(rid, "https://x.com", "lp_t", True))
    st = client.get(f"/api/runs/{rid}").json()
    assert st["status"] == "done" and st["score"] == 46 and st["headline"] == ["paywall bypass"]
