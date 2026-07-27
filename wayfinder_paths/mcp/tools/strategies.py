from __future__ import annotations

import asyncio
import math
from typing import Annotated, Any, Literal

from pydantic import Field

from wayfinder_paths.core.config import CONFIG
from wayfinder_paths.core.engine.manifest import load_strategy_manifest
from wayfinder_paths.core.engine.strategy_loader import load_strategy_module
from wayfinder_paths.core.strategies.Strategy import Strategy
from wayfinder_paths.core.utils.wallets import get_wallet_signing_callback
from wayfinder_paths.mcp.arg_validation import MCPArgumentError
from wayfinder_paths.mcp.utils import (
    catch_errors,
    err,
    ok,
    throw_if_empty_str,
    throw_if_none,
)


def _load_strategy_class(strategy_name: str) -> tuple[type[Strategy], str]:
    """Load strategy class and return (class, status)."""
    module, strat_dir = load_strategy_module(strategy_name)
    manifest = load_strategy_manifest(str(strat_dir / "manifest.yaml"))
    _, class_name = manifest.entrypoint.rsplit(".", 1)
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
    strategy: Annotated[
        str,
        Field(description="Exact installed strategy name from discovery."),
    ],
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
        "reconcile",
    ],
    amount_usdc: Annotated[
        float,
        Field(description="Hypothetical USDC amount for analyze/snapshot/quote."),
    ] = 1000.0,
    main_token_amount: Annotated[
        float | None,
        Field(description="Human-unit main asset amount; required for deposit."),
    ] = None,
    gas_token_amount: Annotated[
        float,
        Field(description="Optional human-unit native gas asset amount for deposit."),
    ] = 0.0,
    amount: Annotated[
        float | None,
        Field(
            description=(
                "Legacy deposit alias for main_token_amount; omit for withdraw/exit."
            )
        ),
    ] = None,
    start: str | None = None,
    end: str | None = None,
    no_fills: bool = False,
) -> dict[str, Any]:
    """Run a lifecycle action against an installed strategy.

    Discover exact names first. status/analyze/snapshot/policy/quote are reads.
    deposit/update/withdraw/exit move funds: deposit needs `main_token_amount`
    (`amount` is legacy) and first deposits may need gas; withdraw fully
    liquidates into the strategy wallet, then exit transfers to main. `reconcile`
    is an ActivePerpsStrategy diagnostic and writes a report.
    """
    throw_if_empty_str("strategy is required", strategy)
    if action in {"analyze", "snapshot", "quote"} and (
        not math.isfinite(float(amount_usdc)) or float(amount_usdc) <= 0
    ):
        raise MCPArgumentError(
            "amount_usdc must be a positive finite number for this read action",
            field="amount_usdc",
            received=amount_usdc,
            suggested_arguments={"amount_usdc": 1000.0},
        )
    if action == "deposit":
        if main_token_amount is None:
            main_token_amount = amount
        if main_token_amount is None:
            raise MCPArgumentError(
                "main_token_amount is required for deposit; amount is a legacy alias",
                field="main_token_amount",
                received=main_token_amount,
                suggested_arguments={"main_token_amount": 1.0},
            )
        if not math.isfinite(float(main_token_amount)) or float(main_token_amount) <= 0:
            raise MCPArgumentError(
                "main_token_amount must be a positive finite human-unit amount",
                field="main_token_amount",
                received=main_token_amount,
            )
        if not math.isfinite(float(gas_token_amount)) or float(gas_token_amount) < 0:
            raise MCPArgumentError(
                "gas_token_amount must be a non-negative finite human-unit amount",
                field="gas_token_amount",
                received=gas_token_amount,
                suggested_arguments={"gas_token_amount": 0.0},
            )
    if action == "withdraw" and amount is not None:
        return err(
            "not_supported",
            "partial withdraw is not supported; omit amount, then use exit to "
            "transfer from the strategy wallet to main",
        )

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

        case "reconcile":
            if not hasattr(strategy_obj, "reconcile"):
                return err(
                    "not_supported",
                    "Strategy does not support reconcile() — only ActivePerpsStrategy subclasses do",
                )
            report = await strategy_obj.reconcile(
                start=start,
                end=end,
                no_fills=no_fills,
            )
            return ok_with_warning(
                {"strategy": strategy, "action": action, "output": report}
            )

        case _:
            return err("invalid_request", f"Unknown action: {action}")
