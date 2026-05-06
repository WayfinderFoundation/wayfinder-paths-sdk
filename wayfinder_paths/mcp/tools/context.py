"""Thin per-call context bundle for the wayfinder-context opencode plugin.

Stitched from the existing MCP tools (core_get_wallets, hyperliquid_get_state,
hyperliquid_get_markets, polymarket_get_state) so wallet-type handling
(local + remote) is inherited rather than reinvented. Thinning strips USD
values, mark prices, and funding rates so the payload is byte-stable across
the 5-min prompt-cache window.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from wayfinder_paths.mcp.tools.hyperliquid import (
    hyperliquid_get_markets,
    hyperliquid_get_state,
)
from wayfinder_paths.mcp.tools.polymarket import polymarket_get_state
from wayfinder_paths.mcp.tools.wallets import core_get_wallets

_TOP_N = 25


def _thin_coins(balances: Any) -> list[dict[str, Any]]:
    if not isinstance(balances, dict):
        return []
    return [
        {"symbol": b["symbol"], "balance": b["amount_decimal"], "chain": b["chain"]}
        for b in balances.get("balances", [])
        if str(b.get("chain", "")).lower() != "solana"
    ]


def _thin_hl(hl_state: dict[str, Any]) -> dict[str, Any]:
    perp = hl_state.get("perp", {}).get("state") or {}
    spot = hl_state.get("spot", {}).get("state") or {}
    return {
        "perp_positions": [
            {"coin": e["position"]["coin"], "size": e["position"]["szi"]}
            for e in perp.get("assetPositions", [])
        ],
        "spot_balances": [
            {"coin": b.get("coin"), "total": b.get("total")}
            for b in spot.get("balances", [])
            if not str(b.get("coin", "")).startswith("+")
        ],
        "outcomes": hl_state.get("outcomes", {}).get("positions", []),
    }


def _thin_pm(pm_result: dict[str, Any]) -> dict[str, Any]:
    state = pm_result.get("state") or {}
    return {
        "positions": [
            {
                "market_slug": p.get("slug"),
                "outcome": p.get("outcome"),
                "shares": p.get("size"),
            }
            for p in state.get("positions") or []
        ],
        "open_orders": [
            {
                "market_slug": o.get("slug"),
                "outcome": o.get("outcome"),
                "side": o.get("side"),
                "shares": o.get("size"),
                "price": o.get("price"),
                "oid": o.get("orderID"),
            }
            for o in state.get("openOrders") or []
        ],
    }


def _thin_universe(markets: dict[str, Any]) -> dict[str, list[str]]:
    perp = markets.get("perp", {}).get("markets") or [{}, []]
    meta = perp[0] if isinstance(perp, list) and perp else {}
    ctxs = perp[1] if isinstance(perp, list) and len(perp) > 1 else []
    paired = sorted(
        (
            (u["name"], float(c.get("dayNtlVlm") or 0))
            for u, c in zip(meta.get("universe", []), ctxs, strict=False)
        ),
        key=lambda x: x[1],
        reverse=True,
    )
    spot = markets.get("spot", {}).get("assets") or {}
    outcomes = markets.get("outcomes", {}).get("markets") or []
    return {
        "top_25_perps": [n for n, _ in paired if ":" not in n][:_TOP_N],
        "top_25_spot": list(spot.keys())[:_TOP_N],
        "all_hip3_perps": [n for n, _ in paired if ":" in n],
        "top_25_hip4": [m["name"] for m in outcomes if m.get("name")][:_TOP_N],
    }


async def _wallet_full(label: str) -> dict[str, Any]:
    hl_str, pm_dict = await asyncio.gather(
        hyperliquid_get_state(label),
        polymarket_get_state(wallet_label=label),
    )
    hl_parsed = json.loads(hl_str)
    return {
        "label": label,
        "hyperliquid": _thin_hl(hl_parsed),
        "polymarket": _thin_pm(pm_dict.get("result") or {}),
    }


async def core_get_context() -> str:
    """Thin context bundle for system-prompt injection — every wallet's state.

    Stitches `core_get_wallets`, `hyperliquid_get_state`, `polymarket_get_state`,
    and `hyperliquid_get_markets`. Per-wallet HL/PM state is fetched in parallel.
    """
    wallets_str, markets_str = await asyncio.gather(
        core_get_wallets(),
        hyperliquid_get_markets(),
    )
    wallets = json.loads(wallets_str)["wallets"]
    markets = json.loads(markets_str)

    labeled = [w for w in wallets if w.get("label") and w.get("address")]
    state_results = await asyncio.gather(*(_wallet_full(w["label"]) for w in labeled))
    state_by_label = {s["label"]: s for s in state_results}

    return json.dumps(
        {
            "labels": [w["label"] for w in wallets],
            "wallets": [
                {
                    "label": w["label"],
                    "address": w["address"],
                    "coins": _thin_coins(w.get("balances")),
                    **state_by_label.get(
                        w["label"],
                        {
                            "hyperliquid": {
                                "perp_positions": [],
                                "spot_balances": [],
                                "outcomes": [],
                            },
                            "polymarket": {"positions": [], "open_orders": []},
                        },
                    ),
                }
                for w in wallets
            ],
            "hyperliquid_universe": _thin_universe(markets),
        }
    )
