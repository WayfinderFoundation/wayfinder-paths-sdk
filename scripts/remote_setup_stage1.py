#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from wayfinder_paths.core.config import load_config_json, write_config_json

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(cmd: list[str]) -> None:
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


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

    config_path = REPO_ROOT / "config.json"
    template_path = REPO_ROOT / "config.example.json"
    config = load_config_json(config_path) or load_config_json(template_path)

    system = config.get("system")
    if not isinstance(system, dict):
        system = {}
    system["api_key"] = api_key
    system.setdefault("api_base_url", "https://wayfinder.ai/api")
    config["system"] = system

    if "strategy" not in config:
        template = load_config_json(template_path)
        if isinstance(template.get("strategy"), dict):
            config["strategy"] = template["strategy"]

    write_config_json(config_path, config)
    print(f"Wrote {config_path}")

    _run(["poetry", "install"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
