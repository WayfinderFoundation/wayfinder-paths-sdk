import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from web3 import AsyncWeb3

from wayfinder_paths.core.constants import SUPPORTED_CHAINS
from wayfinder_paths.core.constants.base import (
    SUGGESTED_GAS_PRICE_MULTIPLIER,
    SUGGESTED_PRIORITY_FEE_MULTIPLIER,
)
from wayfinder_paths.core.utils.transaction import (
    PRE_EIP_1559_CHAIN_IDS,
    TransactionRevertedError,
    _get_transaction_from_address,
    gas_limit_transaction,
    gas_price_transaction,
    nonce_transaction,
    send_transaction,
)
from wayfinder_paths.core.utils.web3 import get_transaction_chain_id

RANDOM_USER_0 = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"


def for_every_chain_id(async_f):
    return asyncio.gather(*[async_f(chain_id) for chain_id in SUPPORTED_CHAINS])


class TestGetChainId:
    def test_valid_chain_id(self):
        transaction = {"chainId": 1}
        result = get_transaction_chain_id(transaction)
        assert result == 1

    def test_chain_id_as_string(self):
        transaction = {"chainId": "1"}
        result = get_transaction_chain_id(transaction)
        assert result == 1

    def test_empty_transaction(self):
        transaction = {}
        with pytest.raises(ValueError, match="Transaction does not contain chainId"):
            get_transaction_chain_id(transaction)


class TestGetFromAddress:
    def test_valid_checksum_address(self):
        transaction = {"from": RANDOM_USER_0}
        result = _get_transaction_from_address(transaction)
        assert result == RANDOM_USER_0
        assert AsyncWeb3.is_checksum_address(result)

    def test_lowercase_address_converted_to_checksum(self):
        lowercase_address = RANDOM_USER_0.lower()
        transaction = {"from": lowercase_address}
        result = _get_transaction_from_address(transaction)
        assert AsyncWeb3.is_checksum_address(result)
        assert result == RANDOM_USER_0

    def test_empty_transaction(self):
        transaction = {}
        with pytest.raises(
            ValueError, match="Transaction does not contain from address"
        ):
            _get_transaction_from_address(transaction)


@pytest.mark.asyncio
class TestNonceTransaction:
    @pytest.fixture
    def mock_web3(self):
        web3 = MagicMock()
        web3.eth = MagicMock()
        web3.eth.get_transaction_count = AsyncMock()
        return web3

    @patch("wayfinder_paths.core.utils.transaction.web3s_from_chain_id")
    async def test_noncing_on_mainnet(self, mock_web3s_context, mock_web3):
        mock_web3.eth.get_transaction_count.return_value = 1
        mock_web3.provider.disconnect = AsyncMock()
        mock_web3s_context.return_value.__aenter__.return_value = [mock_web3]

        transaction = {
            "from": RANDOM_USER_0,
            "chainId": 1,
        }
        result = await nonce_transaction(transaction)
        assert result["nonce"] == 1

    @patch("wayfinder_paths.core.utils.transaction.web3s_from_chain_id")
    async def test_noncing_on_all_chains(self, mock_web3s_context, mock_web3):
        mock_web3.eth.get_transaction_count.return_value = 7
        mock_web3.provider.disconnect = AsyncMock()
        mock_web3s_context.return_value.__aenter__.return_value = [mock_web3]

        for chain_id in SUPPORTED_CHAINS:
            transaction = {
                "from": RANDOM_USER_0,
                "chainId": chain_id,
            }
            result = await nonce_transaction(transaction)
            assert "nonce" in result
            assert result["nonce"] == 7

    @patch("wayfinder_paths.core.utils.transaction.web3s_from_chain_id")
    async def test_multiple_web3s_returns_max_nonce(self, mock_web3s_context):
        mock_web3_1 = MagicMock()
        mock_web3_1.eth = MagicMock()
        mock_web3_1.eth.get_transaction_count = AsyncMock(return_value=5)
        mock_web3_1.provider.disconnect = AsyncMock()

        mock_web3_2 = MagicMock()
        mock_web3_2.eth = MagicMock()
        mock_web3_2.eth.get_transaction_count = AsyncMock(return_value=8)
        mock_web3_2.provider.disconnect = AsyncMock()

        mock_web3_3 = MagicMock()
        mock_web3_3.eth = MagicMock()
        mock_web3_3.eth.get_transaction_count = AsyncMock(return_value=6)
        mock_web3_3.provider.disconnect = AsyncMock()

        mock_web3s_context.return_value.__aenter__.return_value = [
            mock_web3_1,
            mock_web3_2,
            mock_web3_3,
        ]

        transaction = {
            "from": RANDOM_USER_0,
            "chainId": 1,
        }

        result = await nonce_transaction(transaction)

        assert result["nonce"] == 8
        mock_web3_1.eth.get_transaction_count.assert_called_once()
        mock_web3_2.eth.get_transaction_count.assert_called_once()
        mock_web3_3.eth.get_transaction_count.assert_called_once()

    @patch("wayfinder_paths.core.utils.transaction.web3s_from_chain_id")
    async def test_preserves_all_existing_fields(self, mock_web3s_context, mock_web3):
        mock_web3.eth.get_transaction_count.return_value = 5
        mock_web3.provider.disconnect = AsyncMock()
        mock_web3s_context.return_value.__aenter__.return_value = [mock_web3]

        transaction = {
            "from": RANDOM_USER_0,
            "chainId": 1,
            "to": RANDOM_USER_0,
            "value": 100,
            "gas": 21000,
            "gasPrice": 1000000000,
            "data": "0xabcd",
        }

        result = await nonce_transaction(transaction)

        assert result["nonce"] == 5
        assert result["from"] == transaction["from"]
        assert result["chainId"] == transaction["chainId"]
        assert result["to"] == transaction["to"]
        assert result["value"] == transaction["value"]
        assert result["gas"] == transaction["gas"]
        assert result["gasPrice"] == transaction["gasPrice"]
        assert result["data"] == transaction["data"]


@pytest.mark.asyncio
class TestGasPriceTransaction:
    @patch("wayfinder_paths.core.utils.transaction.web3s_from_chain_id")
    async def test_pricing_on_all_chains(self, mock_web3s_context):
        mock_block = MagicMock()
        mock_block.baseFeePerGas = 10_000_000_000
        mock_fee_history = MagicMock()
        mock_fee_history.reward = [[1_000_000_000] for _ in range(10)]

        mock_web3 = MagicMock()
        mock_web3.eth = MagicMock()
        mock_web3.eth.get_block = AsyncMock(return_value=mock_block)
        mock_web3.eth.fee_history = AsyncMock(return_value=mock_fee_history)
        mock_web3.hype = MagicMock()
        mock_web3.hype.big_block_gas_price = AsyncMock(return_value=2_000_000_000)
        mock_web3.provider.disconnect = AsyncMock()

        mock_web3s_context.return_value.__aenter__.return_value = [mock_web3]

        for chain_id in SUPPORTED_CHAINS:
            # gas_price is an awaitable property; only provide it when the code path uses it.
            mock_web3.eth.gas_price = (
                AsyncMock(return_value=5_000_000_000)()
                if chain_id in PRE_EIP_1559_CHAIN_IDS
                else None
            )
            transaction = {
                "chainId": chain_id,
            }
            result = await gas_price_transaction(transaction)
            if chain_id in PRE_EIP_1559_CHAIN_IDS:
                assert "maxFeePerGas" not in result
                assert "maxPriorityFeePerGas" not in result
                assert result["gasPrice"] > 0
            else:
                assert "gasPrice" not in result
                assert result["maxFeePerGas"] > 0
                assert result["maxPriorityFeePerGas"] > 0

    @patch("wayfinder_paths.core.utils.transaction.web3s_from_chain_id")
    async def test_eip1559_max_aggregation(self, mock_web3s_context):
        # Mock multiple web3 instances with different base fees and priority fees
        mock_block_1 = MagicMock()
        mock_block_1.baseFeePerGas = 30_000_000_000
        mock_fee_history_1 = MagicMock()
        mock_fee_history_1.reward = [[2_000_000_000] for _ in range(10)]
        mock_web3_1 = MagicMock()
        mock_web3_1.eth = MagicMock()
        mock_web3_1.eth.get_block = AsyncMock(return_value=mock_block_1)
        mock_web3_1.eth.fee_history = AsyncMock(return_value=mock_fee_history_1)
        mock_web3_1.provider.disconnect = AsyncMock()

        mock_block_2 = MagicMock()
        mock_block_2.baseFeePerGas = 35_000_000_000
        mock_fee_history_2 = MagicMock()
        mock_fee_history_2.reward = [[3_000_000_000] for _ in range(10)]
        mock_web3_2 = MagicMock()
        mock_web3_2.eth = MagicMock()
        mock_web3_2.eth.get_block = AsyncMock(return_value=mock_block_2)
        mock_web3_2.eth.fee_history = AsyncMock(return_value=mock_fee_history_2)
        mock_web3_2.provider.disconnect = AsyncMock()

        mock_block_3 = MagicMock()
        mock_block_3.baseFeePerGas = 32_000_000_000
        mock_fee_history_3 = MagicMock()
        mock_fee_history_3.reward = [[2_500_000_000] for _ in range(10)]
        mock_web3_3 = MagicMock()
        mock_web3_3.eth = MagicMock()
        mock_web3_3.eth.get_block = AsyncMock(return_value=mock_block_3)
        mock_web3_3.eth.fee_history = AsyncMock(return_value=mock_fee_history_3)
        mock_web3_3.provider.disconnect = AsyncMock()

        mock_web3s_context.return_value.__aenter__.return_value = [
            mock_web3_1,
            mock_web3_2,
            mock_web3_3,
        ]

        transaction = {"chainId": 1}

        result = await gas_price_transaction(transaction)

        # Should use max base fee (35 gwei) and max priority fee (3 gwei)
        expected_max_priority_fee = int(
            3_000_000_000 * SUGGESTED_PRIORITY_FEE_MULTIPLIER
        )
        expected_max_fee = int(
            35_000_000_000 * 2 + 3_000_000_000 * SUGGESTED_PRIORITY_FEE_MULTIPLIER
        )

        assert result["maxPriorityFeePerGas"] == expected_max_priority_fee
        assert result["maxFeePerGas"] == expected_max_fee
        assert "gasPrice" not in result

    @patch("wayfinder_paths.core.utils.transaction.web3s_from_chain_id")
    async def test_non_eip1559_max_aggregation(self, mock_web3s_context):
        # Mock multiple web3 instances with different gas prices
        # gas_price is an awaitable property, so we need to make it a coroutine
        mock_web3_1 = MagicMock()
        mock_web3_1.eth = MagicMock()
        mock_web3_1.eth.gas_price = AsyncMock(return_value=5_000_000_000)()
        mock_web3_1.provider.disconnect = AsyncMock()

        mock_web3_2 = MagicMock()
        mock_web3_2.eth = MagicMock()
        mock_web3_2.eth.gas_price = AsyncMock(return_value=8_000_000_000)()
        mock_web3_2.provider.disconnect = AsyncMock()

        mock_web3_3 = MagicMock()
        mock_web3_3.eth = MagicMock()
        mock_web3_3.eth.gas_price = AsyncMock(return_value=6_000_000_000)()
        mock_web3_3.provider.disconnect = AsyncMock()

        mock_web3s_context.return_value.__aenter__.return_value = [
            mock_web3_1,
            mock_web3_2,
            mock_web3_3,
        ]

        transaction = {"chainId": 56}

        result = await gas_price_transaction(transaction)

        # Should use max gas price (8 gwei) * multiplier
        expected_gas_price = int(8_000_000_000 * SUGGESTED_GAS_PRICE_MULTIPLIER)

        assert result["gasPrice"] == expected_gas_price
        assert "maxFeePerGas" not in result
        assert "maxPriorityFeePerGas" not in result


@pytest.mark.asyncio
class TestGasLimitTransaction:
    @patch("wayfinder_paths.core.utils.transaction.web3s_from_chain_id")
    async def test_gas_limit_on_all_chains(self, mock_web3s_context):
        mock_web3 = MagicMock()
        mock_web3.eth = MagicMock()
        mock_web3.eth.estimate_gas = AsyncMock(return_value=21_000)
        mock_web3.provider.disconnect = AsyncMock()

        mock_web3s_context.return_value.__aenter__.return_value = [mock_web3]

        for chain_id in SUPPORTED_CHAINS:
            transaction = {
                "chainId": chain_id,
            }
            result = await gas_limit_transaction(transaction)
            assert "gas" in result
            assert result["gas"] > 0


@pytest.mark.asyncio
class TestSendTransaction:
    @patch("wayfinder_paths.core.utils.transaction.wait_for_transaction_receipt")
    @patch("wayfinder_paths.core.utils.transaction.broadcast_transaction")
    @patch("wayfinder_paths.core.utils.transaction.gas_price_transaction")
    @patch("wayfinder_paths.core.utils.transaction.nonce_transaction")
    @patch("wayfinder_paths.core.utils.transaction.gas_limit_transaction")
    async def test_raises_on_revert(
        self,
        mock_gas_limit,
        mock_nonce,
        mock_gas_price,
        mock_broadcast,
        mock_wait_receipt,
    ):
        mock_gas_limit.return_value = {
            "from": RANDOM_USER_0,
            "chainId": 1,
            "gas": 50_000,
        }
        mock_nonce.return_value = {
            "from": RANDOM_USER_0,
            "chainId": 1,
            "gas": 50_000,
            "nonce": 1,
        }
        mock_gas_price.return_value = {
            "from": RANDOM_USER_0,
            "chainId": 1,
            "gas": 50_000,
            "nonce": 1,
            "maxFeePerGas": 1,
            "maxPriorityFeePerGas": 1,
        }
        mock_broadcast.return_value = "0xdeadbeef"
        mock_wait_receipt.return_value = {"status": 0, "gasUsed": 50_000}

        async def sign_callback(_tx: dict) -> bytes:
            return b"\x00"

        with pytest.raises(TransactionRevertedError, match="Transaction reverted"):
            await send_transaction(
                {"from": RANDOM_USER_0, "chainId": 1},
                sign_callback,
                wait_for_receipt=True,
            )

    @patch("wayfinder_paths.core.utils.transaction.wait_for_transaction_receipt")
    @patch("wayfinder_paths.core.utils.transaction.broadcast_transaction")
    @patch("wayfinder_paths.core.utils.transaction.gas_price_transaction")
    @patch("wayfinder_paths.core.utils.transaction.nonce_transaction")
    @patch("wayfinder_paths.core.utils.transaction.gas_limit_transaction")
    async def test_returns_hash_on_success(
        self,
        mock_gas_limit,
        mock_nonce,
        mock_gas_price,
        mock_broadcast,
        mock_wait_receipt,
    ):
        mock_gas_limit.return_value = {
            "from": RANDOM_USER_0,
            "chainId": 1,
            "gas": 50_000,
        }
        mock_nonce.return_value = {
            "from": RANDOM_USER_0,
            "chainId": 1,
            "gas": 50_000,
            "nonce": 1,
        }
        mock_gas_price.return_value = {
            "from": RANDOM_USER_0,
            "chainId": 1,
            "gas": 50_000,
            "nonce": 1,
            "maxFeePerGas": 1,
            "maxPriorityFeePerGas": 1,
        }
        mock_broadcast.return_value = "0xabc"
        mock_wait_receipt.return_value = {"status": 1, "gasUsed": 40_000}

        async def sign_callback(_tx: dict) -> bytes:
            return b"\x00"

        txn_hash = await send_transaction(
            {"from": RANDOM_USER_0, "chainId": 1},
            sign_callback,
            wait_for_receipt=True,
        )
        assert txn_hash == "0xabc"
