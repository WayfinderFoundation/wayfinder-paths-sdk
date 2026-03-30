from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any, Literal

from wayfinder_paths.core.config import CONFIG
from wayfinder_paths.core.engine.manifest import load_strategy_manifest
from wayfinder_paths.core.strategies.Strategy import Strategy
from wayfinder_paths.core.utils.privy_policies import preview_timebound_rules
from wayfinder_paths.core.utils.wallets import get_wallet_signing_callback
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


async def run_strategy(
    *,
    strategy: str,
    action: Literal[
        "status",
        "analyze",
        "snapshot",
        "policy",
        "policy_preview",
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

    if action in {"policy", "policy_preview"}:
        pol = getattr(strategy_class, "policies", None)
        if not callable(pol):
            if action == "policy_preview":
                policies, policy_status = preview_timebound_rules([])
                return ok_with_warning(
                    {
                        "strategy": strategy,
                        "action": action,
                        "output": policies,
                        "policy_status": policy_status,
                    }
                )
            return ok_with_warning(
                {"strategy": strategy, "action": action, "output": []}
            )
        try:
            res = pol()  # type: ignore[misc]
            if asyncio.iscoroutine(res):
                res = await res
            if action == "policy_preview":
                policies, policy_status = preview_timebound_rules(res)
                return ok_with_warning(
                    {
                        "strategy": strategy,
                        "action": action,
                        "output": policies,
                        "policy_status": policy_status,
                    }
                )
            return ok_with_warning(
                {"strategy": strategy, "action": action, "output": res}
            )
        except Exception as exc:  # noqa: BLE001
            return err("strategy_error", str(exc))

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
