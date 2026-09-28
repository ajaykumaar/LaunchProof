"""Claude as the journey agent: act like a brand-new customer, sign up, and reach the payment form.

Claude sees a numbered list of the visible interactive elements (plus a slice of page text) and
calls tools: click, fill, goto, done. Every successful action is recorded with a stable selector so
the path can be replayed deterministically for the other payment cases without calling Claude again.

Guardrails:
  - same-site navigation only (plus Stripe checkout hosts)
  - only synthetic identity placeholders are ever typed ({email}, {password}, {name}); the card itself
    is filled by payment.fill_card, never by the model
  - max 25 steps and 180 seconds per journey
Model: LP_MODEL (default claude-sonnet-5). Falls back to heuristics when no ANTHROPIC_API_KEY is set.
"""
from __future__ import annotations

import json
import os
import time
from urllib.parse import urljoin, urlparse

from . import journey
from .journey import Identity

MAX_STEPS = 25
MAX_SECONDS = 180

SYSTEM = """You are a QA agent testing a website on launch day, acting as a brand-new customer.
Goal: create an account if the site needs one, then get to the page where a card can be entered
to pay for the cheapest paid plan or product. Stop as soon as a payment/card form is visible.

Rules:
- Use only these values when typing: {email} for email, {password} for passwords, {name} for names.
  For any other required text field, type a short plausible test value such as "Launchproof Test".
- Never enter real personal data. Never click delete, cancel, logout, or social login buttons.
- Prefer the most direct path: pricing -> upgrade/buy -> checkout.
- If an email verification step blocks you, call done with reached_checkout=false and explain.
- Call done when the payment form is visible, or when you are certain it cannot be reached."""

TOOLS = [
    {"name": "click", "description": "Click an element by its number from the element list.",
     "input_schema": {"type": "object", "properties": {"idx": {"type": "integer"}}, "required": ["idx"]}},
    {"name": "fill", "description": "Type into an input by its number. value must be {email}, {password}, {name} or a short test string.",
     "input_schema": {"type": "object", "properties": {"idx": {"type": "integer"}, "value": {"type": "string"}},
                      "required": ["idx", "value"]}},
    {"name": "check", "description": "Tick a checkbox by its number (e.g. accept terms).",
     "input_schema": {"type": "object", "properties": {"idx": {"type": "integer"}}, "required": ["idx"]}},
    {"name": "goto", "description": "Open a URL or path on the same site.",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "done", "description": "Finish the journey.",
     "input_schema": {"type": "object", "properties": {"reached_checkout": {"type": "boolean"},
                                                       "notes": {"type": "string"}},
                      "required": ["reached_checkout", "notes"]}},
]


def agent_available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def _model() -> str:
    return os.getenv("LP_MODEL", "claude-sonnet-5")


async def _observe(page) -> tuple[str, list[dict]]:
    els = await journey.list_elements(page)
    try:
        text = (await page.inner_text("body", timeout=3000))[:1500]
    except Exception:
        text = ""
    lines = []
    for e in els:
        bits = [f"[{e['idx']}] {e['tag']}"]
        if e["type"] and e["tag"] == "input":
            bits.append(f"type={e['type']}")
        for k in ("text", "label", "name", "placeholder"):
            if e.get(k):
                bits.append(f'{k}="{e[k][:50]}"')
        if e.get("href"):
            bits.append(f"href={e['href'][:60]}")
        lines.append(" ".join(bits))
    card = await journey.has_card_form(page)
    obs = (f"URL: {page.url}\nTitle: {await page.title()}\n"
           f"{'PAYMENT FORM DETECTED. Call done with reached_checkout=true.' if card else ''}\n"
           f"Visible text (truncated):\n{text}\n\nInteractive elements:\n" + "\n".join(lines))
    return obs, els


def _allowed(url: str, start_url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host == urlparse(start_url).hostname or host.endswith("stripe.com")


async def agent_journey(page, start_url: str, ident: Identity, on_step=None) -> dict:
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()
    actions: list[dict] = [{"op": "goto", "url": start_url}]
    await journey.run_action(page, actions[0], ident)
    obs, els = await _observe(page)
    messages = [{"role": "user", "content": f"Start URL: {start_url}\n\n{obs}"}]
    t0, notes, reached = time.monotonic(), "", False
    transcript: list[str] = []
    for step in range(MAX_STEPS):
        if time.monotonic() - t0 > MAX_SECONDS:
            notes = "time limit reached"
            break
        resp = await client.messages.create(model=_model(), max_tokens=800, system=SYSTEM, tools=TOOLS,
                                            messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        uses = [b for b in resp.content if b.type == "tool_use"]
        if not uses:
            messages.append({"role": "user", "content": "Use a tool. Call done if you are finished."})
            continue
        results, finished = [], False
        for u in uses:
            name, args = u.name, u.input or {}
            result = "ok"
            try:
                if name == "done":
                    reached, notes, finished = bool(args.get("reached_checkout")), args.get("notes", ""), True
                elif name == "goto":
                    url = urljoin(page.url, args["url"])
                    if not _allowed(url, start_url):
                        result = "refused: other site"
                    else:
                        act = {"op": "goto", "url": url}
                        await journey.run_action(page, act, ident)
                        actions.append(act)
                else:
                    el = next((e for e in els if e["idx"] == args.get("idx")), None)
                    if el is None:
                        result = "no element with that number; look at the latest list"
                    else:
                        act = {"op": name, "sel": journey.selector_for(el)}
                        if name == "fill":
                            act["value"] = str(args.get("value", ""))[:100]
                        # act on the exact element Claude picked, record the stable selector for replay
                        target = page.locator(f'[data-lp-idx="{el["idx"]}"]').first
                        if name == "click":
                            await target.click(timeout=10000)
                        elif name == "check":
                            await target.check(timeout=10000)
                        else:
                            await target.fill(ident.sub(act["value"]), timeout=10000)
                        await journey.settle(page, 6000)
                        actions.append(act)
            except Exception as e:
                result = f"error: {str(e).splitlines()[0][:200]}"
            transcript.append(f"{step}: {name} {json.dumps(args)[:120]} -> {result}")
            if on_step:
                on_step(transcript[-1])
            results.append({"type": "tool_result", "tool_use_id": u.id, "content": result})
        if finished:
            break
        obs, els = await _observe(page)
        if await journey.has_card_form(page):
            reached = True
            notes = notes or "payment form visible"
            break
        results.append({"type": "text", "text": obs})
        messages.append({"role": "user", "content": results})
        # keep the context small: drop old observations beyond the last 6 turns
        if len(messages) > 14:
            messages = messages[:1] + messages[-12:]
            while messages[1]["role"] != "assistant":
                messages.pop(1)
    reached = reached and await journey.has_card_form(page)
    return {"reached_checkout": reached, "actions": actions, "url": page.url, "notes": notes,
            "transcript": transcript}
