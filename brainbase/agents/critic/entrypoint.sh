#!/usr/bin/env bash
# Critic needs browser to re-visit live URLs for visual appeal (Brainbase credits, no Anthropic).
set -uo pipefail

log() { echo "[launchproof-critic-entrypoint] $*"; }

REPO="${LP_REPO:-}"
if [ -z "$REPO" ]; then
  log "ERROR: set secret LP_REPO=github.com/<you>/LaunchProof.git"
  exit 1
fi

case "$REPO" in
  https://*|http://*|git@*) CLONE_HOST="$REPO" ;;
  *) CLONE_HOST="https://${REPO}" ;;
esac

cd /workspace

clone_repo() {
  if [ -d launchproof/.git ]; then
    log "repo present; pulling"
    git -C launchproof pull --ff-only || true
    return 0
  fi
  rm -rf launchproof
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    log "cloning with GITHUB_TOKEN via ${REPO}"
    if git clone --depth 1 "https://oauth2:${GITHUB_TOKEN}@${REPO#https://}" launchproof; then
      return 0
    fi
    log "token clone failed; trying public HTTPS"
  fi
  log "cloning public ${CLONE_HOST}"
  git clone --depth 1 "$CLONE_HOST" launchproof
}

if ! clone_repo; then
  log "ERROR: git clone failed for LP_REPO=${REPO}"
  exit 1
fi

cd /workspace/launchproof
export PYTHONPATH=/workspace/launchproof
log "installing Python deps"
python3 -m pip install --quiet -r requirements.txt \
  || python3 -m pip install --quiet --break-system-packages -r requirements.txt

log "installing Playwright Chromium (best effort)"
if ! python3 -m playwright install --with-deps chromium >/tmp/pw.log 2>&1; then
  (sudo -n python3 -m playwright install-deps chromium >>/tmp/pw.log 2>&1 || true)
  python3 -m playwright install chromium >>/tmp/pw.log 2>&1 \
    || log "WARN: Chromium install failed; see /tmp/pw.log"
fi

if python3 -c "import launchproof; print('critic ready')" ; then
  log "OK: /workspace/launchproof is ready"
else
  log "ERROR: launchproof import failed"
  exit 1
fi
