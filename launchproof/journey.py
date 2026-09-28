"""Shared browser plumbing for the signup-to-checkout journey.

The journey is a list of replayable actions:
  {"op": "goto", "url": ...}
  {"op": "click", "sel": {...}}
  {"op": "fill", "sel": {...}, "value": "{email}" | "{password}" | "{name}" | literal}
The first run (Claude agent, or the heuristic fallback) discovers the path; later cases (decline,
3DS, paywall-bypass probe) replay it with a fresh test identity. Discover once, replay cheaply.

Safety: we only ever type synthetic test identities (see Identity), never real personal data.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from urllib.parse import urlparse

LIST_JS = """
() => {
  const out = [];
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const els = document.querySelectorAll('a[href], button, input:not([type=hidden]), select, textarea, [role=button], [role=link]');
  let i = 0;
  for (const el of els) {
    if (!vis(el)) continue;
    el.setAttribute('data-lp-idx', String(i));
    const text = (el.innerText || el.value || el.getAttribute('aria-label') || el.title || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
    let label = '';
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) label = l.innerText.trim().slice(0, 60); }
    if (!label && el.closest('label')) label = el.closest('label').innerText.trim().slice(0, 60);
    out.push({ idx: i, tag: el.tagName.toLowerCase(), type: el.type || null, text, label,
      id: el.id || null, name: el.getAttribute('name'), placeholder: el.getAttribute('placeholder'),
      href: el.getAttribute('href'), required: !!el.required, form: el.form ? Array.from(document.forms).indexOf(el.form) : null });
    i++;
    if (i >= 150) break;
  }
  return out;
}
"""

BUY_WORDS = re.compile(r"\b(upgrade|buy|checkout|check out|subscribe|go pro|get pro|purchase|pay|start (free )?trial|add to cart|choose|select plan|get started)\b", re.I)
SIGNUP_WORDS = re.compile(r"\b(sign ?up|create (an )?account|register|join|try (it )?free|start free)\b", re.I)
NAV_WORDS = re.compile(r"\b(pricing|plans)\b", re.I)
AVOID_WORDS = re.compile(r"log ?out|sign ?out|delete|cancel|unsubscribe|remove|google|github|apple|microsoft|facebook", re.I)


@dataclass
class Identity:
    email: str
    password: str
    name: str = "Launchproof Test"

    @classmethod
    def new(cls, run_id: str, case: str) -> "Identity":
        import os
        domain = os.getenv("LP_TEST_EMAIL_DOMAIN", "example.com")  # set to your Cloudflare Email Routing domain
        return cls(email=f"lp-{run_id[:10]}-{case}-{secrets.token_hex(3)}@{domain}".lower(),
                   password="Lp!" + secrets.token_urlsafe(12))

    def sub(self, v: str) -> str:
        return v.replace("{email}", self.email).replace("{password}", self.password).replace("{name}", self.name)


def selector_for(el: dict) -> dict:
    """A replayable selector: id > name > visible text."""
    if el.get("id"):
        return {"css": f"#{el['id']}"} if re.fullmatch(r"[A-Za-z][\w-]*", el["id"]) else {"css": f'[id="{el["id"]}"]'}
    if el.get("name") and el["tag"] in ("input", "select", "textarea", "button"):
        return {"css": f'{el["tag"]}[name="{el["name"]}"]'}
    if el.get("text"):
        return {"tag": el["tag"], "text": el["text"][:60]}
    return {"css": f'[data-lp-idx="{el["idx"]}"]'}


def locator(page, sel: dict):
    if "css" in sel:
        return page.locator(sel["css"]).first
    return page.locator(sel.get("tag", "*")).filter(has_text=sel["text"]).first


def is_checkout(page) -> bool:
    host = urlparse(page.url).hostname or ""
    return host.endswith("checkout.stripe.com") or host.endswith("buy.stripe.com") or "checkout" in urlparse(page.url).path


async def has_card_form(page) -> bool:
    if await page.locator("#cardNumber").count():
        return True
    return await page.locator('iframe[title*="payment" i], iframe[name^="__privateStripeFrame"]').count() > 0


async def settle(page, ms: int = 8000):
    try:
        await page.wait_for_load_state("networkidle", timeout=ms)
    except Exception:
        pass


async def list_elements(page) -> list[dict]:
    try:
        return await page.evaluate(LIST_JS)
    except Exception:
        return []


async def run_action(page, act: dict, ident: Identity):
    op = act["op"]
    if op == "goto":
        await page.goto(act["url"], wait_until="load", timeout=20000)
    elif op == "click":
        await locator(page, act["sel"]).click(timeout=10000)
    elif op == "fill":
        await locator(page, act["sel"]).fill(ident.sub(act["value"]), timeout=10000)
    elif op == "check":
        await locator(page, act["sel"]).check(timeout=10000)
    await settle(page, 6000)


async def replay(page, actions: list[dict], ident: Identity) -> list[str]:
    log = []
    for a in actions:
        try:
            await run_action(page, a, ident)
            log.append(f"ok {a['op']}")
        except Exception as e:
            log.append(f"failed {a['op']} {a.get('sel') or a.get('url')}: {str(e).splitlines()[0][:120]}")
            break
    return log


async def heuristic_journey(page, start_url: str, ident: Identity, max_steps: int = 10) -> dict:
    """No-LLM fallback: sign up if a signup form appears, click the most purchase-like control,
    stop when a card form is on screen."""
    actions: list[dict] = [{"op": "goto", "url": start_url}]
    await run_action(page, actions[0], ident)
    clicked: set[str] = set()
    for _ in range(max_steps):
        if await has_card_form(page):
            return {"reached_checkout": True, "actions": actions, "url": page.url}
        els = await list_elements(page)
        inputs = [e for e in els if e["tag"] == "input" and e["type"] not in ("submit", "button", "checkbox", "radio")]
        email = next((e for e in inputs if e["type"] == "email" or re.search(r"e-?mail", f"{e['name']}{e['id']}{e['placeholder']}{e['label']}", re.I)), None)
        pwd = next((e for e in inputs if e["type"] == "password"), None)
        if email and pwd and email["form"] is not None:
            form = email["form"]
            for e in [e for e in inputs if e["form"] == form]:
                value = "{email}" if e is email else "{password}" if e["type"] == "password" else "{name}" if e["type"] in ("text", None) else None
                if value:
                    actions.append({"op": "fill", "sel": selector_for(e), "value": value})
                    await run_action(page, actions[-1], ident)
            for e in [e for e in els if e["form"] == form and e["type"] == "checkbox" and e["required"]]:
                actions.append({"op": "check", "sel": selector_for(e)})  # e.g. accept terms (test identity)
                await run_action(page, actions[-1], ident)
            submit = next((e for e in els if e["form"] == form and (e["type"] == "submit" or e["tag"] == "button")), None)
            if submit:
                actions.append({"op": "click", "sel": selector_for(submit)})
                await run_action(page, actions[-1], ident)
                continue
        clickable = [e for e in els if e["tag"] in ("a", "button") and e["text"] and not AVOID_WORDS.search(e["text"])
                     and e["text"] not in clicked and not (e.get("href") or "").startswith(("mailto:", "tel:", "#"))]
        pick = next((e for words in (BUY_WORDS, SIGNUP_WORDS, NAV_WORDS) for e in clickable if words.search(e["text"])), None)
        if not pick:
            break
        clicked.add(pick["text"])
        actions.append({"op": "click", "sel": selector_for(pick)})
        try:
            await run_action(page, actions[-1], ident)
        except Exception:
            actions.pop()
    return {"reached_checkout": await has_card_form(page), "actions": actions, "url": page.url}
