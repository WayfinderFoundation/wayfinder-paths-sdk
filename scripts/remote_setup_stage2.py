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
    path.write_text(json.dumps(data, indent=2) + "\n")


def _run(cmd: list[str]) -> None:
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def _discover_strategies() -> list[str]:
    strategies_dir = REPO_ROOT / "wayfinder_paths" / "strategies"
    if not strategies_dir.exists():
        return []
    return sorted(
        d.name for d in strategies_dir.iterdir() if (d / "strategy.py").exists()
    )


def _ensure_mcp_json(*, config_path: Path) -> None:
    mcp_path = REPO_ROOT / ".mcp.json"
    mcp = _load_json(mcp_path)

    mcp_servers = mcp.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}
        mcp["mcpServers"] = mcp_servers

    wayfinder = mcp_servers.get("wayfinder")
    if not isinstance(wayfinder, dict):
        wayfinder = {}
        mcp_servers["wayfinder"] = wayfinder

    wayfinder["command"] = "poetry"
    wayfinder["args"] = ["run", "python", "-m", "wayfinder_paths.mcp.server"]

    env = wayfinder.get("env")
    if not isinstance(env, dict):
        env = {}
    try:
        env["WAYFINDER_CONFIG_PATH"] = str(config_path.relative_to(REPO_ROOT))
    except ValueError:
        env["WAYFINDER_CONFIG_PATH"] = str(config_path)
    wayfinder["env"] = env

    _write_json(mcp_path, mcp)
    print(f"Wrote {mcp_path}")


def _has_mnemonic(config_path: Path) -> bool:
    config = _load_json(config_path)
    val = config.get("wallet_mnemonic")
    return isinstance(val, str) and bool(val.strip())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remote bootstrap (stage 2): create .mcp.json + wallets from mnemonic."
    )
    parser.add_argument(
        "--mnemonic",
        nargs="?",
        const="__generate__",
        default=None,
        help=(
            "Use mnemonic-derived deterministic wallets. If config.json has no wallet_mnemonic, "
            "provide this flag (optionally with a phrase) to persist one."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=REPO_ROOT / "config.json",
        help="Path to config.json (default: repo root config.json)",
    )
    args = parser.parse_args()

    os.chdir(REPO_ROOT)
    if not args.config.exists():
        raise SystemExit(
            f"Missing config: {args.config}. Run stage 1 first (scripts/remote_setup_stage1.py)."
        )

    _ensure_mcp_json(config_path=args.config)

    has_mnemonic = _has_mnemonic(args.config)
    if not has_mnemonic and args.mnemonic is None:
        raise SystemExit(
            "config.json has no wallet_mnemonic. Provide --mnemonic (to generate) "
            "or --mnemonic 'your twelve words'."
        )

    base = [
        "poetry",
        "run",
        "python",
        "scripts/make_wallets.py",
        "--out-dir",
        str(args.config.parent),
    ]

    mnemonic_args: list[str] = []
    if args.mnemonic is not None and not has_mnemonic:
        mnemonic_args = (
            ["--mnemonic"]
            if args.mnemonic == "__generate__"
            else ["--mnemonic", args.mnemonic]
        )

    _run([*base, "--label", "main", *mnemonic_args])
    for name in _discover_strategies():
        _run([*base, "--label", name])

    print("\nStage 2 complete.")
    print(
        "- Open Claude Code in this repo and enable the project MCP server when prompted."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
