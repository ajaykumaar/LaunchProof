"""Launchproof MCP server: lets the Brainbase agent's own model be the "buyer" that finds checkout.

Runs inside the agent's sandbox as a command-based MCP server (see brainbase/agents/tester/brainbase.agent.yaml):
  python3 -m launchproof.mcp_server

The agent drives one real Chromium session with these tools, exactly like a first-time customer, and
every step is recorded as a replayable journey. Then the CLI replays that journey for the success,
decline and paywall-bypass cases:  python3 -m launchproof run <url> --journey journey.json

Model calls therefore run on Brainbase credits; no separate Anthropic key is needed.
Only synthetic identities are ever typed ({email}, {password}, {name}); card numbers are never exposed
to the model (payment.py fills them).
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urljoin, urlparse

from mcp.server.fastmcp import FastMCP

from . import journey, ownership
from .journey import Identity

mcp = FastMCP("launchproof")


class _Session:
    def __init__(self):
        self.pw = self.browser = self.page = None
        self.start_url = ""
        self.actions: list[dict] = []
        self.els: list[dict] = []
        self.ident = Identity.new("mcp", "discover")

    async def ensure(self):
        if self.page is None:
            from playwright.async_api import async_playwright
            self.pw = await async_playwright().start()
            self.browser = await self.pw.chromium.launch()
            ctx = await self.browser.new_context(viewport={"width": 1280, "height": 900},
                                                 user_agent="LaunchproofAgent/1.0 (+https://launchproof.xyz/bot)")
            self.page = await ctx.new_page()

    async def observe(self) -> str:
        from .agent import _observe
        obs, self.els = await _observe(self.page)
        return obs


S = _Session()


def _allowed(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host == urlparse(S.start_url).hostname or host.endswith("stripe.com")


@mcp.tool()
async def browser_open(url: str) -> str:
    """Start a fresh customer journey at this URL (resets the recorded journey). Returns the page:
    URL, visible text and a numbered list of interactive elements."""
    await S.ensure()
    S.start_url, S.actions, S.ident = url, [{"op": "goto", "url": url}], Identity.new("mcp", "discover")
    await S.page.context.clear_cookies()
    await journey.run_action(S.page, S.actions[0], S.ident)
    return await S.observe()


@mcp.tool()
async def browser_goto(url: str) -> str:
    """Open a URL or path on the same site."""
    await S.ensure()
    full = urljoin(S.page.url, url)
    if not _allowed(full):
        return "refused: only the site under test (and Stripe checkout) may be opened"
    act = {"op": "goto", "url": full}
    await journey.run_action(S.page, act, S.ident)
    S.actions.append(act)
    return await S.observe()


async def _act(op: str, idx: int, value: str | None = None) -> str:
    await S.ensure()
    el = next((e for e in S.els if e["idx"] == idx), None)
    if el is None:
        return "no element with that number; use the latest list\n\n" + await S.observe()
    act = {"op": op, "sel": journey.selector_for(el)}
    target = S.page.locator(f'[data-lp-idx="{idx}"]').first
    try:
        if op == "click":
            await target.click(timeout=10000)
        elif op == "check":
            await target.check(timeout=10000)
        else:
            act["value"] = (value or "")[:100]
            await target.fill(S.ident.sub(act["value"]), timeout=10000)
        await journey.settle(S.page, 6000)
        S.actions.append(act)
    except Exception as e:
        return f"error: {str(e).splitlines()[0][:200]}\n\n" + await S.observe()
    return await S.observe()


@mcp.tool()
async def browser_click(idx: int) -> str:
    """Click element number idx from the latest list."""
    return await _act("click", idx)


@mcp.tool()
async def browser_fill(idx: int, value: str) -> str:
    """Type into input idx. value must be {email}, {password}, {name} or a short test string. Never real data."""
    return await _act("fill", idx, value)


@mcp.tool()
async def browser_check(idx: int) -> str:
    """Tick checkbox idx (for example accept terms)."""
    return await _act("check", idx)


@mcp.tool()
async def save_journey(path: str = "journey.json") -> str:
    """Save the recorded steps. Call when the payment form is visible. Returns whether checkout was reached."""
    await S.ensure()
    reached = await journey.has_card_form(S.page)
    Path(path).write_text(json.dumps({"start_url": S.start_url, "reached_checkout": reached,
                                      "actions": S.actions}, indent=2))
    return json.dumps({"saved": path, "reached_checkout": reached, "steps": len(S.actions)})


@mcp.tool()
async def browser_close() -> str:
    """Close the Chromium session. Call after save_journey (or if you abort) so the sandbox does not leak browsers."""
    try:
        if S.browser is not None:
            await S.browser.close()
        if S.pw is not None:
            await S.pw.stop()
    finally:
        S.pw = S.browser = S.page = None
        S.els = []
    return "browser closed"


@mcp.tool()
def issue_ownership_token(url: str) -> str:
    """Create a 24-hour ownership token for a site and the three ways to install it."""
    tok = ownership.issue_token(url)
    return json.dumps({"token": tok, **ownership.instructions(url, tok)}, indent=2)


@mcp.tool()
def verify_ownership(url: str, token: str) -> str:
    """Check whether the site shows the token (DNS TXT, /.well-known file, or meta tag)."""
    return json.dumps(ownership.verify(url, token), indent=2)


if __name__ == "__main__":
    mcp.run()
