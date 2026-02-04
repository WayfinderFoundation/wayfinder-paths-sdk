#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import wayfinder_hook_utils as hook_utils


def main() -> None:
    payload = hook_utils.load_payload()
    repo_root = hook_utils.repo_root()

    now = int(time.time())
    session_id = hook_utils.find_session_id(
        payload, env_keys=("CLAUDE_SESSION_ID", "SESSION_ID")
    )
    if not session_id:
        session_id = f"unknown-{now}"

    runs_root = hook_utils.runs_root(repo_root)
    scratch_dir = runs_root / ".scratch" / session_id
    library_dir = runs_root / "library"

    try:
        scratch_dir.mkdir(parents=True, exist_ok=True)
        library_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Never block the session on local FS issues.
        return

    index_path = scratch_dir / "index.json"
    try:
        index_path.write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "created_at_unix_s": now,
                    "runs_root": str(runs_root),
                    "scratch_dir": str(scratch_dir),
                    "library_dir": str(library_dir),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass

    env_file_raw = os.getenv("CLAUDE_ENV_FILE", "").strip()
    if env_file_raw:
        try:
            hook_utils.append_env_exports(
                env_file=Path(env_file_raw),
                exports={
                    "WAYFINDER_SESSION_ID": session_id,
                    "WAYFINDER_SCRATCH_DIR": str(scratch_dir),
                    "WAYFINDER_LIBRARY_DIR": str(library_dir),
                },
            )
        except OSError:
            pass

    scratch_display = hook_utils.rel_display(scratch_dir, repo_root)
    library_display = hook_utils.rel_display(library_dir, repo_root)

    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                "Wayfinder run scripts: write session-only artifacts under "
                f"`{scratch_display}` (env: WAYFINDER_SCRATCH_DIR). "
                "Scratch is auto-deleted on SessionEnd. "
                f"Promote keepers into `{library_display}` (env: WAYFINDER_LIBRARY_DIR), "
                "organized by protocol (e.g. `hyperliquid/`, `moonwell/`). "
                "Avoid hardcoding RPC URLs; use `web3_from_chain_id(...)` + `strategy.rpc_urls` from config.json."
            ),
        }
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
