#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compile_job.py <run-dir>")
    run_dir = Path(sys.argv[1])
    run_dir.mkdir(parents=True, exist_ok=True)
    output_path = run_dir / "job.json"
    payload = {
        "ok": True,
        "mode": "draft",
        "note": "Replace placeholder job compilation with path-specific logic.",
    }
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(output_path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
