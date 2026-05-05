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


async def core_get_context(wallet_label: str = "main") -> str:
    """Thin context bundle for system-prompt injection.

    Args:
        wallet_label: Wallet for HL/PM state lookups. Defaults to "main".
    """
    wallets = await load_wallets()
    target = next((w for w in wallets if w.get("label") == wallet_label), None)
    target_addr = normalize_address(target["address"]) if target else None

    hl = HyperliquidAdapter()
    pm_adapter: PolymarketAdapter | None = None
    coin_tasks = [
        _wallet_coins(normalize_address(w["address"]))
        for w in wallets
        if w.get("address")
    ]

    tasks: list[Any] = [
        asyncio.gather(*coin_tasks),
        hl.get_meta_and_asset_ctxs(),
        hl.get_spot_assets(),
        hl.get_outcome_markets(),
    ]

    if target and target_addr:
        cfg = dict(CONFIG)
        cfg["strategy_wallet"] = {"address": target_addr}
        pm_adapter = PolymarketAdapter(
            config=cfg,
            sign_callback=_try_build(_build_signing_callback, target),
            sign_hash_callback=_try_build(_build_sign_hash_callback, target),
            wallet_address=target_addr,
        )
        tasks.extend(
            [
                hl.get_user_state(target_addr),
                hl.get_open_orders(target_addr),
                pm_adapter.get_full_user_state(
                    account=target_addr, include_orders=True
                ),
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
    if target_addr:
        (perp_ok, hl_perp) = results[4]
        (orders_ok, hl_orders) = results[5]
        (pm_ok, pm_state) = results[6]
    else:
        perp_ok, hl_perp = False, {}
        orders_ok, hl_orders = False, []
        pm_ok, pm_state = False, {}

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
            "wallets": [
                {
                    "label": w["label"],
                    "address": normalize_address(w["address"]),
                    "coins": coins,
                }
                for w, coins in zip(
                    [w for w in wallets if w.get("address")],
                    coins_per_wallet,
                    strict=True,
                )
            ],
            "hyperliquid": {
                "positions": [
                    {"coin": e["position"]["coin"], "size": e["position"]["szi"]}
                    for e in (hl_perp.get("assetPositions", []) if perp_ok else [])
                ],
                "open_orders": [
                    {
                        "coin": o["coin"],
                        "side": o["side"],
                        "size": o["sz"],
                        "px": o["limitPx"],
                        "oid": o["oid"],
                    }
                    for o in (hl_orders if orders_ok else [])
                ],
                "universe": {
                    "top_25_perps": [n for n, _ in paired if ":" not in n][:_TOP_N],
                    "top_25_spot": list(hl_spots.keys())[:_TOP_N] if spots_ok else [],
                    "all_hip3_perps": [n for n, _ in paired if ":" in n],
                    "top_25_hip4": [
                        m["name"]
                        for m in (hl_outcomes if outcomes_ok else [])
                        if m.get("name")
                    ][:_TOP_N],
                },
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
    )
