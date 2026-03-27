#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import json

from wayfinder_paths.adapters.aerodrome_adapter import AerodromeAdapter
from wayfinder_paths.core.config import load_config
from wayfinder_paths.mcp.scripting import get_adapter


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Aerodrome classic user state snapshot",
    )
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--wallet-label", default="main")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--include-votes", action="store_true")
    args = parser.parse_args()

    load_config(args.config, require_exists=True)
    adapter = get_adapter(AerodromeAdapter, args.wallet_label, config_path=args.config)
    wallet = adapter.wallet_address
    if not wallet:
        raise SystemExit(f"Wallet '{args.wallet_label}' missing address in config")

    ok, state = await adapter.get_full_user_state(
        account=wallet,
        start=args.start,
        limit=args.limit,
        include_votes=bool(args.include_votes),
    )
    if not ok:
        raise SystemExit(state)

    print(json.dumps(state, indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
