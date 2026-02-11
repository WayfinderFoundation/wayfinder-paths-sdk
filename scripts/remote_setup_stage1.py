#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _run(cmd: list[str]) -> None:
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remote bootstrap (stage 1): poetry install + write config.json."
    )
    parser.add_argument("--api-key", help="Wayfinder API key (wk_...)")
    parser.add_argument(
        "--api-base-url",
        default=None,
        help="Wayfinder API base URL (default: https://wayfinder.ai/api)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=REPO_ROOT / "config.json",
        help="Path to config.json (default: repo root config.json)",
    )
    args = parser.parse_args()

    os.chdir(REPO_ROOT)

    api_key = (args.api_key or os.environ.get("WAYFINDER_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("Missing API key. Pass --api-key or set WAYFINDER_API_KEY.")

    template_path = REPO_ROOT / "config.example.json"
    config = _load_json(args.config) or _load_json(template_path)
    system = config.get("system")
    if not isinstance(system, dict):
        system = {}
    system["api_key"] = api_key
    system["api_base_url"] = (
        str(args.api_base_url).strip()
        if args.api_base_url
        else system.get("api_base_url") or "https://wayfinder.ai/api"
    )
    config["system"] = system

    if "strategy" not in config:
        template = _load_json(template_path)
        if isinstance(template.get("strategy"), dict):
            config["strategy"] = template["strategy"]

    _write_json(args.config, config)
    print(f"Wrote {args.config}")

    _run(["poetry", "install"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
