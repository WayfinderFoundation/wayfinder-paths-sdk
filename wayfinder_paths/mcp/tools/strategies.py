from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any, Literal

from wayfinder_paths.core.config import CONFIG
from wayfinder_paths.core.engine.manifest import load_strategy_manifest
from wayfinder_paths.core.strategies.Strategy import Strategy
from wayfinder_paths.core.utils.wallets import get_wallet_signing_callback
from wayfinder_paths.mcp.utils import (
    catch_errors,
    err,
    ok,
    repo_root,
    throw_if_empty_str,
    throw_if_none,
)


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
    return getattr(module, class_name), manifest.status


def _get_strategy_config(strategy_name: str) -> dict[str, Any]:
    config = dict(CONFIG.get("strategy", {}))
    if "strategies" in CONFIG:
        config["strategies"] = CONFIG["strategies"]
    wallets = {w["label"]: w for w in CONFIG.get("wallets", [])}

    if "main_wallet" not in config and "main" in wallets:
        config["main_wallet"] = {"address": wallets["main"]["address"]}
    if "strategy_wallet" not in config and strategy_name in wallets:
        config["strategy_wallet"] = {"address": wallets[strategy_name]["address"]}
    return config


@catch_errors
async def core_run_strategy(
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
    """Run a lifecycle action against an installed strategy.

    Strategies are discovered via `wayfinder://strategies`. Each one implements the same surface;
    not every action is supported by every strategy (returns `not_supported` if missing).

    Read-only actions:
      - `status`: current positions, balances, internal state
      - `analyze`: simulate behavior at a hypothetical `amount_usdc` deposit
      - `snapshot`: build batch snapshot for scoring (uses `amount_usdc`)
      - `policy`: declared permission policy (no instance needed)
      - `quote`: point-in-time expected APY for `amount_usdc`

    Fund-moving actions (trigger safety review):
      - `deposit`: requires `main_token_amount`; optional `gas_token_amount` (recommend `0.001`
        on first deposit). `amount` is accepted as a back-compat alias for `main_token_amount`.
      - `update`: rebalance / execute the strategy's recurring logic
      - `withdraw`: liquidate all positions to stablecoins (funds stay in strategy wallet);
        partial withdraw is unsupported — leave `amount` unset.
      - `exit`: transfer remaining funds from strategy wallet → main wallet. Run after `withdraw`.

    Args:
        strategy: Strategy name (folder under `wayfinder_paths/strategies/`).
        action: Lifecycle action above.
        amount_usdc: Used by `analyze` / `snapshot` / `quote` (default 1000).
        main_token_amount / gas_token_amount: Deposit sizing.
        amount: Back-compat alias for `main_token_amount` on deposit.
    """
    throw_if_empty_str("strategy is required", strategy)

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
        res = pol()  # type: ignore[misc]
        if asyncio.iscoroutine(res):
            res = await res
        return ok_with_warning({"strategy": strategy, "action": action, "output": res})

    config = _get_strategy_config(strategy)

    try:
        main_cb, _ = await get_wallet_signing_callback("main")
    except ValueError:
        main_cb = None
    try:
        strategy_cb, _ = await get_wallet_signing_callback(strategy)
    except ValueError:
        strategy_cb = None

    try:
        strategy_obj = strategy_class(
            config,
            main_wallet_signing_callback=main_cb,
            strategy_wallet_signing_callback=strategy_cb,
        )
    except TypeError:
        try:
            strategy_obj = strategy_class(config=config)
        except TypeError:
            strategy_obj = strategy_class()

    if hasattr(strategy_obj, "setup"):
        await strategy_obj.setup()

    match action:
        case "status":
            out = await strategy_obj.status()
            return ok_with_warning(
                {"strategy": strategy, "action": action, "output": out}
            )

        case "analyze":
            if hasattr(strategy_obj, "analyze"):
                out = await strategy_obj.analyze(deposit_usdc=amount_usdc)
                return ok_with_warning(
                    {"strategy": strategy, "action": action, "output": out}
                )
            return err("not_supported", "Strategy does not support analyze()")

        case "snapshot":
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

        case "quote":
            if hasattr(strategy_obj, "quote"):
                out = await strategy_obj.quote(deposit_amount=amount_usdc)
                return ok_with_warning(
                    {"strategy": strategy, "action": action, "output": out}
                )
            return err("not_supported", "Strategy does not support quote()")

        case "deposit":
            # Prefer the canonical strategy kwargs (main_token_amount + gas_token_amount).
            # Back-compat: allow callers to pass `amount` as the main token amount.
            if main_token_amount is None:
                main_token_amount = amount
            throw_if_none(
                "main_token_amount required for deposit (optionally gas_token_amount)",
                main_token_amount,
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

        case "update":
            success, msg = await strategy_obj.update()
            return ok_with_warning(
                {
                    "strategy": strategy,
                    "action": action,
                    "success": success,
                    "message": msg,
                }
            )

        case "withdraw":
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

        case "exit":
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

        case _:
            return err("invalid_request", f"Unknown action: {action}")
