# Shared by Tester / Triage / Janitor entrypoints. Clones or updates the Launchproof repo into /workspace/launchproof.
# Secrets: GITHUB_TOKEN (optional for public repos), LP_REPO (github.com/<you>/launchproof.git).
set -euo pipefail
REPO="${LP_REPO:-}"
if [ -z "$REPO" ]; then
  echo "ERROR: set secret LP_REPO=github.com/<you>/launchproof.git" >&2
  exit 1
fi
if [ ! -d launchproof/.git ]; then
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    git clone --depth 1 "https://oauth2:${GITHUB_TOKEN}@${REPO}" launchproof
  else
    git clone --depth 1 "https://${REPO}" launchproof
  fi
else
  git -C launchproof pull --ff-only || true
fi
export PYTHONPATH="/workspace/launchproof${PYTHONPATH:+:$PYTHONPATH}"
cd /workspace/launchproof
