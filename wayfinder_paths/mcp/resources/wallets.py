from __future__ import annotations

import json
from typing import Any

from wayfinder_paths.core.clients.BalanceClient import BALANCE_CLIENT
from wayfinder_paths.mcp.state.profile_store import WalletProfileStore
from wayfinder_paths.mcp.utils import (
    find_wallet_by_label,
    load_wallets,
    normalize_address,
)


def _public_wallet_view(w: dict[str, Any]) -> dict[str, Any]:
    return {"label": w.get("label"), "address": w.get("address")}


async def list_wallets() -> str:
    store = WalletProfileStore.default()
    existing = load_wallets()
    wallet_list = []
    for w in existing:
        view = _public_wallet_view(w)
        addr = normalize_address(w.get("address"))
        if addr:
            tracked = store.get_protocols_for_wallet(addr.lower())
            view["protocols"] = tracked
        else:
            view["protocols"] = []
        wallet_list.append(view)
    return json.dumps({"wallets": wallet_list}, indent=2)


async def get_wallet(label: str) -> str:
    store = WalletProfileStore.default()
    w = find_wallet_by_label(label)
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


def _balance_usd(entry: dict[str, Any]) -> float:
    val = entry.get("balanceUSD", 0)
    try:
        return float(val or 0)
    except (TypeError, ValueError):
        return 0.0


async def get_wallet_balances(label: str) -> str:
    w = find_wallet_by_label(label)
    if not w:
        return json.dumps({"error": f"Wallet not found: {label}"})

    address = normalize_address(w.get("address"))
    if not address:
        return json.dumps({"error": f"Invalid address for wallet: {label}"})

    try:
        data = await BALANCE_CLIENT.get_enriched_wallet_balances(
            wallet_address=address,
            exclude_spam_tokens=True,
        )
        # Filter out Solana by default (EVM wallets)
        if isinstance(data, dict) and isinstance(data.get("balances"), list):
            balances_list = [b for b in data["balances"] if isinstance(b, dict)]
            filtered = [
                b
                for b in balances_list
                if str(b.get("network", "")).lower() != "solana"
            ]
            if len(filtered) != len(balances_list):
                data = dict(data)
                data["balances"] = filtered
                data["total_balance_usd"] = sum(_balance_usd(b) for b in filtered)
                breakdown: dict[str, float] = {}
                for b in filtered:
                    net = str(b.get("network") or "").strip()
                    if net:
                        breakdown[net] = breakdown.get(net, 0.0) + _balance_usd(b)
                data["chain_breakdown"] = breakdown

        return json.dumps(
            {"label": label, "address": address, "balances": data}, indent=2
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc)})


async def get_wallet_activity(label: str) -> str:
    w = find_wallet_by_label(label)
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
