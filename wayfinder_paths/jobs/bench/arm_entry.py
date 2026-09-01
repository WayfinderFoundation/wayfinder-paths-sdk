"""Private subprocess entry point for one declared-SDK benchmark arm."""

from __future__ import annotations

import sys
from pathlib import Path

from wayfinder_paths.jobs.bench.env import atomic_json, load_json
from wayfinder_paths.jobs.bench.runner import run_arm


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(
            "usage: python -m wayfinder_paths.jobs.bench.arm_entry REQUEST"
        )
    request = load_json(Path(sys.argv[1]))
    result = run_arm(
        config=dict(request["config"]),
        arm=dict(request["arm"]),
        seed=int(request["seed"]),
        world_dir=Path(request["world_dir"]),
        sealed_dir=Path(request["sealed_dir"]),
        output_dir=Path(request["output_dir"]),
    )
    atomic_json(Path(request["result_path"]), result)


if __name__ == "__main__":
    main()
