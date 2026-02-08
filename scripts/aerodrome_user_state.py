#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from eth_utils import to_checksum_address

from wayfinder_paths.adapters.aerodrome_adapter.adapter import AerodromeAdapter
from wayfinder_paths.core.config import load_config


def _load_config(path: Path) -> dict:
    return json.loads(path.read_text())


def _wallet_addr_from_label(cfg: dict, wallet_label: str) -> str:
    wallets = cfg.get("wallets") or []
    for wallet in wallets:
        if wallet.get("label") != wallet_label:
            continue
        addr = wallet.get("address")
        if not addr:
            raise SystemExit(f"Wallet '{wallet_label}' missing address")
        return to_checksum_address(addr)
    raise SystemExit(f"Wallet label '{wallet_label}' not found in config.json")


async def main() -> int:
    p = argparse.ArgumentParser(description="Aerodrome (Base) full user state snapshot")
    p.add_argument("--config", default="config.json")
    p.add_argument("--wallet-label", default="main")
    p.add_argument("--include-zero", action="store_true")
    p.add_argument("--include-usd", action="store_true")
    p.add_argument("--no-slipstream", action="store_true")
    p.add_argument("--multicall-chunk", type=int, default=250)
    args = p.parse_args()

    load_config(args.config, require_exists=True)
    cfg = _load_config(Path(args.config))
    wallet_addr = _wallet_addr_from_label(cfg, args.wallet_label)

    adapter = AerodromeAdapter(config={"strategy_wallet": {"address": wallet_addr}})

    ok, state = await adapter.get_full_user_state(
        account=wallet_addr,
        include_zero_positions=bool(args.include_zero),
        include_usd_values=bool(args.include_usd),
        include_slipstream=not bool(args.no_slipstream),
        multicall_chunk_size=int(args.multicall_chunk),
    )
    if not ok:
        raise SystemExit(str(state))

    print(json.dumps(state, indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
