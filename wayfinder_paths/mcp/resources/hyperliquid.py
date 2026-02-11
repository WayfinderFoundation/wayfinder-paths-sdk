from __future__ import annotations

import json
import re

from wayfinder_paths.adapters.hyperliquid_adapter.adapter import HyperliquidAdapter
from wayfinder_paths.mcp.utils import resolve_wallet_address

_PERP_SUFFIX_RE = re.compile(r"[-_ ]?perp$", re.IGNORECASE)


async def get_user_state(label: str) -> str:
    addr, _ = resolve_wallet_address(wallet_label=label)
    if not addr:
        return json.dumps({"error": f"Wallet not found: {label}"})

    adapter = HyperliquidAdapter()
    success, data = await adapter.get_user_state(addr)
    return json.dumps(
        {"label": label, "address": addr, "success": success, "state": data}, indent=2
    )


async def get_spot_user_state(label: str) -> str:
    addr, _ = resolve_wallet_address(wallet_label=label)
    if not addr:
        return json.dumps({"error": f"Wallet not found: {label}"})

    adapter = HyperliquidAdapter()
    success, data = await adapter.get_spot_user_state(addr)
    return json.dumps(
        {"label": label, "address": addr, "success": success, "spot": data}, indent=2
    )


async def get_mid_prices() -> str:
    adapter = HyperliquidAdapter()
    success, data = await adapter.get_all_mid_prices()
    return json.dumps({"success": success, "prices": data}, indent=2)


async def get_mid_price(coin: str) -> str:
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


async def get_markets() -> str:
    adapter = HyperliquidAdapter()
    success, data = await adapter.get_meta_and_asset_ctxs()
    return json.dumps({"success": success, "markets": data}, indent=2)


async def get_spot_assets() -> str:
    adapter = HyperliquidAdapter()
    success, data = await adapter.get_spot_assets()
    return json.dumps({"success": success, "assets": data}, indent=2)


async def get_orderbook(coin: str) -> str:
    c = coin.strip()
    if not c:
        return json.dumps({"error": "coin is required"})

    adapter = HyperliquidAdapter()
    success, data = await adapter.get_l2_book(c, n_levels=20)
    return json.dumps({"coin": c, "success": success, "book": data}, indent=2)
