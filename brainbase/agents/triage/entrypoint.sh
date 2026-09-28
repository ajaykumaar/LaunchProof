#!/usr/bin/env bash
# Triage only needs notify.py (httpx). No Chromium.
set -euo pipefail
REPO="${LP_REPO:-}"
if [ -z "$REPO" ]; then echo "ERROR: set secret LP_REPO=github.com/<you>/launchproof.git" >&2; exit 1; fi
cd /workspace
if [ ! -d launchproof/.git ]; then
  if [ -n "${GITHUB_TOKEN:-}" ]; then git clone --depth 1 "https://oauth2:${GITHUB_TOKEN}@${REPO}" launchproof
  else git clone --depth 1 "https://${REPO}" launchproof; fi
else
  git -C launchproof pull --ff-only || true
fi
cd /workspace/launchproof
export PYTHONPATH=/workspace/launchproof
python3 -m pip install --quiet httpx || python3 -m pip install --quiet --break-system-packages httpx
python3 -c "import launchproof.notify; print('triage ready')"
