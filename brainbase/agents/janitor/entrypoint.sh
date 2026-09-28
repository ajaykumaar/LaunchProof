#!/usr/bin/env bash
# Janitor only needs the Fly client path (httpx). No Chromium.
set -euo pipefail
# shellcheck disable=SC1091
source /workspace/brainbase/scripts/clone_repo.sh 2>/dev/null || true
# Fallback when this file is the only entrypoint content pushed (scripts/ may not be at /workspace/brainbase):
REPO="${LP_REPO:-}"
if [ -z "$REPO" ]; then echo "ERROR: set secret LP_REPO" >&2; exit 1; fi
if [ ! -d /workspace/launchproof/.git ]; then
  cd /workspace
  if [ -n "${GITHUB_TOKEN:-}" ]; then git clone --depth 1 "https://oauth2:${GITHUB_TOKEN}@${REPO}" launchproof
  else git clone --depth 1 "https://${REPO}" launchproof; fi
else
  git -C /workspace/launchproof pull --ff-only || true
fi
cd /workspace/launchproof
export PYTHONPATH=/workspace/launchproof
python3 -m pip install --quiet httpx || python3 -m pip install --quiet --break-system-packages httpx
python3 -c "import launchproof.load.fly; print('janitor ready')"
