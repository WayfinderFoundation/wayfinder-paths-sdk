#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os

from remote_setup_utils import REPO_ROOT, ensure_config, run_cmd


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remote bootstrap (stage 1): poetry install + write config.json."
    )
    parser.add_argument("--api-key", help="Wayfinder API key (wk_...)")
    args = parser.parse_args()

    os.chdir(REPO_ROOT)

    api_key = (args.api_key or os.environ.get("WAYFINDER_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("Missing API key. Pass --api-key or set WAYFINDER_API_KEY.")

    ensure_config(api_key=api_key)
    run_cmd(["poetry", "install"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
