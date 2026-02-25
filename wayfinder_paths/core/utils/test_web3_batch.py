from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from wayfinder_paths.core.utils.web3_batch import batch_web3_calls

BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ERC20_READ_ABI = [
    {
        "type": "function",
        "name": "balanceOf",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "decimals",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
    {
        "type": "function",
        "name": "symbol",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
    },
]


@pytest.mark.asyncio
class TestBatchWeb3Calls:
    @pytest.fixture
    def mock_web3(self):
        web3 = MagicMock()
        batch = MagicMock()
        batch.add = MagicMock()
        batch.async_execute = AsyncMock(return_value=[])
        batch.cancel = MagicMock()
        web3.batch_requests.return_value = batch
        return web3

    async def test_empty_call_factories(self, mock_web3):
        result = await batch_web3_calls(mock_web3)
        assert result == ()
        mock_web3.batch_requests.assert_not_called()

    async def test_single_call_batched(self, mock_web3):
        mock_web3.batch_requests().async_execute = AsyncMock(return_value=[42])
        result = await batch_web3_calls(mock_web3, lambda: AsyncMock(return_value=42)())
        assert len(result) == 1
        assert result[0] == 42

    async def test_multiple_calls_batched(self, mock_web3):
        mock_web3.batch_requests().async_execute = AsyncMock(return_value=[10, 20, 30])
        result = await batch_web3_calls(
            mock_web3,
            lambda: AsyncMock(return_value=10)(),
            lambda: AsyncMock(return_value=20)(),
            lambda: AsyncMock(return_value=30)(),
        )
        assert result == (10, 20, 30)

    async def test_returns_tuple(self, mock_web3):
        mock_web3.batch_requests().async_execute = AsyncMock(return_value=["a"])
        result = await batch_web3_calls(mock_web3, lambda: AsyncMock(return_value="a")())
        assert isinstance(result, tuple)

    async def test_batch_fails_fallback_to_gather(self, mock_web3):
        mock_web3.batch_requests.return_value.async_execute = AsyncMock(
            side_effect=Exception("batch unsupported")
        )

        factory_a = AsyncMock(return_value=100)
        factory_b = AsyncMock(return_value=200)

        result = await batch_web3_calls(
            mock_web3,
            lambda: factory_a(),
            lambda: factory_b(),
            fallback_to_gather=True,
        )
        assert result == (100, 200)

    async def test_batch_fails_no_fallback_raises(self, mock_web3):
        mock_web3.batch_requests.return_value.async_execute = AsyncMock(
            side_effect=RuntimeError("batch unsupported")
        )

        with pytest.raises(RuntimeError, match="batch unsupported"):
            await batch_web3_calls(
                mock_web3,
                lambda: AsyncMock(return_value=1)(),
                fallback_to_gather=False,
            )

    async def test_batch_fails_gather_fails_raises_gather_error(self, mock_web3):
        mock_web3.batch_requests.return_value.async_execute = AsyncMock(
            side_effect=RuntimeError("batch broke")
        )

        async def failing_call():
            raise ValueError("rpc down")

        with pytest.raises(ValueError, match="rpc down") as exc_info:
            await batch_web3_calls(
                mock_web3,
                lambda: failing_call(),
                fallback_to_gather=True,
            )
        # gather error should chain from batch error
        assert exc_info.value.__cause__ is not None
        assert "batch broke" in str(exc_info.value.__cause__)

    async def test_batch_cancel_called_on_failure(self, mock_web3):
        batch = MagicMock()
        batch.add = MagicMock()
        batch.async_execute = AsyncMock(side_effect=Exception("fail"))
        batch.cancel = MagicMock()
        mock_web3.batch_requests.return_value = batch

        factory = AsyncMock(return_value=1)
        await batch_web3_calls(mock_web3, lambda: factory(), fallback_to_gather=True)

        batch.cancel.assert_called_once()

    async def test_batch_cancel_error_suppressed(self, mock_web3):
        """cancel() raising should not mask the original error."""
        batch = MagicMock()
        batch.add = MagicMock()
        batch.async_execute = AsyncMock(side_effect=RuntimeError("batch fail"))
        batch.cancel = MagicMock(side_effect=Exception("cancel also broke"))
        mock_web3.batch_requests.return_value = batch

        factory = AsyncMock(return_value=99)
        # Should still succeed via gather fallback despite cancel() throwing
        result = await batch_web3_calls(
            mock_web3, lambda: factory(), fallback_to_gather=True
        )
        assert result == (99,)

    async def test_call_factories_added_to_batch(self):
        batch = MagicMock()
        batch.add = MagicMock()
        batch.async_execute = AsyncMock(return_value=[1, 2])

        web3 = MagicMock()
        web3.batch_requests.return_value = batch

        coro_a = AsyncMock(return_value=1)()
        coro_b = AsyncMock(return_value=2)()

        await batch_web3_calls(web3, lambda: coro_a, lambda: coro_b)

        assert batch.add.call_count == 2


@pytest.mark.asyncio
@pytest.mark.requires_config
class TestBatchWeb3CallsLive:
    """Live RPC tests — requires config.json with Base RPC configured."""

    async def test_batch_usdc_reads_on_base(self):
        from wayfinder_paths.core.config import load_config
        from wayfinder_paths.core.utils.web3 import web3_from_chain_id

        load_config("config.json")
        chain_id = 8453  # Base

        async with web3_from_chain_id(chain_id) as web3:
            usdc = web3.eth.contract(address=BASE_USDC, abi=ERC20_READ_ABI)

            balance, decimals, symbol = await batch_web3_calls(
                web3,
                lambda: usdc.functions.balanceOf(BASE_USDC).call(
                    block_identifier="latest"
                ),
                lambda: usdc.functions.decimals().call(block_identifier="latest"),
                lambda: usdc.functions.symbol().call(block_identifier="latest"),
            )

            assert isinstance(balance, int)
            assert balance >= 0
            assert decimals == 6
            assert symbol == "USDC"
