#!/usr/bin/env python3

# Allow running as a script: `python wayfinder_paths/run_strategy.py ...`
if __name__ == "__main__" and __package__ is None:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import asyncio
import importlib
import inspect
import json
import sys
from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger

from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.core.config import CONFIG, load_config
from wayfinder_paths.core.strategies.Strategy import Strategy
from wayfinder_paths.core.utils.evm_helpers import resolve_private_key_for_from_address
from wayfinder_paths.core.utils.gorlami import gorlami_fork
from wayfinder_paths.core.utils.web3 import get_transaction_chain_id, web3_from_chain_id


def get_strategy_config(
    strategy_name: str,
    *,
    wallet_label: str | None = None,
    main_wallet_label: str | None = None,
) -> dict[str, Any]:
    config = dict(CONFIG.get("strategy", {}))
    wallets = {w["label"]: w for w in CONFIG.get("wallets", [])}

    main_label = str(main_wallet_label).strip() if main_wallet_label else "main"
    strat_label = str(wallet_label).strip() if wallet_label else strategy_name

    if "main_wallet" not in config and main_label in wallets:
        config["main_wallet"] = {"address": wallets[main_label]["address"]}
    if "strategy_wallet" not in config and strat_label in wallets:
        config["strategy_wallet"] = {"address": wallets[strat_label]["address"]}

    by_addr = {w["address"].lower(): w for w in CONFIG.get("wallets", [])}
    for key in ("main_wallet", "strategy_wallet"):
        if wallet := config.get(key):
            if entry := by_addr.get(wallet.get("address", "").lower()):
                if pk := entry.get("private_key") or entry.get("private_key_hex"):
                    wallet["private_key_hex"] = pk
    return config


def create_signing_callback(address: str, config: dict[str, Any]):
    async def sign(transaction: dict) -> str:
        pk = resolve_private_key_for_from_address(address, config)
        async with web3_from_chain_id(get_transaction_chain_id(transaction)) as web3:
            return web3.eth.account.sign_transaction(
                transaction, pk
            ).raw_transaction.hex()

    return sign


def find_strategy_class(module) -> type[Strategy]:
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, Strategy) and obj is not Strategy:
            return obj
    raise ValueError(f"No Strategy subclass found in {module.__name__}")


def _to_wei_eth(amount_eth: str) -> int:
    try:
        amt = Decimal(amount_eth)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid ETH amount: {amount_eth}") from exc
    if amt < 0:
        raise ValueError("Amount must be non-negative")
    return int((amt * Decimal(10**18)).to_integral_value())


def _to_erc20_raw(amount_tokens: str, decimals: int) -> int:
    try:
        amt = Decimal(amount_tokens)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid token amount: {amount_tokens}") from exc
    if amt < 0:
        raise ValueError("Amount must be non-negative")
    scale = Decimal(10) ** int(decimals)
    return int((amt * scale).to_integral_value())


def _parse_native_funds(specs: list[str]) -> dict[str, int]:
    balances: dict[str, int] = {}
    for spec in specs:
        parts = [p.strip() for p in str(spec).split(":", 1)]
        if len(parts) != 2:
            raise ValueError(f"Invalid --gorlami-fund-native-eth: {spec}")
        addr, eth_amount = parts
        balances[addr] = _to_wei_eth(eth_amount)
    return balances


def _parse_erc20_funds(specs: list[str]) -> list[tuple[str, str, int]]:
    balances: list[tuple[str, str, int]] = []
    for spec in specs:
        parts = [p.strip() for p in str(spec).split(":")]
        if len(parts) != 4:
            raise ValueError(f"Invalid --gorlami-fund-erc20: {spec}")
        token, wallet, amount, decimals = parts
        balances.append((token, wallet, _to_erc20_raw(amount, int(decimals))))
    return balances


async def _infer_chain_id(module, strategy_cls: type[Strategy]) -> int:
    chain_id = getattr(module, "BASE_CHAIN_ID", None)
    if isinstance(chain_id, int):
        return int(chain_id)

    info = getattr(strategy_cls, "INFO", None)
    token_id = None
    if info is not None:
        token_id = getattr(info, "deposit_token_id", None) or getattr(info, "gas_token_id", None)
    if isinstance(token_id, str) and token_id.strip():
        details = await TOKEN_CLIENT.get_token_details(token_id.strip())
        chain = details.get("chain") or {}
        if isinstance(chain, dict) and chain.get("id") is not None:
            return int(chain["id"])
    raise ValueError(
        "Unable to infer chain id for --gorlami. Pass --gorlami-chain-id explicitly."
    )


async def run_strategy(strategy_name: str, action: str = "status", **kw):
    gorlami = bool(kw.pop("gorlami", False))
    gorlami_chain_id = kw.pop("gorlami_chain_id", None)
    gorlami_fund_native_eth = kw.pop("gorlami_fund_native_eth", []) or []
    gorlami_fund_erc20 = kw.pop("gorlami_fund_erc20", []) or []
    gorlami_no_default_gas = bool(kw.pop("gorlami_no_default_gas", False))

    wallet_label = kw.pop("wallet_label", None)
    main_wallet_label = kw.pop("main_wallet_label", None)
    config = get_strategy_config(
        strategy_name,
        wallet_label=wallet_label,
        main_wallet_label=main_wallet_label,
    )

    def signing_cb(key: str):
        if addr := config.get(key, {}).get("address"):
            return create_signing_callback(addr, config)
        return None

    module = importlib.import_module(
        f"wayfinder_paths.strategies.{strategy_name}.strategy"
    )
    strategy_cls = find_strategy_class(module)

    async def _run() -> Any:
        strategy = strategy_cls(
            config,
            main_wallet_signing_callback=signing_cb("main_wallet"),
            strategy_wallet_signing_callback=signing_cb("strategy_wallet"),
        )
        await strategy.setup()

        if action == "policy":
            policies = await strategy.policies() if hasattr(strategy, "policies") else []
            if wallet_id := kw.get("wallet_id"):
                policies = [p.replace("FORMAT_WALLET_ID", wallet_id) for p in policies]
            return {"policies": policies}
        if action == "status":
            return await strategy.status()
        if action == "deposit":
            return await strategy.deposit(
                main_token_amount=kw.get("main_token_amount") or 0.0,
                gas_token_amount=kw.get("gas_token_amount") or 0.0,
            )
        if action == "withdraw":
            return await strategy.withdraw(
                max_wait_s=kw.get("max_wait_s"),
                poll_interval_s=kw.get("poll_interval_s"),
            )
        if action == "update":
            return await strategy.update()
        if action == "exit":
            return await strategy.exit()
        if action == "analyze":
            if not hasattr(strategy, "analyze"):
                raise ValueError(f"Strategy {strategy_name} does not support analyze")
            deposit_usdc = kw.get("main_token_amount") or kw.get("deposit_usdc") or 1000.0
            verbose = kw.get("verbose", True)
            return await strategy.analyze(
                deposit_usdc=float(deposit_usdc),
                verbose=bool(verbose),
            )
        if action == "quote":
            if not hasattr(strategy, "quote"):
                raise ValueError(f"Strategy {strategy_name} does not support quote")
            deposit_amount = kw.get("amount") or kw.get("main_token_amount")
            return await strategy.quote(deposit_amount=deposit_amount)
        if action == "run":
            while True:
                try:
                    result = await strategy.update()
                    logger.info(f"Update: {result}")
                    await asyncio.sleep(kw.get("interval", 60))
                except asyncio.CancelledError:
                    return (True, "stopped")
        raise ValueError(f"Unknown action: {action}")

    if gorlami:
        chain_id = int(gorlami_chain_id) if gorlami_chain_id is not None else await _infer_chain_id(module, strategy_cls)

        native_balances = _parse_native_funds(list(gorlami_fund_native_eth))
        if not gorlami_no_default_gas:
            default_gas = int(Decimal("0.1") * Decimal(10**18))
            for key in ("main_wallet", "strategy_wallet"):
                addr = config.get(key, {}).get("address")
                if addr and addr not in native_balances:
                    native_balances[addr] = default_gas

        erc20_balances = _parse_erc20_funds(list(gorlami_fund_erc20))

        if action == "deposit" and not gorlami_fund_erc20:
            info = getattr(strategy_cls, "INFO", None)
            token_id = getattr(info, "deposit_token_id", None) if info is not None else None
            main_addr = config.get("main_wallet", {}).get("address")
            amount = kw.get("main_token_amount")
            if isinstance(token_id, str) and main_addr and amount:
                try:
                    details = await TOKEN_CLIENT.get_token_details(token_id.strip())
                    token_address = details.get("address")
                    decimals = int(details.get("decimals", 18) or 18)
                    if token_address:
                        erc20_balances.append(
                            (str(token_address), str(main_addr), _to_erc20_raw(str(amount), decimals))
                        )
                except Exception:
                    # best-effort auto-funding only
                    pass

        async with gorlami_fork(
            chain_id,
            native_balances=native_balances or None,
            erc20_balances=erc20_balances or None,
        ):
            result = await _run()
    else:
        result = await _run()

    print(
        json.dumps(result, indent=2)
        if isinstance(result, dict)
        else f"{action}: {result}"
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument(
        "strategy_pos",
        nargs="?",
        help="Strategy name (positional; or use --strategy)",
    )
    p.add_argument(
        "--strategy",
        dest="strategy",
        default=None,
        help="Strategy name (preferred over positional)",
    )
    p.add_argument(
        "--action",
        default="status",
        choices=[
            "run",
            "deposit",
            "withdraw",
            "status",
            "update",
            "exit",
            "policy",
            "analyze",
            "quote",
        ],
    )
    p.add_argument(
        "--config",
        default=None,
        help="Path to config JSON (default: config.json in CWD)",
    )
    p.add_argument("--amount", type=float)
    p.add_argument("--main-token-amount", type=float, dest="main_token_amount")
    p.add_argument(
        "--gas-token-amount", type=float, dest="gas_token_amount", default=0.0
    )
    p.add_argument("--interval", type=int, default=60)
    p.add_argument(
        "--max-wait-s",
        type=int,
        dest="max_wait_s",
        default=None,
        help="Max seconds to wait for async withdrawals/bridges (withdraw action only)",
    )
    p.add_argument(
        "--poll-interval-s",
        type=int,
        dest="poll_interval_s",
        default=None,
        help="Polling interval seconds for withdraw waits (withdraw action only)",
    )
    p.add_argument("--wallet-id", dest="wallet_id")
    p.add_argument(
        "--wallet-label",
        dest="wallet_label",
        default=None,
        help="Wallet label to use as the strategy wallet (overrides strategy name lookup)",
    )
    p.add_argument(
        "--main-wallet-label",
        dest="main_wallet_label",
        default=None,
        help="Wallet label to use as the main wallet (default: main)",
    )
    p.add_argument("--debug", action="store_true")
    p.add_argument(
        "--gorlami",
        action="store_true",
        help="Run against a Gorlami fork (dry run).",
    )
    p.add_argument(
        "--gorlami-chain-id",
        type=int,
        default=None,
        help="Chain ID to fork (defaults to strategy deposit token chain when possible).",
    )
    p.add_argument(
        "--gorlami-fund-native-eth",
        action="append",
        default=[],
        help="Seed native balance as ADDRESS:ETH (e.g. 0xabc...:0.1).",
    )
    p.add_argument(
        "--gorlami-fund-erc20",
        action="append",
        default=[],
        help="Seed ERC20 balance as TOKEN:WALLET:AMOUNT:DECIMALS (AMOUNT in tokens).",
    )
    p.add_argument(
        "--gorlami-no-default-gas",
        action="store_true",
        help="Disable default gas seeding (0.1 ETH to main+strategy wallets).",
    )
    args = p.parse_args()

    strategy_name = args.strategy or args.strategy_pos
    if not strategy_name:
        raise SystemExit("strategy is required (positional or via --strategy)")

    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if args.debug else "INFO")

    config_path = args.config or "config.json"
    try:
        load_config(config_path, require_exists=bool(args.config))
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc

    asyncio.run(
        run_strategy(
            str(strategy_name),
            args.action,
            amount=args.amount,
            main_token_amount=args.main_token_amount,
            gas_token_amount=args.gas_token_amount,
            interval=args.interval,
            max_wait_s=args.max_wait_s,
            poll_interval_s=args.poll_interval_s,
            wallet_id=args.wallet_id,
            wallet_label=args.wallet_label,
            main_wallet_label=args.main_wallet_label,
            gorlami=args.gorlami,
            gorlami_chain_id=args.gorlami_chain_id,
            gorlami_fund_native_eth=args.gorlami_fund_native_eth,
            gorlami_fund_erc20=args.gorlami_fund_erc20,
            gorlami_no_default_gas=args.gorlami_no_default_gas,
        )
    )


if __name__ == "__main__":
    main()
