from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from wayfinder_paths.adapters.hyperliquid_adapter.adapter import HyperliquidAdapter
from wayfinder_paths.mcp.utils import resolve_wallet_address

_PERP_SUFFIX_RE = re.compile(r"[-_ ]?perp$", re.IGNORECASE)


async def hyperliquid_get_state(label: str) -> str:
    """Return perp + spot + outcome state for a Hyperliquid wallet in one shot."""
    addr, _ = await resolve_wallet_address(wallet_label=label)
    if not addr:
        return json.dumps({"error": f"Wallet not found: {label}"})

    adapter = HyperliquidAdapter()
    perp_ok, perp = await adapter.get_user_state(addr)
    spot_ok, spot = await adapter.get_spot_user_state(addr)

    spot_balances: list[dict[str, Any]] = []
    outcome_positions: list[dict[str, Any]] = []
    if spot_ok and isinstance(spot, dict):
        for bal in spot.get("balances", []):
            coin = str(bal.get("coin") or "")
            if coin.startswith("+"):
                if float(bal.get("total") or 0) == 0:
                    continue
                encoding = int(coin[1:])
                outcome_positions.append(
                    {
                        "coin": coin,
                        "outcome_id": encoding // 10,
                        "side": encoding % 10,
                        "total": bal.get("total"),
                        "hold": bal.get("hold"),
                        "entryNtl": bal.get("entryNtl"),
                    }
                )
            else:
                spot_balances.append(bal)
        spot["balances"] = spot_balances

    return json.dumps(
        {
            "label": label,
            "address": addr,
            "perp": {"success": perp_ok, "state": perp},
            "spot": {"success": spot_ok, "state": spot},
            "outcomes": {"success": spot_ok, "positions": outcome_positions},
        },
        indent=2,
    )


async def hyperliquid_get_mid_prices() -> str:
    adapter = HyperliquidAdapter()
    success, data = await adapter.get_all_mid_prices()
    return json.dumps({"success": success, "prices": data}, indent=2)


async def hyperliquid_get_mid_price(coin: str) -> str:
    adapter = HyperliquidAdapter()
    success, data = await adapter.get_all_mid_prices()

    want = _PERP_SUFFIX_RE.sub("", coin.strip()).strip()
    if not want:
        return json.dumps({"error": "Invalid coin"})

    price = None
    if success and isinstance(data, dict):
        for k, v in data.items():
            if str(k).lower() == want.lower():
                try:
                    price = float(v)
                except (TypeError, ValueError):
                    pass
                break

    return json.dumps({"coin": want, "price": price, "success": price is not None})


async def hyperliquid_get_markets() -> str:
    """Return the full HL universe in one shot: perps (incl. HIP-3 builder dexes), spot, HIP-4 outcomes.

    `perp` already merges across every builder-deployed dex via `_post_across_dexes`, so HIP-3
    listings (e.g. `xyz:SP500`) appear alongside core perps. `outcomes` covers HIP-4 binary /
    multi-outcome markets.
    """
    adapter = HyperliquidAdapter()
    (
        (perp_ok, perp_data),
        (spot_ok, spot_data),
        (outcome_ok, outcome_data),
    ) = await asyncio.gather(
        adapter.get_meta_and_asset_ctxs(),
        adapter.get_spot_assets(),
        adapter.get_outcome_markets(),
    )
    return json.dumps(
        {
            "perp": {"success": perp_ok, "markets": perp_data},
            "spot": {"success": spot_ok, "assets": spot_data},
            "outcomes": {"success": outcome_ok, "markets": outcome_data},
        },
        indent=2,
    )


async def hyperliquid_get_orderbook(coin: str) -> str:
    c = coin.strip()
    if not c:
        return json.dumps({"error": "coin is required"})

    adapter = HyperliquidAdapter()
    success, data = await adapter.get_l2_book(c, n_levels=20)
    return json.dumps({"coin": c, "success": success, "book": data}, indent=2)
