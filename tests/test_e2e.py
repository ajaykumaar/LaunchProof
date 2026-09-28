"""End-to-end against the demo shop (real browser, local server). ~60 s.

Runs the shop twice: with the planted paywall bug, and with it fixed, and checks that Launchproof
tells the difference. Also drives the Claude agent loop with a scripted fake model (no API key needed).
"""
import asyncio
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _start_shop(env_extra: dict) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    env = {**os.environ, **env_extra}
    env.pop("STRIPE_SECRET_KEY", None)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "demo_shop.app:app", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            httpx.get(url + "/signup", timeout=1)
            return proc, url
        except httpx.HTTPError:
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError("demo shop did not start")


@pytest.fixture
def buggy_shop():
    proc, url = _start_shop({"DEMO_TRUST_REDIRECT": "1"})
    yield url
    proc.kill()


@pytest.fixture
def fixed_shop():
    proc, url = _start_shop({"DEMO_TRUST_REDIRECT": "0"})
    yield url
    proc.kill()


def test_ui_check_finds_planted_bugs(buggy_shop, tmp_path):
    from launchproof.ui_check import run_ui_check
    rep = asyncio.run(run_ui_check(buggy_shop + "/", tmp_path, max_pages=6, delay_s=0))
    kinds = {(i["kind"], i["where"].split(" (")[0].rsplit("/", 1)[-1]) for i in rep.issues}
    assert ("horizontal_overflow", "pricing") in kinds
    assert ("broken_images", "about") in kinds
    assert ("console_errors", "about") in kinds
    assert any("/api/feed" in c for p in rep.pages for c in p.api_calls)


def test_payment_check_catches_paywall_bypass(buggy_shop, tmp_path):
    from launchproof.payment import run_payment_check
    rep = asyncio.run(run_payment_check(buggy_shop + "/", tmp_path, use_agent=False,
                                        unlock_selector="#plan-status[data-plan=pro]"))
    by = {c.case: c for c in rep.cases}
    assert by["success"].outcome == "paid" and by["success"].unlocked
    assert by["decline"].outcome == "declined_shown" and not by["decline"].unlocked
    assert by["bypass"].unlocked
    assert rep.issues[0]["kind"] == "paywall_bypass"


def test_payment_check_passes_when_fixed(fixed_shop, tmp_path):
    from launchproof.payment import run_payment_check
    rep = asyncio.run(run_payment_check(fixed_shop + "/", tmp_path, use_agent=False))  # text-based unlock detection
    by = {c.case: c for c in rep.cases}
    assert by["success"].unlocked and not by["bypass"].unlocked
    assert rep.issues == []


def test_load_engine_finds_break_point(buggy_shop):
    from launchproof.load.engine import LoadConfig, run_load
    cfg = LoadConfig(url=buggy_shop, paths=["/", "/api/feed", "/pricing"], stages=[5, 300],
                     stage_seconds=4, think_min=0.5, think_max=1.0)
    r = asyncio.run(run_load(cfg))
    assert r["break_point_users"] == 300 and r["survived_users"] == 5


class _FakeClaude:
    """Scripted stand-in for AsyncAnthropic: reads the element list and clicks like a person would."""

    def __init__(self):
        self.messages = SimpleNamespace(create=self.create)
        self.n = 0

    async def create(self, **kw):
        last = kw["messages"][-1]["content"]
        text = last if isinstance(last, str) else "\n".join(b["text"] for b in last if b.get("type") == "text")

        def idx(pattern):
            m = re.search(r"\[(\d+)\] [^\n]*" + pattern, text)
            return int(m.group(1)) if m else None

        self.n += 1
        if idx('type=email') is not None and idx('type=password') is not None:
            calls = [("fill", {"idx": idx("type=email"), "value": "{email}"}),
                     ("fill", {"idx": idx("type=password"), "value": "{password}"}),
                     ("click", {"idx": idx('text="Create account"')})]
        elif idx('text="Upgrade to Pro"') is not None:
            calls = [("click", {"idx": idx('text="Upgrade to Pro"')})]
        elif idx('text="Get started"') is not None:
            calls = [("click", {"idx": idx('text="Get started"')})]
        else:
            calls = [("done", {"reached_checkout": False, "notes": "lost"})]
        blocks = [SimpleNamespace(type="tool_use", id=f"t{self.n}_{i}", name=n, input=a) for i, (n, a) in enumerate(calls)]
        return SimpleNamespace(content=blocks)


def test_agent_loop_reaches_checkout_and_records_replayable_path(buggy_shop, monkeypatch):
    import anthropic
    from playwright.async_api import async_playwright

    from launchproof import agent, journey
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeClaude)

    async def go():
        async with async_playwright() as pw:
            b = await pw.chromium.launch()
            p = await b.new_page()
            out = await agent.agent_journey(p, buggy_shop + "/", journey.Identity.new("t", "agent"))
            p2 = await b.new_page()
            await journey.replay(p2, out["actions"], journey.Identity.new("t", "replay"))
            replay_ok = await journey.has_card_form(p2)
            await b.close()
            return out, replay_ok

    out, replay_ok = asyncio.run(go())
    assert out["reached_checkout"], out["transcript"]
    assert replay_ok
    assert any(a["op"] == "fill" and a["value"] == "{email}" for a in out["actions"])


def test_mcp_tools_record_a_journey_the_cli_can_replay(buggy_shop, tmp_path, monkeypatch):
    """The Brainbase agent's path: browse with the MCP tools, save journey.json, CLI replays it."""
    import json as _json

    from launchproof import mcp_server as ms
    from launchproof.payment import run_payment_check

    async def go():
        obs = await ms.browser_open(buggy_shop + "/")

        def idx(text, o):
            m = re.search(r"\[(\d+)\] [^\n]*" + text, o)
            return int(m.group(1))
        obs = await ms.browser_click(idx('text="Get started"', obs))
        obs = await ms.browser_fill(idx("type=email", obs), "{email}")
        obs = await ms.browser_fill(idx("type=password", obs), "{password}")
        obs = await ms.browser_click(idx('text="Create account"', obs))
        obs = await ms.browser_click(idx('text="Upgrade to Pro"', obs))
        assert "PAYMENT FORM DETECTED" in obs
        saved = _json.loads(await ms.save_journey(str(tmp_path / "journey.json")))
        await ms.S.browser.close()
        await ms.S.pw.stop()
        return saved

    saved = asyncio.run(go())
    assert saved["reached_checkout"]
    actions = _json.loads((tmp_path / "journey.json").read_text())["actions"]
    rep = asyncio.run(run_payment_check(buggy_shop + "/", tmp_path / "run", use_agent=False, journey_actions=actions))
    assert rep.driver == "recorded"
    assert {c.case: c.outcome for c in rep.cases}["success"] == "paid"
