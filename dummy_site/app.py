"""Neo-brutalist mini product site for Launchproof smoke + Critic style pickup.

  uvicorn dummy_site.app:app --port 8200

Theme (deliberate, one style): neo-brutalism — hard black borders, flat brights,
offset shadows, chunky type. Critic should name this.

Planted bugs (heuristic UI still catches these):
  - /pricing — horizontal overflow on phone (900px table)
  - /about   — broken image + console error
"""
from __future__ import annotations

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
import asyncio
import os

app = FastAPI(title="Stackbolt")

# Shared-resource claim (intentionally racy by default so Launchproof race probe can demo a finding).
# Set LP_CLAIM_SAFE=1 for a locked single-winner implementation.
_claim_holder: str | None = None
_claim_lock = asyncio.Lock()
_accounts: dict[str, str] = {}
_accounts_lock = asyncio.Lock()

CSS = """
:root {
  --ink: #111111;
  --paper: #FFF7E8;
  --yellow: #FFE566;
  --pink: #FF6B9A;
  --blue: #5B8CFF;
  --green: #3DDC97;
  --line: 3px solid var(--ink);
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: "Arial Black", "Arial Bold", Arial, sans-serif;
  line-height: 1.35;
}
a { color: inherit; }
.wrap { max-width: 980px; margin: 0 auto; padding: 20px 16px 64px; }
.nav {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  border: var(--line); background: #fff; padding: 10px 14px;
  box-shadow: 6px 6px 0 var(--ink); margin-bottom: 28px;
}
.brand { font-size: 22px; letter-spacing: -0.03em; text-decoration: none; }
.brand span { background: var(--yellow); border: var(--line); padding: 2px 8px; }
.nav-links { display: flex; flex-wrap: wrap; gap: 8px; }
.nav-links a {
  text-decoration: none; font-size: 13px; border: var(--line);
  background: #fff; padding: 8px 12px; box-shadow: 3px 3px 0 var(--ink);
}
.nav-links a:hover { transform: translate(1px, 1px); box-shadow: 2px 2px 0 var(--ink); }
.btn {
  display: inline-block; text-decoration: none; border: var(--line);
  padding: 12px 18px; font: inherit; font-size: 15px; cursor: pointer;
  box-shadow: 5px 5px 0 var(--ink); background: var(--pink); color: var(--ink);
}
.btn:hover { transform: translate(2px, 2px); box-shadow: 3px 3px 0 var(--ink); }
.btn.blue { background: var(--blue); }
.btn.green { background: var(--green); }
.btn.yellow { background: var(--yellow); }
.btn.ghost { background: #fff; }
.hero {
  border: var(--line); background: var(--yellow); padding: 36px 28px;
  box-shadow: 10px 10px 0 var(--ink); margin-bottom: 28px;
}
.hero h1 { font-size: clamp(36px, 7vw, 64px); line-height: 0.95; margin: 0 0 14px; letter-spacing: -0.04em; }
.hero p { font-family: Arial, Helvetica, sans-serif; font-size: 18px; max-width: 42ch; margin: 0 0 22px; font-weight: 600; }
.cta-row { display: flex; flex-wrap: wrap; gap: 12px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin: 28px 0; }
.card {
  border: var(--line); background: #fff; padding: 18px;
  box-shadow: 6px 6px 0 var(--ink);
}
.card h3 { margin: 0 0 8px; font-size: 20px; }
.card p { margin: 0; font-family: Arial, Helvetica, sans-serif; font-weight: 600; font-size: 15px; }
.badge {
  display: inline-block; border: var(--line); background: var(--blue); color: #fff;
  padding: 4px 10px; font-size: 12px; margin-bottom: 12px; box-shadow: 3px 3px 0 var(--ink);
}
.section-title { font-size: 28px; margin: 8px 0 16px; letter-spacing: -0.03em; }
.footer {
  margin-top: 40px; border-top: var(--line); padding-top: 16px;
  font-family: Arial, Helvetica, sans-serif; font-size: 13px; font-weight: 600;
}
.price-table { border: var(--line); background: #fff; box-shadow: 6px 6px 0 var(--ink); }
/* no overflow:auto — wide table must scroll the page so Launchproof flags horizontal_overflow */
table.wide { width: 900px; border-collapse: collapse; font-family: Arial, Helvetica, sans-serif; }
table.wide th, table.wide td { border: 2px solid var(--ink); padding: 10px; text-align: left; }
table.wide th { background: var(--pink); }
"""


def page(title: str, body: str, description: str = "Ship faster with Stackbolt") -> str:
    nav = """
    <header class="nav">
      <a class="brand" href="/">Stack<span>bolt</span></a>
      <nav class="nav-links">
        <a href="/">Home</a>
        <a href="/features">Features</a>
        <a href="/pricing">Pricing</a>
        <a href="/about">About</a>
        <a href="/signup">Sign up</a>
      </nav>
    </header>"""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title}</title>
  <meta name="description" content="{description}">
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' fill='%23FFE566' stroke='%23111' stroke-width='4'/><text x='6' y='23' font-size='16'>S</text></svg>">
  <style>{CSS}</style>
</head>
<body>
  <div class="wrap">
    {nav}
    {body}
    <footer class="footer">Stackbolt · neo-brutal launch demo · planted bugs on Pricing + About for Launchproof</footer>
  </div>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def home():
    body = """
    <section class="hero">
      <div class="badge">NEW · LAUNCH WEEK</div>
      <h1>Ship the messy version before the crowd shows up.</h1>
      <p>Stackbolt is a fake launch-day toolkit: checklists, status pages, and a big pink button that does nothing useful — on purpose.</p>
      <div class="cta-row">
        <a class="btn" href="/signup">Get started free</a>
        <a class="btn blue" href="/pricing">See pricing</a>
        <a class="btn ghost" href="/features">Browse features</a>
      </div>
    </section>
    <h2 class="section-title">Why teams pretend to use it</h2>
    <div class="grid">
      <article class="card">
        <h3>One bright CTA</h3>
        <p>Chunky buttons with offset shadows so nobody misses the next step.</p>
      </article>
      <article class="card">
        <h3>Hard borders only</h3>
        <p>No soft gradients. No glass blur. Just ink lines and loud color blocks.</p>
      </article>
      <article class="card">
        <h3>Built for critics</h3>
        <p>One committed neo-brutalist system from nav to footer — name it if you see it.</p>
      </article>
    </div>
    <div class="cta-row">
      <a class="btn green" href="/signup">Create account</a>
      <a class="btn yellow" href="/about">Meet the team</a>
    </div>
    """
    return page("Stackbolt — launch toolkit", body)


@app.get("/features", response_class=HTMLResponse)
def features():
    body = """
    <h1 class="section-title">Features</h1>
    <div class="grid">
      <article class="card"><h3>Launch checklist</h3><p>Tick boxes until you feel brave enough to tweet.</p></article>
      <article class="card"><h3>Status page</h3><p>Green squares that stay green even when things are on fire.</p></article>
      <article class="card"><h3>Crowd counter</h3><p>A number that goes up. Morale optional.</p></article>
      <article class="card"><h3>Fix prompts</h3><p>Paste-ready notes for whoever is still awake.</p></article>
    </div>
    <div class="cta-row">
      <a class="btn" href="/signup">Start free</a>
      <a class="btn ghost" href="/pricing">Compare plans</a>
    </div>
    """
    return page("Features · Stackbolt", body)


@app.get("/pricing", response_class=HTMLResponse)
def pricing():
    # Planted bug: 900px table forces horizontal overflow on phone.
    cols = "".join(f"<th>Col {i}</th>" for i in range(1, 9))
    cells = "".join("<td>cell</td>" for _ in range(8))
    body = f"""
    <h1 class="section-title">Pricing</h1>
    <p style="font-family:Arial,Helvetica,sans-serif;font-weight:600;max-width:50ch">
      Simple plans. The comparison table below is intentionally too wide on phones — Launchproof should flag it.
    </p>
    <div class="cta-row" style="margin:18px 0">
      <a class="btn" href="/signup">Start Hobby</a>
      <a class="btn blue" href="/signup">Go Pro</a>
      <button class="btn yellow" type="button" onclick="this.textContent='Loading…'; this.disabled=true">Buy now</button>
    </div>
    <div class="price-table">
      <table class="wide">
        <tr>{cols}</tr>
        <tr>{cells}</tr>
        <tr>{cells}</tr>
      </table>
    </div>
    """
    return page("Pricing · Stackbolt", body, "Stackbolt pricing with a planted overflow bug")


@app.get("/about", response_class=HTMLResponse)
def about():
    body = """
    <h1 class="section-title">About</h1>
    <div class="card" style="max-width:560px">
      <img src="/missing-logo.png" alt="broken logo" width="160" height="48">
      <script>console.error("analytics.track is not a function");</script>
      <p style="margin-top:14px;font-family:Arial,Helvetica,sans-serif;font-weight:600">
        We built Stackbolt in a weekend to stress-test launch-day agents. This page plants a broken image and a console error on purpose.
      </p>
      <div class="cta-row" style="margin-top:16px">
        <a class="btn green" href="/signup">Join the waitlist</a>
        <a class="btn ghost" href="/">Back home</a>
      </div>
    </div>
    """
    return page("About · Stackbolt", body)


@app.get("/signup", response_class=HTMLResponse)
def signup():
    body = """
    <h1 class="section-title">Create your account</h1>
    <form class="card" style="max-width:420px" method="post" action="/api/signup" id="signup-form">
      <label style="display:block;margin:8px 0 4px;font-size:13px">Email</label>
      <input name="email" type="email" required placeholder="you@launch.dev"
        style="width:100%;border:var(--line);padding:12px;font:inherit;box-shadow:3px 3px 0 var(--ink)">
      <label style="display:block;margin:14px 0 4px;font-size:13px">Password</label>
      <input name="password" type="password" required minlength="8" placeholder="••••••••"
        style="width:100%;border:var(--line);padding:12px;font:inherit;box-shadow:3px 3px 0 var(--ink)">
      <div class="cta-row" style="margin-top:18px">
        <button class="btn" type="submit">Sign up</button>
        <button class="btn ghost" type="submit">Sign up again (chaos bait)</button>
      </div>
    </form>
    """
    return page("Sign up · Stackbolt", body)


@app.post("/api/signup")
async def api_signup(request: Request, email: str = Form(...), password: str = Form(...)):
    """Create-account endpoint for Playwright signup check + httpx signup storm."""
    email_n = email.strip().lower()
    want_json = "application/json" in (request.headers.get("accept") or "")
    async with _accounts_lock:
        if email_n in _accounts:
            if want_json:
                return JSONResponse({"status": "already_exists", "email": email_n,
                                     "message": "account already exists"}, status_code=409)
            return HTMLResponse(
                page("Sign up · Stackbolt",
                     '<div class="card"><p>Account already exists. Try logging in.</p></div>'),
                status_code=409)
        # Tiny yield so concurrent storms can race if lock were absent; lock keeps this correct.
        await asyncio.sleep(0.01)
        _accounts[email_n] = password
    if want_json:
        return JSONResponse({"status": "created", "email": email_n,
                             "message": "account created welcome aboard"}, status_code=201)
    body = """
    <div class="card" style="max-width:420px" data-lp-signup="ok">
      <p style="font-family:Arial,Helvetica,sans-serif;font-weight:600">
        Fake signup accepted. Welcome aboard — check your inbox.
      </p>
      <a class="btn" href="/">Back home</a>
    </div>
    """
    return HTMLResponse(page("Welcome · Stackbolt", body), status_code=201)


@app.post("/api/claim")
async def api_claim(request: Request, code: str = Form("launch")):
    """Single-seat claim. Default is intentionally racy (no lock) for race-probe demos."""
    global _claim_holder
    want_json = "application/json" in (request.headers.get("accept") or "")
    safe = os.getenv("LP_CLAIM_SAFE", "").strip() in ("1", "true", "yes")

    async def _win(who: str):
        global _claim_holder
        if _claim_holder is not None:
            payload = {"status": "already_claimed", "holder": _claim_holder, "message": "seat already taken"}
            return (JSONResponse(payload, status_code=409) if want_json
                    else JSONResponse(payload, status_code=409))
        await asyncio.sleep(0.05)  # widen race window when unlocked
        _claim_holder = who
        payload = {"status": "claimed", "holder": who, "message": "seat claimed successfully"}
        return JSONResponse(payload, status_code=201)

    who = (request.headers.get("x-launchproof-vu") or code or "anon").strip()
    if safe:
        async with _claim_lock:
            return await _win(who)
    return await _win(who)


@app.post("/api/claim/reset")
async def api_claim_reset():
    global _claim_holder
    async with _claim_lock:
        _claim_holder = None
    return {"status": "ok"}
