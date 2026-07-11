from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wayfinder_paths.adapters.brap_adapter.adapter import BRAPAdapter

_ADAPTER_MODULE = "wayfinder_paths.adapters.brap_adapter.adapter"

SOL_OWNER = "4Nd1mBQtrMJVYVfKf2PJy9NZUZdTAsp7D4xWLs4gDB4T"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"

SOLANA_FROM_TOKEN = {
    "id": 101,
    "token_id": "solana",
    "address": WRAPPED_SOL_MINT,
    "decimals": 9,
    "chain": {"id": 900},
}
SOLANA_TO_TOKEN = {
    "id": 102,
    "token_id": "usd-coin-solana",
    "address": USDC_MINT,
    "decimals": 6,
    "chain": {"id": 900},
}


def _solana_quote() -> dict:
    return {
        "provider": "jupiter",
        "input_amount": 1_000_000_000,
        "output_amount": 150_000_000,
        "from_amount_usd": 150.0,
        "to_amount_usd": 149.5,
        "calldata": {
            "chainType": "solana",
            "chainId": 900,
            "serializedTransaction": "c2VyaWFsaXplZC10eA==",
            "lastValidBlockHeight": 250_000_000,
        },
    }


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
    async def test_swap_from_quote_solana_envelope(self):
        sign_callback = AsyncMock()
        adapter = BRAPAdapter(sign_callback=sign_callback, wallet_address=SOL_OWNER)
        adapter.ledger_adapter.record_operation = AsyncMock(
            return_value=(True, {"id": "ledger-1"})
        )
        quote = _solana_quote()
        signature = "5SigBase58SignatureFromSvmSend"

        with (
            patch(
                f"{_ADAPTER_MODULE}.send_solana_versioned_transaction",
                new=AsyncMock(return_value=signature),
            ) as mock_svm_send,
            patch(
                f"{_ADAPTER_MODULE}.send_transaction", new=AsyncMock()
            ) as mock_evm_send,
            patch(
                f"{_ADAPTER_MODULE}.ensure_allowance", new=AsyncMock()
            ) as mock_allowance,
            patch(f"{_ADAPTER_MODULE}.Web3", new=MagicMock()) as mock_web3,
        ):
            success, payload = await adapter.swap_from_quote(
                from_token=SOLANA_FROM_TOKEN,
                to_token=SOLANA_TO_TOKEN,
                from_address=SOL_OWNER,
                quote=quote,
            )

        assert success is True
        assert payload["tx_hash"] == signature
        assert payload["from_amount"] == quote["input_amount"]
        assert payload["to_amount"] == quote["output_amount"]
        assert payload["ledger_record"] == {"id": "ledger-1"}

        # Envelope routed straight to the SVM send flow.
        mock_svm_send.assert_awaited_once_with(
            "c2VyaWFsaXplZC10eA==", sign_callback, chain_id=900
        )
        # No EVM machinery: no checksumming, no allowance, no EVM broadcast.
        mock_web3.to_checksum_address.assert_not_called()
        mock_allowance.assert_not_awaited()
        mock_evm_send.assert_not_awaited()

        # Swap operation recorded with the base58 signature as the tx hash.
        adapter.ledger_adapter.record_operation.assert_awaited_once()
        record_kwargs = adapter.ledger_adapter.record_operation.await_args.kwargs
        assert record_kwargs["wallet_address"] == SOL_OWNER
        operation = record_kwargs["operation_data"]
        assert operation.transaction_hash == signature
        assert operation.transaction_chain_id == 900
        assert operation.from_amount == str(quote["input_amount"])
        assert operation.to_amount == str(quote["output_amount"])

    @pytest.mark.asyncio
    async def test_swap_from_quote_solana_missing_serialized_transaction(self):
        adapter = BRAPAdapter(sign_callback=AsyncMock(), wallet_address=SOL_OWNER)
        quote = _solana_quote()
        del quote["calldata"]["serializedTransaction"]

        with patch(
            f"{_ADAPTER_MODULE}.send_solana_versioned_transaction", new=AsyncMock()
        ) as mock_svm_send:
            success, error = await adapter.swap_from_quote(
                from_token=SOLANA_FROM_TOKEN,
                to_token=SOLANA_TO_TOKEN,
                from_address=SOL_OWNER,
                quote=quote,
            )

        assert success is False
        assert error == "Quote missing serializedTransaction"
        mock_svm_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_swap_from_quote_evm_regression(self):
        sign_callback = AsyncMock()
        from_address = "0x1234567890123456789012345678901234567890"
        from_token = {
            "id": 1,
            "token_id": "usd-coin-base",
            "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "decimals": 6,
            "chain": {"id": 8453},
        }
        to_token = {
            "id": 2,
            "token_id": "weth-base",
            "address": "0x4200000000000000000000000000000000000006",
            "decimals": 18,
            "chain": {"id": 8453},
        }
        quote = {
            "provider": "enso",
            "input_amount": 1_000_000,
            "output_amount": 400_000_000_000_000,
            "from_amount_usd": 1.0,
            "to_amount_usd": 0.99,
            "calldata": {
                "data": "0xdeadbeef",
                "to": "0x1111111111111111111111111111111111111111",
                "value": "0",
                "chainId": 8453,
            },
        }
        adapter = BRAPAdapter(sign_callback=sign_callback, wallet_address=from_address)
        adapter.ledger_adapter.record_operation = AsyncMock(
            return_value=(True, {"id": "ledger-2"})
        )

        with (
            patch(
                f"{_ADAPTER_MODULE}.send_transaction",
                new=AsyncMock(return_value="0xevmhash"),
            ) as mock_evm_send,
            patch(
                f"{_ADAPTER_MODULE}.ensure_allowance", new=AsyncMock()
            ) as mock_allowance,
            patch(
                f"{_ADAPTER_MODULE}.send_solana_versioned_transaction",
                new=AsyncMock(),
            ) as mock_svm_send,
        ):
            success, payload = await adapter.swap_from_quote(
                from_token=from_token,
                to_token=to_token,
                from_address=from_address,
                quote=quote,
            )

        assert success is True
        assert payload["tx_hash"] == "0xevmhash"
        mock_svm_send.assert_not_awaited()

        mock_allowance.assert_awaited_once_with(
            token_address=from_token["address"],
            owner=from_address,
            spender="0x1111111111111111111111111111111111111111",
            amount=1_000_000,
            chain_id=8453,
            signing_callback=sign_callback,
        )

        mock_evm_send.assert_awaited_once()
        transaction = mock_evm_send.await_args.args[0]
        assert transaction["chainId"] == 8453
        assert transaction["from"] == from_address
        assert transaction["data"] == "0xdeadbeef"
        assert transaction["value"] == 0

        operation = adapter.ledger_adapter.record_operation.await_args.kwargs[
            "operation_data"
        ]
        assert operation.transaction_hash == "0xevmhash"
        assert operation.transaction_chain_id == 8453
