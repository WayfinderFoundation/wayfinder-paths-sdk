#!/usr/bin/env python3
# ruff: noqa: E402
"""Run the Wayfinder strategy/evolution A/B harness."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from wayfinder_paths.jobs.bench.cli import main

if __name__ == "__main__":
    main()
