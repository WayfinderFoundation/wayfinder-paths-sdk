from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import unquote

from starlette.requests import Request
from starlette.responses import JSONResponse

from wayfinder_paths.adapters.hyperliquid_adapter.adapter import HyperliquidAdapter
from wayfinder_paths.mcp.tools.hyperliquid import resolve_coin
from wayfinder_paths.mcp.utils import resolve_wallet_address

_PERP_SUFFIX_RE = re.compile(r"[-_ ]?perp$", re.IGNORECASE)


async def get_user_state(label: str) -> str:
    addr, _ = await resolve_wallet_address(wallet_label=label)
    if not addr:
        return json.dumps({"error": f"Wallet not found: {label}"})

    adapter = HyperliquidAdapter()
    success, data = await adapter.get_user_state(addr)
    return json.dumps(
        {"label": label, "address": addr, "success": success, "state": data}, indent=2
    )


async def get_spot_user_state(label: str) -> str:
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


async def get_mid_prices() -> str:
    adapter = HyperliquidAdapter()
    success, data = await adapter.get_all_mid_prices()
    return json.dumps({"success": success, "prices": data}, indent=2)


async def get_mid_price(coin: str) -> str:
    decoded = unquote(coin or "").strip()
    if not decoded:
        return json.dumps({"error": "Invalid coin"})

    adapter = HyperliquidAdapter()
    ok_resolve, resolved = await resolve_coin(adapter, coin=decoded)
    if not ok_resolve:
        payload = resolved if isinstance(resolved, dict) else {}
        return json.dumps(
            {
                "coin": decoded,
                "price": None,
                "success": False,
                "error": payload.get("message") or "Could not resolve coin",
            }
        )

    success, data = await adapter.get_all_mid_prices()
    price = None
    if success and isinstance(data, dict):
        raw = data.get(resolved.mid_key)
        if raw is not None:
            try:
                price = float(raw)
            except (TypeError, ValueError):
                price = None

    return json.dumps(
        {
            "coin": decoded,
            "hl_coin": resolved.hl_coin,
            "surface": resolved.surface,
            "price": price,
            "success": price is not None,
        }
    )


async def get_markets() -> str:
    adapter = HyperliquidAdapter()
    success, data = await adapter.get_meta_and_asset_ctxs()
    return json.dumps({"success": success, "markets": data}, indent=2)


async def get_spot_assets() -> str:
    adapter = HyperliquidAdapter()
    success, data = await adapter.get_spot_assets()
    return json.dumps({"success": success, "assets": data}, indent=2)


async def get_orderbook(coin: str) -> str:
    decoded = unquote(coin or "").strip()
    if not decoded:
        return json.dumps({"error": "coin is required"})

    adapter = HyperliquidAdapter()
    ok_resolve, resolved = await resolve_coin(adapter, coin=decoded)
    if not ok_resolve:
        payload = resolved if isinstance(resolved, dict) else {}
        return json.dumps(
            {
                "coin": decoded,
                "success": False,
                "error": payload.get("message") or "Could not resolve coin",
            },
            indent=2,
        )
    success, data = await adapter.get_l2_book(resolved.hl_coin, n_levels=20)
    return json.dumps(
        {
            "coin": decoded,
            "hl_coin": resolved.hl_coin,
            "surface": resolved.surface,
            "success": success,
            "book": data,
        },
        indent=2,
    )


async def get_outcomes() -> str:
    adapter = HyperliquidAdapter()
    success, data = await adapter.get_outcome_markets()
    return json.dumps({"success": success, "outcomes": data}, indent=2)


async def get_outcome_user_state(label: str) -> str:
    addr, _ = await resolve_wallet_address(wallet_label=label)
    if not addr:
        return json.dumps({"error": f"Wallet not found: {label}"})

    adapter = HyperliquidAdapter()
    success, data = await adapter.get_spot_user_state(addr)
    positions = _outcome_positions_from_spot(data) if success else []
    return json.dumps(
        {"label": label, "address": addr, "success": success, "positions": positions},
        indent=2,
    )


def _outcome_positions_from_spot(spot_state: Any) -> list[dict[str, Any]]:
    positions: list[dict[str, Any]] = []
    if not isinstance(spot_state, dict):
        return positions
    for bal in spot_state.get("balances", []):
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
    return positions


async def preflight_route(request: Request) -> JSONResponse:
    """GET /preflight/{label} — bundled HL state for opencode plugin.

    Returns spot balances, perp clearinghouse state, outcome positions, the
    live outcome market list, and the spot+perp asset id maps in one call so
    the hl-preflight plugin can show the agent everything before an HL execute
    runs (matches django's _tag_user_state pattern).
    """
    label = request.path_params["label"]
    addr, _ = await resolve_wallet_address(wallet_label=label)
    if not addr:
        return JSONResponse({"error": f"wallet not found: {label}"}, status_code=404)

    adapter = HyperliquidAdapter()
    perp_res, spot_res, outcomes_res, spot_assets_res = await asyncio.gather(
        adapter.get_user_state(addr),
        adapter.get_spot_user_state(addr),
        adapter.get_outcome_markets(),
        adapter.get_spot_assets(),
        return_exceptions=True,
    )

    def _ok(res: Any) -> tuple[bool, Any]:
        if isinstance(res, BaseException):
            return False, str(res)
        return res

    perp_ok, perp = _ok(perp_res)
    spot_ok, spot = _ok(spot_res)
    outcomes_ok, outcomes = _ok(outcomes_res)
    spot_assets_ok, spot_assets = _ok(spot_assets_res)

    return JSONResponse(
        {
            "wallet_label": label,
            "address": addr,
            "perp": perp if perp_ok else None,
            "spot": spot if spot_ok else None,
            "outcome_positions": _outcome_positions_from_spot(spot) if spot_ok else [],
            "outcome_markets": outcomes if outcomes_ok else None,
            "spot_assets": spot_assets if spot_assets_ok else None,
            "perp_assets": adapter.coin_to_asset,
        }
    )
