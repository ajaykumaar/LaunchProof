"""Signup / create-account flow check: find signup, fill synthetic identity, assert feedback.

Runs as part of a Launchproof run (after UI crawl). Same-site only. Never types real PII —
uses journey.Identity like payment discovery.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright

from . import journey
from .journey import Identity, SIGNUP_WORDS, AVOID_WORDS, list_elements, selector_for, settle, locator

UA = "LaunchproofSignupCheck/1.0 (+https://launchproof.xyz/bot)"
SUCCESS_RE = re.compile(
    r"(account (created|ready)|sign[ -]?up (successful|complete)|welcome|check your (email|inbox)|"
    r"verify your email|you.?re in|fake signup accepted|success)",
    re.I,
)
ERROR_RE = re.compile(r"(invalid|required|error|failed|try again|already (exists|registered))", re.I)


@dataclass
class SignupReport:
    reached_form: bool = False
    submitted: bool = False
    success: bool = False
    validation_ok: bool = False  # empty submit shows required/invalid feedback
    signup_url: str | None = None
    action_url: str | None = None  # form action for httpx signup storm
    detail: str = ""
    screenshots: list[str] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


async def _shot(page, out: Path, name: str, shots: list[str]):
    path = out / name
    try:
        await page.screenshot(path=str(path), full_page=False, timeout=10000)
        shots.append(path.name)
    except Exception:
        pass


async def _find_signup_url(page, start_url: str) -> str | None:
    """Prefer /signup paths, then links whose text matches SIGNUP_WORDS."""
    host = urlparse(start_url).netloc.lower()
    for path in ("/signup", "/sign-up", "/register", "/join"):
        cand = urljoin(start_url, path)
        if urlparse(cand).netloc.lower() == host:
            try:
                resp = await page.context.request.get(cand, timeout=8000)
                if resp.status < 400:
                    return cand
            except Exception:
                pass
    els = await list_elements(page)
    for e in els:
        href = e.get("href") or ""
        text = e.get("text") or ""
        if e["tag"] != "a" or not href or AVOID_WORDS.search(text):
            continue
        full = urljoin(page.url, href)
        if urlparse(full).netloc.lower() != host:
            continue
        if SIGNUP_WORDS.search(text) or SIGNUP_WORDS.search(href.replace("-", " ").replace("/", " ")):
            return full
    return None


async def _fill_signup_form(page, ident: Identity) -> tuple[bool, list[str]]:
    """Fill email/password/(name) on the visible signup form. Returns (filled, log)."""
    log: list[str] = []
    els = await list_elements(page)
    inputs = [e for e in els if e["tag"] == "input" and e["type"] not in ("submit", "button", "checkbox", "radio", "hidden")]
    email = next((e for e in inputs if e["type"] == "email" or re.search(
        r"e-?mail", f"{e['name']}{e['id']}{e['placeholder']}{e['label']}", re.I)), None)
    pwd = next((e for e in inputs if e["type"] == "password"), None)
    if not (email and pwd):
        log.append("no email+password fields")
        return False, log
    form = email["form"]
    for e in [e for e in inputs if e["form"] == form]:
        if e is email:
            value = "{email}"
        elif e["type"] == "password":
            value = "{password}"
        elif e["type"] in ("text", None) and re.search(r"name|user", f"{e['name']}{e['id']}{e['label']}", re.I):
            value = "{name}"
        else:
            value = "{name}" if e["type"] in ("text", None) else None
        if not value:
            continue
        try:
            await locator(page, selector_for(e)).fill(ident.sub(value), timeout=8000)
            log.append(f"filled {e.get('name') or e.get('type')}")
        except Exception as ex:
            log.append(f"fill failed: {ex}")
            return False, log
    for e in [e for e in els if e["form"] == form and e["type"] == "checkbox" and e.get("required")]:
        try:
            await locator(page, selector_for(e)).check(timeout=5000)
            log.append("checked required box")
        except Exception:
            pass
    return True, log


async def _click_primary_submit(page) -> bool:
    els = await list_elements(page)
    # Prefer submit whose text looks like signup, else first submit in a form with password.
    candidates = [e for e in els if (e["type"] == "submit" or e["tag"] == "button") and not AVOID_WORDS.search(e.get("text") or "")]
    pick = next((e for e in candidates if SIGNUP_WORDS.search(e.get("text") or "") or re.search(
        r"create|continue|submit|get started|join", e.get("text") or "", re.I)), None)
    if not pick and candidates:
        pick = candidates[0]
    if not pick:
        return False
    await locator(page, selector_for(pick)).click(timeout=10000)
    await settle(page, 5000)
    return True


async def _page_text(page) -> str:
    try:
        return await page.inner_text("body")
    except Exception:
        return ""


async def _has_success_marker(page) -> bool:
    if await page.locator('[data-lp-signup="ok"]').count():
        return True
    if await page.locator('[data-signup-success], [data-testid="signup-success"]').count():
        return True
    return bool(SUCCESS_RE.search(await _page_text(page)))


async def run_signup_check(start_url: str, out_dir: Path, run_id: str = "signup") -> SignupReport:
    out_dir.mkdir(parents=True, exist_ok=True)
    res = SignupReport()
    ident = Identity.new(run_id, "signup")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            ctx = await browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
            page = await ctx.new_page()
            await page.goto(start_url, wait_until="load", timeout=20000)
            await settle(page)

            target = await _find_signup_url(page, start_url)
            if not target:
                res.detail = "No signup / create-account entry point found from the start URL"
                res.issues.append({"severity": "high", "kind": "signup_unreachable", "where": start_url,
                                   "detail": res.detail, "shot": None})
                return res

            res.signup_url = target
            await page.goto(target, wait_until="load", timeout=20000)
            await settle(page)
            await _shot(page, out_dir, "signup-form.png", res.screenshots)
            try:
                action = await page.eval_on_selector(
                    "form",
                    """f => {
                      const a = (f.getAttribute('action') || '').trim();
                      if (!a || a === '#') return location.href;
                      try { return new URL(a, location.href).href; } catch { return location.href; }
                    }""")
                res.action_url = action
                res.log.append(f"form action={action}")
            except Exception:
                res.action_url = target

            # Validation: empty submit should show required/invalid feedback (HTML5 or custom).
            clicked_empty = await _click_primary_submit(page)
            if clicked_empty:
                await settle(page, 2000)
                # native validity: any required empty input
                invalid = await page.evaluate("""() => {
                  const els = [...document.querySelectorAll('input,select,textarea')];
                  return els.some(el => el.required && !el.checkValidity());
                }""")
                body = await _page_text(page)
                res.validation_ok = bool(invalid) or bool(ERROR_RE.search(body))
                res.log.append(f"empty submit validation={'ok' if res.validation_ok else 'missing'}")

            # Happy path with synthetic identity
            await page.goto(target, wait_until="load", timeout=20000)
            await settle(page)
            filled, flog = await _fill_signup_form(page, ident)
            res.log.extend(flog)
            res.reached_form = filled
            if not filled:
                res.detail = "Signup page found but email/password fields could not be filled"
                await _shot(page, out_dir, "signup-nofields.png", res.screenshots)
                res.issues.append({"severity": "high", "kind": "signup_no_form", "where": target,
                                   "detail": res.detail, "shot": res.screenshots[-1] if res.screenshots else None})
                return res

            res.submitted = await _click_primary_submit(page)
            await settle(page, 6000)
            await _shot(page, out_dir, "signup-after.png", res.screenshots)
            res.success = await _has_success_marker(page)
            body = await _page_text(page)

            if not res.submitted:
                res.detail = "Could not click a signup submit control"
                res.issues.append({"severity": "high", "kind": "signup_failed", "where": target,
                                   "detail": res.detail, "shot": res.screenshots[-1] if res.screenshots else None})
            elif res.success:
                res.detail = f"Signup succeeded for {ident.email}"
                res.log.append("success marker seen")
            else:
                res.detail = "Submitted signup but no success feedback (welcome / verify email / data-lp-signup=ok)"
                if ERROR_RE.search(body):
                    res.detail += "; page shows an error-like message"
                res.issues.append({"severity": "high", "kind": "signup_no_feedback", "where": target,
                                   "detail": res.detail, "shot": res.screenshots[-1] if res.screenshots else None})

            if filled and not res.validation_ok:
                res.issues.append({"severity": "medium", "kind": "signup_validation_weak", "where": target,
                                   "detail": "Empty submit did not surface required-field validation",
                                   "shot": None})
        finally:
            await browser.close()
    return res
