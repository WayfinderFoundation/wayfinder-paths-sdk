"""Per-box FLOP state under `.wayfinder/flop/` (on a Shell this is the persisted user vault).

It holds the gateway bearer, so the directory is 0700 and the file 0600.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from wayfinder_paths.runner.paths import find_repo_root

MAX_ACTIVITY_ENTRIES = 200


def state_dir() -> Path:
    override = os.environ.get("WAYFINDER_FLOP_STATE_DIR")
    base = (
        Path(override).expanduser()
        if override
        else find_repo_root() / ".wayfinder" / "flop"
    )
    base.mkdir(parents=True, exist_ok=True)
    base.chmod(0o700)
    return base


def _state_path() -> Path:
    return state_dir() / "state.json"


def read_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def write_state(state: dict[str, Any]) -> None:
    path = _state_path()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def update_state(**sections: Any) -> dict[str, Any]:
    state = read_state()
    state.update(sections)
    write_state(state)
    return state


def record_activity(entry: dict[str, Any]) -> None:
    state = read_state()
    log = state.get("activity", [])
    log.append({"at": int(time.time()), **entry})
    state["activity"] = log[-MAX_ACTIVITY_ENTRIES:]
    write_state(state)


def utc_day(now: float | None = None) -> str:
    return time.strftime(
        "%Y-%m-%d", time.gmtime(now if now is not None else time.time())
    )


def spent_today(now: float | None = None) -> int:
    spend = read_state().get("spend", {})
    return int(spend.get(utc_day(now), 0))


def add_spend(raw: int, now: float | None = None) -> int:
    state = read_state()
    day = utc_day(now)
    total = int(state.get("spend", {}).get(day, 0)) + raw
    # Only today's total is kept; older days are of no use to the cap.
    state["spend"] = {day: str(total)}
    write_state(state)
    return total
