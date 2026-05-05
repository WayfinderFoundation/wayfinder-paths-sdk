from __future__ import annotations

import asyncio
import json
from typing import Any

from wayfinder_paths.core.clients.BalanceClient import BALANCE_CLIENT
from wayfinder_paths.mcp.state.profile_store import WalletProfileStore
from wayfinder_paths.mcp.utils import (
    find_wallet_by_label,
    load_wallets,
    normalize_address,
    public_wallet_view,
)


def _balance_usd(entry: dict[str, Any]) -> float:
    val = entry.get("balanceUSD", 0)
    try:
        return float(val or 0)
    except (TypeError, ValueError):
        return 0.0


def _strip_solana(data: Any) -> Any:
    """Drop Solana entries from an enriched-balances response (EVM-only view)."""
    if not isinstance(data, dict) or not isinstance(data.get("balances"), list):
        return data
    balances_list = [b for b in data["balances"] if isinstance(b, dict)]
    filtered = [
        b for b in balances_list if str(b.get("network", "")).lower() != "solana"
    ]
    if len(filtered) == len(balances_list):
        return data
    out = dict(data)
    out["balances"] = filtered
    out["total_balance_usd"] = sum(_balance_usd(b) for b in filtered)
    breakdown: dict[str, float] = {}
    for b in filtered:
        net = str(b.get("network") or "").strip()
        if net:
            breakdown[net] = breakdown.get(net, 0.0) + _balance_usd(b)
    out["chain_breakdown"] = breakdown
    return out


async def _fetch_balances(address: str) -> dict[str, Any] | None:
    try:
        data = await BALANCE_CLIENT.get_enriched_wallet_balances(
            wallet_address=address, exclude_spam_tokens=True
        )
        return _strip_solana(data)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


async def core_get_wallets() -> str:
    """List every configured wallet with its protocols and current balances."""
    store = WalletProfileStore.default()
    existing = await load_wallets()

    views: list[dict[str, Any]] = []
    addresses: list[str | None] = []
    for w in existing:
        view = public_wallet_view(w)
        addr = normalize_address(w.get("address"))
        view["protocols"] = store.get_protocols_for_wallet(addr.lower()) if addr else []
        views.append(view)
        addresses.append(addr)

    balances = await asyncio.gather(
        *(_fetch_balances(a) if a else asyncio.sleep(0, result=None) for a in addresses)
    )
    for view, bal in zip(views, balances, strict=True):
        view["balances"] = bal

    return json.dumps({"wallets": views}, indent=2)


async def core_get_wallet(label: str) -> str:
    store = WalletProfileStore.default()
    w = await find_wallet_by_label(label)
    if not w:
        return json.dumps({"error": f"Wallet not found: {label}"})

    address = normalize_address(w.get("address"))
    if not address:
        return json.dumps({"error": f"Invalid address for wallet: {label}"})

    profile = store.get_profile(address)
    return json.dumps(
        {
            "label": label,
            "address": address,
            "profile": profile,
        },
        indent=2,
    )


async def core_get_wallet_balances(label: str) -> str:
    w = await find_wallet_by_label(label)
    if not w:
        return json.dumps({"error": f"Wallet not found: {label}"})

    address = normalize_address(w.get("address"))
    if not address:
        return json.dumps({"error": f"Invalid address for wallet: {label}"})

    data = await _fetch_balances(address)
    if isinstance(data, dict) and "error" in data and len(data) == 1:
        return json.dumps(data)
    return json.dumps({"label": label, "address": address, "balances": data}, indent=2)


async def onchain_get_wallet_activity(label: str) -> str:
    w = await find_wallet_by_label(label)
    if not w:
        return json.dumps({"error": f"Wallet not found: {label}"})

    address = normalize_address(w.get("address"))
    if not address:
        return json.dumps({"error": f"Invalid address for wallet: {label}"})

    try:
        data = await BALANCE_CLIENT.get_wallet_activity(
            wallet_address=address, limit=20
        )
        return json.dumps(
            {
                "label": label,
                "address": address,
                "activity": data.get("activity", []),
                "next_offset": data.get("next_offset"),
            },
            indent=2,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc)})
