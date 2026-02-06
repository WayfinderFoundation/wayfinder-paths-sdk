import asyncio
import math
from collections.abc import Callable
from typing import Any

from eth_account import Account
from loguru import logger
from web3 import AsyncWeb3

from wayfinder_paths.core.constants.base import (
    GAS_BUFFER_MULTIPLIER,
    MAX_BASE_FEE_GROWTH_MULTIPLIER,
    SUGGESTED_GAS_PRICE_MULTIPLIER,
    SUGGESTED_PRIORITY_FEE_MULTIPLIER,
)
from wayfinder_paths.core.constants.chains import (
    CHAIN_ID_HYPEREVM,
    PRE_EIP_1559_CHAIN_IDS,
)
from wayfinder_paths.core.utils.web3 import (
    get_transaction_chain_id,
    web3_from_chain_id,
    web3s_from_chain_id,
)


class TransactionRevertedError(RuntimeError):
    def __init__(
        self,
        txn_hash: str,
        receipt: dict[str, Any] | None = None,
        message: str | None = None,
    ):
        self.txn_hash = txn_hash
        self.receipt = receipt or {}
        super().__init__(message or f"Transaction reverted: {txn_hash}")


def _raise_revert_error(
    txn_hash: str,
    receipt: dict[str, Any],
    transaction: dict[str, Any],
    cause: Exception | None = None,
) -> None:
    gas_used = 0
    try:
        gas_used = int(receipt.get("gasUsed") or 0)
    except Exception:
        gas_used = 0

    gas_limit = 0
    try:
        gas_limit = int(transaction.get("gas") or 0)
    except Exception:
        gas_limit = 0

    oogs = bool(gas_used and gas_limit and gas_used >= gas_limit)
    suffix = (
        f" gasUsed={gas_used} gasLimit={gas_limit}"
        + (" (likely out of gas)" if oogs else "")
        if gas_used or gas_limit
        else ""
    )
    error = TransactionRevertedError(
        txn_hash,
        receipt,
        message=f"Transaction reverted (status=0): {txn_hash}{suffix}",
    )
    if cause:
        raise error from cause
    raise error


def _get_transaction_from_address(transaction: dict) -> str:
    if "from" not in transaction:
        raise ValueError("Transaction does not contain from address")
    return AsyncWeb3.to_checksum_address(transaction["from"])


async def nonce_transaction(transaction: dict):
    transaction = transaction.copy()

    from_address = _get_transaction_from_address(transaction)

    async def _get_nonce(web3: AsyncWeb3, from_address: str) -> int:
        return await web3.eth.get_transaction_count(
            from_address, block_identifier="pending"
        )

    async with web3s_from_chain_id(get_transaction_chain_id(transaction)) as web3s:
        nonces = await asyncio.gather(
            *[_get_nonce(web3, from_address) for web3 in web3s]
        )

        nonce = max(nonces)
        transaction["nonce"] = nonce

    return transaction


async def gas_price_transaction(transaction: dict):
    transaction = transaction.copy()

    async def _get_gas_price(web3: AsyncWeb3) -> int:
        return await web3.eth.gas_price

    async def _get_hyperevm_big_block_gas_price(web3: AsyncWeb3) -> int:
        # Hyperevm exposes a chain-specific RPC for a recommended max fee and does not
        # use EIP-1559 priority fees in the normal way.
        return await web3.hype.big_block_gas_price()

    async def _get_base_fee(web3: AsyncWeb3) -> int:
        latest_block = await web3.eth.get_block("latest")
        return latest_block.baseFeePerGas

    async def _get_priority_fee(web3: AsyncWeb3) -> int:
        lookback_blocks = 10
        percentile = 80
        fee_history = await web3.eth.fee_history(
            lookback_blocks, "latest", [percentile]
        )
        historical_priority_fees = [i[0] for i in fee_history.reward]
        return sum(historical_priority_fees) // len(historical_priority_fees)

    chain_id = get_transaction_chain_id(transaction)
    async with web3s_from_chain_id(chain_id) as web3s:
        if chain_id in PRE_EIP_1559_CHAIN_IDS:
            gas_prices = await asyncio.gather(*[_get_gas_price(web3) for web3 in web3s])
            gas_price = max(gas_prices)

            transaction["gasPrice"] = int(gas_price * SUGGESTED_GAS_PRICE_MULTIPLIER)
        elif chain_id == CHAIN_ID_HYPEREVM:
            big_block_prices = await asyncio.gather(
                *[_get_hyperevm_big_block_gas_price(web3) for web3 in web3s]
            )
            max_fee = max(big_block_prices)
            transaction["maxFeePerGas"] = int(max_fee * SUGGESTED_GAS_PRICE_MULTIPLIER)
            transaction["maxPriorityFeePerGas"] = 0
        else:
            base_fees = await asyncio.gather(*[_get_base_fee(web3) for web3 in web3s])
            priority_fees = await asyncio.gather(
                *[_get_priority_fee(web3) for web3 in web3s]
            )

            base_fee = max(base_fees)
            priority_fee = max(priority_fees)

            transaction["maxFeePerGas"] = int(
                base_fee * MAX_BASE_FEE_GROWTH_MULTIPLIER
                + priority_fee * SUGGESTED_PRIORITY_FEE_MULTIPLIER
            )
            transaction["maxPriorityFeePerGas"] = int(
                priority_fee * SUGGESTED_PRIORITY_FEE_MULTIPLIER
            )

    return transaction


async def gas_limit_transaction(transaction: dict):
    transaction = transaction.copy()

    # prevents RPCs from taking this as a serious limit
    transaction.pop("gas", None)

    async def _estimate_gas(web3: AsyncWeb3, transaction: dict) -> int:
        try:
            return await web3.eth.estimate_gas(transaction, block_identifier="latest")
        except Exception as e:
            logger.info(
                f"Failed to estimate gas using {web3.provider.endpoint_uri}. Error: {e}"
            )
            return 0

    async with web3s_from_chain_id(get_transaction_chain_id(transaction)) as web3s:
        gas_limits = await asyncio.gather(
            *[_estimate_gas(web3, transaction) for web3 in web3s]
        )

        gas_limit = max(gas_limits)
        if gas_limit == 0:
            logger.error("Gas estimation failed on all RPCs")
            raise Exception("Gas estimation failed on all RPCs")

        # Add a defensive buffer. Some transactions (especially swaps) can use more gas
        # at execution time than at estimation time due to state changes between
        # estimation and inclusion.
        buffered_gas_limit = int(math.ceil(gas_limit * GAS_BUFFER_MULTIPLIER))
        transaction["gas"] = buffered_gas_limit

    return transaction


async def broadcast_transaction(chain_id, signed_transaction: bytes) -> str:
    async with web3_from_chain_id(chain_id) as web3:
        tx_hash = await web3.eth.send_raw_transaction(signed_transaction)
        return tx_hash.hex()


async def wait_for_transaction_receipt(
    chain_id: int,
    txn_hash: str,
    poll_interval: float = 0.1,
    timeout: int = 300,
    confirmations: int = 3,
) -> dict:
    if isinstance(txn_hash, str) and not txn_hash.startswith("0x"):
        txn_hash = f"0x{txn_hash}"

    async def _wait_for_receipt(web3: AsyncWeb3, tx_hash: str) -> dict:
        return await web3.eth.wait_for_transaction_receipt(
            tx_hash, poll_latency=poll_interval, timeout=timeout
        )

    async def _get_block_number(web3: AsyncWeb3) -> int:
        return await web3.eth.block_number

    async with web3s_from_chain_id(chain_id) as web3s:
        tasks = [
            asyncio.create_task(_wait_for_receipt(web3, txn_hash)) for web3 in web3s
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        receipt = done.pop().result()

        if receipt.get("status") == 0:
            raise TransactionRevertedError(txn_hash, receipt)

        target_block = receipt["blockNumber"] + confirmations - 1
        while (
            max(await asyncio.gather(*[_get_block_number(w) for w in web3s]))
            < target_block
        ):
            await asyncio.sleep(poll_interval)
        return receipt


async def send_transaction(
    transaction: dict, sign_callback: Callable, wait_for_receipt=True
) -> str:
    if sign_callback is None:
        raise ValueError("sign_callback must be provided to send transaction")

    logger.info(f"Broadcasting transaction {transaction}...")
    chain_id = get_transaction_chain_id(transaction)
    transaction = await gas_limit_transaction(transaction)
    transaction = await nonce_transaction(transaction)
    transaction = await gas_price_transaction(transaction)
    signed_transaction = await sign_callback(transaction)
    txn_hash = await broadcast_transaction(chain_id, signed_transaction)
    if isinstance(txn_hash, str) and not txn_hash.startswith("0x"):
        txn_hash = f"0x{txn_hash}"
    logger.info(f"Transaction broadcasted: {txn_hash}")
    if wait_for_receipt:
        try:
            receipt = await wait_for_transaction_receipt(chain_id, txn_hash)
        except TransactionRevertedError as exc:
            receipt = (
                exc.receipt if isinstance(getattr(exc, "receipt", None), dict) else {}
            )
            _raise_revert_error(txn_hash, receipt, transaction, cause=exc)

        status = None
        try:
            status = receipt.get("status")
        except Exception:
            status = None

        # Defensive: should have been raised inside wait_for_transaction_receipt.
        if status is not None and int(status) == 0:
            _raise_revert_error(txn_hash, receipt, transaction)
    return txn_hash


async def sign_and_send_transaction(
    transaction: dict, private_key: str, wait_for_receipt: bool = True
) -> str:
    account = Account.from_key(private_key)

    async def sign_callback(tx: dict) -> bytes:
        signed = account.sign_transaction(tx)
        return signed.raw_transaction

    return await send_transaction(transaction, sign_callback, wait_for_receipt)


async def encode_call(
    *,
    target: str,
    abi: list[dict[str, Any]],
    fn_name: str,
    args: list[Any],
    from_address: str,
    chain_id: int,
    value: int = 0,
) -> dict[str, Any]:
    async with web3_from_chain_id(chain_id) as web3:
        try:
            contract = web3.eth.contract(
                address=web3.to_checksum_address(target),
                abi=abi,
            )
            data = contract.encode_abi(fn_name, args)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Failed to encode {fn_name}: {exc}") from exc

        return {
            "chainId": int(chain_id),
            "from": AsyncWeb3.to_checksum_address(from_address),
            "to": AsyncWeb3.to_checksum_address(target),
            "data": data,
            "value": int(value),
        }


# TODO: HypeEVM Big Blocks: Setting and detecting
