"""Load launchproof-v1_brainbase/.env.local into os.environ (does not override existing vars).

Called at CLI / API / brainbase-client startup so ANTHROPIC_API_KEY and friends work without
manually exporting them in PowerShell each session.
"""
from __future__ import annotations

import os
from pathlib import Path

_LOADED = False


def load_env_local(override: bool = False) -> Path | None:
    """Find .env.local next to the project root (parent of the launchproof package). Returns path if loaded."""
    global _LOADED
    if _LOADED and not override:
        return None
    root = Path(__file__).resolve().parents[1]
    path = root / ".env.local"
    if not path.is_file():
        _LOADED = True
        return None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if not key:
            continue
        if override or key not in os.environ or os.environ.get(key, "") == "":
            os.environ[key] = val
    _LOADED = True
    return path
