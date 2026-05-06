"""Thin per-call context bundle for the wayfinder-context opencode plugin.

Stripped to byte-stable fields (no USD values, mark prices, funding rates) so
the plugin can inject this into every system prompt and still hit the 5-min
prompt-cache window.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from wayfinder_paths.adapters.hyperliquid_adapter import HyperliquidAdapter
from wayfinder_paths.adapters.polymarket_adapter.adapter import PolymarketAdapter
from wayfinder_paths.core.clients.BalanceClient import BALANCE_CLIENT
from wayfinder_paths.core.config import CONFIG
from wayfinder_paths.core.utils.wallets import (
    _build_sign_hash_callback,
    _build_signing_callback,
)
from wayfinder_paths.mcp.utils import load_wallets, normalize_address

_TOP_N = 25


def _try_build(builder: Any, wallet: dict[str, Any]) -> Any:
    try:
        cb, _ = builder(wallet, wallet["label"])
    except ValueError:
        return None
    return cb


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


async def _wallet_state(
    wallet: dict[str, Any], hl: HyperliquidAdapter
) -> dict[str, Any]:
    """Per-wallet thin state: coins + HL positions/open_orders + PM positions/open_orders."""
    addr = normalize_address(wallet["address"])
    cfg = dict(CONFIG)
    cfg["strategy_wallet"] = {"address": addr}
    pm = PolymarketAdapter(
        config=cfg,
        sign_callback=_try_build(_build_signing_callback, wallet),
        sign_hash_callback=_try_build(_build_sign_hash_callback, wallet),
        wallet_address=addr,
    )
    try:
        (
            coins,
            (perp_ok, perp),
            (orders_ok, orders),
            (pm_ok, pm_state),
        ) = await asyncio.gather(
            _wallet_coins(addr),
            hl.get_user_state(addr),
            hl.get_open_orders(addr),
            pm.get_full_user_state(account=addr, include_orders=True),
        )
    finally:
        await pm.close()

    return {
        "label": wallet["label"],
        "address": addr,
        "coins": coins,
        "hyperliquid": {
            "positions": [
                {"coin": e["position"]["coin"], "size": e["position"]["szi"]}
                for e in (perp.get("assetPositions", []) if perp_ok else [])
            ],
            "open_orders": [
                {
                    "coin": o["coin"],
                    "side": o["side"],
                    "size": o["sz"],
                    "px": o["limitPx"],
                    "oid": o["oid"],
                }
                for o in (orders if orders_ok else [])
            ],
        },
        "polymarket": {
            "positions": [
                {
                    "market_slug": p.get("slug"),
                    "outcome": p.get("outcome"),
                    "shares": p.get("size"),
                }
                for p in (pm_state.get("positions") or [] if pm_ok else [])
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
                for o in (pm_state.get("openOrders") or [] if pm_ok else [])
            ],
        },
    }


async def core_get_context() -> str:
    """Thin context bundle for system-prompt injection — every wallet's state.

    Top-level `labels` lists every configured wallet label (cheap reference);
    `wallets` carries each wallet's coin balances + HL positions/open_orders +
    Polymarket positions/open_orders. `hyperliquid_universe` is the shared
    market-name set.
    """
    wallets = await load_wallets()
    addressed = [w for w in wallets if w.get("address")]
    hl = HyperliquidAdapter()

    universe_task = asyncio.gather(
        hl.get_meta_and_asset_ctxs(),
        hl.get_spot_assets(),
        hl.get_outcome_markets(),
    )
    state_tasks = [_wallet_state(w, hl) for w in addressed]

    universe_results, wallet_states = await asyncio.gather(
        universe_task,
        asyncio.gather(*state_tasks),
    )
    (meta_ok, hl_meta), (spots_ok, hl_spots), (outcomes_ok, hl_outcomes) = (
        universe_results
    )

    # Universe sort by 24h notional volume; HIP-3 names contain ":".
    # _post_across_dexes returns [{}, []] when all dex calls fail; .get guards.
    paired: list[tuple[str, float]] = []
    if meta_ok:
        meta, ctxs = hl_meta[0], hl_meta[1]
        paired = [
            (u["name"], float(c.get("dayNtlVlm") or 0))
            for u, c in zip(meta.get("universe", []), ctxs, strict=False)
        ]
        paired.sort(key=lambda x: x[1], reverse=True)

    return json.dumps(
        {
            "labels": [w["label"] for w in wallets],
            "wallets": wallet_states,
            "hyperliquid_universe": {
                "top_25_perps": [n for n, _ in paired if ":" not in n][:_TOP_N],
                "top_25_spot": list(hl_spots.keys())[:_TOP_N] if spots_ok else [],
                "all_hip3_perps": [n for n, _ in paired if ":" in n],
                "top_25_hip4": [
                    m["name"]
                    for m in (hl_outcomes if outcomes_ok else [])
                    if m.get("name")
                ][:_TOP_N],
            },
        }
    )
