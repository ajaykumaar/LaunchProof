"""Demo target: a tiny vibe-coded-style SaaS with deliberate launch-day bugs.

Bugs planted on purpose (the checker should find all of them):
  - /pricing overflows sideways on phones (fixed-width table)
  - /about has a broken image and throws a console error
  - /api/feed and the homepage/pricing share a 2-connection db pool (~50 queries/s): it slows down, then returns 503 under load
  - checkout: works with 4242..., shows a real error for the decline card
  - /checkout/success unlocks Pro for anyone who opens it (no payment verification). Fix: DEMO_TRUST_REDIRECT=0

Checkout modes:
  - STRIPE_SECRET_KEY set (test mode) → real Stripe Checkout Session, success via redirect + webhook
  - otherwise → a local mock that mirrors Stripe hosted Checkout field ids (for offline testing)

Run: uvicorn demo_shop.app:app --port 8100
"""
from __future__ import annotations

import asyncio
import os
import secrets

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

app = FastAPI(title="Brightboard demo shop")
USERS: dict[str, dict] = {}  # email -> {"plan": "free"|"pro"}
SESSIONS: dict[str, str] = {}  # cookie -> email
POOL_SIZE = int(os.getenv("DEMO_POOL_SIZE", "2"))  # db connections; capacity ~= POOL_SIZE / QUERY_S req/s
QUERY_S = float(os.getenv("DEMO_QUERY_S", "0.04"))
POOL_TIMEOUT_S = float(os.getenv("DEMO_POOL_TIMEOUT_S", "1.0"))
_pool: asyncio.Semaphore | None = None
MOCK_PAID: dict[str, str] = {}  # mock session id -> email
# Planted bug switch: 1 = unlock on redirect without verifying (bug), 0 = verify the session (fixed)
TRUST_REDIRECT = os.getenv("DEMO_TRUST_REDIRECT", "1") == "1"
VERIFY_TOKEN = os.getenv("LAUNCHPROOF_VERIFY_TOKEN", "")

CSS = """<style>body{font-family:system-ui,sans-serif;margin:0;background:#fafafa;color:#111}
header{display:flex;gap:16px;padding:14px 20px;background:#111;color:#fff}header a{color:#fff}
main{max-width:860px;margin:0 auto;padding:24px 20px}.btn{display:inline-block;background:#4f46e5;color:#fff;
padding:10px 16px;border-radius:8px;text-decoration:none;border:0;font-size:15px;cursor:pointer}
input{padding:9px;border:1px solid #ccc;border-radius:6px;width:260px;display:block;margin:6px 0 12px}</style>"""


def page(title: str, body: str, extra_head: str = "") -> HTMLResponse:
    meta = f'<meta name="launchproof-verify" content="{VERIFY_TOKEN}">' if VERIFY_TOKEN else ""
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title} · Brightboard</title>{meta}{CSS}{extra_head}</head>
<body><header><b>Brightboard</b><a href="/">Home</a><a href="/pricing">Pricing</a><a href="/about">About</a><a href="/signup">Sign up</a></header>
<main>{body}</main></body></html>""")


def current_user(request: Request) -> str | None:
    return SESSIONS.get(request.cookies.get("sid", ""))


@app.get("/", response_class=HTMLResponse)
async def home():
    await db_query()  # homepage renders "boards updated today" from the database
    return page("Home", """<h1>Brightboard</h1><p>Team dashboards that update themselves. Start free, upgrade to Pro
for unlimited boards.</p><p><a class="btn" href="/signup">Get started</a> <a href="/pricing">See pricing</a></p>
<div id="feed">Loading feed…</div>
<script>fetch('/api/feed').then(r=>r.json()).then(d=>{document.getElementById('feed').textContent=d.items.length+' boards updated today'})</script>""")


@app.get("/pricing", response_class=HTMLResponse)
async def pricing():
    await db_query()  # plans come from the database
    # BUG: fixed 900px table overflows on phones
    return page("Pricing", """<h1>Pricing</h1><table style="width:900px;border-collapse:collapse" border="1">
<tr><th>Plan</th><th>Boards</th><th>Refresh</th><th>Support</th><th>Price</th></tr>
<tr><td>Free</td><td>3</td><td>Daily</td><td>Community</td><td>$0</td></tr>
<tr><td>Pro</td><td>Unlimited</td><td>Every minute</td><td>Email</td><td>$12/month</td></tr></table>
<p><a class="btn" href="/upgrade">Upgrade to Pro</a></p>""")


@app.get("/about", response_class=HTMLResponse)
def about():
    # BUG: broken image + console error
    return page("About", """<h1>About</h1><img src="/static/team.png" alt="Team" width="300">
<p>We are two people who hate stale dashboards.</p><script>window.analytics.track('about_view')</script>""")


@app.get("/signup", response_class=HTMLResponse)
def signup_form():
    return page("Sign up", """<h1>Create your account</h1><form method="post" action="/signup">
<label>Email<input type="email" name="email" required></label>
<label>Password<input type="password" name="password" required minlength="8"></label>
<button class="btn" type="submit">Create account</button></form>""")


@app.post("/signup")
def signup(email: str = Form(...), password: str = Form(...)):
    USERS.setdefault(email.lower(), {"plan": "free"})
    sid = secrets.token_hex(12)
    SESSIONS[sid] = email.lower()
    r = RedirectResponse("/dashboard", status_code=303)
    r.set_cookie("sid", sid, httponly=True, samesite="lax")
    return r


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    email = current_user(request)
    if not email:
        return RedirectResponse("/signup", status_code=303)
    plan = USERS[email]["plan"]
    badge = '<p id="plan-status" data-plan="pro"><b>Pro plan active</b>. Unlimited boards unlocked.</p>' if plan == "pro" \
        else '<p id="plan-status" data-plan="free">Free plan: 3 boards.</p><p><a class="btn" href="/upgrade">Upgrade to Pro</a></p>'
    return page("Dashboard", f"<h1>Your dashboard</h1><p>Signed in as {email}</p>{badge}")


@app.get("/upgrade")
def upgrade(request: Request):
    email = current_user(request)
    if not email:
        return RedirectResponse("/signup", status_code=303)
    key = os.getenv("STRIPE_SECRET_KEY")
    if key:
        import stripe
        stripe.api_key = key
        base = str(request.base_url).rstrip("/")
        s = stripe.checkout.Session.create(
            mode="payment", customer_email=email,
            line_items=[{"price_data": {"currency": "usd", "unit_amount": 1200,
                                        "product_data": {"name": "Brightboard Pro (1 month)"}}, "quantity": 1}],
            success_url=base + "/checkout/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=base + "/pricing", client_reference_id=email)
        return RedirectResponse(s.url, status_code=303)
    return RedirectResponse("/mock-checkout", status_code=303)


@app.get("/checkout/success")
def checkout_success(request: Request, session_id: str = ""):
    email = current_user(request)
    if TRUST_REDIRECT:
        # BUG (planted, and very common): unlock because the browser reached the success URL,
        # without checking with Stripe that this session was actually paid.
        if email:
            USERS.setdefault(email, {"plan": "free"})["plan"] = "pro"
        return RedirectResponse("/dashboard", status_code=303)
    paid, owner = False, None
    if session_id in MOCK_PAID:
        paid, owner = True, MOCK_PAID[session_id]
    elif os.getenv("STRIPE_SECRET_KEY") and session_id.startswith("cs_"):
        import stripe
        stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
        try:
            s = stripe.checkout.Session.retrieve(session_id)
            paid, owner = s.payment_status == "paid", s.client_reference_id
        except Exception:
            paid = False
    if paid and owner:
        USERS.setdefault(owner, {"plan": "free"})["plan"] = "pro"
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/mock-checkout", response_class=HTMLResponse)
def mock_checkout(request: Request, error: str = ""):
    """Mirrors Stripe hosted Checkout field ids so the same filler works offline."""
    err = f'<p id="error" role="alert" style="color:#b00">{error}</p>' if error else ""
    return page("Checkout", f"""<h1>Pay Brightboard</h1><p>Brightboard Pro · $12.00</p>{err}
<form method="post" action="/mock-checkout"><input id="email" name="email" placeholder="Email" value="{current_user(request) or ''}">
<input id="cardNumber" name="cardNumber" placeholder="1234 1234 1234 1234"><input id="cardExpiry" name="cardExpiry" placeholder="MM / YY">
<input id="cardCvc" name="cardCvc" placeholder="CVC"><input id="billingName" name="billingName" placeholder="Full name on card">
<input id="billingPostalCode" name="billingPostalCode" placeholder="ZIP">
<button class="btn SubmitButton" type="submit" data-testid="hosted-payment-submit-button">Pay</button></form>""")


@app.post("/mock-checkout")
def mock_checkout_submit(request: Request, cardNumber: str = Form(""), email: str = Form("")):
    digits = cardNumber.replace(" ", "")
    if digits.endswith("0002"):
        return RedirectResponse("/mock-checkout?error=Your+card+was+declined.", status_code=303)
    if digits != "4242424242424242":
        return RedirectResponse("/mock-checkout?error=Your+card+number+is+invalid.", status_code=303)
    sid = "mock_" + secrets.token_hex(8)
    MOCK_PAID[sid] = current_user(request) or email.lower()
    return RedirectResponse(f"/checkout/success?session_id={sid}", status_code=303)


async def db_query():
    """Fake database with a tiny connection pool (the classic launch-day failure). Each query holds a
    connection for QUERY_S; if none frees up within POOL_TIMEOUT_S we raise, like a real pool timeout."""
    global _pool
    if _pool is None:
        _pool = asyncio.Semaphore(POOL_SIZE)
    try:
        await asyncio.wait_for(_pool.acquire(), timeout=POOL_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise PoolExhausted()
    try:
        await asyncio.sleep(QUERY_S)
        return list(range(12))
    finally:
        _pool.release()


class PoolExhausted(Exception):
    pass


@app.exception_handler(PoolExhausted)
async def pool_exhausted(request: Request, exc: PoolExhausted):
    return JSONResponse({"error": "database connection pool exhausted"}, status_code=503)


@app.get("/api/feed")
async def feed():
    return {"items": await db_query()}


@app.get("/.well-known/launchproof.txt", response_class=PlainTextResponse)
def verify_file():
    return VERIFY_TOKEN or "not configured"
