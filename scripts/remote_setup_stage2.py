#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from wayfinder_paths.core.config import (
    load_config_json,
    load_wallet_mnemonic,
    write_config_json,
    write_wallet_mnemonic,
)
from wayfinder_paths.core.utils.wallets import ensure_wallet_mnemonic

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(cmd: list[str]) -> None:
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def _read_mnemonic_file(path: Path) -> str:
    phrase = " ".join(path.read_text().strip().split())
    if not phrase:
        raise SystemExit(f"Mnemonic file is empty: {path}")
    return phrase


def _discover_strategies() -> list[str]:
    strategies_dir = REPO_ROOT / "wayfinder_paths" / "strategies"
    if not strategies_dir.exists():
        return []
    return sorted(
        d.name for d in strategies_dir.iterdir() if (d / "strategy.py").exists()
    )


def _ensure_mcp_json(*, config_path: Path) -> None:
    mcp_path = REPO_ROOT / ".mcp.json"
    mcp = load_config_json(mcp_path)

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

    write_config_json(mcp_path, mcp)
    print(f"Wrote {mcp_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remote bootstrap (stage 2): create .mcp.json + wallets from mnemonic."
    )
    parser.add_argument(
        "--mnemonic",
        action="store_true",
        help="Generate and persist a new wallet mnemonic in config.json (if missing).",
    )
    parser.add_argument(
        "--mnemonic-file",
        type=Path,
        default=None,
        help=(
            "Path to a file containing a BIP-39 mnemonic phrase. This avoids putting the "
            "mnemonic in shell history."
        ),
    )
    args = parser.parse_args()

    os.chdir(REPO_ROOT)
    config_path = REPO_ROOT / "config.json"
    if not config_path.exists():
        raise SystemExit("Missing config.json. Run stage 1 first.")

    _ensure_mcp_json(config_path=config_path)

    if args.mnemonic and args.mnemonic_file is not None:
        raise SystemExit("Pass only one of --mnemonic or --mnemonic-file.")

    existing_mnemonic = load_wallet_mnemonic(config_path)
    if existing_mnemonic:
        if args.mnemonic_file is not None:
            phrase = _read_mnemonic_file(args.mnemonic_file.expanduser())
            if phrase != existing_mnemonic:
                raise SystemExit(
                    "config.json already contains wallet_mnemonic; refusing to overwrite."
                )
    else:
        if args.mnemonic_file is not None:
            phrase = _read_mnemonic_file(args.mnemonic_file.expanduser())
            write_wallet_mnemonic(phrase, config_path)
        elif args.mnemonic:
            mnemonic = ensure_wallet_mnemonic(config_path=config_path)
            print("Generated wallet mnemonic (saved to config.json):")
            print(mnemonic)
        else:
            raise SystemExit(
                "config.json has no wallet_mnemonic. Provide --mnemonic (to generate) "
                "or --mnemonic-file /path/to/mnemonic.txt."
            )

    base = [
        "poetry",
        "run",
        "python",
        "scripts/make_wallets.py",
        "--out-dir",
        str(config_path.parent),
    ]

    _run([*base, "--label", "main"])
    for name in _discover_strategies():
        _run([*base, "--label", name])

    print("\nStage 2 complete.")
    print(
        "- Open Claude Code in this repo and enable the project MCP server when prompted."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
