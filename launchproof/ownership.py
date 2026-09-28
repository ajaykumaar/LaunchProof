"""Ownership verification. Load tests and checkout runs only happen on sites the user proves they control.

Any one of these passes:
  1. DNS TXT   _launchproof.<host>   "launchproof-verify=<token>"
  2. File      https://<host>/.well-known/launchproof.txt   containing <token>
  3. Meta tag  <meta name="launchproof-verify" content="<token>"> on the homepage

Tokens are signed with LP_SECRET and carry their issue time, so no database is needed and they
expire after 24 hours. Tokens are bound to the host they were issued for.
localhost / 127.0.0.1 / *.local are allowed without a token (development only).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from urllib.parse import urlparse

import httpx

TTL_S = 24 * 3600
DEV_HOSTS = re.compile(r"^(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|.+\.local)$", re.I)


def _secret() -> bytes:
    return (os.getenv("LP_SECRET") or "dev-secret-change-me").encode()


def host_of(url: str) -> str:
    return (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()


def issue_token(url: str, now: float | None = None) -> str:
    ts = int(now or time.time())
    nonce = secrets.token_hex(6)
    msg = f"{host_of(url)}|{ts}|{nonce}".encode()
    sig = base64.urlsafe_b64encode(hmac.new(_secret(), msg, hashlib.sha256).digest()[:12]).decode().rstrip("=")
    return f"lp_{ts}_{nonce}_{sig}"


def token_valid(url: str, token: str, now: float | None = None) -> tuple[bool, str]:
    m = re.fullmatch(r"lp_(\d+)_([0-9a-f]{12})_([A-Za-z0-9_-]+)", token or "")
    if not m:
        return False, "malformed token"
    ts, nonce, sig = int(m.group(1)), m.group(2), m.group(3)
    msg = f"{host_of(url)}|{ts}|{nonce}".encode()
    want = base64.urlsafe_b64encode(hmac.new(_secret(), msg, hashlib.sha256).digest()[:12]).decode().rstrip("=")
    if not hmac.compare_digest(want, sig):
        return False, "token was issued for a different site"
    if (now or time.time()) - ts > TTL_S:
        return False, "token expired (older than 24 hours), issue a new one"
    return True, ""


def instructions(url: str, token: str) -> dict:
    host = host_of(url)
    return {
        "dns": {"type": "TXT", "name": f"_launchproof.{host}", "value": f"launchproof-verify={token}"},
        "file": {"url": f"https://{host}/.well-known/launchproof.txt", "contents": token},
        "meta": f'<meta name="launchproof-verify" content="{token}">',
    }


def _check_dns(host: str, token: str) -> bool:
    try:
        import dns.resolver
        answers = dns.resolver.resolve(f"_launchproof.{host}", "TXT", lifetime=5)
        for rr in answers:
            txt = b"".join(rr.strings).decode(errors="ignore")
            if txt.strip() == f"launchproof-verify={token}":
                return True
    except Exception:
        pass
    return False


def _check_http(url: str, token: str) -> tuple[bool, bool]:
    """Returns (file_ok, meta_ok)."""
    p = urlparse(url if "://" in url else "https://" + url)
    base = f"{p.scheme}://{p.netloc}"
    file_ok = meta_ok = False
    with httpx.Client(timeout=10, follow_redirects=True,
                      headers={"User-Agent": "LaunchproofVerify/1.0 (+https://launchproof.xyz/bot)"}) as c:
        try:
            r = c.get(base + "/.well-known/launchproof.txt")
            file_ok = r.status_code == 200 and token in r.text[:2000]
        except httpx.HTTPError:
            pass
        try:
            r = c.get(base + "/")
            pat = r'<meta[^>]+name=["\']launchproof-verify["\'][^>]+content=["\']' + re.escape(token) + r'["\']'
            meta_ok = r.status_code == 200 and re.search(pat, r.text[:200000], re.I) is not None
        except httpx.HTTPError:
            pass
    return file_ok, meta_ok


def verify(url: str, token: str | None, allow_dev: bool = True) -> dict:
    host = host_of(url)
    if allow_dev and DEV_HOSTS.match(host):
        return {"verified": True, "method": "dev-host", "host": host}
    ok, why = token_valid(url, token or "")
    if not ok:
        return {"verified": False, "method": None, "host": host, "reason": why}
    if _check_dns(host, token):
        return {"verified": True, "method": "dns", "host": host}
    file_ok, meta_ok = _check_http(url, token)
    if file_ok or meta_ok:
        return {"verified": True, "method": "file" if file_ok else "meta", "host": host}
    return {"verified": False, "method": None, "host": host,
            "reason": "token not found in DNS, /.well-known/launchproof.txt, or homepage meta tag",
            "how_to": instructions(url, token)}


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) == 2:
        t = issue_token(sys.argv[1])
        print(json.dumps({"token": t, **instructions(sys.argv[1], t)}, indent=2))
    elif len(sys.argv) == 3:
        print(json.dumps(verify(sys.argv[1], sys.argv[2]), indent=2))
    else:
        print("usage: python -m launchproof.ownership <url> [token]")
