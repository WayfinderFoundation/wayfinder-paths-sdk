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

TOP_BALANCE_LIMIT = 5
ACTIVITY_LIMIT = 10


def _public_wallet_view(w: dict[str, Any]) -> dict[str, Any]:
    return {"label": w.get("label"), "address": w.get("address")}


def _balance_usd(entry: dict[str, Any]) -> float:
    val = entry.get("balanceUSD", 0)
    try:
        return float(val or 0)
    except (TypeError, ValueError):
        return 0.0


def _filter_evm_balances(data: dict[str, Any]) -> dict[str, Any]:
    balances_list = [b for b in data.get("balances", []) if isinstance(b, dict)]
    filtered = [
        b for b in balances_list if str(b.get("network", "")).lower() != "solana"
    ]
    chain_breakdown: dict[str, float] = {}
    for entry in filtered:
        network = str(entry.get("network") or "").strip()
        if network:
            chain_breakdown[network] = chain_breakdown.get(network, 0.0) + _balance_usd(
                entry
            )

    enriched = dict(data)
    enriched["balances"] = filtered
    enriched["total_balance_usd"] = sum(_balance_usd(entry) for entry in filtered)
    enriched["chain_breakdown"] = chain_breakdown
    return enriched


def _top_positions(
    balances: list[dict[str, Any]], *, limit: int = TOP_BALANCE_LIMIT
) -> list[dict[str, Any]]:
    return [
        {
            "symbol": entry.get("symbol"),
            "network": entry.get("network"),
            "balance_usd": _balance_usd(entry),
            "balance": entry.get("balance"),
            "address": entry.get("address"),
        }
        for entry in sorted(balances, key=_balance_usd, reverse=True)[:limit]
    ]


def _compact_activity(
    events: list[Any], *, limit: int = ACTIVITY_LIMIT
) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for event in events[:limit]:
        if not isinstance(event, dict):
            continue
        compact.append(
            {
                "type": event.get("type"),
                "network": event.get("network"),
                "amount": event.get("amount"),
                "symbol": event.get("symbol"),
                "timestamp": event.get("timestamp"),
                "direction": event.get("direction"),
            }
        )
    return compact


def _resolve_address(label: str) -> tuple[str, None] | tuple[None, str]:
    """Return (address, None) on success or (None, error_json) on failure."""
    w = find_wallet_by_label(label)
    if not w:
        return None, json.dumps({"error": f"Wallet not found: {label}"})
    address = normalize_address(w.get("address"))
    if not address:
        return None, json.dumps({"error": f"Invalid address for wallet: {label}"})
    return address, None


async def list_wallets() -> str:
    store = WalletProfileStore.default()
    existing = load_wallets()
    wallet_list = []
    for w in existing:
        view = _public_wallet_view(w)
        addr = normalize_address(w.get("address"))
        view["protocols"] = store.get_protocols_for_wallet(addr.lower()) if addr else []
        wallet_list.append(view)
    return json.dumps({"wallets": wallet_list, "detail_level": "route"}, indent=2)


async def _fetch_balances(address: str) -> dict[str, Any]:
    data = await BALANCE_CLIENT.get_enriched_wallet_balances(
        wallet_address=address,
        exclude_spam_tokens=True,
    )
    return _filter_evm_balances(data if isinstance(data, dict) else {})


def _safe_activity(data: Any) -> tuple[list[Any], Any]:
    if not isinstance(data, dict):
        return [], None
    return data.get("activity", []), data.get("next_offset")


async def get_wallet(label: str) -> str:
    store = WalletProfileStore.default()
    address, error = _resolve_address(label)
    if error:
        return error

    profile = store.get_profile(address)
    profile_summary = {
        "protocols": sorted(profile.keys()) if isinstance(profile, dict) else [],
        "annotation_count": sum(len(v) for v in profile.values() if isinstance(v, list))
        if isinstance(profile, dict)
        else 0,
    }
    return json.dumps(
        {
            "label": label,
            "address": address,
            "profile_summary": profile_summary,
            "detail_uri": f"wayfinder://wallets/{label}/full",
        },
        indent=2,
    )


async def get_wallet_full(label: str) -> str:
    store = WalletProfileStore.default()
    address, error = _resolve_address(label)
    if error:
        return error

    profile = store.get_profile(address)
    return json.dumps(
        {
            "label": label,
            "address": address,
            "profile": profile,
            "detail_level": "full",
        },
        indent=2,
    )


async def get_wallet_balances(label: str) -> str:
    address, error = _resolve_address(label)
    if error:
        return error

    try:
        data = await _fetch_balances(address)
        balances_list = [b for b in data.get("balances", []) if isinstance(b, dict)]
        return json.dumps(
            {
                "label": label,
                "address": address,
                "balances": {
                    "total_balance_usd": data.get("total_balance_usd", 0),
                    "chain_breakdown": data.get("chain_breakdown", {}),
                    "position_count": len(balances_list),
                    "top_positions": _top_positions(balances_list),
                    "detail_uri": f"wayfinder://balances/{label}/full",
                },
            },
            indent=2,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc)})


async def get_wallet_balances_full(label: str) -> str:
    address, error = _resolve_address(label)
    if error:
        return error

    try:
        data = await _fetch_balances(address)
        return json.dumps(
            {
                "label": label,
                "address": address,
                "balances": data,
                "detail_level": "full",
            },
            indent=2,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc)})


async def get_wallet_activity(label: str) -> str:
    address, error = _resolve_address(label)
    if error:
        return error

    try:
        raw = await BALANCE_CLIENT.get_wallet_activity(
            wallet_address=address, limit=ACTIVITY_LIMIT
        )
        activity, next_offset = _safe_activity(raw)
        return json.dumps(
            {
                "label": label,
                "address": address,
                "activity": _compact_activity(activity),
                "next_offset": next_offset,
                "detail_uri": f"wayfinder://activity/{label}/full",
            },
            indent=2,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc)})


async def get_wallet_activity_full(label: str) -> str:
    address, error = _resolve_address(label)
    if error:
        return error

    try:
        raw = await BALANCE_CLIENT.get_wallet_activity(wallet_address=address, limit=20)
        activity, next_offset = _safe_activity(raw)
        return json.dumps(
            {
                "label": label,
                "address": address,
                "activity": activity,
                "next_offset": next_offset,
                "detail_level": "full",
            },
            indent=2,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc)})
