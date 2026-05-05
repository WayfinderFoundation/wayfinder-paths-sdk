from __future__ import annotations

import json
import re
from typing import Any

from wayfinder_paths.adapters.hyperliquid_adapter.adapter import HyperliquidAdapter
from wayfinder_paths.mcp.utils import resolve_wallet_address

_PERP_SUFFIX_RE = re.compile(r"[-_ ]?perp$", re.IGNORECASE)


async def hyperliquid_get_user_state(label: str) -> str:
    addr, _ = await resolve_wallet_address(wallet_label=label)
    if not addr:
        return json.dumps({"error": f"Wallet not found: {label}"})

    adapter = HyperliquidAdapter()
    success, data = await adapter.get_user_state(addr)
    return json.dumps(
        {"label": label, "address": addr, "success": success, "state": data}, indent=2
    )


async def hyperliquid_get_spot_user_state(label: str) -> str:
    addr, _ = await resolve_wallet_address(wallet_label=label)
    if not addr:
        return json.dumps({"error": f"Wallet not found: {label}"})

    adapter = HyperliquidAdapter()
    success, data = await adapter.get_spot_user_state(addr)
    if success and isinstance(data, dict):
        data["balances"] = [
            b
            for b in data.get("balances", [])
            if not str(b.get("coin") or "").startswith("+")
        ]
    return json.dumps(
        {"label": label, "address": addr, "success": success, "spot": data}, indent=2
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
    adapter = HyperliquidAdapter()
    success, data = await adapter.get_meta_and_asset_ctxs()
    return json.dumps({"success": success, "markets": data}, indent=2)


async def hyperliquid_get_spot_assets() -> str:
    adapter = HyperliquidAdapter()
    success, data = await adapter.get_spot_assets()
    return json.dumps({"success": success, "assets": data}, indent=2)


async def hyperliquid_get_orderbook(coin: str) -> str:
    c = coin.strip()
    if not c:
        return json.dumps({"error": "coin is required"})

    adapter = HyperliquidAdapter()
    success, data = await adapter.get_l2_book(c, n_levels=20)
    return json.dumps({"coin": c, "success": success, "book": data}, indent=2)


async def hyperliquid_get_outcomes() -> str:
    adapter = HyperliquidAdapter()
    success, data = await adapter.get_outcome_markets()
    return json.dumps({"success": success, "outcomes": data}, indent=2)


async def hyperliquid_get_outcome_user_state(label: str) -> str:
    addr, _ = await resolve_wallet_address(wallet_label=label)
    if not addr:
        return json.dumps({"error": f"Wallet not found: {label}"})

    adapter = HyperliquidAdapter()
    success, data = await adapter.get_spot_user_state(addr)
    positions: list[dict[str, Any]] = []
    if success and isinstance(data, dict):
        for bal in data.get("balances", []):
            coin = str(bal.get("coin") or "")
            if not coin.startswith("+"):
                continue
            if float(bal.get("total") or 0) == 0:
                continue
            encoding = int(coin[1:])
            positions.append(
                {
                    "coin": coin,
                    "outcome_id": encoding // 10,
                    "side": encoding % 10,
                    "total": bal.get("total"),
                    "hold": bal.get("hold"),
                    "entryNtl": bal.get("entryNtl"),
                }
            )
    return json.dumps(
        {"label": label, "address": addr, "success": success, "positions": positions},
        indent=2,
    )
