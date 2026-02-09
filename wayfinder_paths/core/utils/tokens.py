from collections.abc import Callable
from typing import Any

from eth_utils import to_checksum_address
from web3 import AsyncWeb3

from wayfinder_paths.core.constants.contracts import TOKENS_REQUIRING_APPROVAL_RESET
from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI
from wayfinder_paths.core.utils.transaction import send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

NATIVE_TOKEN_ADDRESSES: set = {
    "0x0000000000000000000000000000000000000000",
    "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    # TODO: This is not a proper SOL address, this short form is for LIFI only, fix this after fixing lifi
    "11111111111111111111111111111111",
    "0x0000000000000000000000000000000000001010",
}


def is_native_token(token_address: str | None) -> bool:
    if not token_address:
        return False
    return token_address.lower() in NATIVE_TOKEN_ADDRESSES


async def get_token_balance(
    token_address: str, chain_id: int, wallet_address: str
) -> int:
    async with web3_from_chain_id(chain_id) as web3:
        checksum_wallet = AsyncWeb3.to_checksum_address(wallet_address)

        if is_native_token(token_address):
            balance = await web3.eth.get_balance(
                checksum_wallet, block_identifier="pending"
            )
            return int(balance)
        else:
            checksum_token = AsyncWeb3.to_checksum_address(token_address)
            contract = web3.eth.contract(address=checksum_token, abi=ERC20_ABI)
            balance = await contract.functions.balanceOf(checksum_wallet).call(
                block_identifier="pending"
            )
            return int(balance)


async def get_token_allowance(
    token_address: str, chain_id: int, owner_address: str, spender_address: str
):
    async with web3_from_chain_id(chain_id) as web3:
        contract = web3.eth.contract(
            address=web3.to_checksum_address(token_address), abi=ERC20_ABI
        )
        return await contract.functions.allowance(
            web3.to_checksum_address(owner_address),
            web3.to_checksum_address(spender_address),
        ).call(block_identifier="pending")


async def build_approve_transaction(
    from_address: str,
    chain_id: int,
    token_address: str,
    spender_address: str,
    amount: int,
) -> dict:
    async with web3_from_chain_id(chain_id) as web3:
        contract = web3.eth.contract(
            address=web3.to_checksum_address(token_address), abi=ERC20_ABI
        )
        data = contract.encode_abi(
            "approve",
            [
                web3.to_checksum_address(spender_address),
                amount,
            ],
        )
        return {
            "to": web3.to_checksum_address(token_address),
            "from": web3.to_checksum_address(from_address),
            "data": data,
            "chainId": chain_id,
        }


async def build_send_transaction(
    from_address: str,
    to_address: str,
    token_address: str | None,
    chain_id: int,
    amount: int,
) -> dict:
    async with web3_from_chain_id(chain_id) as web3:
        from_checksum = web3.to_checksum_address(from_address)
        to_checksum = web3.to_checksum_address(to_address)

        if is_native_token(token_address):
            return {
                "to": to_checksum,
                "from": from_checksum,
                "value": amount,
                "chainId": chain_id,
            }
        else:
            token_checksum = web3.to_checksum_address(token_address)
            contract = web3.eth.contract(address=token_checksum, abi=ERC20_ABI)
            data = contract.encode_abi("transfer", [to_checksum, amount])

            return {
                "to": token_checksum,
                "from": from_checksum,
                "data": data,
                "chainId": chain_id,
            }


async def ensure_allowance(
    *,
    token_address: str,
    owner: str,
    spender: str,
    amount: int,
    chain_id: int,
    signing_callback: Callable,
    approval_amount: int | None = None,
) -> tuple[bool, Any]:
    allowance = await get_token_allowance(token_address, chain_id, owner, spender)
    if allowance >= amount:
        return True, {}

    if (
        int(chain_id),
        to_checksum_address(token_address),
    ) in TOKENS_REQUIRING_APPROVAL_RESET:
        clear_transaction = await build_approve_transaction(
            from_address=owner,
            chain_id=chain_id,
            token_address=token_address,
            spender_address=spender,
            amount=0,
        )
        await send_transaction(clear_transaction, signing_callback)

    approve_tx = await build_approve_transaction(
        from_address=owner,
        chain_id=chain_id,
        token_address=token_address,
        spender_address=spender,
        amount=approval_amount if approval_amount is not None else amount,
    )
    txn_hash = await send_transaction(approve_tx, signing_callback)
    return True, txn_hash
