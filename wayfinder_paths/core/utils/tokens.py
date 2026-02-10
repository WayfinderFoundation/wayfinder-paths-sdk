import asyncio
from collections.abc import Callable
from typing import Any

from eth_utils import to_checksum_address
from web3 import AsyncWeb3
from web3.exceptions import BadFunctionCallOutput

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
    if token_address is None:
        return True
    normalized = str(token_address).strip().lower()
    if normalized in ("", "native"):
        return True
    return normalized in NATIVE_TOKEN_ADDRESSES


def _coerce_bytes32_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).rstrip(b"\x00").decode("utf-8", errors="ignore")
    return str(value)


async def _erc20_string(
    web3: AsyncWeb3,
    token_address: str,
    field: str,
    *,
    block_identifier: str = "latest",
) -> str:
    checksum_token = web3.to_checksum_address(token_address)
    contract = web3.eth.contract(address=checksum_token, abi=ERC20_ABI)
    fn = getattr(contract.functions, field)
    try:
        value = await fn().call(block_identifier=block_identifier)
        return _coerce_bytes32_str(value)
    except (BadFunctionCallOutput, ValueError):
        # Some ERC20s use bytes32 for name/symbol (non-standard).
        bytes32_abi = [
            {
                "constant": True,
                "inputs": [],
                "name": field,
                "outputs": [{"name": "", "type": "bytes32"}],
                "type": "function",
            }
        ]
        contract32 = web3.eth.contract(address=checksum_token, abi=bytes32_abi)
        fn32 = getattr(contract32.functions, field)
        value = await fn32().call(block_identifier=block_identifier)
        return _coerce_bytes32_str(value)


async def get_erc20_metadata(
    token_address: str, chain_id: int, *, block_identifier: str = "latest"
) -> tuple[str, str, int]:
    async with web3_from_chain_id(chain_id) as web3:
        checksum_token = web3.to_checksum_address(token_address)
        contract = web3.eth.contract(address=checksum_token, abi=ERC20_ABI)

        symbol, name, decimals = await asyncio.gather(
            _erc20_string(
                web3, checksum_token, "symbol", block_identifier=block_identifier
            ),
            _erc20_string(
                web3, checksum_token, "name", block_identifier=block_identifier
            ),
            contract.functions.decimals().call(block_identifier=block_identifier),
        )
        return str(symbol), str(name), int(decimals)


async def get_token_balance(
    token_address: str | None,
    chain_id: int,
    wallet_address: str,
    *,
    web3: AsyncWeb3 | None = None,
    block_identifier: str | int = "pending",
) -> int:
    async def _read_with_web3(w3: AsyncWeb3) -> int:
        checksum_wallet = w3.to_checksum_address(wallet_address)

        if is_native_token(token_address):
            balance = await w3.eth.get_balance(
                checksum_wallet,
                block_identifier=block_identifier,
            )
            return int(balance)

        if token_address is None:
            raise ValueError("token_address is required for ERC20 balance reads")

        checksum_token = w3.to_checksum_address(str(token_address))
        contract = w3.eth.contract(address=checksum_token, abi=ERC20_ABI)
        balance = await contract.functions.balanceOf(checksum_wallet).call(
            block_identifier=block_identifier
        )
        return int(balance)

    if web3 is None:
        async with web3_from_chain_id(chain_id) as w3:
            return await _read_with_web3(w3)
    return await _read_with_web3(web3)


async def get_token_decimals(
    token_address: str | None,
    chain_id: int,
    *,
    web3: AsyncWeb3 | None = None,
    block_identifier: str | int = "latest",
    default_native_decimals: int = 18,
) -> int:
    async def _read_with_web3(w3: AsyncWeb3) -> int:
        if is_native_token(token_address):
            return int(default_native_decimals)

        if token_address is None:
            raise ValueError("token_address is required for ERC20 decimals reads")

        checksum_token = w3.to_checksum_address(str(token_address))
        contract = w3.eth.contract(address=checksum_token, abi=ERC20_ABI)
        decimals = await contract.functions.decimals().call(
            block_identifier=block_identifier
        )
        return int(decimals)

    if web3 is None:
        async with web3_from_chain_id(chain_id) as w3:
            return await _read_with_web3(w3)
    return await _read_with_web3(web3)


async def get_token_balance_with_decimals(
    token_address: str | None,
    chain_id: int,
    wallet_address: str,
    *,
    web3: AsyncWeb3 | None = None,
    balance_block_identifier: str | int = "pending",
    decimals_block_identifier: str | int = "latest",
    default_native_decimals: int = 18,
) -> tuple[int, int]:
    async def _read_with_web3(w3: AsyncWeb3) -> tuple[int, int]:
        checksum_wallet = w3.to_checksum_address(wallet_address)

        if is_native_token(token_address):
            balance = await w3.eth.get_balance(
                checksum_wallet,
                block_identifier=balance_block_identifier,
            )
            return int(balance), int(default_native_decimals)

        if token_address is None:
            raise ValueError("token_address is required for ERC20 balance reads")

        checksum_token = w3.to_checksum_address(str(token_address))
        contract = w3.eth.contract(address=checksum_token, abi=ERC20_ABI)
        balance_coro = contract.functions.balanceOf(checksum_wallet).call(
            block_identifier=balance_block_identifier
        )
        decimals_coro = contract.functions.decimals().call(
            block_identifier=decimals_block_identifier
        )
        balance, decimals = await asyncio.gather(balance_coro, decimals_coro)
        return int(balance), int(decimals)

    if web3 is None:
        async with web3_from_chain_id(chain_id) as w3:
            return await _read_with_web3(w3)
    return await _read_with_web3(web3)


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
