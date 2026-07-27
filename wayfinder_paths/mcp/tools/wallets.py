from __future__ import annotations

import asyncio
import importlib
import time
from typing import Any, Literal

from wayfinder_paths.core.clients.BalanceClient import BALANCE_CLIENT
from wayfinder_paths.core.clients.OpenCodeClient import OPENCODE_CLIENT
from wayfinder_paths.core.config import (
    load_config,
    load_wallet_mnemonic,
    resolve_config_path,
)
from wayfinder_paths.core.utils.wallets import (
    create_remote_wallet,
    make_local_wallet,
    write_wallet_to_json,
)
from wayfinder_paths.mcp.state.profile_store import WalletProfileStore
from wayfinder_paths.mcp.utils import (
    catch_errors,
    err,
    find_wallet_by_label,
    load_wallets,
    normalize_address,
    ok,
    public_wallet_view,
    resolve_wallet_address,
    throw_if_empty_str,
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
    "morpho": {
        "module": "wayfinder_paths.adapters.morpho_adapter.adapter",
        "class": "MorphoAdapter",
        "init_kwargs": {},
        "method": "get_full_user_state",
        "method_per_chain": "get_full_user_state_per_chain",
        "chain_param": "chain_id",
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
        "method_per_chain": "get_full_user_state_per_chain",
        "chain_param": "chain",
        "account_param": "account",
        "extra_kwargs": {"include_zero_positions": False},
    },
    "polymarket": {
        "module": "wayfinder_paths.adapters.polymarket_adapter.adapter",
        "class": "PolymarketAdapter",
        "init_kwargs": {},
        "method": "get_full_user_state",
        "account_param": "account",
        "extra_kwargs": {"include_orders": False},
    },
    "aave": {
        "module": "wayfinder_paths.adapters.aave_v3_adapter.adapter",
        "class": "AaveV3Adapter",
        "init_kwargs": {},
        "method": "get_full_user_state",
        "method_per_chain": "get_full_user_state_per_chain",
        "chain_param": "chain_id",
        "account_param": "account",
        "extra_kwargs": {"include_zero_positions": False},
    },
}


async def _query_adapter(
    protocol: str,
    address: str,
    include_zero_positions: bool = False,
    chain_id: int | None = None,
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

        method_name = config["method"]
        kwargs = {config["account_param"]: address, **config["extra_kwargs"]}

        if "include_zero_positions" in config["extra_kwargs"]:
            kwargs["include_zero_positions"] = include_zero_positions

        if chain_id is not None:
            method_per_chain = config.get("method_per_chain")
            chain_param = config.get("chain_param")
            if method_per_chain and chain_param:
                method_name = str(method_per_chain)
                kwargs[str(chain_param)] = int(chain_id)
            if "chain_id" in kwargs:
                kwargs["chain_id"] = int(chain_id)
            elif "chain" in kwargs:
                kwargs["chain"] = int(chain_id)

        method = getattr(adapter, method_name)
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


@catch_errors
async def core_wallets(
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
    remote: bool = False,
    policies: list[dict] = [],  # noqa: B006
    wallet_type: str | None = None,
) -> dict[str, Any]:
    """Create wallets, annotate wallet profiles, and discover cross-protocol portfolios.

    `create` needs a label; Shells requires `remote=True` plus wallet_type
    session/policy/strategy. `annotate` records protocol/action/tool/status.
    `discover_portfolio` reads touched or selected protocols; three or more
    require `parallel=True`. Read wallets with `core_get_wallets`, not config files.
    """
    config_path = resolve_config_path()
    store = WalletProfileStore.default()

    match action:
        case "create":
            load_config(config_path)
            if not remote and OPENCODE_CLIENT.healthy():
                return err(
                    "invalid_request",
                    "Local wallets are discouraged for OpenCode instances",
                )
            existing = await load_wallets()
            want = throw_if_empty_str(
                "label is required for wallets(action=create)", label or wallet_label
            )

            for w in existing:
                if str(w.get("label", "")).strip() == want:
                    return ok(
                        {
                            "wallets": [public_wallet_view(x) for x in existing],
                            "created": public_wallet_view(w),
                            "note": "Wallet label already existed; returning existing wallet.",
                        }
                    )

            if remote:
                wallet_type = throw_if_empty_str(
                    "wallet_type is required for remote wallets (one of: session, policy, strategy)",
                    wallet_type,
                )
                result = await create_remote_wallet(
                    label=want, wallet_type=wallet_type, policies=policies
                )
                refreshed = await load_wallets()
                return ok(
                    {
                        "wallets": [public_wallet_view(x) for x in refreshed],
                        "created": {
                            "label": result["evm"]["label"],
                            "evm_address": result["evm"]["wallet_address"],
                            "svm_address": result["svm"]["wallet_address"],
                        },
                    }
                )
            else:
                mnemonic = load_wallet_mnemonic()
                w = make_local_wallet(
                    label=want, existing_wallets=existing, mnemonic=mnemonic
                )
                write_wallet_to_json(
                    w, out_dir=config_path.parent, filename=config_path.name
                )
                load_config(config_path)

                refreshed = await load_wallets()
                return ok(
                    {
                        "wallets": [public_wallet_view(x) for x in refreshed],
                        "created": public_wallet_view(w),
                    }
                )

        case "annotate":
            address, lbl = await resolve_wallet_address(
                wallet_label=wallet_label or label, wallet_address=wallet_address
            )
            if not address:
                return err(
                    "invalid_request",
                    "wallet_label or wallet_address is required",
                )
            throw_if_empty_str("protocol is required for annotate", protocol)
            throw_if_empty_str(
                "annotate_action is required for annotate", annotate_action
            )
            throw_if_empty_str("tool is required for annotate", tool)
            throw_if_empty_str("status is required for annotate", status)

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

        case "discover_portfolio":
            address, lbl = await resolve_wallet_address(
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

            supported_protocols = [
                p for p in target_protocols if p in PROTOCOL_ADAPTERS
            ]
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
                    _query_adapter(
                        proto, address, include_zero_positions, chain_id=chain_id
                    )
                    for proto in supported_protocols
                ]
                results = await asyncio.gather(*tasks)
            else:
                for proto in supported_protocols:
                    result = await _query_adapter(
                        proto,
                        address,
                        include_zero_positions,
                        chain_id=chain_id,
                    )
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

        case _:
            return err("invalid_request", f"Unknown action: {action}")


async def _fetch_balances(address: str, chain_type: str) -> dict[str, Any] | None:
    # Solana wallets are base58 and must go to the svm_address param — the
    # default address param is the EVM path and returns nothing for them.
    try:
        if chain_type == "solana":
            return await BALANCE_CLIENT.get_enriched_wallet_balances(
                svm_address=address, exclude_spam_tokens=True
            )
        return await BALANCE_CLIENT.get_enriched_wallet_balances(
            wallet_address=address, exclude_spam_tokens=True
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@catch_errors
async def core_get_wallets(
    label: str | None = None, transactions_limit: int = 5
) -> dict[str, Any]:
    """List configured wallets with profile + protocols + current balances.

    Omit `label` for all wallets. Balances are fetched live for each EVM/Solana
    leg. `transactions_limit` defaults to 5; keep it under 20 unless auditing history.
    """
    store = WalletProfileStore.default()
    if label is not None:
        w = await find_wallet_by_label(label)
        if not w:
            return err("not_found", f"Wallet not found: {label}")
        existing = [w]
    else:
        existing = await load_wallets()

    views: list[dict[str, Any]] = []
    addr_chains: list[tuple[str | None, str]] = []
    for w in existing:
        view = public_wallet_view(w)
        addr = normalize_address(w.get("address"))
        if addr:
            view["protocols"] = store.get_protocols_for_wallet(addr.lower())
            view["profile"] = store.get_profile(
                addr, transactions_limit=transactions_limit
            )
        else:
            view["protocols"] = []
        views.append(view)
        addr_chains.append((addr, w.get("chain_type") or "ethereum"))

    balances = await asyncio.gather(
        *(
            _fetch_balances(a, ct) if a else asyncio.sleep(0, result=None)
            for a, ct in addr_chains
        )
    )
    for view, bal in zip(views, balances, strict=True):
        view["balances"] = bal

    return ok({"wallets": views})


@catch_errors
async def onchain_get_wallet_activity(
    label: str | None = None,
    wallet_address: str | None = None,
    limit: int = 20,
    offset: str | None = None,
) -> dict[str, Any]:
    """Return on-chain transactions for a wallet across supported chains.

    Pass a label or raw address; address wins. Continue pagination with the
    previous response's `next_offset`.
    """
    address, lbl = await resolve_wallet_address(
        wallet_label=label, wallet_address=wallet_address
    )
    if not address:
        if lbl:
            return err("not_found", f"Wallet not found: {lbl}")
        return err("invalid_request", "label or wallet_address is required")

    data = await BALANCE_CLIENT.get_wallet_activity(
        wallet_address=address, limit=limit, offset=offset
    )

    return ok(
        {
            "label": lbl,
            "address": address,
            "activity": data.get("activity", []),
            "next_offset": data.get("next_offset"),
        }
    )
