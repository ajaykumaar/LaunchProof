"""Score, fix prompts, HTML report and the 1200x630 share card.

Score out of 100 = UI 35 + Payments 35 + Load 30. Parts that were not run are left out and the
score is rescaled, so a UI-only free run still gets a fair number (and says which parts were skipped).
"""
from __future__ import annotations

import html
import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

SEV_POINTS = {"critical": 10, "high": 5, "medium": 3, "low": 1}
LOAD_STEPS = [(800, 30), (400, 25), (200, 20), (100, 15), (50, 10), (25, 6), (10, 3)]


# ---------- issues ----------

def dedupe_ui(issues: list[dict]) -> list[dict]:
    """Phone and desktop often report the same problem on the same URL: merge them."""
    merged: dict[tuple, dict] = {}
    for i in issues:
        url = i["where"].split(" (")[0]
        key = (i["kind"], url)
        vp = i["where"][len(url):].strip(" ()") or None
        if key in merged:
            if vp and vp not in merged[key]["viewports"]:
                merged[key]["viewports"].append(vp)
        else:
            merged[key] = {**i, "where": url, "viewports": [vp] if vp else []}
    return list(merged.values())


def load_issues(load: dict | None) -> list[dict]:
    if not load:
        return []
    out = []
    for issue in load.get("issues") or []:
        if isinstance(issue, dict) and issue.get("kind"):
            out.append(issue)
    for region, r in (load.get("regions") or {}).items():
        for issue in r.get("issues") or []:
            if isinstance(issue, dict) and issue.get("kind"):
                out.append(issue)
        bp = r.get("break_point_users")
        if not bp:
            continue
        last = (r.get("stages") or [{}])[-1]
        sev = "critical" if bp <= 50 else "high" if bp <= 200 else "medium"
        errs = "; ".join(f"{k} (x{v})" for k, v in (last.get("top_errors") or {}).items())
        out.append({"severity": sev, "kind": "load_break", "where": f"{r.get('url')} from {region}",
                    "detail": f"Broke at {bp} concurrent users: {r.get('stop_reason', '').split(': ', 1)[-1]}. "
                              f"p95 {last.get('p95_ms')} ms, errors {last.get('error_rate', 0):.0%}. {errs}".strip(),
                    "paths": r.get("paths"), "shot": None})
    return out


# ---------- score ----------

def score(ui: dict | None, pay: dict | None, load: dict | None) -> dict:
    parts, skipped = {}, []
    if ui is not None:
        ded = dedupe_ui(ui.get("issues", []))
        lows = sum(1 for i in ded if i["severity"] == "low")
        pts = sum(SEV_POINTS[i["severity"]] for i in ded if i["severity"] != "low") + min(lows, 5)
        parts["ui"] = (max(0, 35 - pts), 35)
    else:
        skipped.append("ui")
    if pay is not None:
        by = {c["case"]: c for c in pay.get("cases", [])}
        s, d, b = by.get("success", {}), by.get("decline"), by.get("bypass")
        p = 0
        p += 10 if s.get("reached_checkout") else 0
        p += 10 if s.get("outcome") == "paid" else 0
        p += 5 if s.get("unlocked") else 0
        p += 5 if (d and d.get("outcome") == "declined_shown" and not d.get("unlocked")) else 0
        p += 5 if (b and b.get("outcome") == "not_unlocked") else 0
        if b and b.get("unlocked"):
            p = min(p, 15)  # anyone can get the paid plan for free: payments cannot score well
        parts["payments"] = (p, 35)
    else:
        skipped.append("payments")
    if load is not None:
        survived = load.get("combined", {}).get("survived_users_per_region") or \
            min((r.get("survived_users", 0) for r in (load.get("regions") or {}).values()), default=0)
        parts["load"] = (next((pts for users, pts in LOAD_STEPS if survived >= users), 0), 30)
    else:
        skipped.append("load")
    got = sum(v[0] for v in parts.values())
    of = sum(v[1] for v in parts.values()) or 1
    total = round(100 * got / of)
    grade = "A" if total >= 90 else "B" if total >= 75 else "C" if total >= 60 else "D" if total >= 40 else "F"
    return {"total": total, "grade": grade, "parts": {k: {"got": v[0], "of": v[1]} for k, v in parts.items()},
            "skipped": skipped}


# ---------- fix prompts ----------

TEMPLATES = {
    "horizontal_overflow": "On {where}, the page scrolls sideways on a 390px-wide phone. The elements sticking out are: {detail_tail}. "
                           "Make this page responsive: no element may be wider than the viewport. Wrap wide tables in a container with "
                           "overflow-x:auto, replace fixed pixel widths with max-width:100% or responsive units, and let flex rows wrap. "
                           "Check at 390px width that document.documentElement.scrollWidth equals window.innerWidth.",
    "console_errors": "On {where}, the browser console shows these errors: {detail}. Find the code that causes each one and fix it. "
                      "If a third-party script (analytics, chat widget) is not loaded yet, guard the call (e.g. window.analytics?.track) "
                      "or load the script before calling it. The page must load with zero console errors.",
    "failed_requests": "On {where}, these requests fail: {detail}. For each one, either add the missing file or route, fix the URL, "
                       "or remove the reference. Nothing the page requests on load should return 4xx or 5xx.",
    "broken_images": "On {where}, these images do not load: {detail}. Add the missing image files (or fix their paths) and give every "
                     "<img> an alt text and explicit width/height.",
    "blank_page": "On {where}, the page renders almost nothing (blank or stuck loading). Check for a crash during render, a failed data "
                  "fetch with no fallback, or a missing environment variable in production. Show a real error state instead of a blank screen.",
    "http_error": "{where} returns {detail}. Fix the route or the server error so it returns 200, or remove links pointing to it.",
    "page_failed": "{where} failed to load: {detail}. Make sure the page loads within 20 seconds on a normal connection.",
    "slow_page": "{where} took {detail_tail} to load. Find what blocks the load event (large images, render-blocking scripts, slow API "
                 "calls during server render) and get it under 3 seconds.",
    "small_tap_targets": "On {where} (phone), {detail}. Make every button and link at least 44x44 px on mobile (padding, not just font size).",
    "missing_title": "Add a descriptive <title> to {where}.",
    "missing_description": "Add a <meta name=\"description\"> and Open Graph tags (og:title, og:description, og:image) to {where} so "
                           "links shared on X, LinkedIn and Slack show a proper preview.",
    "missing_favicon": "Add a favicon (<link rel=\"icon\" href=\"/favicon.ico\">) plus an apple-touch-icon to {where}.",
    "security_headers": "Add these HTTP response headers to every page: {detail_tail}. Recommended values: Strict-Transport-Security: "
                        "max-age=31536000; includeSubDomains, X-Content-Type-Options: nosniff, X-Frame-Options: DENY, Referrer-Policy: "
                        "strict-origin-when-cross-origin, and a Content-Security-Policy that allows only the domains you use.",
    "checkout_unreachable": "A new visitor to {where} cannot find a way to pay. Make the path obvious: a clear Upgrade/Buy button on the "
                            "pricing page and after signup, leading straight to Stripe Checkout.",
    "checkout_failed": "Paying with Stripe's test card 4242 4242 4242 4242 fails on {where}: {detail}. Check the Checkout Session "
                       "creation (price id, mode, success_url and cancel_url are absolute URLs) and the Stripe keys for this environment.",
    "no_unlock": "After a successful test payment the customer lands on {where} but the paid feature is not unlocked. Handle the "
                 "checkout.session.completed webhook: verify the signature, look up the user by client_reference_id, set their plan, "
                 "and make the success page read the plan from your database.",
    "decline_silent": "When a card is declined on {where} the customer sees no error. Show the error message Stripe returns "
                      "(error.message from confirmPayment) next to the card field and keep the form usable.",
    "decline_unlocked": "A declined card still unlocked the paid plan on {where}. Only grant access after Stripe confirms payment "
                        "(checkout.session.completed with payment_status=paid, or payment_intent.succeeded).",
    "paywall_bypass": "Critical: opening {where} with a made-up session_id unlocks the paid plan without paying. Never grant access "
                      "because the browser reached the success URL. In the success handler, retrieve the Checkout Session from Stripe "
                      "with your secret key, check payment_status == 'paid' and that client_reference_id matches the signed-in user, "
                      "and grant access in the checkout.session.completed webhook as the source of truth.",
    "signup_unreachable": "A new visitor to {where} cannot find Sign up / Create account. Add a clear signup CTA in the nav and hero "
                          "that leads to a form with email and password (or magic link).",
    "signup_no_form": "On {where}, the signup entry point loads but there is no usable email+password form: {detail}. Expose labeled "
                      "email and password fields (and name if needed) so a first-time user can register.",
    "signup_failed": "Signup submit failed on {where}: {detail}. Make the primary Sign up button work and return a clear success or error state.",
    "signup_no_feedback": "After submitting signup on {where}: {detail}. On success show a welcome / verify-email state (or "
                          "data-lp-signup=\"ok\" for tests). On failure show the validation or server error next to the form.",
    "signup_validation_weak": "On {where}, submitting an empty signup form does not show required-field validation: {detail}. Mark "
                              "email/password as required and surface native or custom error messages before calling the API.",
    "stripe_backend": "Stripe reports: {detail}. Fix the webhook endpoint (correct URL, returns 2xx within 10 seconds, verifies the "
                      "signature with the right signing secret for this mode).",
    "load_break": "Under load, {where} {detail_lower} The failing paths were {paths}. Likely causes: too few database connections, "
                  "no caching on read-heavy endpoints, or a single small server. Add a connection pooler, cache this response "
                  "(even 10 seconds helps), and move slow work out of the request. Then re-run the load test.",
    # Smart UI — Chaos (messy human)
    "chaos_double_submit": "On {where}, a messy visitor can trigger duplicate submits: {detail}. Debounce the submit button "
                           "(disable on first click), ignore duplicate POSTs server-side with an idempotency key, and show a single "
                           "in-progress state until the response returns.",
    "chaos_race_click": "On {where}, clicking the primary action before the page finishes loading causes: {detail}. Guard handlers "
                        "until hydration/networkidle, or queue a single navigation and ignore further clicks until it settles.",
    "chaos_nav_spam": "On {where}, rapid nav clicks while loading cause: {detail}. Abort in-flight fetches on route change, cancel "
                      "pending transitions, and keep a stable loading state so spam clicks cannot stack navigations.",
    "chaos_disabled_click": "On {where}, clicking a loading or disabled control still fires work: {detail}. Make disabled buttons "
                            "non-interactive (pointer-events and no handler), and ignore events while aria-busy is true.",
    "chaos_history": "On {where}, Back/Forward mid-flow breaks state: {detail}. Persist wizard/step state in the URL or sessionStorage, "
                     "and restore it on pageshow/popstate instead of rendering a blank or half-initialized screen.",
    "chaos_crash": "On {where}, messy interaction crashed or blanked the UI: {detail}. Add an error boundary, catch unhandled promise "
                   "rejections from racey clicks, and show a recoverable error state instead of a white screen.",
    # Smart UI — Critic (visual appeal)
    "visual_hierarchy": "On {where}, visual hierarchy is weak: {detail}. Make one clear H1, reduce competing sizes/weights, and put the "
                        "primary message above secondary claims so a first-time visitor knows what matters.",
    "visual_contrast": "On {where}, contrast/readability fails: {detail}. Raise text/background contrast to at least WCAG AA, and avoid "
                       "light grey body copy on pale backgrounds.",
    "visual_clutter": "On {where}, the layout feels cluttered: {detail}. Remove or collapse secondary blocks, add consistent spacing, "
                      "and leave one clear focal area above the fold.",
    "visual_cta": "On {where}, the primary call to action is unclear: {detail}. Use one high-contrast primary button with a concrete "
                  "label (Get started / Buy / Upgrade) and demote secondary actions to ghost/text buttons.",
    "visual_mobile": "On {where} (phone), density or stacking is hard to use: {detail}. Increase tap targets to 44px, stack sections "
                     "single-column, and reduce competing sticky bars.",
    "visual_polish": "On {where}, polish issues hurt trust: {detail}. Fix alignment, replace placeholder copy/images, and make spacing "
                     "and corner radii consistent across the page.",
    "visual_broken_layout": "On {where}, the layout is broken: {detail}. Fix overlapping or cut-off elements at this viewport; check "
                            "absolute positioning, z-index, and overflow hidden that clips content.",
    "visual_style_incoherent": "On {where}, the UI mixes design languages without a clear primary style: {detail}. Pick one system "
                               "(type, color, radius, borders, imagery) and apply it end-to-end; demote or remove the conflicting cues.",
    "visual_style_generic": "On {where}, the look reads as an interchangeable template: {detail}. Add a stronger brand signal "
                            "(custom type pairing, distinctive color, original imagery) so it does not look like every other SaaS/AI landing page.",
    "visual_style_dated": "On {where}, the execution feels behind current product-UI taste: {detail}. Either refresh toward a coherent "
                          "contemporary direction, or lean into a deliberate retro choice and make that commitment obvious.",
    "visual_style_mismatch": "On {where}, details fight the apparent style: {detail}. Align borders, shadows, type, and color with the "
                             "named design language, or rename/reframe the style and restyle consistently.",
    # Launch-day concurrency
    "thundering_herd": "A synchronized burst against {where} failed while a ramped load held: {detail}. Add queueing, shedding, "
                       "or warm capacity for launch spikes; do not only test gradual ramps.",
    "race_condition_exploit": "Concurrent identical requests to {where} produced multiple success outcomes: {detail}. "
                              "Serialize claims with a transaction/unique constraint so exactly one winner is recorded.",
    "race_inconclusive": "Race probe on {where} returned ambiguous 2xx bodies: {detail}. Add clear created vs conflict responses.",
    "signup_storm_errors": "Concurrent signups against {where} errored: {detail}. Harden the create-account path under launch traffic.",
    "signup_rate_limit": "Signup storm hit rate limits on {where}: {detail}. Confirm limits are intentional and return clear UX.",
    "signup_duplicate_collision": "Two concurrent signups with the same email both succeeded on {where}: {detail}. "
                                  "Enforce uniqueness at the database layer, not only in the UI.",
    "persona_skimmer_fail": "Skimmer persona failed on {where}: {detail}. Ensure primary nav and pricing links work on first visit.",
    "persona_impatient_fail": "Impatient persona failed on {where}: {detail}. Guard early clicks and history navigation in the hero funnel.",
    "persona_mobile_fail": "Mobile persona hit a problem on {where}: {detail}. Fix phone layout / overflow for first paint.",
    "visual_blank": "On {where}: {detail}. Ensure the page renders meaningful content above the fold.",
    "launch_cross_link": "UI and backend both show a double-submit / race story on {where}: {detail}. Fix client debounce and "
                         "server idempotency together so launch-day spam cannot create duplicates.",
}


def fix_prompt(issue: dict) -> str:
    detail = issue.get("detail", "")
    tail = detail.split(": ", 1)[-1] if ": " in detail else detail.replace("Missing: ", "").replace("Loaded in ", "")
    t = TEMPLATES.get(issue["kind"], "Fix this issue on {where}: {detail}")
    return t.format(where=issue.get("where", ""), detail=detail, detail_tail=tail,
                    detail_lower=detail[0].lower() + detail[1:] if detail else "",
                    paths=", ".join(issue.get("paths") or []) or "the tested pages")


def claude_fix_prompts(issues: list[dict]) -> list[str] | None:
    """One call for all issues: Claude rewrites each template into a specific, paste-ready prompt."""
    if not os.getenv("ANTHROPIC_API_KEY") or not issues:
        return None
    try:
        from anthropic import Anthropic
        payload = [{"n": n, "kind": i["kind"], "where": i["where"], "evidence": i.get("detail", ""),
                    "draft": fix_prompt(i)} for n, i in enumerate(issues)]
        msg = Anthropic().messages.create(
            model=os.getenv("LP_MODEL", "claude-sonnet-5"), max_tokens=4000,
            system="You write fix prompts that a founder pastes into Cursor, Lovable, Bolt or Claude Code. "
                   "Each prompt: 2-5 sentences, names the exact page and evidence, says what done looks like. "
                   "No preamble, no em dashes. Return JSON: a list of strings in the same order.",
            messages=[{"role": "user", "content": json.dumps(payload)}])
        text = "".join(b.text for b in msg.content if b.type == "text")
        out = json.loads(text[text.index("["): text.rindex("]") + 1])
        if len(out) == len(issues):
            print(f"fix prompts: Claude rewrote {len(out)} prompts", flush=True)
            return out
        print(f"fix prompts: Claude returned {len(out)} prompts for {len(issues)} issues; using templates", flush=True)
        return None
    except Exception as ex:
        print(f"fix prompts: Claude failed ({ex}); using templates", flush=True)
        return None


# ---------- headline + share card ----------

def headline(sc: dict, ui: dict | None, pay: dict | None, load: dict | None, signup: dict | None = None) -> list[str]:
    bits = []
    kinds = {i.get("kind") for i in ((ui or {}).get("issues") or [])}
    if load:
        for i in load.get("issues") or []:
            kinds.add(i.get("kind"))
        for r in (load.get("regions") or {}).values():
            for i in (r.get("issues") or []):
                kinds.add(i.get("kind"))
    # Launch-day integrity first
    if "race_condition_exploit" in kinds or "signup_duplicate_collision" in kinds:
        bits.append("Race / duplicate claim under concurrency")
    if "thundering_herd" in kinds:
        bits.append("Thundering herd: burst fails, ramp holds")
    if pay:
        by = {c["case"]: c for c in pay.get("cases", [])}
        if by.get("bypass", {}).get("unlocked"):
            bits.append("Paywall can be bypassed")
        elif by.get("success", {}).get("outcome") == "paid" and by["success"].get("unlocked"):
            bits.append("Checkout works")
        else:
            bits.append("Checkout broken")
    if load:
        regions = load.get("regions") or {}
        n = len(regions)
        survived = min((r.get("survived_users", 0) for r in regions.values()), default=0)
        broke = [r["break_point_users"] for r in regions.values() if r.get("break_point_users")]
        bits.append(f"Survived {survived} users{' per region' if n > 1 else ''}" + (f" from {n} regions" if n > 1 else "")
                    + (f", broke at {min(broke)}" if broke else ""))
    if signup:
        if signup.get("success"):
            bits.append("Signup works")
        elif signup.get("reached_form"):
            bits.append("Signup broken")
        else:
            bits.append("Signup not found")
    if "chaos_double_submit" in kinds and (
            "race_condition_exploit" in kinds or "signup_duplicate_collision" in kinds):
        bits.append("UI debounce gap + backend race")
    if ui:
        n = len(dedupe_ui(ui.get("issues", [])))
        bits.append(f"{n} UI issue{'s' if n != 1 else ''}" if n else "No UI issues")
        appeal = ui.get("appeal_score")
        if appeal is None and isinstance(ui.get("smart_ui"), dict):
            appeal = ui["smart_ui"].get("appeal_score")
        if appeal is not None:
            bits.append(f"Visual appeal {int(appeal)}/100")
        style = ui.get("style")
        if style is None and isinstance(ui.get("smart_ui"), dict):
            style = ui["smart_ui"].get("style")
        if style:
            bits.append(f"Style: {style}")
    return bits


CARD_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
*{{margin:0;box-sizing:border-box}}body{{width:1200px;height:630px;font-family:Inter,system-ui,-apple-system,sans-serif;
background:#0B0D12;color:#F4F5F7;padding:64px 72px;display:flex;flex-direction:column;justify-content:space-between}}
.top{{display:flex;justify-content:space-between;align-items:center;font-size:26px;color:#9AA3AF;letter-spacing:.02em}}
.brand b{{color:#F4F5F7}}.site{{font-size:64px;font-weight:700;letter-spacing:-.02em;margin-top:8px;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap;max-width:760px}}
.row{{display:flex;align-items:flex-end;justify-content:space-between}}
.score{{font-size:190px;font-weight:800;line-height:.85;color:{color}}}.score small{{font-size:48px;color:#6B7280;font-weight:600}}
ul{{list-style:none;font-size:34px;line-height:1.55}}li:before{{content:"";display:inline-block;width:14px;height:14px;
border-radius:50%;background:{color};margin-right:18px;vertical-align:middle}}
.foot{{font-size:22px;color:#6B7280}}</style></head><body>
<div><div class="top"><span class="brand"><b>Launchproof</b> launch-day test</span><span>{date}</span></div>
<div class="site">{site}</div></div>
<div class="row"><ul>{items}</ul><div class="score">{score}<small>/100</small></div></div>
<div class="foot">UI on phone + desktop · real test checkout · load from {regions}</div></body></html>"""


async def share_card(out_png: Path, site: str, sc: dict, bits: list[str], regions: str) -> Path:
    from playwright.async_api import async_playwright
    color = "#34D399" if sc["total"] >= 80 else "#FBBF24" if sc["total"] >= 60 else "#F87171"
    doc = CARD_HTML.format(color=color, site=html.escape(site), date=datetime.now().strftime("%b %d, %Y"),
                           items="".join(f"<li>{html.escape(b)}</li>" for b in bits[:3]), score=sc["total"],
                           regions=html.escape(regions))
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        p = await b.new_page(viewport={"width": 1200, "height": 630})
        await p.set_content(doc)
        await p.screenshot(path=str(out_png))
        await b.close()
    return out_png


# ---------- HTML report ----------

REPORT_CSS = """
:root{--bg:#F7F7F8;--card:#fff;--ink:#111318;--muted:#5E6470;--line:#E6E7EA;--crit:#DC2626;--high:#EA580C;--med:#CA8A04;--low:#6B7280;--ok:#059669}
@media (prefers-color-scheme:dark){:root{--bg:#0B0D12;--card:#141821;--ink:#F4F5F7;--muted:#9AA3AF;--line:#262B36}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 Inter,system-ui,-apple-system,sans-serif}
main{max-width:980px;margin:0 auto;padding:32px 16px 80px}h1{font-size:30px;margin:0 0 4px}h2{font-size:20px;margin:36px 0 12px}
.muted{color:var(--muted)}.hero{display:grid;grid-template-columns:1fr auto;gap:24px;align-items:center}
.big{font-size:84px;font-weight:800;line-height:1}.big small{font-size:24px;color:var(--muted)}
.parts{display:flex;gap:12px;flex-wrap:wrap;margin-top:14px}.pill{background:var(--card);border:1px solid var(--line);border-radius:999px;padding:6px 14px;font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:12px 0}
.sev{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;padding:2px 8px;border-radius:6px;color:#fff}
.critical{background:var(--crit)}.high{background:var(--high)}.medium{background:var(--med)}.low{background:var(--low)}
.kind{font-weight:600;margin-left:8px}.where{font-size:14px;color:var(--muted);word-break:break-all}
pre{white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:12px;font:13px/1.5 ui-monospace,Menlo,monospace;margin:10px 0 0}
button{font:inherit;font-size:13px;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:6px;padding:4px 10px;cursor:pointer;margin-top:8px}
img.shot{max-width:100%;max-height:420px;object-fit:cover;object-position:top;border:1px solid var(--line);border-radius:8px;margin-top:10px}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left}
.tablewrap{overflow-x:auto}.share img{max-width:100%;border-radius:12px;border:1px solid var(--line)}
@media (max-width:640px){.hero{grid-template-columns:1fr}.big{font-size:64px}}
"""


def render_html(run: dict, issues: list[dict], prompts: list[str], sc: dict, bits: list[str], card_name: str | None) -> str:
    e = html.escape
    site = urlparse(run["url"]).netloc
    parts = "".join(f'<span class="pill">{ {"ui": "UI"}.get(k, k.title())} {v["got"]}/{v["of"]}</span>' for k, v in sc["parts"].items())
    skipped = f'<p class="muted">Not run: {", ".join(sc["skipped"])}.</p>' if sc["skipped"] else ""
    ui = run.get("ui") or {}
    appeal = ui.get("appeal_score")
    if appeal is None and isinstance(ui.get("smart_ui"), dict):
        appeal = ui["smart_ui"].get("appeal_score")
    style = ui.get("style")
    if style is None and isinstance(ui.get("smart_ui"), dict):
        style = ui["smart_ui"].get("style")
    appeal_bits = []
    if appeal is not None:
        appeal_bits.append(f"Visual appeal: <b>{int(appeal)}/100</b> (Critic)")
    if style:
        appeal_bits.append(f"Style: <b>{e(str(style))}</b>")
    appeal_html = f'<p class="muted">{" · ".join(appeal_bits)}</p>' if appeal_bits else ""
    cards = []
    for n, (i, p) in enumerate(zip(issues, prompts)):
        shot = f'<img class="shot" loading="lazy" src="{e(i["shot"])}" alt="screenshot">' if i.get("shot") else ""
        vps = f' ({", ".join(i["viewports"])})' if i.get("viewports") else ""
        cards.append(f'''<div class="card"><span class="sev {i["severity"]}">{i["severity"]}</span><span class="kind">{e(i["kind"].replace("_", " "))}</span>
<div class="where">{e(i["where"])}{e(vps)}</div><p>{e(i.get("detail", ""))}</p>
<details><summary>Fix prompt (paste into your AI coding tool)</summary><pre id="p{n}">{e(p)}</pre>
<button onclick="navigator.clipboard.writeText(document.getElementById('p{n}').innerText);this.innerText='Copied'">Copy</button></details>{shot}</div>''')
    load_rows = ""
    for region, r in ((run.get("load") or {}).get("regions") or {}).items():
        for s in r.get("stages", []):
            load_rows += (f"<tr><td>{e(region)}</td><td>{s['users']}</td><td>{s['rps']}</td><td>{s['p50_ms']}</td>"
                          f"<td>{s['p95_ms']}</td><td>{s['error_rate']:.1%}</td><td>{e(s.get('reason') or '')}</td></tr>")
    load_tbl = (f'<h2>Load test</h2><div class="tablewrap"><table><tr><th>Region</th><th>Users</th><th>Req/s</th><th>p50 ms</th>'
                f'<th>p95 ms</th><th>Errors</th><th>Break</th></tr>{load_rows}</table></div>') if load_rows else ""
    # Launch-day extras (burst / race / storm) — additive summary
    launch_bits = []
    load = run.get("load") or {}
    for region, r in (load.get("regions") or {}).items():
        b = r.get("burst")
        if b:
            launch_bits.append(
                f"Burst {b.get('users')}× {e(str(b.get('path')))} "
                f"{'THUNDERING HERD' if b.get('thundering_herd') else 'held'} ({e(region)})")
    race = run.get("race") or load.get("race")
    if race:
        launch_bits.append(
            f"Race {e(str(race.get('method')))} {e(str(race.get('path')))}: "
            f"{race.get('successes')} success-shaped / {race.get('concurrency')}")
    storm = run.get("signup_storm")
    if storm:
        launch_bits.append(
            f"Signup storm n={storm.get('n')}: ok={storm.get('unique_ok')} "
            f"dup_successes={storm.get('duplicate_successes')}")
    launch_sec = ""
    if launch_bits:
        launch_sec = ("<h2>Launch day</h2><ul>" +
                      "".join(f"<li>{b}</li>" for b in launch_bits) + "</ul>")
    pay_rows = "".join(f"<tr><td>{e(c['case'])}</td><td>{e(c['outcome'])}</td><td>{'' if c.get('unlocked') is None else ('yes' if c['unlocked'] else 'no')}</td>"
                       f"<td>{e(c.get('error_text') or '')}</td><td>{c.get('seconds', '')}s</td></tr>"
                       for c in (run.get("payments") or {}).get("cases", []))
    pay_tbl = (f'<h2>Payment test</h2><div class="tablewrap"><table><tr><th>Case</th><th>Outcome</th><th>Unlocked</th><th>Message</th>'
               f'<th>Time</th></tr>{pay_rows}</table></div>') if pay_rows else ""
    share = f'<h2>Share card</h2><div class="share"><img src="{e(card_name)}" alt="share card"></div>' if card_name else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Launchproof report</title><meta property="og:title" content="{e(site)} scored {sc['total']}/100 on Launchproof">
<meta property="og:description" content="{e(' · '.join(bits))}">{f'<meta property="og:image" content="{e(card_name)}">' if card_name else ''}
<style>{REPORT_CSS}</style></head><body><main>
<div class="hero"><div><h1>{e(site)}</h1><div class="muted">{e(run['url'])} · run {e(run.get('run_id') or '')} · {e(run.get('finished_at') or '')}</div>
<div class="parts">{parts}</div>{skipped}<p>{e(' · '.join(bits))}</p>{appeal_html}</div><div class="big">{sc['total']}<small>/100</small></div></div>
<h2>What broke ({len(issues)})</h2>{''.join(cards) or '<p>Nothing. Ship it.</p>'}
{launch_sec}{pay_tbl}{load_tbl}{share}
<p class="muted" style="margin-top:40px">Launchproof ran a headless browser on phone and desktop, optional scripted Chaos/Critic/personas,
optional Stripe test checkout, and a labeled load test (User-Agent LaunchproofLoadTest/1.0) with optional synchronized burst and race probes.</p>
</main></body></html>"""


async def build_report(run: dict, out_dir: Path, prompts_override: list[str] | None = None) -> dict:
    from .smart_ui import merge_into_ui

    ui, pay, load = run.get("ui"), run.get("payments"), run.get("load")
    smart = run.get("smart_ui")
    smart_path = out_dir / "smart_ui.json"
    if smart is None and smart_path.is_file():
        try:
            smart = json.loads(smart_path.read_text())
        except Exception:
            smart = None
    if ui is not None and smart is not None:
        # Avoid double-merging if Tester already appended the same issues.
        already = {(i.get("kind"), i.get("where"), i.get("detail")) for i in (ui.get("issues") or [])}
        filtered = dict(smart)
        for key in ("chaos_issues", "visual_issues"):
            filtered[key] = [
                i for i in (smart.get(key) or [])
                if (i.get("kind"), i.get("where"), i.get("detail")) not in already
            ]
        ui = merge_into_ui(ui, filtered)
        run = {**run, "ui": ui, "smart_ui": ui.get("smart_ui")}

    issues = []
    if ui:
        issues += dedupe_ui(ui.get("issues", []))
    if pay:
        issues += pay.get("issues", [])
    issues += load_issues(load)
    kinds = {i.get("kind") for i in issues}
    if "chaos_double_submit" in kinds and (
            "race_condition_exploit" in kinds or "signup_duplicate_collision" in kinds):
        issues.append({
            "severity": "high", "kind": "launch_cross_link",
            "where": (run.get("url") or ""),
            "detail": "UI allowed double submit and backend accepted concurrent success-shaped claims. "
                      "Fix debounce and server idempotency together.",
            "shot": None,
        })
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    issues.sort(key=lambda i: order.get(i.get("severity"), 9))
    if prompts_override is not None:
        if len(prompts_override) == len(issues):
            prompts = [str(p) for p in prompts_override]  # Brainbase agent rewrite from evidence
        else:
            print(f"prompts_override length {len(prompts_override)} != issues {len(issues)}; using templates", flush=True)
            prompts = claude_fix_prompts(issues) or [fix_prompt(i) for i in issues]
    else:
        prompts = claude_fix_prompts(issues) or [fix_prompt(i) for i in issues]
    sc = score(ui, pay, load)
    bits = headline(sc, ui, pay, load, signup=run.get("signup"))
    regions = ", ".join((load or {}).get("regions", {}).keys()) or "not run"
    card = None
    try:
        card = await share_card(out_dir / "share-card.png", urlparse(run["url"]).netloc, sc, bits, regions)
    except Exception as ex:
        print(f"share card failed: {ex}")
    doc = render_html(run, issues, prompts, sc, bits, card.name if card else None)
    (out_dir / "report.html").write_text(doc)
    appeal = (ui or {}).get("appeal_score")
    if appeal is None and isinstance((ui or {}).get("smart_ui"), dict):
        appeal = ui["smart_ui"].get("appeal_score")
    result = {
        "score": sc,
        "headline": bits,
        "appeal_score": appeal,
        "smart_ui": (ui or {}).get("smart_ui") or run.get("smart_ui"),
        "signup": run.get("signup"),
        "issues": [{**i, "fix_prompt": p} for i, p in zip(issues, prompts)],
    }
    (out_dir / "report.json").write_text(json.dumps(result, indent=2))
    return result
