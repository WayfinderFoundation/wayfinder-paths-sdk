from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.adapters.brap_adapter.adapter import BRAPAdapter


class TestBRAPAdapter:
    @pytest.fixture
    def mock_brap_client(self):
        return AsyncMock()

    @pytest.fixture
    def adapter(self):
        return BRAPAdapter()

    @pytest.mark.asyncio
    async def test_best_quote_success(self, adapter, mock_brap_client):
        mock_response = {
            "quotes": [],
            "best_quote": {
                "provider": "enso",
                "input_amount": 1000000000000000000,
                "output_amount": 995000000000000000,
                "calldata": {
                    "data": "0x",
                    "to": "0x",
                    "from_address": "0x",
                    "value": "0",
                    "chainId": 8453,
                },
                "fee_estimate": {"fee_total_usd": 0.008, "fee_breakdown": []},
            },
        }
        mock_brap_client.get_quote = AsyncMock(return_value=mock_response)

        with patch(
            "wayfinder_paths.adapters.brap_adapter.adapter.BRAP_CLIENT",
            mock_brap_client,
        ):
            success, data = await adapter.best_quote(
                from_token_address="0x" + "a" * 40,
                to_token_address="0x" + "b" * 40,
                from_chain_id=8453,
                to_chain_id=1,
                from_address="0x1234567890123456789012345678901234567890",
                amount="1000000000000000000",
            )

            assert success
            assert data["input_amount"] == 1000000000000000000
            assert data["output_amount"] == 995000000000000000

    @pytest.mark.asyncio
    async def test_best_quote_accepts_legacy_nested_quote_shape(
        self, adapter, mock_brap_client
    ):
        mock_response = {
            "quotes": {
                "quote_count": 2,
                "all_quotes": [{"provider": "lifi"}, {"provider": "enso"}],
                "best_quote": {
                    "provider": "enso",
                    "input_amount": 1000000000000000000,
                    "output_amount": 995000000000000000,
                    "calldata": {
                        "data": "0x",
                        "to": "0x",
                        "value": "0",
                        "chainId": 8453,
                    },
                },
            }
        }
        mock_brap_client.get_quote = AsyncMock(return_value=mock_response)

        with patch(
            "wayfinder_paths.adapters.brap_adapter.adapter.BRAP_CLIENT",
            mock_brap_client,
        ):
            success, data = await adapter.best_quote(
                from_token_address="0x" + "a" * 40,
                to_token_address="0x" + "b" * 40,
                from_chain_id=8453,
                to_chain_id=1,
                from_address="0x1234567890123456789012345678901234567890",
                amount="1000000000000000000",
            )

        assert success
        assert data["provider"] == "enso"
        assert data["output_amount"] == 995000000000000000

    @pytest.mark.asyncio
    async def test_best_quote_no_quotes(self, adapter, mock_brap_client):
        mock_response = {"quotes": [], "best_quote": None}
        mock_brap_client.get_quote = AsyncMock(return_value=mock_response)

        with patch(
            "wayfinder_paths.adapters.brap_adapter.adapter.BRAP_CLIENT",
            mock_brap_client,
        ):
            success, data = await adapter.best_quote(
                from_token_address="0x" + "a" * 40,
                to_token_address="0x" + "b" * 40,
                from_chain_id=8453,
                to_chain_id=1,
                from_address="0x1234567890123456789012345678901234567890",
                amount="1000000000000000000",
            )

            assert success is False
            assert "No quotes available" in data

    @pytest.mark.asyncio
    async def test_best_quote_failure(self, adapter, mock_brap_client):
        mock_brap_client.get_quote = AsyncMock(side_effect=Exception("API Error"))

        with patch(
            "wayfinder_paths.adapters.brap_adapter.adapter.BRAP_CLIENT",
            mock_brap_client,
        ):
            success, data = await adapter.best_quote(
                from_token_address="0x" + "a" * 40,
                to_token_address="0x" + "b" * 40,
                from_chain_id=8453,
                to_chain_id=1,
                from_address="0x1234567890123456789012345678901234567890",
                amount="1000000000000000000",
            )

            assert success is False
            assert "API Error" in data

    def test_adapter_type(self, adapter):
        assert adapter.adapter_type == "BRAP"

    @pytest.mark.asyncio
    async def test_swap_from_quote_skips_approval_for_native_input(self, adapter):
        quote = {
            "provider": "lifi",
            "input_amount": "1000000000000000000",
            "output_amount": "990000000000000000",
            "calldata": {
                "chainId": 8453,
                "data": "0xswap",
                "from": "0x1234567890123456789012345678901234567890",
                "to": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "value": "1000000000000000000",
            },
        }

        with (
            patch(
                "wayfinder_paths.adapters.brap_adapter.adapter.ensure_allowance",
                new_callable=AsyncMock,
            ) as mock_ensure_allowance,
            patch(
                "wayfinder_paths.adapters.brap_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send_transaction,
            patch.object(
                adapter,
                "_record_swap_operation",
                new_callable=AsyncMock,
                return_value={"id": "record"},
            ),
        ):
            mock_send_transaction.return_value = "0xswap"

            success, result = await adapter.swap_from_quote(
                from_token={
                    "id": "native-base",
                    "address": "native",
                    "chain": {"id": 8453},
                    "decimals": 18,
                },
                to_token={
                    "id": "prompt-base",
                    "address": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "chain": {"id": 8453},
                    "decimals": 18,
                },
                from_address="0x1234567890123456789012345678901234567890",
                quote=quote,
            )

        assert success
        assert result["tx_hash"] == "0xswap"
        mock_ensure_allowance.assert_not_awaited()
        mock_send_transaction.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_swap_from_quote_waits_for_non_native_approval_before_swap(
        self, adapter
    ):
        events: list[str] = []
        quote = {
            "provider": "lifi",
            "input_amount": "1000000",
            "output_amount": "990000000000000000",
            "calldata": {
                "chainId": 8453,
                "data": "0xswap",
                "from": "0x1234567890123456789012345678901234567890",
                "to": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "value": "0",
            },
        }

        async def approve_then_continue(**_kwargs):
            events.append("approval")
            return True, "0xapproval"

        async def send_swap(*_args, **_kwargs):
            events.append("swap")
            return "0xswap"

        with (
            patch(
                "wayfinder_paths.adapters.brap_adapter.adapter.ensure_allowance",
                new=AsyncMock(side_effect=approve_then_continue),
            ) as mock_ensure_allowance,
            patch(
                "wayfinder_paths.adapters.brap_adapter.adapter.send_transaction",
                new=AsyncMock(side_effect=send_swap),
            ) as mock_send_transaction,
            patch.object(
                adapter,
                "_record_swap_operation",
                new_callable=AsyncMock,
                return_value={"id": "record"},
            ),
        ):
            success, result = await adapter.swap_from_quote(
                from_token={
                    "id": "usdc-base",
                    "address": "0xcccccccccccccccccccccccccccccccccccccccc",
                    "chain": {"id": 8453},
                    "decimals": 6,
                },
                to_token={
                    "id": "prompt-base",
                    "address": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "chain": {"id": 8453},
                    "decimals": 18,
                },
                from_address="0x1234567890123456789012345678901234567890",
                quote=quote,
            )

        assert success
        assert result["tx_hash"] == "0xswap"
        assert events == ["approval", "swap"]
        mock_ensure_allowance.assert_awaited_once_with(
            token_address="0xcccccccccccccccccccccccccccccccccccccccc",
            owner="0x1234567890123456789012345678901234567890",
            spender="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            amount=1000000,
            chain_id=8453,
            signing_callback=adapter.sign_callback,
        )
        mock_send_transaction.assert_awaited_once()
