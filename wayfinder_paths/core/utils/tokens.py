import asyncio
from collections.abc import Callable
from typing import Any

from eth_utils import to_checksum_address
from web3 import AsyncWeb3
from web3.exceptions import BadFunctionCallOutput

from wayfinder_paths.core.constants.contracts import TOKENS_REQUIRING_APPROVAL_RESET
from wayfinder_paths.core.constants.erc20_abi import (
    ERC20_ABI,
    ERC20_NAME_BYTES32_ABI,
    ERC20_SYMBOL_BYTES32_ABI,
)
from wayfinder_paths.core.constants.erc1155_abi import ERC1155_APPROVAL_ABI
from wayfinder_paths.core.utils.transaction import (
    encode_call,
    send_transaction,
    wait_for_transaction_receipt,
)
from wayfinder_paths.core.utils.web3 import web3_from_chain_id, web3s_from_chain_id

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
    normalized = token_address.strip().lower()
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
    block_identifier: str | int = "latest",
) -> str:
    checksum_token = web3.to_checksum_address(token_address)
    contract = web3.eth.contract(address=checksum_token, abi=ERC20_ABI)
    fn = getattr(contract.functions, field)
    try:
        value = await fn().call(block_identifier=block_identifier)
        return _coerce_bytes32_str(value)
    except (BadFunctionCallOutput, ValueError):
        # Some ERC20s use bytes32 for name/symbol (non-standard).
        fallback_abi = (
            ERC20_NAME_BYTES32_ABI if field == "name" else ERC20_SYMBOL_BYTES32_ABI
        )
        contract32 = web3.eth.contract(address=checksum_token, abi=fallback_abi)
        fn32 = getattr(contract32.functions, field)
        value = await fn32().call(block_identifier=block_identifier)
        return _coerce_bytes32_str(value)


async def get_erc20_metadata(
    token_address: str,
    *,
    web3: AsyncWeb3,
    block_identifier: str | int = "latest",
) -> tuple[str, str, int]:
    checksum_token = web3.to_checksum_address(token_address)
    contract = web3.eth.contract(address=checksum_token, abi=ERC20_ABI)

    symbol, name, decimals = await asyncio.gather(
        _erc20_string(
            web3, checksum_token, "symbol", block_identifier=block_identifier
        ),
        _erc20_string(web3, checksum_token, "name", block_identifier=block_identifier),
        contract.functions.decimals().call(block_identifier=block_identifier),
    )
    return symbol, name, int(decimals)


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
    token_address: str,
    chain_id: int,
    owner_address: str,
    spender_address: str,
    *,
    web3: AsyncWeb3 | None = None,
    block_identifier: str | int = "pending",
) -> int:
    async def _read_with_web3(w3: AsyncWeb3) -> int:
        contract = w3.eth.contract(
            address=w3.to_checksum_address(token_address), abi=ERC20_ABI
        )
        allowance = await contract.functions.allowance(
            w3.to_checksum_address(owner_address),
            w3.to_checksum_address(spender_address),
        ).call(block_identifier=block_identifier)
        return int(allowance)

    if web3 is None:
        async with web3_from_chain_id(chain_id) as w3:
            return await _read_with_web3(w3)
    return await _read_with_web3(web3)


async def wait_for_allowance_visible(
    *,
    token_address: str,
    chain_id: int,
    owner: str,
    spender: str,
    amount: int,
    approval_tx_hash: str | None = None,
    approval_block: int | None = None,
    max_attempts: int = 8,
    poll_interval: float = 0.75,
) -> dict[str, Any]:
    """Wait until a confirmed allowance is visible to an RPC used for tx prep.

    Gas estimation fans out across configured RPCs and succeeds if one endpoint can
    simulate the transaction. This helper mirrors that behavior: once the approval
    block is known, it accepts any endpoint that has reached that block and can see
    sufficient allowance at `latest`.
    """
    required = int(amount)
    token_checksum = to_checksum_address(token_address)
    owner_checksum = to_checksum_address(owner)
    spender_checksum = to_checksum_address(spender)
    attempts = max(1, int(max_attempts))
    observed_allowance = 0
    errors: list[str] = []

    if approval_block is None and approval_tx_hash:
        try:
            receipt = await wait_for_transaction_receipt(
                int(chain_id),
                approval_tx_hash,
                confirmations=0,
            )
            approval_block = int(receipt.get("blockNumber"))
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "approval_receipt_unavailable",
                "spender": spender_checksum,
                "required_allowance_raw": required,
                "observed_allowance_raw": observed_allowance,
                "approval_block": approval_block,
                "attempts": 0,
                "error": str(exc),
            }

    async def _block_number(w3: AsyncWeb3) -> int:
        return int(await w3.eth.block_number)

    async with web3s_from_chain_id(int(chain_id)) as web3s:
        for attempt in range(1, attempts + 1):
            errors.clear()
            block_results = await asyncio.gather(
                *[_block_number(w3) for w3 in web3s],
                return_exceptions=True,
            )
            visible_web3s: list[AsyncWeb3] = []
            for w3, block_result in zip(web3s, block_results, strict=False):
                if isinstance(block_result, Exception):
                    errors.append(str(block_result))
                    continue
                if approval_block is None or int(block_result) >= approval_block:
                    visible_web3s.append(w3)

            if visible_web3s:
                allowance_results = await asyncio.gather(
                    *[
                        get_token_allowance(
                            token_checksum,
                            int(chain_id),
                            owner_checksum,
                            spender_checksum,
                            web3=w3,
                            block_identifier="latest",
                        )
                        for w3 in visible_web3s
                    ],
                    return_exceptions=True,
                )
                allowances: list[int] = []
                for allowance_result in allowance_results:
                    if isinstance(allowance_result, Exception):
                        errors.append(str(allowance_result))
                        continue
                    allowances.append(int(allowance_result))
                if allowances:
                    observed_allowance = max(observed_allowance, max(allowances))
                    if observed_allowance >= required:
                        return {
                            "status": (
                                "approval_confirmed_visible"
                                if approval_tx_hash or approval_block is not None
                                else "already_sufficient"
                            ),
                            "spender": spender_checksum,
                            "required_allowance_raw": required,
                            "observed_allowance_raw": observed_allowance,
                            "approval_block": approval_block,
                            "attempts": attempt,
                        }

            if attempt < attempts:
                await asyncio.sleep(poll_interval)

    status = "approval_not_visible_yet"
    if observed_allowance == 0 and errors:
        status = "allowance_read_failed"
    return {
        "status": status,
        "spender": spender_checksum,
        "required_allowance_raw": required,
        "observed_allowance_raw": observed_allowance,
        "approval_block": approval_block,
        "attempts": attempts,
        "errors": errors,
    }


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


async def ensure_erc1155_approval(
    *,
    token_address: str,
    owner: str,
    operator: str,
    approved: bool,
    chain_id: int,
    signing_callback: Callable,
) -> tuple[bool, str]:
    owner = to_checksum_address(owner)
    operator = to_checksum_address(operator)
    token_address = to_checksum_address(token_address)

    async with web3_from_chain_id(chain_id) as web3:
        contract = web3.eth.contract(address=token_address, abi=ERC1155_APPROVAL_ABI)
        is_approved = await contract.functions.isApprovedForAll(owner, operator).call(
            block_identifier="pending"
        )
        if bool(is_approved) == bool(approved):
            return True, "already-approved"

    tx = await encode_call(
        target=token_address,
        abi=ERC1155_APPROVAL_ABI,
        fn_name="setApprovalForAll",
        args=[operator, bool(approved)],
        from_address=owner,
        chain_id=chain_id,
    )
    tx_hash = await send_transaction(tx, signing_callback)
    return True, tx_hash


async def ensure_allowance(
    *,
    token_address: str,
    owner: str,
    spender: str,
    amount: int,
    chain_id: int,
    signing_callback: Callable,
    approval_amount: int | None = None,
    confirmations: int | None = None,
    allowance_block_identifier: str | int = "pending",
) -> tuple[bool, Any]:
    allowance = await get_token_allowance(
        token_address,
        chain_id,
        owner,
        spender,
        block_identifier=allowance_block_identifier,
    )
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
        await send_transaction(
            clear_transaction, signing_callback, confirmations=confirmations
        )

    approve_tx = await build_approve_transaction(
        from_address=owner,
        chain_id=chain_id,
        token_address=token_address,
        spender_address=spender,
        amount=approval_amount if approval_amount is not None else amount,
    )
    txn_hash = await send_transaction(
        approve_tx, signing_callback, confirmations=confirmations
    )

    expected = approval_amount if approval_amount is not None else amount
    for _ in range(32):
        await asyncio.sleep(0.25)
        on_chain = await get_token_allowance(token_address, chain_id, owner, spender)
        if on_chain >= expected:
            return True, txn_hash

    return False, txn_hash
