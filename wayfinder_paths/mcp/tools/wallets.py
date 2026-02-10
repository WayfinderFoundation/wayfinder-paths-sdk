from __future__ import annotations

import asyncio
import importlib
import time
from typing import Any, Literal

from wayfinder_paths.core.utils.wallets import make_random_wallet, write_wallet_to_json
from wayfinder_paths.mcp.state.profile_store import WalletProfileStore
from wayfinder_paths.mcp.utils import (
    err,
    load_wallets,
    ok,
    repo_root,
    resolve_wallet_address,
)

PROTOCOL_ADAPTERS: dict[str, dict[str, Any]] = {
    "hyperliquid": {
        "module": "wayfinder_paths.adapters.hyperliquid_adapter.adapter",
        "class": "HyperliquidAdapter",
        "init_kwargs": {},
        "method": "get_full_user_state",
        "account_param": "account",
        "extra_kwargs": {},
    },
    "hyperlend": {
        "module": "wayfinder_paths.adapters.hyperlend_adapter.adapter",
        "class": "HyperlendAdapter",
        "init_kwargs": {},
        "method": "get_full_user_state",
        "account_param": "account",
        "extra_kwargs": {"include_zero_positions": False},
    },
    "moonwell": {
        "module": "wayfinder_paths.adapters.moonwell_adapter.adapter",
        "class": "MoonwellAdapter",
        "init_kwargs": {},
        "method": "get_full_user_state",
        "account_param": "account",
        "extra_kwargs": {"include_zero_positions": False},
    },
    "boros": {
        "module": "wayfinder_paths.adapters.boros_adapter.adapter",
        "class": "BorosAdapter",
        "init_kwargs": {},
        "method": "get_full_user_state",
        "account_param": "account",
        "extra_kwargs": {},
    },
    "pendle": {
        "module": "wayfinder_paths.adapters.pendle_adapter.adapter",
        "class": "PendleAdapter",
        "init_kwargs": {},
        "method": "get_full_user_state",
        "account_param": "account",
        "extra_kwargs": {"chain": 42161, "include_zero_positions": False},
    },
}


def _public_wallet_view(w: dict[str, Any]) -> dict[str, Any]:
    return {"label": w.get("label"), "address": w.get("address")}


async def _query_adapter(
    protocol: str,
    address: str,
    include_zero_positions: bool = False,
) -> dict[str, Any]:
    config = PROTOCOL_ADAPTERS.get(protocol)
    if not config:
        return {
            "protocol": protocol,
            "ok": False,
            "error": f"Unknown protocol: {protocol}",
        }

    start = time.time()
    try:
        module = importlib.import_module(config["module"])
        adapter_class = getattr(module, config["class"])
        adapter = adapter_class(**config["init_kwargs"])

        method = getattr(adapter, config["method"])
        kwargs = {config["account_param"]: address, **config["extra_kwargs"]}

        if "include_zero_positions" in config["extra_kwargs"]:
            kwargs["include_zero_positions"] = include_zero_positions

        success, data = await method(**kwargs)
        duration = time.time() - start

        return {
            "protocol": protocol,
            "ok": bool(success),
            "data": data if success else None,
            "error": data if not success else None,
            "duration_s": round(duration, 3),
        }

    except Exception as exc:
        duration = time.time() - start
        return {
            "protocol": protocol,
            "ok": False,
            "error": str(exc),
            "duration_s": round(duration, 3),
        }


async def wallets(
    action: Literal["create", "annotate", "discover_portfolio"],
    *,
    label: str | None = None,
    wallet_label: str | None = None,
    wallet_address: str | None = None,
    protocol: str | None = None,
    annotate_action: str | None = None,
    tool: str | None = None,
    status: str | None = None,
    chain_id: int | None = None,
    details: dict[str, Any] | None = None,
    protocols: list[str] | None = None,
    parallel: bool = False,
    include_zero_positions: bool = False,
) -> dict[str, Any]:
    root = repo_root()
    store = WalletProfileStore.default()

    if action == "create":
        existing = load_wallets()
        want = (label or "").strip()
        if not want:
            return err(
                "invalid_request", "label is required for wallets(action=create)"
            )

        for w in existing:
            if str(w.get("label", "")).strip() == want:
                return ok(
                    {
                        "config_path": "config.json",
                        "wallets": [_public_wallet_view(x) for x in existing],
                        "created": _public_wallet_view(w),
                        "note": "Wallet label already existed; returning existing wallet.",
                    }
                )

        w = make_random_wallet()
        w["label"] = want
        write_wallet_to_json(w, out_dir=root, filename="config.json")

        refreshed = load_wallets()
        return ok(
            {
                "config_path": "config.json",
                "wallets": [_public_wallet_view(x) for x in refreshed],
                "created": _public_wallet_view(w),
            }
        )

    if action == "annotate":
        address, lbl = resolve_wallet_address(
            wallet_label=wallet_label or label, wallet_address=wallet_address
        )
        if not address:
            return err(
                "invalid_request",
                "wallet_label or wallet_address is required",
            )
        if not protocol:
            return err("invalid_request", "protocol is required for annotate")
        if not annotate_action:
            return err("invalid_request", "annotate_action is required for annotate")
        if not tool:
            return err("invalid_request", "tool is required for annotate")
        if not status:
            return err("invalid_request", "status is required for annotate")

        store.annotate(
            address=address,
            label=lbl,
            protocol=protocol,
            action=annotate_action,
            tool=tool,
            status=status,
            chain_id=chain_id,
            details=details,
        )

        return ok(
            {
                "action": "annotate",
                "address": address,
                "protocol": protocol,
                "annotated": True,
            }
        )

    if action == "discover_portfolio":
        address, lbl = resolve_wallet_address(
            wallet_label=wallet_label or label, wallet_address=wallet_address
        )
        if not address:
            return err(
                "invalid_request",
                "wallet_label or wallet_address is required for discover_portfolio",
            )

        profile_protocols = store.get_protocols_for_wallet(address)

        if protocols:
            target_protocols = list(dict.fromkeys(protocols))
        else:
            target_protocols = profile_protocols

        supported_protocols = [p for p in target_protocols if p in PROTOCOL_ADAPTERS]
        unsupported = [p for p in target_protocols if p not in PROTOCOL_ADAPTERS]

        if not supported_protocols:
            return ok(
                {
                    "action": "discover_portfolio",
                    "address": address,
                    "label": lbl,
                    "profile_protocols": profile_protocols,
                    "positions": [],
                    "warning": "No supported protocols to query",
                    "unsupported_protocols": unsupported,
                }
            )

        if len(supported_protocols) >= 3 and not parallel:
            return ok(
                {
                    "action": "discover_portfolio",
                    "address": address,
                    "label": lbl,
                    "profile_protocols": profile_protocols,
                    "supported_protocols": supported_protocols,
                    "requires_confirmation": True,
                    "warning": f"Found {len(supported_protocols)} protocols to query. "
                    f"Set parallel=true for concurrent queries, or filter with protocols=[...] "
                    f"to query specific protocols.",
                    "protocols_to_query": supported_protocols,
                }
            )

        start = time.time()
        results: list[dict[str, Any]] = []

        if parallel:
            tasks = [
                _query_adapter(proto, address, include_zero_positions)
                for proto in supported_protocols
            ]
            results = await asyncio.gather(*tasks)
        else:
            for proto in supported_protocols:
                result = await _query_adapter(proto, address, include_zero_positions)
                results.append(result)

        total_duration = time.time() - start
        all_positions: list[dict[str, Any]] = []
        for r in results:
            if r.get("ok") and r.get("data"):
                data = r["data"]
                positions = data.get("positions", [])
                if positions:
                    for pos in positions:
                        all_positions.append(
                            {"protocol": r["protocol"], "position": pos}
                        )
                r["data"] = data

        return ok(
            {
                "action": "discover_portfolio",
                "address": address,
                "label": lbl,
                "profile_protocols": profile_protocols,
                "queried_protocols": supported_protocols,
                "results": results,
                "positions_count": len(all_positions),
                "positions_summary": all_positions[:10],
                "total_duration_s": round(total_duration, 3),
                "parallel": parallel,
                "unsupported_protocols": unsupported if unsupported else None,
            }
        )

    return err("invalid_request", f"Unknown action: {action}")
