"""Parallel Smart UI: Chaos + Critic + personas in isolated Playwright contexts.

Deterministic scripts (not LLM). Brainbase Tester should shell this after the UI crawl.
Local CLI: `python -m launchproof run URL --smart-ui` or this module directly.

  python -m launchproof.smart_ui_parallel --url https://site --run-id ID --out runs/ID
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright

from .smart_ui import budget_from_env, merge_into_ui

UA = "LaunchproofSmartUI/1.0 (+https://launchproof.xyz/bot)"


def _pages_from_run(out: Path, start: str, limit: int, extra: list[str] | None = None) -> list[str]:
    urls: list[str] = []
    host = urlparse(start).netloc
    for u in extra or []:
        if u and urlparse(u).netloc == host and u not in urls:
            urls.append(u)
    for name in ("run.json", "report.json"):
        f = out / name
        if not f.is_file():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        ui = data.get("ui") or data
        for p in ui.get("pages") or []:
            u = p.get("url") if isinstance(p, dict) else None
            if u and urlparse(u).netloc == host and u not in urls:
                urls.append(u)
        if urls:
            break
    return (urls or [start])[:limit]


async def _chaos(context, url: str, max_n: int, out: Path) -> dict:
    issues, states, log = [], [], []
    if max_n <= 0:
        return {"chaos_issues": [], "interesting_states": [], "scenarios_run": 0, "log": ["skipped"]}
    page = await context.new_page()
    n = 0
    try:
        # 1 rage CTA before idle
        if n < max_n:
            n += 1
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            btn = page.locator("a.btn, button.btn, a:has-text('Sign up'), a:has-text('Get started')").first
            if await btn.count():
                await btn.click(timeout=3000, no_wait_after=True)
                await btn.click(timeout=3000, no_wait_after=True)
                await page.wait_for_timeout(400)
                log.append("rage_cta")
        # 2 nav spam
        if n < max_n:
            n += 1
            links = page.locator("nav a, .nav-links a")
            count = await links.count()
            for i in range(min(4, count)):
                try:
                    await links.nth(i).click(timeout=1500, no_wait_after=True)
                except Exception:
                    pass
            await page.wait_for_timeout(500)
            log.append("nav_spam")
        # 3 double form submit
        if n < max_n:
            n += 1
            signup = urljoin(url, "/signup")
            await page.goto(signup, wait_until="domcontentloaded", timeout=20000)
            email = page.locator("input[type=email]").first
            pwd = page.locator("input[type=password]").first
            if await email.count() and await pwd.count():
                await email.fill("chaos@example.com")
                await pwd.fill("ChaosTest1!")
                submit = page.locator("button[type=submit], input[type=submit]").first
                if await submit.count():
                    await submit.click(no_wait_after=True)
                    await submit.click(no_wait_after=True)
                    await page.wait_for_timeout(800)
                    shot = f"chaos-double-submit.png"
                    try:
                        await page.screenshot(path=str(out / shot), full_page=False)
                    except Exception:
                        shot = None
                    # Heuristic: two success markers or no disabled state after first click
                    disabled = await page.locator("button[type=submit][disabled]").count()
                    if disabled == 0:
                        issues.append({
                            "severity": "medium", "kind": "chaos_double_submit", "where": signup,
                            "detail": "Submit stayed clickable; double-click was accepted without debounce.",
                            "shot": shot,
                        })
                        states.append({"url": page.url, "viewport": "desktop",
                                       "why": "double_submit", "evidence": "no disabled after submit"})
            log.append("double_submit")
    except Exception as ex:
        log.append(f"chaos error: {ex}")
        issues.append({"severity": "low", "kind": "chaos_crash", "where": url,
                       "detail": str(ex)[:200], "shot": None})
    finally:
        await page.close()
    return {"chaos_issues": issues, "interesting_states": states, "scenarios_run": n, "log": log}


async def _critic(context, pages: list[str], max_views: int, out: Path) -> dict:
    issues, log = [], []
    if max_views <= 0:
        return {"visual_issues": [], "appeal_score": None, "style": None, "views_used": 0, "log": ["skipped"]}
    views = 0
    scores: list[int] = []
    style_votes: dict[str, int] = {}
    page = await context.new_page()
    try:
        for u in pages:
            if views >= max_views:
                break
            for vp_name, vp in (("desktop", {"width": 1440, "height": 900}),
                                ("phone", {"width": 390, "height": 844})):
                if views >= max_views:
                    break
                views += 1
                await page.set_viewport_size(vp)
                await page.goto(u, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(300)
                metrics = await page.evaluate("""() => {
                  const cs = getComputedStyle(document.body);
                  const btns = [...document.querySelectorAll('a,button')].filter(e => e.offsetParent);
                  const borders = [...document.querySelectorAll('*')].slice(0, 80)
                    .filter(e => getComputedStyle(e).borderWidth && parseFloat(getComputedStyle(e).borderWidth) >= 2).length;
                  return {
                    bg: cs.backgroundColor, font: cs.fontFamily,
                    btnCount: btns.length, hardBorders: borders,
                    textLen: (document.body.innerText || '').length
                  };
                }""")
                score = 55
                style = "unknown"
                if metrics.get("hardBorders", 0) >= 8:
                    style = "Neo-brutalism"
                    score += 15
                elif "serif" in (metrics.get("font") or "").lower():
                    style = "Editorial serif"
                    score += 5
                if metrics.get("btnCount", 0) == 0:
                    issues.append({"severity": "medium", "kind": "visual_cta", "where": f"{u} ({vp_name})",
                                   "detail": "No visible primary actions", "shot": None})
                    score -= 15
                if metrics.get("textLen", 0) < 40:
                    issues.append({"severity": "medium", "kind": "visual_blank", "where": f"{u} ({vp_name})",
                                   "detail": "Very little visible text", "shot": None})
                    score -= 20
                scores.append(max(0, min(100, score)))
                style_votes[style] = style_votes.get(style, 0) + 1
                log.append(f"{vp_name}:{u}:{style}:{score}")
    except Exception as ex:
        log.append(f"critic error: {ex}")
    finally:
        await page.close()
    style = max(style_votes, key=style_votes.get) if style_votes else None
    appeal = int(sum(scores) / len(scores)) if scores else None
    return {"visual_issues": issues, "appeal_score": appeal, "style": style,
            "views_used": views, "log": log}


async def _personas(context, url: str, out: Path) -> dict:
    issues, log = [], []

    async def skimmer():
        p = await context.new_page()
        try:
            await p.goto(url, wait_until="domcontentloaded", timeout=20000)
            await p.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.4)")
            await p.wait_for_timeout(200)
            pricing = p.locator("a[href*='pricing'], a:has-text('Pricing')").first
            if await pricing.count():
                await pricing.click(timeout=5000)
            log.append("skimmer ok")
        except Exception as ex:
            issues.append({"severity": "low", "kind": "persona_skimmer_fail", "where": url,
                           "detail": str(ex)[:160], "shot": None})
        finally:
            await p.close()

    async def impatient():
        p = await context.new_page()
        try:
            await p.goto(url, wait_until="commit", timeout=20000)
            cta = p.locator("a.btn, a:has-text('Sign up')").first
            if await cta.count():
                await cta.click(timeout=3000, no_wait_after=True)
                await cta.click(timeout=3000, no_wait_after=True)
            await p.go_back(timeout=5000)
            await p.go_forward(timeout=5000)
            log.append("impatient ok")
        except Exception as ex:
            issues.append({"severity": "low", "kind": "persona_impatient_fail", "where": url,
                           "detail": str(ex)[:160], "shot": None})
        finally:
            await p.close()

    async def mobile():
        p = await context.new_page()
        try:
            await p.set_viewport_size({"width": 390, "height": 844})
            await p.goto(url, wait_until="domcontentloaded", timeout=20000)
            overflow = await p.evaluate(
                "() => document.documentElement.scrollWidth > Math.min(390, document.documentElement.clientWidth) + 1")
            if overflow:
                shot = "persona-mobile-overflow.png"
                try:
                    await p.screenshot(path=str(out / shot), full_page=False)
                except Exception:
                    shot = None
                issues.append({"severity": "medium", "kind": "persona_mobile_fail", "where": url,
                               "detail": "Mobile persona saw horizontal overflow on first paint.",
                               "shot": shot})
            log.append("mobile ok")
        except Exception as ex:
            issues.append({"severity": "low", "kind": "persona_mobile_fail", "where": url,
                           "detail": str(ex)[:160], "shot": None})
        finally:
            await p.close()

    await asyncio.gather(skimmer(), impatient(), mobile())
    return {"persona_issues": issues, "log": log}


async def run_smart_ui_parallel(url: str, out_dir: Path, budget: dict | None = None,
                                pages: list[str] | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    b = budget_from_env(budget)
    page_list = _pages_from_run(out_dir, url, b["max_pages_hint"], extra=pages)
    payload = {
        "status": "done",
        "budget": b,
        "spent": {"chaos_scenarios": 0, "vision_views": 0},
        "appeal_score": None,
        "style": None,
        "chaos_issues": [],
        "visual_issues": [],
        "interesting_states": [],
        "persona_issues": [],
        "notes": "scripted parallel Smart UI (not LLM)",
    }
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            chaos_ctx = await browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
            critic_ctx = await browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900})
            persona_ctx = await browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
            chaos_r, critic_r, persona_r = await asyncio.gather(
                _chaos(chaos_ctx, url, b["max_chaos_scenarios"], out_dir),
                _critic(critic_ctx, page_list, b["max_vision_views"], out_dir),
                _personas(persona_ctx, url, out_dir),
                return_exceptions=True,
            )
            if isinstance(chaos_r, Exception):
                payload["chaos_issues"] = [{"severity": "low", "kind": "chaos_crash", "where": url,
                                            "detail": str(chaos_r)[:200], "shot": None}]
            else:
                payload["chaos_issues"] = chaos_r.get("chaos_issues") or []
                payload["interesting_states"] = chaos_r.get("interesting_states") or []
                payload["spent"]["chaos_scenarios"] = chaos_r.get("scenarios_run") or 0
                payload["scenarios_run"] = payload["spent"]["chaos_scenarios"]
            if isinstance(critic_r, Exception):
                payload["notes"] = f"critic failed: {critic_r}"
            else:
                payload["visual_issues"] = critic_r.get("visual_issues") or []
                payload["appeal_score"] = critic_r.get("appeal_score")
                payload["style"] = critic_r.get("style")
                payload["spent"]["vision_views"] = critic_r.get("views_used") or 0
                payload["views_used"] = payload["spent"]["vision_views"]
            if isinstance(persona_r, Exception):
                payload["persona_issues"] = [{"severity": "low", "kind": "persona_skimmer_fail",
                                              "where": url, "detail": str(persona_r)[:200], "shot": None}]
            else:
                payload["persona_issues"] = persona_r.get("persona_issues") or []
            # Fold persona issues into visual_issues so merge_into_ui picks them up.
            payload["visual_issues"] = list(payload["visual_issues"]) + list(payload["persona_issues"])
        finally:
            await browser.close()

    (out_dir / "smart_ui.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    run_path = out_dir / "run.json"
    if run_path.is_file():
        try:
            record = json.loads(run_path.read_text(encoding="utf-8"))
            ui = record.get("ui") or {"issues": [], "pages": [], "start_url": url}
            record["ui"] = merge_into_ui(ui, payload)
            record["smart_ui"] = record["ui"].get("smart_ui")
            run_path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
            from .report import build_report
            await build_report(record, out_dir)
        except Exception:
            pass
    return payload


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", required=True)
    ap.add_argument("--run-id", default="smart")
    ap.add_argument("--out", required=True, help="runs/<id> directory")
    ap.add_argument("--max-chaos", type=int)
    ap.add_argument("--max-vision", type=int)
    a = ap.parse_args(argv)
    ov = {}
    if a.max_chaos is not None:
        ov["max_chaos_scenarios"] = a.max_chaos
    if a.max_vision is not None:
        ov["max_vision_views"] = a.max_vision
    out = Path(a.out)
    result = asyncio.run(run_smart_ui_parallel(a.url, out, ov or None))
    print(json.dumps({"status": result.get("status"), "appeal_score": result.get("appeal_score"),
                      "style": result.get("style"),
                      "chaos": len(result.get("chaos_issues") or []),
                      "visual": len(result.get("visual_issues") or [])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
