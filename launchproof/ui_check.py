"""UI and visual check: crawl a site like a first-time visitor on phone and desktop.

For every page and viewport we record: screenshot, console errors, failed requests, blank-page
detection, horizontal overflow, broken images, small tap targets (phone), load time, basic launch
polish (title, description, favicon) and security headers.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

from playwright.async_api import async_playwright

VIEWPORTS = {"phone": {"width": 390, "height": 844}, "desktop": {"width": 1440, "height": 900}}
UA = "LaunchproofUICheck/1.0 (+https://launchproof.xyz/bot)"
SKIP_LINK = re.compile(r"logout|log-out|signout|sign-out|delete|remove|unsubscribe|cancel|mailto:|tel:|javascript:", re.I)

PAGE_PROBE_JS = """
(deviceWidth) => {
  // Mobile Chrome zooms out to fit wide content, which inflates innerWidth. Compare to the device width.
  const vw = Math.min(deviceWidth, document.documentElement.clientWidth || deviceWidth);
  const body = document.body;
  const text = (body && body.innerText || '').trim();
  const overflowX = document.documentElement.scrollWidth > vw + 1;
  const wide = [];
  if (overflowX) {
    for (const el of document.querySelectorAll('body *')) {
      const r = el.getBoundingClientRect();
      if (r.right > vw + 1 && r.width > 0) { wide.push((el.tagName + (el.id ? '#' + el.id : '') + (el.className && typeof el.className === 'string' ? '.' + el.className.split(' ')[0] : '')).slice(0, 60)); if (wide.length >= 5) break; }
    }
  }
  const brokenImgs = [...document.images].filter(i => i.complete && i.naturalWidth === 0 && i.src).map(i => i.src).slice(0, 10);
  let smallTargets = 0;
  for (const el of document.querySelectorAll('a, button, [role=button], input[type=submit]')) {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && (r.width < 24 || r.height < 24)) smallTargets++;
  }
  const links = [...document.querySelectorAll('a[href]')].map(a => a.href);
  const meta = (n) => { const m = document.querySelector(`meta[name="${n}"]`) || document.querySelector(`meta[property="${n}"]`); return m ? m.content : null; };
  return {
    title: document.title || null,
    description: meta('description'),
    ogImage: meta('og:image'),
    favicon: !!document.querySelector('link[rel~="icon"]'),
    textLength: text.length,
    elementCount: document.querySelectorAll('body *').length,
    overflowX, wideElements: wide, brokenImages: brokenImgs, smallTargets, links,
  };
}
"""


@dataclass
class PageResult:
    url: str
    viewport: str
    status: int | None = None
    load_ms: int | None = None
    screenshot: str | None = None
    console_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    blank: bool = False
    overflow_x: bool = False
    wide_elements: list[str] = field(default_factory=list)
    broken_images: list[str] = field(default_factory=list)
    small_targets: int = 0
    title: str | None = None
    description: str | None = None
    favicon: bool = True
    error: str | None = None
    api_calls: list[str] = field(default_factory=list)  # same-site fetch/XHR GETs, reused as load-test paths


@dataclass
class UIReport:
    start_url: str
    pages: list[PageResult] = field(default_factory=list)
    security_headers: dict = field(default_factory=dict)
    issues: list[dict] = field(default_factory=list)

    def to_dict(self):
        return {"start_url": self.start_url, "pages": [asdict(p) for p in self.pages],
                "security_headers": self.security_headers, "issues": self.issues}


def _same_site(a: str, b: str) -> bool:
    return urlparse(a).netloc.lower() == urlparse(b).netloc.lower()


def _norm(u: str) -> str:
    u = urldefrag(u)[0]
    return u.rstrip("/") or u


async def _visit(context, url: str, viewport: str, out_dir: Path, idx: int) -> tuple[PageResult, list[str]]:
    res = PageResult(url=url, viewport=viewport)
    page = await context.new_page()
    page.on("console", lambda m: res.console_errors.append(m.text[:300]) if m.type == "error" else None)
    page.on("pageerror", lambda e: res.console_errors.append(f"Uncaught: {str(e)[:300]}"))

    def on_response(r):
        req = r.request
        if req.resource_type in ("fetch", "xhr") and req.method == "GET" and _same_site(r.url, url) \
                and r.url not in res.api_calls and len(res.api_calls) < 10:
            res.api_calls.append(r.url)
        if r.status >= 400:
            res.failed_requests.append(f"{r.status} {r.url[:200]}")

    page.on("response", on_response)
    page.on("requestfailed", lambda r: res.failed_requests.append(f"FAILED {r.url[:200]} ({r.failure})"))
    links: list[str] = []
    t0 = time.monotonic()
    try:
        resp = await page.goto(url, wait_until="load", timeout=20000)
        res.status = resp.status if resp else None
        res.load_ms = int((time.monotonic() - t0) * 1000)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        probe = await page.evaluate(PAGE_PROBE_JS, VIEWPORTS[viewport]["width"])
        res.title, res.description, res.favicon = probe["title"], probe["description"], probe["favicon"]
        res.blank = probe["textLength"] < 40 or probe["elementCount"] < 5
        res.overflow_x, res.wide_elements = probe["overflowX"], probe["wideElements"]
        res.broken_images = probe["brokenImages"]
        res.small_targets = probe["smallTargets"] if viewport == "phone" else 0
        links = probe["links"]
        shot = out_dir / f"{idx:02d}-{viewport}.png"
        await page.screenshot(path=str(shot), full_page=True, timeout=15000)
        res.screenshot = shot.name
    except Exception as e:  # timeouts, DNS errors, crashes
        res.error = str(e).split("\n")[0][:300]
    finally:
        await page.close()
    return res, links


async def _headers(context, url: str) -> dict:
    try:
        r = await context.request.get(url, timeout=15000)
        h = {k.lower(): v for k, v in r.headers.items()}
        return {k: (k in h) for k in ["strict-transport-security", "content-security-policy",
                                       "x-content-type-options", "x-frame-options", "referrer-policy"]}
    except Exception:
        return {}


def summarize(report: UIReport) -> list[dict]:
    """Turn raw page results into ranked issues (severity: critical, high, medium, low)."""
    issues = []
    for p in report.pages:
        where = f"{p.url} ({p.viewport})"
        if p.error:
            issues.append({"severity": "critical", "kind": "page_failed", "where": where, "detail": p.error, "shot": p.screenshot})
            continue
        if p.status and p.status >= 400:
            issues.append({"severity": "critical", "kind": "http_error", "where": where, "detail": f"HTTP {p.status}", "shot": p.screenshot})
        if p.blank:
            issues.append({"severity": "critical", "kind": "blank_page", "where": where,
                           "detail": "Page rendered almost no content (blank or loading-forever screen)", "shot": p.screenshot})
        if p.console_errors:
            issues.append({"severity": "high", "kind": "console_errors", "where": where,
                           "detail": "; ".join(dict.fromkeys(p.console_errors))[:600], "shot": p.screenshot})
        bad = [f for f in p.failed_requests if not f.startswith(("401", "403"))]
        if bad:
            issues.append({"severity": "high", "kind": "failed_requests", "where": where,
                           "detail": "; ".join(dict.fromkeys(bad))[:600], "shot": p.screenshot})
        if p.overflow_x:
            issues.append({"severity": "medium", "kind": "horizontal_overflow", "where": where,
                           "detail": "Page scrolls sideways. Widest elements: " + ", ".join(p.wide_elements), "shot": p.screenshot})
        if p.broken_images:
            issues.append({"severity": "medium", "kind": "broken_images", "where": where,
                           "detail": ", ".join(p.broken_images[:5]), "shot": p.screenshot})
        if p.load_ms and p.load_ms > 5000:
            issues.append({"severity": "medium", "kind": "slow_page", "where": where, "detail": f"Loaded in {p.load_ms/1000:.1f}s", "shot": p.screenshot})
        if p.viewport == "phone" and p.small_targets >= 5:
            issues.append({"severity": "low", "kind": "small_tap_targets", "where": where,
                           "detail": f"{p.small_targets} buttons/links smaller than 24px", "shot": p.screenshot})
    home = next((p for p in report.pages if p.viewport == "desktop"), None)
    if home and not home.error:
        if not home.title:
            issues.append({"severity": "low", "kind": "missing_title", "where": home.url, "detail": "No <title>", "shot": None})
        if not home.description:
            issues.append({"severity": "low", "kind": "missing_description", "where": home.url, "detail": "No meta description (bad link previews)", "shot": None})
        if not home.favicon:
            issues.append({"severity": "low", "kind": "missing_favicon", "where": home.url, "detail": "No favicon", "shot": None})
    missing = [k for k, v in report.security_headers.items() if not v]
    if missing:
        issues.append({"severity": "low", "kind": "security_headers", "where": report.start_url,
                       "detail": "Missing: " + ", ".join(missing), "shot": None})
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return sorted(issues, key=lambda i: order[i["severity"]])


def _robots(start_url: str):
    """Honor robots.txt for unverified sites. Returns can_fetch(url) -> bool."""
    from urllib.robotparser import RobotFileParser
    import httpx
    p = urlparse(start_url)
    rp = RobotFileParser()
    try:
        r = httpx.get(f"{p.scheme}://{p.netloc}/robots.txt", timeout=10, headers={"User-Agent": UA})
        rp.parse(r.text.splitlines() if r.status_code == 200 else [])
    except Exception:
        rp.parse([])
    return lambda u: rp.can_fetch("LaunchproofUICheck", u)


async def run_ui_check(start_url: str, out_dir: Path, max_pages: int = 20, delay_s: float = 1.0,
                       respect_robots: bool = False) -> UIReport:
    out_dir.mkdir(parents=True, exist_ok=True)
    report = UIReport(start_url=start_url)
    allowed = _robots(start_url) if respect_robots else (lambda u: True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            contexts = {name: await browser.new_context(viewport=vp, user_agent=UA,
                                                          is_mobile=(name == "phone"), has_touch=(name == "phone"))
                        for name, vp in VIEWPORTS.items()}
            report.security_headers = await _headers(contexts["desktop"], start_url)
            queue, seen, idx = [_norm(start_url)], set(), 0
            while queue and idx < max_pages:
                url = queue.pop(0)
                if url in seen or not allowed(url):
                    continue
                seen.add(url)
                links = []
                for name, ctx in contexts.items():
                    res, found = await _visit(ctx, url, name, out_dir, idx)
                    report.pages.append(res)
                    links = links or found
                for l in links:
                    l = _norm(urljoin(url, l))
                    if _same_site(l, start_url) and l not in seen and l not in queue and not SKIP_LINK.search(l) \
                            and not re.search(r"\.(pdf|zip|png|jpe?g|gif|svg|mp4|webp)$", l, re.I):
                        queue.append(l)
                idx += 1
                await asyncio.sleep(delay_s)
        finally:
            await browser.close()
    report.issues = summarize(report)
    return report
