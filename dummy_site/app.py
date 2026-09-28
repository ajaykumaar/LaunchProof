"""Tiny buggy site for Launchproof smoke tests (2 planted issues).

  uvicorn dummy_site.app:app --port 8200

Bugs:
  - /wide  — horizontal overflow on phone (900px table)
  - /about — broken image + console error

Not for load/payment demos (use demo_shop for those).
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="Dummy Bugs")

NAV = '<p><a href="/">home</a> · <a href="/wide">wide</a> · <a href="/about">about</a></p>'


@app.get("/", response_class=HTMLResponse)
def home():
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Dummy Bugs</title>
<meta name="description" content="Tiny site with planted layout bugs">
<link rel="icon" href="data:,">
</head><body style="font-family:system-ui;margin:2rem">
<h1>Dummy Bugs</h1>
<p>Two intentional issues for Launchproof UI smoke.</p>
{NAV}
</body></html>"""


@app.get("/wide", response_class=HTMLResponse)
def wide():
    # Phone viewport ~390px; this table forces horizontal overflow.
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Wide page</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
</head><body style="font-family:system-ui;margin:1rem">
<h1>Pricing (broken on phone)</h1>
{NAV}
<table style="width:900px;border-collapse:collapse">
<tr>{''.join(f'<th style="border:1px solid #ccc;padding:8px">Col {i}</th>' for i in range(1, 9))}</tr>
<tr>{''.join(f'<td style="border:1px solid #ccc;padding:8px">cell</td>' for _ in range(8))}</tr>
</table>
</body></html>"""


@app.get("/about", response_class=HTMLResponse)
def about():
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>About</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
</head><body style="font-family:system-ui;margin:2rem">
<h1>About</h1>
{NAV}
<img src="/missing-logo.png" alt="broken logo" width="120" height="40">
<script>console.error("analytics.track is not a function");</script>
<p>Short about page with a broken image and a console error.</p>
</body></html>"""
