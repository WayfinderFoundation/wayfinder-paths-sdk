from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from wayfinder_paths.mcp.utils import repo_root


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def runs_root() -> Path:
    candidate = (os.getenv("WAYFINDER_RUNS_DIR") or ".wayfinder_runs").strip()
    p = Path(candidate)
    if not p.is_absolute():
        p = repo_root() / p
    return p.resolve(strict=False)
