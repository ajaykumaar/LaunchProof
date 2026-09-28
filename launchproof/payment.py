"""Payment check: does money actually flow, and does the product react correctly?

Cases (all Stripe TEST mode by default):
  success   4242 4242 4242 4242  -> checkout completes, user lands back on the site, paid feature unlocks
  decline   4000 0000 0000 0002  -> a clear error is shown and nothing unlocks
  bypass    no card at all       -> visit the post-payment success URL with a fake session id; the paid
                                    feature must NOT unlock (catches "unlock on redirect" bugs)
  3ds       4000 0025 0000 3155  -> optional, completes the test 3D Secure challenge

Optional deep verification with a restricted, read-only Stripe key (Checkout Sessions, Charges, Events,
Webhook Endpoints: read): session complete, charge succeeded, webhooks delivered (pending_webhooks == 0).

Live mode is deliberately not implemented here. It needs: verified domain, explicit consent, a dedicated
spend-limited card, exactly one attempt per run (never retry declines: that looks like card testing),
and an automatic refund.
"""
from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from playwright.async_api import async_playwright

from . import journey
from .journey import Identity

CARDS = {"success": "4242424242424242", "decline": "4000000000000002", "3ds": "4000002500003155"}
EXPIRY, CVC, ZIP = "12 / 34", "123", "94107"
SUCCESS_TEXT = re.compile(r"thank you|payment (successful|received|confirmed|complete)|order (confirmed|complete)|"
                          r"you'?re (now )?(on )?(pro|premium)|pro plan active|subscription (is )?active|unlocked|"
                          r"welcome to (pro|premium)|purchase complete", re.I)
DECLINE_TEXT = re.compile(r"declined|insufficient funds|card was|card has been|payment failed|could not be processed|"
                          r"try another|incorrect|invalid", re.I)
UA = "LaunchproofPaymentCheck/1.0 (+https://launchproof.xyz/bot)"


@dataclass
class CaseResult:
    case: str
    reached_checkout: bool = False
    submitted: bool = False
    outcome: str = "not_run"  # paid | declined_shown | declined_silent | error | stuck | unlocked | not_unlocked
    unlocked: bool | None = None
    error_text: str | None = None
    final_url: str | None = None
    screenshots: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    seconds: float = 0.0


@dataclass
class PaymentReport:
    start_url: str
    mode: str = "test"
    driver: str = "heuristic"  # agent | heuristic
    journey: list[dict] = field(default_factory=list)
    cases: list[CaseResult] = field(default_factory=list)
    stripe: dict | None = None
    issues: list[dict] = field(default_factory=list)

    def to_dict(self):
        d = asdict(self)
        return d


async def fill_card(page, card: str, ident: Identity) -> list[str]:
    """Fill Stripe hosted Checkout (and look-alikes using the same ids), or an embedded Payment Element."""
    log = []
    acc = page.locator('[data-testid="card-accordion-item-button"]')
    if await acc.count():
        await acc.first.click()
        log.append("opened card accordion")
    if await page.locator("#cardNumber").count():
        async def put(sel, val):
            loc = page.locator(sel)
            if await loc.count() and await loc.first.is_visible() and await loc.first.is_editable():
                await loc.first.fill(val)
                log.append(f"filled {sel}")
        await put("#email", ident.email)
        await put("#cardNumber", card)
        await put("#cardExpiry", EXPIRY)
        await put("#cardCvc", CVC)
        await put("#billingName", ident.name)
        await put("#billingPostalCode", ZIP)
        return log
    # Embedded Payment Element / Card Element inside Stripe iframes
    for frame in page.frames:
        if "js.stripe.com" not in (frame.url or ""):
            continue
        for sel, val in (('input[name="number"], input[name="cardnumber"]', card),
                         ('input[name="expiry"], input[name="exp-date"]', EXPIRY),
                         ('input[name="cvc"]', CVC), ('input[name="postalCode"], input[name="postal"]', ZIP)):
            loc = frame.locator(sel)
            if await loc.count():
                await loc.first.fill(val)
                log.append(f"filled iframe {sel.split(',')[0]}")
    return log


async def submit_payment(page) -> bool:
    for sel in ('[data-testid="hosted-payment-submit-button"]', "button.SubmitButton", 'button[type="submit"]',
                'button:has-text("Pay")', 'button:has-text("Subscribe")', 'button:has-text("Complete")'):
        loc = page.locator(sel)
        if await loc.count() and await loc.first.is_visible():
            await loc.first.click()
            return True
    return False


async def _complete_3ds(page, log: list[str]):
    """Stripe's test 3DS page has a 'Complete' button inside nested iframes."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        for frame in page.frames:
            btn = frame.locator('button:has-text("Complete"), #test-source-authorize-3ds')
            try:
                if await btn.count():
                    await btn.first.click()
                    log.append("completed 3DS challenge")
                    return
            except Exception:
                pass
        await page.wait_for_timeout(1000)
    log.append("3DS challenge not found")


async def _await_outcome(page, checkout_host: str, timeout_s: float = 30) -> tuple[str, str | None]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        host = urlparse(page.url).hostname or ""
        left_checkout = host != checkout_host or not journey.is_checkout(page) and not await journey.has_card_form(page)
        if left_checkout:
            await journey.settle(page)
            return "left_checkout", None
        for sel in ('[role="alert"]', ".FieldError", ".Error", "#error", ".error", '[class*="error" i]'):
            loc = page.locator(sel)
            try:
                n = await loc.count()
                for i in range(min(n, 5)):
                    t = (await loc.nth(i).inner_text(timeout=500)).strip()
                    if t and DECLINE_TEXT.search(t):
                        return "error_shown", t[:200]
            except Exception:
                continue
        await page.wait_for_timeout(700)
    return "stuck", None


async def _is_unlocked(page, unlock_selector: str | None) -> bool:
    if unlock_selector:
        try:
            return await page.locator(unlock_selector).count() > 0
        except Exception:
            return False
    try:
        text = await page.inner_text("body", timeout=3000)
    except Exception:
        return False
    return bool(SUCCESS_TEXT.search(text))


def _pick_success_url(navs: list[str], checkout_url: str) -> str | None:
    """The first page the customer lands on after paying (before any further redirects).
    Prefer a hop that carries a session/payment id in the query string."""
    after = [u for u in navs if u.split("?")[0] != checkout_url.split("?")[0]
             and "stripe.com" not in (urlparse(u).hostname or "")]
    with_id = [u for u in after if re.search(r"(session|payment|order|checkout)[_-]?id|payment_intent", urlparse(u).query, re.I)]
    return (with_id or after or [None])[0]


def _fake_success_url(url: str) -> str:
    p = urlparse(url)
    q = [(k, ("cs_test_launchproof_fake" if "session" in k.lower() or k.lower() in ("id", "payment_intent") else v))
         for k, v in parse_qsl(p.query)]
    return urlunparse(p._replace(query=urlencode(q)))


async def _shot(page, out_dir: Path, name: str, res: CaseResult):
    path = out_dir / f"pay-{name}.png"
    try:
        await page.screenshot(path=str(path), full_page=False, timeout=10000)
        res.screenshots.append(path.name)
    except Exception:
        pass


async def _card_case(browser, case: str, rpt: PaymentReport, actions: list[dict], run_id: str, out_dir: Path,
                     unlock_selector: str | None, unlock_url: str | None) -> tuple[CaseResult, str | None, str | None]:
    """Returns (result, success_url_seen, unlock_url_seen)."""
    t0 = time.monotonic()
    res = CaseResult(case=case)
    ctx = await browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
    page = await ctx.new_page()
    ident = Identity.new(run_id, case)
    success_url = None
    try:
        res.log += await journey.replay(page, actions, ident)
        res.reached_checkout = await journey.has_card_form(page)
        if not res.reached_checkout:
            res.outcome = "error"
            res.error_text = "could not reach the payment form by replaying the journey"
            await _shot(page, out_dir, f"{case}-no-checkout", res)
            return res, None, None
        checkout_host = urlparse(page.url).hostname or ""
        checkout_url = page.url
        navs: list[str] = []  # every main-frame navigation hop, including server redirects
        page.on("request", lambda r: navs.append(r.url) if r.is_navigation_request() and r.frame == page.main_frame else None)
        res.log += await fill_card(page, CARDS[case], ident)
        await _shot(page, out_dir, f"{case}-filled", res)
        res.submitted = await submit_payment(page)
        if not res.submitted:
            res.outcome, res.error_text = "error", "no pay/submit button found"
            return res, None, None
        if case == "3ds":
            await _complete_3ds(page, res.log)
        state, err = await _await_outcome(page, checkout_host)
        res.final_url = page.url
        await _shot(page, out_dir, f"{case}-after", res)
        if case in ("success", "3ds"):
            if state == "left_checkout":
                success_url = _pick_success_url(navs, checkout_url) or page.url
                res.unlocked = await _is_unlocked(page, unlock_selector)
                if not res.unlocked and unlock_url:
                    await page.goto(unlock_url, wait_until="load")
                    res.unlocked = await _is_unlocked(page, unlock_selector)
                res.outcome = "paid"
                await _shot(page, out_dir, f"{case}-unlock", res)
            else:
                res.outcome, res.error_text = ("error", err) if err else ("stuck", "no redirect after paying (30 s)")
        else:  # decline
            if state == "error_shown":
                res.outcome, res.error_text = "declined_shown", err
            elif state == "left_checkout":
                res.outcome = "declined_but_redirected"
                res.unlocked = await _is_unlocked(page, unlock_selector)
            else:
                res.outcome = "declined_silent"
            if unlock_url:
                await page.goto(unlock_url, wait_until="load")
                res.unlocked = await _is_unlocked(page, unlock_selector)
        return res, success_url, (page.url if res.unlocked else None)
    except Exception as e:
        res.outcome, res.error_text = "error", str(e).splitlines()[0][:300]
        return res, success_url, None
    finally:
        res.seconds = round(time.monotonic() - t0, 1)
        await ctx.close()


async def _bypass_case(browser, actions: list[dict], success_url: str, unlock_url: str | None, run_id: str,
                       out_dir: Path, unlock_selector: str | None) -> CaseResult:
    """Sign up a fresh unpaid user, skip checkout, open the success URL with a fake session id."""
    res = CaseResult(case="bypass")
    t0 = time.monotonic()
    ctx = await browser.new_context(user_agent=UA)
    page = await ctx.new_page()
    ident = Identity.new(run_id, "bypass")
    try:
        res.log += await journey.replay(page, actions, ident)  # signs up a fresh, unpaid user
        fake = _fake_success_url(success_url)
        res.log.append(f"visiting {fake}")
        res.error_text = f"opened {fake} without paying"
        await page.goto(fake, wait_until="load", timeout=20000)
        await journey.settle(page)
        if unlock_url:
            await page.goto(unlock_url, wait_until="load")
        res.final_url = page.url
        res.unlocked = await _is_unlocked(page, unlock_selector)
        res.outcome = "unlocked" if res.unlocked else "not_unlocked"
        await _shot(page, out_dir, "bypass", res)
    except Exception as e:
        res.outcome, res.error_text = "error", str(e).splitlines()[0][:300]
    finally:
        res.seconds = round(time.monotonic() - t0, 1)
        await ctx.close()
    return res


def verify_with_stripe(restricted_key: str, since_ts: int, emails: list[str]) -> dict:
    """Read-only deep check. Needs a restricted key with read on Checkout Sessions, Charges, Events, Webhook Endpoints."""
    import stripe
    client = stripe.StripeClient(restricted_key)
    out = {"sessions": [], "webhooks": [], "problems": []}
    try:
        sessions = client.checkout.sessions.list(params={"created": {"gte": since_ts}, "limit": 20})
        for s in sessions.data:
            email = (s.customer_details.email if s.customer_details else None) or s.customer_email
            if emails and (email or "").lower() not in emails:
                continue
            out["sessions"].append({"id": s.id, "status": s.status, "payment_status": s.payment_status, "email": email})
            if s.status != "complete" or s.payment_status not in ("paid", "no_payment_required"):
                out["problems"].append(f"session {s.id} is {s.status}/{s.payment_status}")
        events = client.events.list(params={"created": {"gte": since_ts}, "type": "checkout.session.completed", "limit": 20})
        for ev in events.data:
            out["webhooks"].append({"event": ev.id, "pending_webhooks": ev.pending_webhooks})
            if ev.pending_webhooks:
                out["problems"].append(f"event {ev.id}: {ev.pending_webhooks} webhook endpoint(s) have not acknowledged it")
        endpoints = client.webhook_endpoints.list(params={"limit": 20})
        live = [e for e in endpoints.data if e.status == "enabled"]
        out["endpoints"] = [{"url": e.url, "events": e.enabled_events} for e in live]
        if not live:
            out["problems"].append("no enabled webhook endpoint: your app cannot learn about payments made outside the redirect")
    except Exception as e:
        out["problems"].append(f"Stripe API: {str(e)[:200]}")
    return out


def summarize(rpt: PaymentReport) -> list[dict]:
    issues = []
    by = {c.case: c for c in rpt.cases}
    s = by.get("success")
    if not rpt.cases or not s or not s.reached_checkout:
        issues.append({"severity": "critical", "kind": "checkout_unreachable", "where": rpt.start_url,
                       "detail": "Could not get from the homepage to a payment form as a new user.", "shot": None})
        return issues
    if s.outcome != "paid":
        issues.append({"severity": "critical", "kind": "checkout_failed", "where": s.final_url or rpt.start_url,
                       "detail": f"Test card 4242 did not complete: {s.outcome} {s.error_text or ''}".strip(),
                       "shot": s.screenshots[-1] if s.screenshots else None})
    elif not s.unlocked:
        issues.append({"severity": "critical", "kind": "no_unlock", "where": s.final_url,
                       "detail": "Payment went through but the paid feature did not unlock (or no success message).",
                       "shot": s.screenshots[-1] if s.screenshots else None})
    d = by.get("decline")
    if d and d.outcome == "declined_silent":
        issues.append({"severity": "high", "kind": "decline_silent", "where": d.final_url,
                       "detail": "A declined card showed no error to the customer.", "shot": d.screenshots[-1] if d.screenshots else None})
    if d and d.unlocked:
        issues.append({"severity": "critical", "kind": "decline_unlocked", "where": d.final_url,
                       "detail": "A declined card still unlocked the paid feature.", "shot": d.screenshots[-1] if d.screenshots else None})
    b = by.get("bypass")
    if b and b.unlocked:
        issues.append({"severity": "critical", "kind": "paywall_bypass",
                       "where": (b.error_text or "").replace("opened ", "").replace(" without paying", "") or b.final_url,
                       "detail": "Opening the payment success URL with a fake session id unlocked the paid plan without paying. "
                                 "The app trusts the redirect instead of verifying the payment with Stripe.",
                       "shot": b.screenshots[-1] if b.screenshots else None})
    if rpt.stripe:
        for p in rpt.stripe.get("problems", []):
            issues.append({"severity": "high", "kind": "stripe_backend", "where": "Stripe", "detail": p, "shot": None})
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return sorted(issues, key=lambda i: order[i["severity"]])


async def run_payment_check(start_url: str, out_dir: Path, run_id: str = "local", use_agent: bool = True,
                            unlock_selector: str | None = None, cases=("success", "decline", "bypass"),
                            stripe_key: str | None = None, journey_actions: list[dict] | None = None) -> PaymentReport:
    out_dir.mkdir(parents=True, exist_ok=True)
    rpt = PaymentReport(start_url=start_url)
    since = int(time.time()) - 60
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            # 1. Discover the path to checkout once: a journey recorded by the Brainbase agent (MCP tools),
            #    else Claude via the Anthropic API, else heuristics.
            found = None
            if journey_actions:
                rpt.driver = "recorded"
                rpt.journey = journey_actions
                ctx = await browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
                page = await ctx.new_page()
                await journey.replay(page, journey_actions, Identity.new(run_id, "check"))
                ok = await journey.has_card_form(page)
                await ctx.close()
                if ok:
                    found = {"reached_checkout": True, "actions": journey_actions}
                else:
                    journey_actions = None  # stale recording: fall through to discovery
            if not found:
                ctx = await browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
                page = await ctx.new_page()
                ident = Identity.new(run_id, "discover")
                if use_agent:
                    from .agent import agent_available, agent_journey
                    if agent_available():
                        rpt.driver = "agent"
                        found = await agent_journey(page, start_url, ident)
                if not found or not found.get("reached_checkout"):
                    if found:
                        rpt.journey = found.get("actions", [])
                    await page.goto("about:blank")
                    rpt.driver = "heuristic" if not found else "agent+heuristic"
                    found = await journey.heuristic_journey(page, start_url, ident)
                await ctx.close()
            rpt.journey = found["actions"]
            if not found["reached_checkout"]:
                rpt.cases.append(CaseResult(case="success", outcome="error",
                                            error_text="payment form not found", final_url=found.get("url")))
                rpt.issues = summarize(rpt)
                return rpt
            # 2. Replay for each case with a fresh identity.
            success_url = unlock_url = None
            for case in cases:
                if case == "bypass":
                    if success_url:
                        rpt.cases.append(await _bypass_case(browser, rpt.journey, success_url, unlock_url, run_id,
                                                            out_dir, unlock_selector))
                    continue
                res, s_url, u_url = await _card_case(browser, case, rpt, rpt.journey, run_id, out_dir,
                                                     unlock_selector, unlock_url)
                if case == "success":
                    success_url, unlock_url = s_url, u_url or s_url
                rpt.cases.append(res)
        finally:
            await browser.close()
    if stripe_key:
        rpt.stripe = verify_with_stripe(stripe_key, since, [])
    rpt.issues = summarize(rpt)
    return rpt
