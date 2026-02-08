#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from eth_account import Account
from eth_utils import to_checksum_address
from web3 import Web3

from wayfinder_paths.adapters.aerodrome_adapter.adapter import AerodromeAdapter
from wayfinder_paths.core.config import load_config
from wayfinder_paths.core.constants.aerodrome import BASE_AERO
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.contracts import BASE_USDC, ZERO_ADDRESS
from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI
from wayfinder_paths.core.utils.etherscan import get_etherscan_transaction_link
from wayfinder_paths.core.utils.tokens import get_token_balance
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

TRANSFER_TOPIC0 = Web3.keccak(text="Transfer(address,address,uint256)").hex()


def _load_config(path: Path) -> dict:
    return json.loads(path.read_text())


def _wallet_from_label(cfg: dict, wallet_label: str) -> tuple[str, str]:
    wallets = cfg.get("wallets") or []
    for wallet in wallets:
        if wallet.get("label") != wallet_label:
            continue
        addr = wallet.get("address")
        pk = wallet.get("private_key") or wallet.get("private_key_hex")
        if not addr or not pk:
            raise SystemExit(f"Wallet '{wallet_label}' missing address/private_key")
        return to_checksum_address(addr), str(pk)
    raise SystemExit(f"Wallet label '{wallet_label}' not found in config.json")


def _parse_token_id_from_create_lock_receipt(
    receipt: dict,
    *,
    ve_address: str,
    to_addr: str,
) -> int:
    ve_address = ve_address.lower()
    to_addr = to_addr.lower()

    logs = receipt.get("logs") or []
    for log in logs:
        try:
            if str(log.get("address", "")).lower() != ve_address:
                continue
            topics = log.get("topics") or []
            if len(topics) < 4:
                continue

            topic0 = topics[0].hex() if hasattr(topics[0], "hex") else str(topics[0])
            if topic0.lower() != TRANSFER_TOPIC0.lower():
                continue

            from_topic = (
                topics[1].hex() if hasattr(topics[1], "hex") else str(topics[1])
            )
            to_topic = topics[2].hex() if hasattr(topics[2], "hex") else str(topics[2])
            token_id_topic = (
                topics[3].hex() if hasattr(topics[3], "hex") else str(topics[3])
            )

            # ERC721 indexed topics are 32-byte values.
            from_addr = "0x" + from_topic[-40:]
            to_addr_log = "0x" + to_topic[-40:]
            if int(from_addr, 16) != 0:
                continue
            if to_addr_log.lower() != to_addr:
                continue
            return int(token_id_topic, 16)
        except Exception:
            continue
    raise RuntimeError("Unable to parse veNFT tokenId from createLock receipt")


async def _erc20_balance(token: str, wallet: str) -> int:
    return await get_token_balance(
        token_address=to_checksum_address(token),
        chain_id=CHAIN_ID_BASE,
        wallet_address=to_checksum_address(wallet),
    )


async def _erc20_decimals(token: str) -> int:
    async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
        c = web3.eth.contract(address=to_checksum_address(token), abi=ERC20_ABI)
        return int(await c.functions.decimals().call())


def _fmt(amount_raw: int, decimals: int) -> str:
    return f"{amount_raw / (10**decimals):,.6f}"


async def main() -> int:
    p = argparse.ArgumentParser(
        description="Live Aerodrome (Base) LP + veAERO lock + vote smoke test",
    )
    p.add_argument("--config", default="config.json")
    p.add_argument("--wallet-label", default="main")
    p.add_argument(
        "--usdc-swap", type=float, default=2.0, help="USDC to swap into AERO"
    )
    p.add_argument(
        "--lock-aero",
        type=float,
        default=2.0,
        help="AERO to lock into veNFT (will clamp to available)",
    )
    p.add_argument("--lock-weeks", type=int, default=4)
    p.add_argument(
        "--usdc-liquidity",
        type=float,
        default=1.0,
        help="USDC to pair with AERO for LP",
    )
    p.add_argument(
        "--pool-stable",
        action="store_true",
        help="Use stable pool (default: volatile)",
    )
    p.add_argument(
        "--slippage-bps",
        type=int,
        default=100,
        help="Swap slippage in bps (default 100 = 1 percent)",
    )
    args = p.parse_args()

    # Ensure global CONFIG (rpc_urls, etc.) matches the config file used by this run.
    load_config(args.config, require_exists=True)

    cfg = _load_config(Path(args.config))
    wallet_addr, pk = _wallet_from_label(cfg, args.wallet_label)

    account = Account.from_key(pk)

    async def sign_callback(tx: dict) -> bytes:
        signed = account.sign_transaction(tx)
        return signed.raw_transaction

    adapter = AerodromeAdapter(
        config={"strategy_wallet": {"address": wallet_addr}},
        strategy_wallet_signing_callback=sign_callback,
    )

    usdc_dec = await _erc20_decimals(BASE_USDC)
    aero_dec = await _erc20_decimals(BASE_AERO)

    usdc_before = await _erc20_balance(BASE_USDC, wallet_addr)
    aero_before = await _erc20_balance(BASE_AERO, wallet_addr)
    print(f"wallet={wallet_addr}")
    print(
        f"USDC(before)={_fmt(usdc_before, usdc_dec)} "
        f"AERO(before)={_fmt(aero_before, aero_dec)}"
    )

    # 1) Swap USDC -> AERO
    usdc_swap_raw = int(args.usdc_swap * (10**usdc_dec))
    if usdc_swap_raw > 0:
        if usdc_before < usdc_swap_raw:
            raise SystemExit(
                "Insufficient USDC: have "
                f"{_fmt(usdc_before, usdc_dec)} need {_fmt(usdc_swap_raw, usdc_dec)}"
            )
        tx_hash, route, out_min = await adapter.swap_exact_tokens_for_tokens(
            token_in=BASE_USDC,
            token_out=BASE_AERO,
            amount_in=usdc_swap_raw,
            slippage_bps=int(args.slippage_bps),
        )
        print(
            "swap tx",
            tx_hash,
            get_etherscan_transaction_link(CHAIN_ID_BASE, tx_hash),
        )
        print(f"swap route stable={route.stable} outMin={_fmt(out_min, aero_dec)}")

    aero_after_swap = await _erc20_balance(BASE_AERO, wallet_addr)
    print(f"AERO(after swap)={_fmt(aero_after_swap, aero_dec)}")

    # 2) Create ve lock
    lock_duration_s = int(args.lock_weeks) * 7 * 24 * 60 * 60
    requested_lock_raw = int(args.lock_aero * (10**aero_dec))
    min_left_for_lp_raw = int(0.1 * (10**aero_dec)) if args.usdc_liquidity > 0 else 0
    max_lock_raw = max(int(aero_after_swap) - min_left_for_lp_raw, 0)
    lock_raw = min(requested_lock_raw, max_lock_raw)
    if lock_raw <= 0:
        raise SystemExit(
            "Not enough AERO to lock: have "
            f"{_fmt(aero_after_swap, aero_dec)}; need > "
            f"{_fmt(min_left_for_lp_raw, aero_dec)} free"
        )

    lock_tx, lock_receipt = await adapter.create_lock(
        aero_token=BASE_AERO,
        amount=lock_raw,
        lock_duration_s=lock_duration_s,
        wait_for_receipt=True,
    )
    print(
        "lock tx",
        lock_tx,
        get_etherscan_transaction_link(CHAIN_ID_BASE, lock_tx),
    )
    token_id = _parse_token_id_from_create_lock_receipt(
        lock_receipt or {},
        ve_address=adapter.ve,
        to_addr=wallet_addr,
    )
    print("veNFT tokenId", token_id)

    # 3) Add liquidity AERO/USDC
    stable = bool(args.pool_stable)
    pool = await adapter.get_pool(BASE_AERO, BASE_USDC, stable)
    if pool == ZERO_ADDRESS:
        raise SystemExit("Pool not found (try toggling --pool-stable)")
    gauge = await adapter.gauge_for_pool(pool)
    if gauge == ZERO_ADDRESS:
        raise SystemExit("Gauge not found for pool")

    usdc_liq_raw = int(args.usdc_liquidity * (10**usdc_dec))
    if usdc_liq_raw <= 0:
        raise SystemExit("--usdc-liquidity must be > 0")

    usdc_now = await _erc20_balance(BASE_USDC, wallet_addr)
    aero_now = await _erc20_balance(BASE_AERO, wallet_addr)
    if usdc_now < usdc_liq_raw:
        raise SystemExit(
            "Insufficient USDC for LP: have "
            f"{_fmt(usdc_now, usdc_dec)} need {_fmt(usdc_liq_raw, usdc_dec)}"
        )
    if aero_now <= 0:
        raise SystemExit("No AERO available for LP after locking")

    lp_tx = await adapter.add_liquidity(
        token_a=BASE_USDC,
        token_b=BASE_AERO,
        stable=stable,
        amount_a_desired=usdc_liq_raw,
        amount_b_desired=int(aero_now),
        amount_a_min=0,
        amount_b_min=0,
    )
    print(
        "addLiquidity tx",
        lp_tx,
        get_etherscan_transaction_link(CHAIN_ID_BASE, lp_tx),
    )

    # Wait briefly then fetch LP balance (pool token == LP token)
    await asyncio.sleep(1.0)
    lp_bal = await adapter.lp_balance(pool)
    print("LP token", pool, "balance", lp_bal)

    # 4) Deposit LP into gauge
    if lp_bal > 0:
        dep_tx = await adapter.deposit_gauge(gauge=gauge, lp_token=pool, amount=lp_bal)
        print(
            "gauge deposit tx",
            dep_tx,
            get_etherscan_transaction_link(CHAIN_ID_BASE, dep_tx),
        )

    # 5) Vote
    vote_tx = await adapter.vote(token_id=token_id, pools=[pool], weights=[10_000])
    print(
        "vote tx",
        vote_tx,
        get_etherscan_transaction_link(CHAIN_ID_BASE, vote_tx),
    )

    usdc_final = await _erc20_balance(BASE_USDC, wallet_addr)
    aero_final = await _erc20_balance(BASE_AERO, wallet_addr)
    print(
        f"USDC(final)={_fmt(usdc_final, usdc_dec)} AERO(final)={_fmt(aero_final, aero_dec)}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
