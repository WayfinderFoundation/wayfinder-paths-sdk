"""Thin per-call context bundle for the wayfinder-context opencode plugin.

Returns a deliberately stripped-down view (coin balances, HL positions/orders,
top market names, polymarket positions/orders) so the plugin can inject this
into every system prompt without blowing up cost — and without volatile
mark-to-market fields that would invalidate prompt caching every tick.
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


async def _wallet_coins(addr: str) -> list[dict[str, Any]]:
    try:
        data = await BALANCE_CLIENT.get_enriched_wallet_balances(
            wallet_address=addr, exclude_spam_tokens=True
        )
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    out: list[dict[str, Any]] = []
    for b in data.get("balances") or []:
        if not isinstance(b, dict):
            continue
        if str(b.get("network", "")).lower() == "solana":
            continue
        out.append(
            {
                "symbol": b.get("symbol"),
                "balance": b.get("balance"),
                "chain": b.get("network"),
            }
        )
    return out


def _hl_universe(meta_and_ctxs: Any) -> tuple[list[str], list[str]]:
    """Return (top_25_perps, all_hip3_perps) sorted by 24h notional volume."""
    if not isinstance(meta_and_ctxs, list) or len(meta_and_ctxs) < 2:
        return [], []
    meta, ctxs = meta_and_ctxs[0], meta_and_ctxs[1]
    if not isinstance(meta, dict) or not isinstance(ctxs, list):
        return [], []
    paired: list[tuple[str, float]] = []
    for u, c in zip(meta.get("universe") or [], ctxs, strict=False):
        if not isinstance(u, dict) or not isinstance(c, dict):
            continue
        name = str(u.get("name") or "")
        if not name:
            continue
        try:
            vol = float(c.get("dayNtlVlm") or 0)
        except (TypeError, ValueError):
            vol = 0.0
        paired.append((name, vol))
    paired.sort(key=lambda x: x[1], reverse=True)
    standard = [n for n, _ in paired if ":" not in n][:_TOP_N]
    hip3 = [n for n, _ in paired if ":" in n]
    return standard, hip3


def _hl_spots(spot_assets: Any) -> list[str]:
    if not isinstance(spot_assets, dict):
        return []
    return list(spot_assets.keys())[:_TOP_N]


def _hl_outcomes(outcomes: Any) -> list[str]:
    if not isinstance(outcomes, list):
        return []
    names: list[str] = []
    for m in outcomes:
        if isinstance(m, dict):
            n = m.get("name") or m.get("title") or m.get("description")
            if n:
                names.append(str(n))
    return names[:_TOP_N]


def _hl_positions(perp_state: Any) -> list[dict[str, Any]]:
    if not isinstance(perp_state, dict):
        return []
    out: list[dict[str, Any]] = []
    for entry in perp_state.get("assetPositions") or []:
        if not isinstance(entry, dict):
            continue
        pos = entry.get("position") or {}
        if isinstance(pos, dict) and pos.get("coin"):
            out.append({"coin": pos.get("coin"), "size": pos.get("szi")})
    return out


def _hl_open_orders(orders: Any) -> list[dict[str, Any]]:
    if not isinstance(orders, list):
        return []
    out: list[dict[str, Any]] = []
    for o in orders:
        if not isinstance(o, dict):
            continue
        out.append(
            {
                "coin": o.get("coin"),
                "side": o.get("side"),
                "size": o.get("sz"),
                "px": o.get("limitPx"),
                "oid": o.get("oid"),
            }
        )
    return out


def _pm_positions(pm_state: Any) -> list[dict[str, Any]]:
    if not isinstance(pm_state, dict):
        return []
    out: list[dict[str, Any]] = []
    for p in pm_state.get("positions") or []:
        if not isinstance(p, dict):
            continue
        out.append(
            {
                "market_slug": p.get("slug") or p.get("market_slug"),
                "outcome": p.get("outcome"),
                "shares": p.get("size"),
            }
        )
    return out


def _pm_open_orders(pm_state: Any) -> list[dict[str, Any]]:
    if not isinstance(pm_state, dict):
        return []
    src = pm_state.get("openOrders") or pm_state.get("open_orders") or []
    if not isinstance(src, list):
        return []
    out: list[dict[str, Any]] = []
    for o in src:
        if not isinstance(o, dict):
            continue
        out.append(
            {
                "market_slug": o.get("market_slug") or o.get("slug"),
                "outcome": o.get("outcome"),
                "side": o.get("side"),
                "shares": o.get("size"),
                "price": o.get("price"),
                "oid": o.get("orderID") or o.get("oid"),
            }
        )
    return out


async def core_get_context(wallet_label: str = "main") -> str:
    """Thin context bundle for system-prompt injection.

    Returns coin balances per wallet, HL positions/open-orders/top-market-names,
    and Polymarket positions/open-orders. Strips USD values, mark prices, and
    funding rates so the payload is byte-stable across the 5-min prompt-cache
    window.

    Args:
        wallet_label: Wallet for HL/PM state lookups. Defaults to "main".
    """
    wallets = await load_wallets()
    target = next((w for w in wallets if w.get("label") == wallet_label), None)
    target_addr = normalize_address(target.get("address")) if target else None

    hl = HyperliquidAdapter()

    coin_tasks = [
        _wallet_coins(normalize_address(w.get("address")))
        for w in wallets
        if w.get("address")
    ]

    if target_addr:
        hl_perp_task = hl.get_user_state(target_addr)
        hl_orders_task = hl.get_open_orders(target_addr)
    else:
        hl_perp_task = asyncio.sleep(0, result=(False, {}))
        hl_orders_task = asyncio.sleep(0, result=(False, []))

    pm_adapter: PolymarketAdapter | None = None
    if target and target_addr:
        sign_cb = None
        sign_hash_cb = None
        try:
            sign_cb, _ = await get_wallet_signing_callback(target.get("label"))
        except ValueError:
            pass
        try:
            sign_hash_cb, _ = await get_wallet_sign_hash_callback(target.get("label"))
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
        pm_state_task = pm_adapter.get_full_user_state(
            account=target_addr, include_orders=True
        )
    else:
        pm_state_task = asyncio.sleep(0, result=(False, {}))

    try:
        (
            coins_per_wallet,
            hl_meta,
            hl_spots,
            hl_outcomes,
            hl_perp,
            hl_orders,
            pm_state,
        ) = await asyncio.gather(
            asyncio.gather(*coin_tasks) if coin_tasks else asyncio.sleep(0, result=[]),
            hl.get_meta_and_asset_ctxs(),
            hl.get_spot_assets(),
            hl.get_outcome_markets(),
            hl_perp_task,
            hl_orders_task,
            pm_state_task,
        )
    finally:
        if pm_adapter is not None:
            await pm_adapter.close()

    wallets_thin: list[dict[str, Any]] = []
    coins_iter = iter(coins_per_wallet)
    for w in wallets:
        if not w.get("address"):
            continue
        wallets_thin.append(
            {
                "label": w.get("label"),
                "address": normalize_address(w.get("address")),
                "coins": next(coins_iter, []),
            }
        )

    top_25_perps, all_hip3_perps = _hl_universe(
        hl_meta[1] if isinstance(hl_meta, tuple) else None
    )

    return json.dumps(
        {
            "wallets": wallets_thin,
            "hyperliquid": {
                "positions": _hl_positions(
                    hl_perp[1] if isinstance(hl_perp, tuple) else None
                ),
                "open_orders": _hl_open_orders(
                    hl_orders[1] if isinstance(hl_orders, tuple) else None
                ),
                "universe": {
                    "top_25_perps": top_25_perps,
                    "top_25_spot": _hl_spots(
                        hl_spots[1] if isinstance(hl_spots, tuple) else None
                    ),
                    "all_hip3_perps": all_hip3_perps,
                    "top_25_hip4": _hl_outcomes(
                        hl_outcomes[1] if isinstance(hl_outcomes, tuple) else None
                    ),
                },
            },
            "polymarket": {
                "positions": _pm_positions(
                    pm_state[1] if isinstance(pm_state, tuple) else None
                ),
                "open_orders": _pm_open_orders(
                    pm_state[1] if isinstance(pm_state, tuple) else None
                ),
            },
        },
        indent=2,
    )
