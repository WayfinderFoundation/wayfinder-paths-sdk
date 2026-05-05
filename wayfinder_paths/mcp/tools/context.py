"""Thin per-call context bundle for the wayfinder-context opencode plugin.

Stripped to byte-stable fields (no USD values, mark prices, funding rates) so
the plugin can inject this into every system prompt and still hit the 5-min
prompt-cache window.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from wayfinder_paths.adapters.hyperliquid_adapter.adapter import HyperliquidAdapter
from wayfinder_paths.adapters.polymarket_adapter.adapter import PolymarketAdapter
from wayfinder_paths.core.clients.BalanceClient import BALANCE_CLIENT
from wayfinder_paths.core.config import CONFIG
from wayfinder_paths.core.utils.wallets import (
    get_wallet_sign_hash_callback,
    get_wallet_signing_callback,
)
from wayfinder_paths.mcp.utils import load_wallets, normalize_address

_TOP_N = 25

# Module-level adapter so its aiocache (60s/300s TTLs on meta calls) survives
# across invocations. Constructing a fresh adapter per call defeats the cache.
_HL = HyperliquidAdapter()


async def _wallet_coins(addr: str) -> list[dict[str, Any]]:
    try:
        data = await BALANCE_CLIENT.get_enriched_wallet_balances(
            wallet_address=addr, exclude_spam_tokens=True
        )
    except Exception:
        return []
    return [
        {"symbol": b["symbol"], "balance": b["amount_decimal"], "chain": b["chain"]}
        for b in data["balances"]
        if b["chain"].lower() != "solana"
    ]


def _hl_universe(meta_and_ctxs: list[Any]) -> tuple[list[str], list[str]]:
    """Return (top_25_perps, all_hip3_perps) sorted by 24h notional volume."""
    meta, ctxs = meta_and_ctxs[0], meta_and_ctxs[1]
    # _aggregate in HyperliquidAdapter._post_across_dexes can return [{}, []]
    # when every dex call failed — meta won't have a "universe" key.
    paired = [
        (u["name"], float(c.get("dayNtlVlm") or 0))
        for u, c in zip(meta.get("universe", []), ctxs, strict=False)
    ]
    paired.sort(key=lambda x: x[1], reverse=True)
    standard = [n for n, _ in paired if ":" not in n][:_TOP_N]
    hip3 = [n for n, _ in paired if ":" in n]
    return standard, hip3


def _hl_outcomes(outcomes: list[dict[str, Any]]) -> list[str]:
    return [m["name"] for m in outcomes if m.get("name")][:_TOP_N]


def _hl_positions(perp_state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"coin": entry["position"]["coin"], "size": entry["position"]["szi"]}
        for entry in perp_state.get("assetPositions", [])
    ]


def _hl_open_orders(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "coin": o["coin"],
            "side": o["side"],
            "size": o["sz"],
            "px": o["limitPx"],
            "oid": o["oid"],
        }
        for o in orders
    ]


def _pm_positions(pm_state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "market_slug": p.get("slug"),
            "outcome": p.get("outcome"),
            "shares": p.get("size"),
        }
        for p in pm_state.get("positions") or []
    ]


def _pm_open_orders(pm_state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "market_slug": o.get("slug"),
            "outcome": o.get("outcome"),
            "side": o.get("side"),
            "shares": o.get("size"),
            "price": o.get("price"),
            "oid": o.get("orderID"),
        }
        for o in pm_state.get("openOrders") or []
    ]


async def core_get_context(wallet_label: str = "main") -> str:
    """Thin context bundle for system-prompt injection.

    Args:
        wallet_label: Wallet for HL/PM state lookups. Defaults to "main".
    """
    wallets = await load_wallets()
    target = next((w for w in wallets if w.get("label") == wallet_label), None)
    target_addr = normalize_address(target["address"]) if target else None

    pm_adapter: PolymarketAdapter | None = None
    coin_tasks = [
        _wallet_coins(normalize_address(w["address"]))
        for w in wallets
        if w.get("address")
    ]

    tasks: list[Any] = [
        asyncio.gather(*coin_tasks),
        _HL.get_meta_and_asset_ctxs(),
        _HL.get_spot_assets(),
        _HL.get_outcome_markets(),
    ]

    if target_addr:
        sign_cb = None
        sign_hash_cb = None
        try:
            sign_cb, _ = await get_wallet_signing_callback(target["label"])
        except ValueError:
            pass
        try:
            sign_hash_cb, _ = await get_wallet_sign_hash_callback(target["label"])
        except ValueError:
            pass
        cfg = dict(CONFIG)
        cfg["strategy_wallet"] = {"address": target_addr}
        pm_adapter = PolymarketAdapter(
            config=cfg,
            sign_callback=sign_cb,
            sign_hash_callback=sign_hash_cb,
            wallet_address=target_addr,
        )
        tasks.extend(
            [
                _HL.get_user_state(target_addr),
                _HL.get_open_orders(target_addr),
                pm_adapter.get_full_user_state(
                    account=target_addr, include_orders=True
                ),
            ]
        )
    else:
        tasks.extend(
            [
                asyncio.sleep(0, result=(False, {})),
                asyncio.sleep(0, result=(False, [])),
                asyncio.sleep(0, result=(False, {})),
            ]
        )

    try:
        results = await asyncio.gather(*tasks)
    finally:
        if pm_adapter is not None:
            await pm_adapter.close()

    coins_per_wallet = results[0]
    (meta_ok, hl_meta) = results[1]
    (spots_ok, hl_spots) = results[2]
    (outcomes_ok, hl_outcomes) = results[3]
    (perp_ok, hl_perp) = results[4]
    (orders_ok, hl_orders) = results[5]
    (pm_ok, pm_state) = results[6]

    wallets_with_addrs = [w for w in wallets if w.get("address")]
    wallets_thin = [
        {
            "label": w["label"],
            "address": normalize_address(w["address"]),
            "coins": coins,
        }
        for w, coins in zip(wallets_with_addrs, coins_per_wallet, strict=True)
    ]

    top_25_perps, all_hip3_perps = _hl_universe(hl_meta) if meta_ok else ([], [])

    return json.dumps(
        {
            "wallets": wallets_thin,
            "hyperliquid": {
                "positions": _hl_positions(hl_perp) if perp_ok else [],
                "open_orders": _hl_open_orders(hl_orders) if orders_ok else [],
                "universe": {
                    "top_25_perps": top_25_perps,
                    "top_25_spot": list(hl_spots.keys())[:_TOP_N] if spots_ok else [],
                    "all_hip3_perps": all_hip3_perps,
                    "top_25_hip4": _hl_outcomes(hl_outcomes) if outcomes_ok else [],
                },
            },
            "polymarket": {
                "positions": _pm_positions(pm_state) if pm_ok else [],
                "open_orders": _pm_open_orders(pm_state) if pm_ok else [],
            },
        }
    )
