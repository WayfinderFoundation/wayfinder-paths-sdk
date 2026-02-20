#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_utils import to_checksum_address
from loguru import logger

from wayfinder_paths.core.constants.contracts import BASE_USDC
from wayfinder_paths.core.utils.contracts import deploy_contract
from wayfinder_paths.core.utils.gorlami import gorlami_fork
from wayfinder_paths.core.utils.tokens import (
    ensure_allowance,
    get_token_balance,
    get_token_decimals,
)
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id
from wayfinder_paths.mcp.utils import find_wallet_by_label

CHAIN_ID_BASE = 8453


def _raw(amount: str | int | Decimal, decimals: int) -> int:
    return int(Decimal(str(amount)) * (Decimal(10) ** int(decimals)))


def _human(amount_raw: int, decimals: int) -> Decimal:
    return Decimal(int(amount_raw)) / (Decimal(10) ** int(decimals))


def _make_sign_callback(private_key: str):
    account = Account.from_key(private_key)

    async def sign_callback(transaction: dict) -> bytes:
        signed = account.sign_transaction(transaction)
        return signed.raw_transaction

    return sign_callback


def _repo_root() -> Path:
    cur = Path(__file__).resolve()
    for parent in [cur, *cur.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


def _read_sol(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


@dataclass(frozen=True)
class DeployedContract:
    address: str
    abi: list[dict[str, Any]]


async def _deploy_from_file(
    *,
    rel_path: str,
    contract_name: str,
    constructor_args: list[Any],
    from_address: str,
    chain_id: int,
    sign_callback,
) -> DeployedContract:
    root = _repo_root()
    src_path = root / rel_path
    source_code = _read_sol(src_path)
    result = await deploy_contract(
        source_code=source_code,
        contract_name=contract_name,
        source_filename=rel_path.replace("\\", "/"),
        constructor_args=constructor_args,
        from_address=from_address,
        chain_id=chain_id,
        sign_callback=sign_callback,
        verify=False,
        escape_hatch=False,
        project_root=str(root),
    )
    return DeployedContract(
        address=to_checksum_address(result["contract_address"]),
        abi=result["abi"],
    )


async def _keccak_text(text: str) -> bytes:
    async with web3_from_chain_id(CHAIN_ID_BASE) as w3:
        return w3.keccak(text=str(text))


async def _run_flow(
    *,
    wallet_address: str,
    sign_callback,
    deposit_usdc: Decimal,
    redeem_bps: int,
    withdraw_usdc: Decimal,
    test_xchain: bool,
) -> int:
    usdc_decimals = await get_token_decimals(BASE_USDC, CHAIN_ID_BASE)

    # Fund the wallet on the fork.
    deposit_raw = _raw(deposit_usdc, usdc_decimals)
    seed_usdc_raw = max(deposit_raw * 10, _raw("50", usdc_decimals))

    native_balances = {wallet_address: 2 * 10**18}
    erc20_balances = [(BASE_USDC, wallet_address, int(seed_usdc_raw))]

    async with gorlami_fork(
        CHAIN_ID_BASE,
        native_balances=native_balances,
        erc20_balances=erc20_balances,
    ):
        logger.info("Fork ready. Deploying vault...")

        vault = await _deploy_from_file(
            rel_path="contracts/ecosystem/WayfinderVault4626.sol",
            contract_name="WayfinderVault4626",
            constructor_args=[
                BASE_USDC,
                "Wayfinder Vault Share",
                "wfV",
                wallet_address,
            ],
            from_address=wallet_address,
            chain_id=CHAIN_ID_BASE,
            sign_callback=sign_callback,
        )
        logger.info(f"Vault deployed: {vault.address}")

        # Deposit (mint V).
        await ensure_allowance(
            token_address=BASE_USDC,
            owner=wallet_address,
            spender=vault.address,
            amount=int(deposit_raw),
            chain_id=CHAIN_ID_BASE,
            signing_callback=sign_callback,
            approval_amount=int(deposit_raw),
        )
        deposit_tx = await encode_call(
            target=vault.address,
            abi=vault.abi,
            fn_name="deposit",
            args=[int(deposit_raw), wallet_address],
            from_address=wallet_address,
            chain_id=CHAIN_ID_BASE,
        )
        await send_transaction(deposit_tx, sign_callback, wait_for_receipt=True)

        async with web3_from_chain_id(CHAIN_ID_BASE) as w3:
            vault_ct = w3.eth.contract(address=vault.address, abi=vault.abi)

            shares_bal = int(
                await vault_ct.functions.balanceOf(wallet_address).call()
            )
            max_wd = int(await vault_ct.functions.maxWithdraw(wallet_address).call())
            logger.info(
                f"Post-deposit: shares={shares_bal} maxWithdraw={_human(max_wd, usdc_decimals)} USDC"
            )

            # ---- Async redeem (shares -> assets; forward priced at fulfillment) ----
            redeem_shares = (shares_bal * int(redeem_bps)) // 10_000
            if redeem_shares <= 0:
                raise RuntimeError("Redeem amount too small (increase --deposit-usdc or --redeem-bps)")

            deadline = int(time.time()) + 3600
            req_tx = await encode_call(
                target=vault.address,
                abi=vault.abi,
                fn_name="requestRedeem",
                args=[int(redeem_shares), wallet_address, wallet_address, 0, int(deadline)],
                from_address=wallet_address,
                chain_id=CHAIN_ID_BASE,
            )
            await send_transaction(req_tx, sign_callback, wait_for_receipt=True)

            proc_tx = await encode_call(
                target=vault.address,
                abi=vault.abi,
                fn_name="processQueue",
                args=[10],
                from_address=wallet_address,
                chain_id=CHAIN_ID_BASE,
            )
            await send_transaction(proc_tx, sign_callback, wait_for_receipt=True)

            usdc_after_redeem = await get_token_balance(BASE_USDC, CHAIN_ID_BASE, wallet_address)
            logger.info(
                f"After redeem+process: wallet USDC={_human(int(usdc_after_redeem), usdc_decimals)}"
            )

            # ---- Async withdraw (assets -> burn <= max shares; refund remainder) ----
            withdraw_raw = _raw(withdraw_usdc, usdc_decimals)
            shares_needed = int(await vault_ct.functions.previewWithdraw(int(withdraw_raw)).call())
            max_shares_in = shares_needed + 10  # small buffer to exercise refund path

            req_wd_tx = await encode_call(
                target=vault.address,
                abi=vault.abi,
                fn_name="requestWithdraw",
                args=[
                    int(withdraw_raw),
                    wallet_address,
                    wallet_address,
                    int(max_shares_in),
                    int(deadline),
                ],
                from_address=wallet_address,
                chain_id=CHAIN_ID_BASE,
            )
            await send_transaction(req_wd_tx, sign_callback, wait_for_receipt=True)

            await send_transaction(proc_tx, sign_callback, wait_for_receipt=True)

            usdc_after_withdraw = await get_token_balance(BASE_USDC, CHAIN_ID_BASE, wallet_address)
            shares_after = int(await vault_ct.functions.balanceOf(wallet_address).call())
            logger.info(
                f"After withdraw+process: wallet USDC={_human(int(usdc_after_withdraw), usdc_decimals)} "
                f"shares={shares_after}"
            )

            if test_xchain:
                # ---- Cross-chain accounting sanity check ----
                logger.info("Testing xchain accounting (expected/report + staleness gating)...")

                remote_chain_id = 1
                register_tx = await encode_call(
                    target=vault.address,
                    abi=vault.abi,
                    fn_name="registerChain",
                    args=[int(remote_chain_id), 3600, 0],
                    from_address=wallet_address,
                    chain_id=CHAIN_ID_BASE,
                )
                await send_transaction(register_tx, sign_callback, wait_for_receipt=True)

                # Book "expected" remote value without a report => vault becomes stale for deposits.
                bridged_out = _raw("5", usdc_decimals)
                bridge_tx = await encode_call(
                    target=vault.address,
                    abi=vault.abi,
                    fn_name="notifyBridgedOut",
                    args=[int(remote_chain_id), int(bridged_out)],
                    from_address=wallet_address,
                    chain_id=CHAIN_ID_BASE,
                )
                await send_transaction(bridge_tx, sign_callback, wait_for_receipt=True)

                max_dep = int(await vault_ct.functions.maxDeposit(wallet_address).call())
                logger.info(f"maxDeposit while stale={max_dep} (expected 0)")

                # Add an asset type + report NAV => fresh again.
                type_id = await _keccak_text("TEST_ASSET_TYPE")
                set_type_tx = await encode_call(
                    target=vault.address,
                    abi=vault.abi,
                    fn_name="setAssetType",
                    args=[type_id, True, 1000, "TEST_ASSET_TYPE"],
                    from_address=wallet_address,
                    chain_id=CHAIN_ID_BASE,
                )
                await send_transaction(set_type_tx, sign_callback, wait_for_receipt=True)

                report_tx = await encode_call(
                    target=vault.address,
                    abi=vault.abi,
                    fn_name="reportNav",
                    args=[int(remote_chain_id), 1, [type_id], [int(bridged_out)]],
                    from_address=wallet_address,
                    chain_id=CHAIN_ID_BASE,
                )
                await send_transaction(report_tx, sign_callback, wait_for_receipt=True)

                max_dep2 = int(await vault_ct.functions.maxDeposit(wallet_address).call())
                logger.info(f"maxDeposit after report={max_dep2} (expected > 0)")

        logger.info("Async vault queue dry-run complete.")
        return 0


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run: deploy WayfinderVault4626 on a Base Gorlami fork and exercise async queue + xchain accounting."
    )
    parser.add_argument("--wallet-label", default="main")
    parser.add_argument("--deposit-usdc", default="10")
    parser.add_argument("--redeem-bps", type=int, default=2500, help="Percent of shares to requestRedeem (bps)")
    parser.add_argument("--withdraw-usdc", default="1")
    parser.add_argument("--test-xchain", action="store_true", help="Also test chain accounting + staleness gating")
    args = parser.parse_args()

    wallet = find_wallet_by_label(args.wallet_label)
    if not wallet:
        raise SystemExit(f"Wallet not found: {args.wallet_label}")

    wallet_address = to_checksum_address(wallet["address"])
    private_key = wallet.get("private_key_hex") or wallet.get("private_key")
    if not private_key:
        raise SystemExit("Wallet is missing private_key_hex in config.json")

    sign_callback = _make_sign_callback(private_key)

    deposit_usdc = Decimal(str(args.deposit_usdc))
    withdraw_usdc = Decimal(str(args.withdraw_usdc))
    redeem_bps = int(args.redeem_bps)
    if not (0 < redeem_bps <= 10_000):
        raise SystemExit("--redeem-bps must be 1..10000")

    return await _run_flow(
        wallet_address=wallet_address,
        sign_callback=sign_callback,
        deposit_usdc=deposit_usdc,
        redeem_bps=redeem_bps,
        withdraw_usdc=withdraw_usdc,
        test_xchain=bool(args.test_xchain),
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
