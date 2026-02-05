from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any, Literal

from wayfinder_paths.core.config import CONFIG, load_config
from wayfinder_paths.core.engine.manifest import load_strategy_manifest
from wayfinder_paths.core.strategies.Strategy import Strategy
from wayfinder_paths.core.utils.evm_helpers import resolve_private_key_for_from_address
from wayfinder_paths.core.utils.web3 import get_transaction_chain_id, web3_from_chain_id
from wayfinder_paths.mcp.utils import err, ok, repo_root


def _strategy_dir(name: str) -> Path:
    return repo_root() / "wayfinder_paths" / "strategies" / name


def _load_strategy_class(strategy_name: str) -> tuple[type[Strategy], str]:
    """Load strategy class and return (class, status)."""
    manifest_path = _strategy_dir(strategy_name) / "manifest.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing manifest.yaml for strategy: {strategy_name}")
    manifest = load_strategy_manifest(str(manifest_path))
    module_path, class_name = manifest.entrypoint.rsplit(".", 1)
    module = importlib.import_module(module_path)
    importlib.reload(module)  # pick up code changes without MCP restart
    return getattr(module, class_name), manifest.status


def _get_strategy_config(strategy_name: str) -> dict[str, Any]:
    load_config()  # re-read config.json so new wallets are visible
    config = dict(CONFIG.get("strategy", {}))
    wallets = {w["label"]: w for w in CONFIG.get("wallets", [])}

    if "main_wallet" not in config and "main" in wallets:
        config["main_wallet"] = {"address": wallets["main"]["address"]}
    if "strategy_wallet" not in config and strategy_name in wallets:
        config["strategy_wallet"] = {"address": wallets[strategy_name]["address"]}

    by_addr = {w["address"].lower(): w for w in CONFIG.get("wallets", [])}
    for key in ("main_wallet", "strategy_wallet"):
        if wallet := config.get(key):
            if entry := by_addr.get(wallet.get("address", "").lower()):
                if pk := entry.get("private_key") or entry.get("private_key_hex"):
                    wallet["private_key_hex"] = pk
    return config


def _create_signing_callback(address: str, config: dict[str, Any]):
    async def sign(transaction: dict) -> str:
        pk = resolve_private_key_for_from_address(address, config)
        async with web3_from_chain_id(get_transaction_chain_id(transaction)) as web3:
            return web3.eth.account.sign_transaction(
                transaction, pk
            ).raw_transaction.hex()

    return sign


async def run_strategy(
    *,
    strategy: str,
    action: Literal[
        "status",
        "analyze",
        "snapshot",
        "policy",
        "quote",
        "deposit",
        "update",
        "withdraw",
        "exit",
    ],
    amount_usdc: float = 1000.0,
    main_token_amount: float | None = None,
    gas_token_amount: float = 0.0,
    amount: float | None = None,
) -> dict[str, Any]:
    if not strategy.strip():
        return err("invalid_request", "strategy is required")

    try:
        strategy_class, strategy_status = _load_strategy_class(strategy)
    except Exception as exc:  # noqa: BLE001
        return err("not_found", str(exc))

    wip_warning = None
    if strategy_status == "wip":
        wip_warning = f"Strategy '{strategy}' is marked as work-in-progress (WIP). It may have incomplete features or known issues."

    def ok_with_warning(result: dict[str, Any]) -> dict[str, Any]:
        response = ok(result)
        if wip_warning:
            response["warning"] = wip_warning
        return response

    if action == "policy":
        pol = getattr(strategy_class, "policies", None)
        if not callable(pol):
            return ok_with_warning(
                {"strategy": strategy, "action": action, "output": []}
            )
        try:
            res = pol()  # type: ignore[misc]
            if asyncio.iscoroutine(res):
                res = await res
            return ok_with_warning(
                {"strategy": strategy, "action": action, "output": res}
            )
        except Exception as exc:  # noqa: BLE001
            return err("strategy_error", str(exc))

    config = _get_strategy_config(strategy)

    def signing_cb(key: str):
        if addr := config.get(key, {}).get("address"):
            return _create_signing_callback(addr, config)
        return None

    try:
        strategy_obj = strategy_class(
            config,
            main_wallet_signing_callback=signing_cb("main_wallet"),
            strategy_wallet_signing_callback=signing_cb("strategy_wallet"),
        )
    except TypeError:
        try:
            strategy_obj = strategy_class(config=config)
        except TypeError:
            strategy_obj = strategy_class()

    try:
        if hasattr(strategy_obj, "setup"):
            await strategy_obj.setup()

        if action == "status":
            out = await strategy_obj.status()
            return ok_with_warning(
                {"strategy": strategy, "action": action, "output": out}
            )

        if action == "analyze":
            if hasattr(strategy_obj, "analyze"):
                out = await strategy_obj.analyze(deposit_usdc=amount_usdc)
                return ok_with_warning(
                    {"strategy": strategy, "action": action, "output": out}
                )
            return err("not_supported", "Strategy does not support analyze()")

        if action == "snapshot":
            if hasattr(strategy_obj, "build_batch_snapshot"):
                out = await strategy_obj.build_batch_snapshot(
                    score_deposit_usdc=amount_usdc
                )
                return ok_with_warning(
                    {"strategy": strategy, "action": action, "output": out}
                )
            return err(
                "not_supported", "Strategy does not support build_batch_snapshot()"
            )

        if action == "quote":
            if hasattr(strategy_obj, "quote"):
                out = await strategy_obj.quote(deposit_amount=amount_usdc)
                return ok_with_warning(
                    {"strategy": strategy, "action": action, "output": out}
                )
            return err("not_supported", "Strategy does not support quote()")

        if action == "deposit":
            # Prefer the canonical strategy kwargs (main_token_amount + gas_token_amount).
            # Back-compat: allow callers to pass `amount` as the main token amount.
            if main_token_amount is None:
                main_token_amount = amount
            if main_token_amount is None:
                return err(
                    "invalid_request",
                    "main_token_amount required for deposit (optionally gas_token_amount)",
                )
            success, msg = await strategy_obj.deposit(
                main_token_amount=float(main_token_amount),
                gas_token_amount=float(gas_token_amount),
            )
            return ok_with_warning(
                {
                    "strategy": strategy,
                    "action": action,
                    "success": success,
                    "message": msg,
                }
            )

        if action == "update":
            success, msg = await strategy_obj.update()
            return ok_with_warning(
                {
                    "strategy": strategy,
                    "action": action,
                    "success": success,
                    "message": msg,
                }
            )

        if action == "withdraw":
            if amount is not None:
                return err(
                    "not_supported",
                    "partial withdraw is not supported; omit amount",
                )
            success, msg = await strategy_obj.withdraw()
            return ok_with_warning(
                {
                    "strategy": strategy,
                    "action": action,
                    "success": success,
                    "message": msg,
                }
            )

        if action == "exit":
            if hasattr(strategy_obj, "exit"):
                success, msg = await strategy_obj.exit()
                return ok_with_warning(
                    {
                        "strategy": strategy,
                        "action": action,
                        "success": success,
                        "message": msg,
                    }
                )
            return err("not_supported", "Strategy does not support exit()")

        return err("invalid_request", f"Unknown action: {action}")
    except Exception as exc:  # noqa: BLE001
        return err("strategy_error", str(exc))
