#!/usr/bin/env python3
from __future__ import annotations

import json
import os


def main() -> int:
    payload = {
        "ok": True,
        "run_id": os.environ.get("RUN_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
        or "unknown",
    }
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
