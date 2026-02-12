#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from remote_setup_utils import (
    REPO_ROOT,
    discover_strategies,
    load_core_config_module,
    run_cmd,
)

_config = load_core_config_module(REPO_ROOT)
load_config_json = _config.load_config_json
load_wallet_mnemonic = _config.load_wallet_mnemonic
write_wallet_mnemonic = _config.write_wallet_mnemonic


def _read_mnemonic_file(path: Path) -> str:
    phrase = " ".join(path.read_text().strip().split())
    if not phrase:
        raise SystemExit(f"Mnemonic file is empty: {path}")
    return phrase


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
    parser.add_argument(
        "--mnemonic-stdin",
        action="store_true",
        help="Read BIP-39 mnemonic phrase from stdin (pipe-friendly).",
    )
    args = parser.parse_args()

    os.chdir(REPO_ROOT)
    config_path = REPO_ROOT / "config.json"
    if not config_path.exists():
        raise SystemExit("Missing config.json. Run stage 1 first.")

    sources = sum([args.mnemonic, args.mnemonic_file is not None, args.mnemonic_stdin])
    if sources > 1:
        raise SystemExit(
            "Pass only one of --mnemonic, --mnemonic-file, or --mnemonic-stdin."
        )

    existing_mnemonic = load_wallet_mnemonic(config_path)
    main_wallet_args: list[str] = []

    phrase_from_input: str | None = None
    if args.mnemonic_file is not None:
        phrase_from_input = _read_mnemonic_file(args.mnemonic_file.expanduser())
    elif args.mnemonic_stdin:
        phrase_from_input = " ".join(sys.stdin.read().strip().split())
        if not phrase_from_input:
            raise SystemExit("No mnemonic received on stdin.")

    if existing_mnemonic:
        if phrase_from_input and phrase_from_input != existing_mnemonic:
            raise SystemExit(
                "config.json already contains wallet_mnemonic; refusing to overwrite."
            )
    else:
        if phrase_from_input:
            write_wallet_mnemonic(phrase_from_input, config_path)
        elif args.mnemonic:
            # Let `scripts/make_wallets.py` generate + persist the mnemonic.
            main_wallet_args = ["--mnemonic"]
        else:
            raise SystemExit(
                "config.json has no wallet_mnemonic. Provide --mnemonic (to generate), "
                "--mnemonic-file /path/to/mnemonic.txt, or --mnemonic-stdin."
            )

    base = [
        "poetry",
        "run",
        "python",
        "scripts/make_wallets.py",
        "--out-dir",
        str(config_path.parent),
    ]

    run_cmd([*base, "--label", "main", *main_wallet_args])
    for name in discover_strategies():
        run_cmd([*base, "--label", name])

    config = load_config_json(config_path)
    wallets = config.get("wallets", [])
    main_wallet = next((w for w in wallets if w.get("label") == "main"), None)
    main_address = main_wallet["address"] if main_wallet else None

    print(
        json.dumps(
            {"stage": 2, "status": "complete", "main_wallet_address": main_address}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
