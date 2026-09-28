"""Launchproof web app: landing page, ownership token, start a run, live status, report, and our own
$9 Launch Pass via Stripe Checkout.

  uvicorn api.server:app --port 8000
  # open http://localhost:8000

Env: LP_SECRET, ANTHROPIC_API_KEY, STRIPE_SECRET_KEY + STRIPE_PRICE_ID + STRIPE_WEBHOOK_SECRET (our billing),
PUBLIC_URL (for Stripe redirects), plus everything the CLI reads (FLY_*, SLACK_*, LINEAR_*).

Storage is in-memory + the runs/ folder: fine for the hackathon, swap for SQLite/D1 after.
With BRAINBASE_API_KEY + BRAINBASE_AGENT_ID set, runs are handed to the Launchproof agent on Brainbase and
this app only does landing, verification, billing and report hosting (no Chromium needed).
Free run = UI check only. A paid pass (checkout.session.completed webhook) unlocks payment + load for one site.
"""
from __future__ import annotations

import asyncio
import html
import os
import secrets
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from launchproof.envfile import load_env_local

load_env_local()

from launchproof import ownership
from launchproof.__main__ import LOG_SINK, run, run_args

RUNS_DIR = Path(os.getenv("LP_RUNS_DIR", "runs")).resolve()
app = FastAPI(title="Launchproof")
RUNS: dict[str, dict] = {}          # run_id -> {"status", "log", "url", "result"}
PASSES: dict[str, str] = {}         # host -> checkout session id (paid)
MAX_CONCURRENT = int(os.getenv("LP_MAX_CONCURRENT", "2"))
_sem = asyncio.Semaphore(MAX_CONCURRENT)

CSS = """<style>:root{--bg:#F7F7F8;--card:#fff;--ink:#111318;--muted:#5E6470;--line:#E6E7EA;--acc:#4F46E5}
@media (prefers-color-scheme:dark){:root{--bg:#0B0D12;--card:#141821;--ink:#F4F5F7;--muted:#9AA3AF;--line:#262B36;--acc:#818CF8}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 Inter,system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:40px 16px}h1{font-size:36px;line-height:1.15;margin:0 0 10px}
.muted{color:var(--muted)}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin:16px 0}
input[type=text],input[type=url]{width:100%;padding:12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font:inherit}
button,.btn{display:inline-block;background:var(--acc);color:#fff;border:0;border-radius:8px;padding:11px 16px;font:inherit;font-weight:600;cursor:pointer;text-decoration:none;margin-top:10px}
pre{white-space:pre-wrap;word-break:break-all;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:12px;font:13px/1.5 ui-monospace,Menlo,monospace}
label{display:block;margin:10px 0 4px;font-weight:600}.small{font-size:13px}</style>"""


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<meta name="description" content="An agent that tests your launch: phone and desktop UI, a real test checkout, and a load test from several regions.">
{CSS}</head><body><main>{body}</main></body></html>""")


@app.get("/", response_class=HTMLResponse)
def home():
    return page("Launchproof", """<h1>Will your site survive launch day?</h1>
<p class="muted">Paste your URL. An agent uses it like a new customer on phone and desktop, buys with a Stripe test card,
then sends a crowd at it from several regions. You get a score, what broke, and a fix prompt for each problem.</p>
<form class="card" method="post" action="/verify"><label for="u">Your site</label>
<input id="u" type="url" name="url" placeholder="https://yourapp.com" required>
<button type="submit">Test my site</button>
<p class="small muted">Free: UI check on any site. Payment and load tests need proof you own the site, and a $9 Launch Pass.</p></form>""")


@app.post("/verify", response_class=HTMLResponse)
def verify_page(url: str = Form(...)):
    tok = ownership.issue_token(url)
    how = ownership.instructions(url, tok)
    e = html.escape
    paid = ownership.host_of(url) in PASSES
    return page("Verify", f"""<h1>Prove it's yours</h1><p class="muted">Pick one. The token is valid for 24 hours.</p>
<div class="card"><b>DNS</b><pre>TXT  {e(how['dns']['name'])}  "{e(how['dns']['value'])}"</pre>
<b>File</b><pre>{e(how['file']['url'])}\n{e(how['file']['contents'])}</pre><b>Meta tag</b><pre>{e(how['meta'])}</pre></div>
<form class="card" method="post" action="/runs"><input type="hidden" name="url" value="{e(url)}"><input type="hidden" name="token" value="{e(tok)}">
<label><input type="checkbox" name="full" value="1" {'checked' if paid else ''}> Full test (payment + load). {'Launch Pass active.' if paid else 'Needs a Launch Pass.'}</label>
<label><input type="checkbox" name="consent" value="1" required> I own this site or have written permission to test it, and I accept that load tests
send real traffic that may slow the site and cost me bandwidth (see <a href="/legal/terms">Terms</a>).</label>
<button type="submit">Start the test</button> {'' if paid else f'<a class="btn" href="/buy?url={e(url)}">Buy Launch Pass ($9)</a>'}</form>
<p class="small muted">Only doing a UI check? You can skip verification: we crawl up to 20 pages and honor robots.txt.</p>""")


async def _do_run_brainbase(run_id: str, url: str, token: str | None, full: bool):
    """Hand the run to the Launchproof agent on Brainbase (its sandbox, its model, our credits)."""
    import json

    from launchproof import brainbase as bb
    rec = RUNS[run_id]
    rec["status"] = "running"
    try:
        tid = await asyncio.to_thread(bb.start, bb.task_message(url, run_id, token, full))
        rec["log"].append(f"Brainbase agent started (thread {tid})")
        st = await asyncio.to_thread(bb.wait, tid, 1500, lambda m: rec["log"].append(m[:1500]))
        for name in ("report.html", "share-card.png", "report.json"):
            await asyncio.to_thread(bb.download, tid, f"launchproof/runs/{run_id}/{name}", RUNS_DIR / run_id / name)
        report_json = RUNS_DIR / run_id / "report.json"
        if report_json.is_file():
            try:
                rec["result"] = json.loads(report_json.read_text())
            except Exception as ex:
                rec["log"].append(f"could not parse report.json: {ex}")
        rec["status"] = "done" if (RUNS_DIR / run_id / "report.html").exists() else "failed"
        rec["log"].append(f"agent finished: {st}")
    except Exception as ex:
        rec["status"] = "failed"
        rec["log"].append(f"run failed: {ex}")


async def _do_run(run_id: str, url: str, token: str | None, full: bool):
    if os.getenv("BRAINBASE_API_KEY") and os.getenv("BRAINBASE_AGENT_ID"):
        return await _do_run_brainbase(run_id, url, token, full)
    rec = RUNS[run_id]
    LOG_SINK.set(rec["log"])
    async with _sem:
        rec["status"] = "running"
        try:
            a = run_args(url, token=token, run_id=run_id, out=str(RUNS_DIR), skip_pay=not full, skip_load=not full,
                         regions=_regions_for_run(),
                         stage_seconds=float(os.getenv("LP_STAGE_SECONDS", "30")))
            rec["result"] = await run(a)
            rec["status"] = "done"
        except Exception as ex:
            rec["status"] = "failed"
            rec["log"].append(f"run failed: {ex}")


def _regions_for_run() -> list[str] | None:
    """Multi-region Fly only when credentials exist; otherwise local load (no Fly card required)."""
    from launchproof.load.fly import fly_configured
    if not fly_configured():
        return None
    parts = os.getenv("LP_REGIONS", "").split()
    return parts or None


@app.post("/runs")
async def start_run(background: BackgroundTasks, url: str = Form(...), token: str = Form(""),
                    full: str = Form(""), consent: str = Form("")):
    if not consent:
        raise HTTPException(400, "consent is required")
    want_full = bool(full)
    if want_full and ownership.host_of(url) not in PASSES and not ownership.DEV_HOSTS.match(ownership.host_of(url)):
        return RedirectResponse(f"/buy?url={url}", status_code=303)
    run_id = secrets.token_hex(5)
    RUNS[run_id] = {"status": "queued", "log": [], "url": url, "result": None}
    background.add_task(_do_run, run_id, url, token or None, want_full)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(run_id: str):
    if run_id not in RUNS:
        raise HTTPException(404)
    return page("Running", f"""<h1>Testing {html.escape(RUNS[run_id]['url'])}</h1><p id="s" class="muted">starting</p>
<pre id="log"></pre><p id="done"></p>
<script>
async function tick(){{const r=await fetch('/api/runs/{run_id}');const d=await r.json();
document.getElementById('s').textContent=d.status;document.getElementById('log').textContent=d.log.join('\\n');
if(d.status==='done'){{document.getElementById('done').innerHTML='<a class="btn" href="/r/{run_id}/report.html">Open the report</a>';return}}
if(d.status==='failed')return;setTimeout(tick,1500)}}tick();</script>""")


@app.get("/api/runs/{run_id}")
def run_status(run_id: str):
    rec = RUNS.get(run_id) or HTTPException(404)
    if isinstance(rec, HTTPException):
        raise rec
    res = rec["result"] or {}
    return {"status": rec["status"], "log": rec["log"][-200:], "score": (res.get("score") or {}).get("total"),
            "headline": res.get("headline")}


@app.get("/r/{run_id}/{name}")
def report_file(run_id: str, name: str):
    if not run_id.isalnum() or "/" in name or ".." in name:
        raise HTTPException(404)
    f = RUNS_DIR / run_id / name
    if not f.is_file():
        raise HTTPException(404)
    return FileResponse(f)


@app.get("/legal/{doc}")
def legal(doc: str):
    f = Path(__file__).resolve().parents[1] / "legal" / f"{doc}.md"
    if doc not in ("terms", "privacy", "acceptable-use") or not f.exists():
        raise HTTPException(404)
    return page(doc.title(), f"<pre>{html.escape(f.read_text())}</pre>")


# ---------- load engine callbacks (Fly machines POST here, the orchestrator polls) ----------

LOAD_RESULTS: dict[str, dict] = {}  # run_id -> {region: summary}


def _check_result_token(request: Request):
    """Require LP_RESULT_TOKEN in every environment that exposes these endpoints (no open callback)."""
    want = os.getenv("LP_RESULT_TOKEN", "")
    if not want:
        raise HTTPException(503, "LP_RESULT_TOKEN is not configured")
    if request.headers.get("authorization", "") != f"Bearer {want}":
        raise HTTPException(401)


@app.post("/api/load-results/{run_id}/{region}")
async def post_load_result(run_id: str, region: str, request: Request):
    _check_result_token(request)
    if len(LOAD_RESULTS) > 500:
        LOAD_RESULTS.pop(next(iter(LOAD_RESULTS)))
    LOAD_RESULTS.setdefault(run_id, {})[region] = await request.json()
    return {"ok": True}


@app.get("/api/load-results/{run_id}")
def get_load_results(run_id: str, request: Request):
    _check_result_token(request)
    return LOAD_RESULTS.get(run_id, {})


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------- our own billing: $9 Launch Pass ----------

@app.get("/buy")
def buy(request: Request, url: str):
    import stripe
    key, price = os.getenv("STRIPE_SECRET_KEY"), os.getenv("STRIPE_PRICE_ID")
    if not (key and price):
        raise HTTPException(503, "Stripe is not configured (STRIPE_SECRET_KEY, STRIPE_PRICE_ID)")
    stripe.api_key = key
    base = os.getenv("PUBLIC_URL") or str(request.base_url).rstrip("/")
    s = stripe.checkout.Session.create(
        mode="payment", line_items=[{"price": price, "quantity": 1}],
        client_reference_id=ownership.host_of(url), metadata={"target_url": url},
        success_url=f"{base}/paid?session_id={{CHECKOUT_SESSION_ID}}", cancel_url=f"{base}/")
    return RedirectResponse(s.url, status_code=303)


@app.get("/paid", response_class=HTMLResponse)
def paid(session_id: str):
    """Verify with Stripe before granting (never trust the redirect: that is the bug we test for)."""
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
    s = stripe.checkout.Session.retrieve(session_id)
    if s.payment_status != "paid":
        return page("Not paid", "<h1>Payment not completed</h1><p>Nothing was charged.</p>")
    PASSES[s.client_reference_id] = s.id
    target = (s.metadata or {}).get("target_url", "")
    return page("Paid", f"""<h1>Launch Pass active</h1><p>For {html.escape(s.client_reference_id)}.</p>
<form method="post" action="/verify"><input type="hidden" name="url" value="{html.escape(target)}"><button>Continue to verification</button></form>""")


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    import stripe
    payload, sig = await request.body(), request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, os.getenv("STRIPE_WEBHOOK_SECRET", ""))
    except Exception:
        raise HTTPException(400, "bad signature")
    if event["type"] == "checkout.session.completed":
        s = event["data"]["object"]
        if s.get("payment_status") == "paid" and s.get("client_reference_id"):
            PASSES[s["client_reference_id"]] = s["id"]
    return JSONResponse({"received": True})
