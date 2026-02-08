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
from wayfinder_paths.core.constants.aerodrome import AERODROME_VOTER, BASE_AERO
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.contracts import BASE_USDC
from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI
from wayfinder_paths.core.utils.etherscan import get_etherscan_transaction_link
from wayfinder_paths.core.utils.tokens import get_token_balance
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

TRANSFER_TOPIC0 = Web3.keccak(text="Transfer(address,address,uint256)").hex()
WEEK_S = 604800

VOTER_LAST_VOTED_ABI = [
    {
        "name": "lastVoted",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"type": "uint256"}],
    }
]


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


async def _native_balance(wallet: str) -> int:
    async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
        return int(await web3.eth.get_balance(to_checksum_address(wallet)))


async def _erc20_balance(token: str, wallet: str) -> int:
    return int(
        await get_token_balance(
            token_address=to_checksum_address(token),
            chain_id=CHAIN_ID_BASE,
            wallet_address=to_checksum_address(wallet),
        )
    )


async def _erc20_decimals(token: str) -> int:
    async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
        c = web3.eth.contract(address=to_checksum_address(token), abi=ERC20_ABI)
        return int(await c.functions.decimals().call())


def _fmt(amount_raw: int, decimals: int) -> str:
    return f"{amount_raw / (10**decimals):,.6f}"


async def _safe_symbol(adapter: AerodromeAdapter, token: str) -> str:
    try:
        return await adapter.token_symbol(token)
    except Exception:
        t = to_checksum_address(token)
        return f"{t[:6]}…{t[-4:]}"


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


async def _can_vote_now(token_id: int) -> tuple[bool, int, int]:
    import datetime

    async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
        latest = await web3.eth.get_block("latest")
        now = int(latest["timestamp"])
        epoch_start = (now // WEEK_S) * WEEK_S

        c = web3.eth.contract(address=AERODROME_VOTER, abi=VOTER_LAST_VOTED_ABI)
        last_voted = int(await c.functions.lastVoted(int(token_id)).call())

    can_vote = last_voted < epoch_start
    if not can_vote:
        next_epoch = epoch_start + WEEK_S
        ts = datetime.datetime.fromtimestamp(next_epoch, datetime.UTC).isoformat()
        print(f"tokenId {token_id} already voted this epoch; next epoch starts {ts}")
    return can_vote, last_voted, epoch_start


async def main() -> int:
    p = argparse.ArgumentParser(
        description="Rank Aerodrome vote pools by latest epoch (fees+bribes) and optionally lock+vote.",
    )
    p.add_argument("--config", default="config.json")
    p.add_argument("--wallet-label", default="main")
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--require-all-prices", action="store_true")
    p.add_argument("--token-id", type=int, default=None, help="Existing veNFT tokenId")
    p.add_argument(
        "--lock-aero",
        type=float,
        default=2.0,
        help="AERO to lock if estimating/creating a lock",
    )
    p.add_argument("--lock-weeks", type=int, default=4)
    p.add_argument(
        "--usdc-swap",
        type=float,
        default=0.0,
        help="USDC to swap into AERO before locking",
    )
    p.add_argument("--pick", type=int, default=0, help="Pick index from ranked list")
    p.add_argument("--vote-weight", type=int, default=10_000)
    p.add_argument("--vote", action="store_true", help="Submit vote tx for picked pool")
    p.add_argument(
        "--create-lock",
        action="store_true",
        help="Create a new lock (and use it to vote). Requires wallet + AERO balance.",
    )
    p.add_argument("--slippage-bps", type=int, default=100)
    p.add_argument("--dry-run", action="store_true", help="Only print ranking")
    args = p.parse_args()

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

    eth_bal = await _native_balance(wallet_addr)
    usdc_bal = await _erc20_balance(BASE_USDC, wallet_addr)
    aero_bal = await _erc20_balance(BASE_AERO, wallet_addr)
    print(
        f"wallet={wallet_addr}  ETH={eth_bal / 1e18:.6f}  USDC={_fmt(usdc_bal, usdc_dec)}  "
        f"AERO={_fmt(aero_bal, aero_dec)}"
    )

    ranked = await adapter.rank_pools_by_usdc_per_ve(
        top_n=max(int(args.top_n), int(args.pick) + 1),
        limit=int(args.limit),
        require_all_prices=bool(args.require_all_prices),
    )
    if not ranked:
        raise SystemExit("No pools ranked (pricing may have failed)")

    pools_by_lp = await adapter.pools_by_lp()

    print("\nTop pools (fees+bribes per veAERO vote):")
    for i, (usdc_per_ve, ep, total_usdc) in enumerate(
        ranked[: max(1, int(args.top_n))]
    ):
        pinfo = pools_by_lp.get(ep.lp)
        symbol = pinfo.symbol if pinfo else f"{ep.lp[:6]}…{ep.lp[-4:]}"
        t0 = pinfo.token0 if pinfo else None
        t1 = pinfo.token1 if pinfo else None
        s0 = await _safe_symbol(adapter, t0) if t0 else "?"
        s1 = await _safe_symbol(adapter, t1) if t1 else "?"

        votes_raw = None
        aero_locked_raw = None
        if args.token_id is not None:
            votes_raw = await adapter.ve_balance_of_nft(int(args.token_id))
            aero_locked_raw, _, _ = await adapter.ve_locked(int(args.token_id))
            aero_locked_raw = abs(int(aero_locked_raw))
        else:
            aero_locked_raw = int(args.lock_aero * (10**aero_dec))
            votes_raw = await adapter.estimate_votes_for_lock(
                aero_amount_raw=int(aero_locked_raw),
                lock_duration_s=int(args.lock_weeks) * 7 * 24 * 60 * 60,
            )

        apr = await adapter.estimate_ve_apr_percent(
            usdc_per_ve=float(usdc_per_ve),
            votes_raw=int(votes_raw),
            aero_locked_raw=int(aero_locked_raw),
        )
        apr_str = f"{apr:,.2f}%" if apr is not None else "n/a"
        print(
            f"[{i:02d}] usdc_per_ve={usdc_per_ve:,.6f}  veAPR≈{apr_str:>10}  "
            f"incentives=${total_usdc:,.2f}  {symbol:28}  {s0}/{s1}  lp={ep.lp}"
        )

    if args.dry_run:
        return 0

    if not args.vote:
        return 0

    if not args.token_id and not args.create_lock:
        raise SystemExit("--vote requires --token-id or --create-lock")

    token_id = int(args.token_id) if args.token_id else None

    # Optional swap USDC->AERO for lock funding.
    usdc_swap_raw = int(float(args.usdc_swap) * (10**usdc_dec))
    if args.create_lock and usdc_swap_raw > 0:
        if usdc_bal < usdc_swap_raw:
            raise SystemExit(
                f"Insufficient USDC: have {_fmt(usdc_bal, usdc_dec)} need "
                f"{_fmt(usdc_swap_raw, usdc_dec)}"
            )
        (
            tx_hash,
            routes,
            out_min,
        ) = await adapter.swap_exact_tokens_for_tokens_best_route(
            token_in=BASE_USDC,
            token_out=BASE_AERO,
            amount_in=usdc_swap_raw,
            slippage_bps=int(args.slippage_bps),
        )
        print(
            "swap tx", tx_hash, get_etherscan_transaction_link(CHAIN_ID_BASE, tx_hash)
        )
        print(f"swap outMin={_fmt(out_min, aero_dec)} routes={len(routes)}")

    # Optional createLock
    if args.create_lock and token_id is None:
        aero_now = await _erc20_balance(BASE_AERO, wallet_addr)
        lock_raw = min(int(args.lock_aero * (10**aero_dec)), int(aero_now))
        if lock_raw <= 0:
            raise SystemExit("No AERO available to lock")
        lock_duration_s = int(args.lock_weeks) * 7 * 24 * 60 * 60
        tx_hash, receipt = await adapter.create_lock(
            aero_token=BASE_AERO,
            amount=int(lock_raw),
            lock_duration_s=int(lock_duration_s),
            wait_for_receipt=True,
        )
        print(
            "createLock tx",
            tx_hash,
            get_etherscan_transaction_link(CHAIN_ID_BASE, tx_hash),
        )
        if not receipt:
            raise SystemExit("No receipt returned for createLock")
        token_id = _parse_token_id_from_create_lock_receipt(
            receipt, ve_address=adapter.ve, to_addr=wallet_addr
        )
        print("created veNFT tokenId", token_id)

    if token_id is None:
        raise SystemExit("No token_id available to vote with")

    pick = int(args.pick)
    if pick < 0 or pick >= len(ranked):
        raise SystemExit(f"--pick out of range (0..{len(ranked) - 1})")

    _, ep, _ = ranked[pick]

    can_vote, _, _ = await _can_vote_now(int(token_id))
    if not can_vote:
        raise SystemExit("Vote blocked by epoch rule; pass --create-lock to vote now.")

    vote_tx = await adapter.vote(
        token_id=int(token_id),
        pools=[ep.lp],
        weights=[int(args.vote_weight)],
    )
    print("vote tx", vote_tx, get_etherscan_transaction_link(CHAIN_ID_BASE, vote_tx))

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
