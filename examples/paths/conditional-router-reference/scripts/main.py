from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    manifest = yaml.safe_load((ROOT / "wfpath.yaml").read_text(encoding="utf-8")) or {}
    policy = (
        yaml.safe_load((ROOT / "policy" / "default.yaml").read_text(encoding="utf-8"))
        or {}
    )
    pipeline = manifest.get("pipeline") or {}
    summary = {
        "slug": manifest.get("slug"),
        "archetype": policy.get("archetype"),
        "entry_command": pipeline.get("entry_command"),
        "signals": sorted((policy.get("signals") or {}).keys()),
        "playbooks": sorted((policy.get("playbooks") or {}).keys()),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
