#!/usr/bin/env bash
# Runs in /workspace before the agent starts. Idempotent. Secrets are already in the environment.
set -euo pipefail
REPO="${LP_REPO:-}"
if [ -z "$REPO" ]; then
  echo "ERROR: set secret LP_REPO=github.com/<you>/launchproof.git (do not leave YOUR_GITHUB_USER)" >&2
  exit 1
fi
cd /workspace
if [ ! -d launchproof/.git ]; then
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    git clone --depth 1 "https://oauth2:${GITHUB_TOKEN}@${REPO}" launchproof
  else
    git clone --depth 1 "https://${REPO}" launchproof
  fi
else
  git -C launchproof pull --ff-only || true
fi
cd /workspace/launchproof
export PYTHONPATH=/workspace/launchproof
python3 -m pip install --quiet -r requirements.txt || python3 -m pip install --quiet --break-system-packages -r requirements.txt
# Chromium + its system libraries. --with-deps needs root; fall back to browser only.
if ! python3 -m playwright install --with-deps chromium >/tmp/pw.log 2>&1; then
  (sudo -n python3 -m playwright install-deps chromium >>/tmp/pw.log 2>&1 || true)
  python3 -m playwright install chromium >>/tmp/pw.log 2>&1 || echo "WARN: Chromium install failed, see /tmp/pw.log"
fi
python3 -c "import launchproof, playwright; print('launchproof ready')"
