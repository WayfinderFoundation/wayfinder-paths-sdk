from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wayfinder_paths.adapters.compound_adapter.adapter import CompoundAdapter
from wayfinder_paths.core.constants.base import MAX_UINT256
from wayfinder_paths.core.constants.compound_contracts import COMPOUND_COMET_BY_CHAIN


class TestCompoundAdapter:
    @pytest.fixture
    def adapter(self) -> CompoundAdapter:
        return CompoundAdapter(
            config={},
            sign_callback=AsyncMock(return_value=b"signed"),
            wallet_address="0x1234567890123456789012345678901234567890",
        )

    def test_init(self, adapter: CompoundAdapter) -> None:
        assert adapter.adapter_type == "COMPOUND"
        assert adapter.name == "compound_adapter"

    def test_registry_covers_expected_market_count(
        self, adapter: CompoundAdapter
    ) -> None:
        seeds = adapter._list_market_seeds()
        assert len(seeds) == 28
        assert any(seed.chain_id == 1 and seed.market_name == "usdc" for seed in seeds)
        assert any(
            seed.chain_id == 8453 and seed.market_name == "usdc" for seed in seeds
        )
        assert any(
            seed.chain_id == 1
            and seed.market_name == "wsteth"
            and seed.bulker.lower() == "0x2c776041ccfe903071af44aa147368a9c8eea518"
            for seed in seeds
        )

    @pytest.mark.asyncio
    async def test_get_full_user_state_filters_zero_positions(
        self, adapter: CompoundAdapter
    ) -> None:
        with (
            patch.object(
                adapter,
                "_list_market_seeds",
                return_value=[
                    adapter._find_market_seed(
                        chain_id=1,
                        comet=COMPOUND_COMET_BY_CHAIN[1]["markets"]["usdc"]["comet"],
                    ),
                    adapter._find_market_seed(
                        chain_id=1,
                        comet=COMPOUND_COMET_BY_CHAIN[1]["markets"]["weth"]["comet"],
                    ),
                ],
            ),
            patch.object(adapter, "get_pos", new_callable=AsyncMock) as mock_get_pos,
        ):
            mock_get_pos.side_effect = [
                (
                    True,
                    {
                        "chain_id": 1,
                        "market_name": "usdc",
                        "comet": COMPOUND_COMET_BY_CHAIN[1]["markets"]["usdc"]["comet"],
                        "supplied_base": 0,
                        "borrowed_base": 0,
                        "collateral_positions": [],
                    },
                ),
                (
                    True,
                    {
                        "chain_id": 1,
                        "market_name": "weth",
                        "comet": COMPOUND_COMET_BY_CHAIN[1]["markets"]["weth"]["comet"],
                        "supplied_base": 0,
                        "borrowed_base": 1,
                        "collateral_positions": [],
                    },
                ),
            ]

            ok, state = await adapter.get_full_user_state(
                account="0x1234567890123456789012345678901234567890",
                chain_id=1,
                include_zero_positions=False,
            )

        assert ok is True
        assert isinstance(state, dict)
        assert state["position_count"] == 1
        assert len(state["positions"]) == 1
        assert state["positions"][0]["market_name"] == "weth"

    @pytest.mark.asyncio
    async def test_withdraw_collateral_full_uses_exact_balance(
        self, adapter: CompoundAdapter
    ) -> None:
        mock_contract = MagicMock()
        collateral_fn = MagicMock()
        collateral_fn.call = AsyncMock(return_value=12345)
        mock_contract.functions.collateralBalanceOf.return_value = collateral_fn

        mock_web3 = MagicMock()
        mock_web3.eth.contract.return_value = mock_contract

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id: int):
            yield mock_web3

        with (
            patch.object(
                adapter,
                "_get_collateral_asset_info",
                new=AsyncMock(
                    return_value={
                        "asset": "0x1111111111111111111111111111111111111111",
                    }
                ),
            ),
            patch(
                "wayfinder_paths.adapters.compound_adapter.adapter.web3_from_chain_id",
                mock_web3_ctx,
            ),
            patch(
                "wayfinder_paths.adapters.compound_adapter.adapter.encode_call",
                new_callable=AsyncMock,
                return_value={"chainId": 1},
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.compound_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
                return_value="0xabc",
            ),
        ):
            ok, tx = await adapter.withdraw_collateral(
                chain_id=1,
                comet=COMPOUND_COMET_BY_CHAIN[1]["markets"]["usdc"]["comet"],
                collateral_asset="0x1111111111111111111111111111111111111111",
                amount=0,
                withdraw_full=True,
            )

        assert ok is True
        assert tx == "0xabc"
        assert mock_encode.call_args.kwargs["args"][1] == 12345

    @pytest.mark.asyncio
    async def test_unlend_full_uses_max_uint256(self, adapter: CompoundAdapter) -> None:
        with (
            patch.object(
                adapter,
                "_resolve_base_token",
                new=AsyncMock(
                    return_value="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
                ),
            ),
            patch(
                "wayfinder_paths.adapters.compound_adapter.adapter.encode_call",
                new_callable=AsyncMock,
                return_value={"chainId": 1},
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.compound_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
                return_value="0xabc",
            ),
        ):
            ok, tx = await adapter.unlend(
                chain_id=1,
                comet=COMPOUND_COMET_BY_CHAIN[1]["markets"]["usdc"]["comet"],
                base_token="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                amount=0,
                withdraw_full=True,
            )

        assert ok is True
        assert tx == "0xabc"
        assert mock_encode.call_args.kwargs["args"][1] == MAX_UINT256
