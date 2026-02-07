from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
from typing import Any

from eth_abi import encode
from eth_account import Account
from eth_utils import keccak
from web3 import AsyncWeb3

from wayfinder_paths.core.clients.BRAPClient import BRAP_CLIENT
from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI
from wayfinder_paths.core.utils.gorlami import gorlami_fork
from wayfinder_paths.core.utils.transaction import send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id
from wayfinder_paths.mcp.utils import find_wallet_by_label


def _selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def _erc20_call_data(signature: str, types: list[str], values: list[Any]) -> str:
    return "0x" + (_selector(signature) + encode(types, values)).hex()


def _parse_int(value: int | str | None) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s:
        return 0
    return int(s, 0) if s.startswith("0x") else int(s)


def _parse_amount_to_raw(amount: str, decimals: int) -> int:
    d = Decimal(str(amount).strip())
    scale = Decimal(10) ** int(decimals)
    raw = int((d * scale).to_integral_value())
    if raw <= 0:
        raise ValueError("Amount too small")
    return raw


async def _erc20_balance(web3: AsyncWeb3, token: str, wallet: str) -> int:
    contract = web3.eth.contract(address=web3.to_checksum_address(token), abi=ERC20_ABI)
    return int(
        await contract.functions.balanceOf(web3.to_checksum_address(wallet)).call(
            block_identifier="latest"
        )
    )


async def _erc20_meta(web3: AsyncWeb3, token: str) -> tuple[str, str, int]:
    contract = web3.eth.contract(address=web3.to_checksum_address(token), abi=ERC20_ABI)
    symbol, name, decimals = await asyncio.gather(
        contract.functions.symbol().call(block_identifier="latest"),
        contract.functions.name().call(block_identifier="latest"),
        contract.functions.decimals().call(block_identifier="latest"),
    )
    return str(symbol), str(name), int(decimals)


async def run_swap(
    *,
    chain_id: int,
    wallet_address: str,
    private_key_hex: str,
    from_token: str,
    to_token: str,
    amount_raw: int,
    slippage: float,
    confirmations: int,
) -> None:
    account = Account.from_key(private_key_hex)
    if account.address.lower() != wallet_address.lower():
        raise ValueError("private_key_hex does not match wallet_address")

    async def sign_callback(tx: dict) -> bytes:
        signed = account.sign_transaction(tx)
        return signed.raw_transaction

    async with web3_from_chain_id(chain_id) as web3:
        if confirmations == 0:
            # Safety: make sure we're on a forked node when running in dry-run mode
            version = await web3.client_version
            if "HardhatNetwork" not in str(version):
                raise RuntimeError(
                    f"Expected Hardhat fork for confirmations=0, got clientVersion={version!r}"
                )

        from_sym, _, from_dec = await _erc20_meta(web3, from_token)
        to_sym, _, to_dec = await _erc20_meta(web3, to_token)

        before_from, before_to = await asyncio.gather(
            _erc20_balance(web3, from_token, wallet_address),
            _erc20_balance(web3, to_token, wallet_address),
        )
        print(f"Before: {from_sym}={before_from} (dec={from_dec}) {to_sym}={before_to} (dec={to_dec})")

    quote_resp = await BRAP_CLIENT.get_quote(
        from_token=from_token,
        to_token=to_token,
        from_chain=chain_id,
        to_chain=chain_id,
        from_wallet=wallet_address,
        from_amount=str(amount_raw),
        slippage=slippage,
    )

    best = quote_resp.get("best_quote")
    if not best:
        raise RuntimeError(f"No best_quote in BRAP response: keys={list(quote_resp.keys())}")

    calldata = best.get("calldata") or {}
    spender = calldata.get("to")
    data = calldata.get("data")
    if not (spender and data):
        raise RuntimeError(
            f"Quote missing calldata. provider={best.get('provider')} calldata_keys={list(calldata.keys())}"
        )

    spender = AsyncWeb3.to_checksum_address(str(spender))

    approve_data = _erc20_call_data(
        "approve(address,uint256)",
        ["address", "uint256"],
        [spender, amount_raw],
    )

    approve_tx = {
        "chainId": chain_id,
        "from": AsyncWeb3.to_checksum_address(wallet_address),
        "to": AsyncWeb3.to_checksum_address(from_token),
        "data": approve_data,
        "value": 0,
    }
    approve_hash = await send_transaction(
        approve_tx,
        sign_callback,
        wait_for_receipt=True,
        confirmations=confirmations,
    )
    print("approve tx:", approve_hash)

    swap_tx = {
        "chainId": chain_id,
        "from": AsyncWeb3.to_checksum_address(wallet_address),
        "to": spender,
        "data": str(data),
        "value": _parse_int(calldata.get("value")),
    }
    swap_hash = await send_transaction(
        swap_tx,
        sign_callback,
        wait_for_receipt=True,
        confirmations=confirmations,
    )
    print("swap tx:", swap_hash)

    async with web3_from_chain_id(chain_id) as web3:
        from_sym, _, from_dec = await _erc20_meta(web3, from_token)
        to_sym, _, to_dec = await _erc20_meta(web3, to_token)
        after_from, after_to = await asyncio.gather(
            _erc20_balance(web3, from_token, wallet_address),
            _erc20_balance(web3, to_token, wallet_address),
        )
        print(f"After:  {from_sym}={after_from} (dec={from_dec}) {to_sym}={after_to} (dec={to_dec})")

        if after_to <= before_to:
            raise RuntimeError("to_token balance did not increase")
        if after_from >= before_from:
            raise RuntimeError("from_token balance did not decrease")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a BRAP swap live or as a Gorlami dry-run fork.")
    parser.add_argument("--chain-id", type=int, default=8453)
    parser.add_argument("--wallet-label", type=str, default="main")
    parser.add_argument("--from-token", type=str, required=True)
    parser.add_argument("--to-token", type=str, required=True)
    parser.add_argument("--amount", type=str, required=True, help="Human amount (e.g. 1 for 1 USDC)")
    parser.add_argument("--from-decimals", type=int, default=6)
    parser.add_argument("--slippage", type=float, default=0.01)
    parser.add_argument("--gorlami", action="store_true", help="Dry-run on a Gorlami fork")
    parser.add_argument("--fund-native-wei", type=int, default=2 * 10**18)
    parser.add_argument("--confirm-live", action="store_true", help="Actually broadcast to the real RPC")
    args = parser.parse_args()

    wallet = find_wallet_by_label(args.wallet_label)
    if not wallet:
        raise SystemExit(f"Wallet label not found in config.json: {args.wallet_label}")
    private_key_hex = str(wallet.get("private_key") or wallet.get("private_key_hex") or "").strip()
    if not private_key_hex:
        raise SystemExit(f"Wallet '{args.wallet_label}' missing private_key_hex in config.json")

    wallet_address = str(wallet.get("address") or "").strip()
    if not wallet_address:
        raise SystemExit(f"Wallet '{args.wallet_label}' missing address in config.json")

    amount_raw = _parse_amount_to_raw(args.amount, args.from_decimals)

    async def _run() -> None:
        if args.gorlami:
            async with gorlami_fork(
                args.chain_id,
                native_balances={wallet_address: int(args.fund_native_wei)},
                erc20_balances=[(args.from_token, wallet_address, int(amount_raw))],
            ) as (_, fork_info):
                print("gorlami fork:", fork_info["fork_id"], "rpc:", fork_info["rpc_url"])
                await run_swap(
                    chain_id=args.chain_id,
                    wallet_address=wallet_address,
                    private_key_hex=private_key_hex,
                    from_token=args.from_token,
                    to_token=args.to_token,
                    amount_raw=amount_raw,
                    slippage=float(args.slippage),
                    confirmations=0,
                )
                return

        if not args.confirm_live:
            raise SystemExit("Refusing to broadcast to live RPC without --confirm-live")
        await run_swap(
            chain_id=args.chain_id,
            wallet_address=wallet_address,
            private_key_hex=private_key_hex,
            from_token=args.from_token,
            to_token=args.to_token,
            amount_raw=amount_raw,
            slippage=float(args.slippage),
            confirmations=3,
        )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
