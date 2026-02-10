from __future__ import annotations

import argparse
import asyncio
import json
import sys

from loguru import logger

from wayfinder_paths.core.config import load_config
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.contracts import BASE_USDC
from wayfinder_paths.core.utils.gorlami import gorlami_fork
from wayfinder_paths.core.utils.units import to_erc20_raw, to_wei_eth
from wayfinder_paths.run_strategy import create_signing_callback, get_strategy_config
from wayfinder_paths.strategies.moonwell_wsteth_loop_strategy.strategy import (
    MoonwellWstethLoopStrategy,
)


async def _run(args: argparse.Namespace) -> None:
    load_config(args.config, require_exists=bool(args.config))

    strategy_name = "moonwell_wsteth_loop_strategy"
    config = get_strategy_config(
        strategy_name,
        wallet_label=args.wallet_label,
        main_wallet_label=args.main_wallet_label,
    )

    main_addr = (config.get("main_wallet") or {}).get("address")
    strat_addr = (config.get("strategy_wallet") or {}).get("address")
    if not main_addr or not strat_addr:
        raise SystemExit(
            "main_wallet + strategy_wallet must be configured (address + private key) in config.json"
        )

    usdc_raw = to_erc20_raw(str(args.amount_usdc), decimals=6)
    native_balances = {
        str(main_addr): to_wei_eth(str(args.fund_main_eth)),
        str(strat_addr): to_wei_eth(str(args.fund_strategy_eth)),
    }
    erc20_balances = [(BASE_USDC, str(main_addr), int(usdc_raw))]

    def signing_cb(address: str):
        return create_signing_callback(address, config)

    async with gorlami_fork(
        CHAIN_ID_BASE,
        native_balances=native_balances,
        erc20_balances=erc20_balances,
    ) as (_, fork_info):
        print("gorlami fork:", fork_info["fork_id"], "rpc:", fork_info["rpc_url"])

        strategy = MoonwellWstethLoopStrategy(
            config,
            main_wallet_signing_callback=signing_cb(str(main_addr)),
            strategy_wallet_signing_callback=signing_cb(str(strat_addr)),
        )
        await strategy.setup()

        deposit_res = await strategy.deposit(main_token_amount=float(args.amount_usdc))
        print("deposit:", deposit_res)

        update_res = await strategy.update()
        print("update:", update_res)

        status = await strategy.status()
        print(json.dumps(status, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(
        description="Run Moonwell strategy on a Gorlami Base fork (dry run)."
    )
    p.add_argument("--config", default=None, help="Config path (default: config.json)")
    p.add_argument(
        "--wallet-label", default=None, help="Strategy wallet label override"
    )
    p.add_argument(
        "--main-wallet-label",
        default=None,
        help="Main wallet label override (default: main)",
    )
    p.add_argument(
        "--amount-usdc", type=str, default="20", help="USDC to deposit (default: 20)"
    )
    p.add_argument(
        "--fund-main-eth",
        type=str,
        default="0.2",
        help="Seed main wallet ETH (default: 0.2)",
    )
    p.add_argument(
        "--fund-strategy-eth",
        type=str,
        default="1.0",
        help="Seed strategy wallet ETH (default: 1.0)",
    )
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if args.debug else "INFO")

    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
