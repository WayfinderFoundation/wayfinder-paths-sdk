from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wayfinder_paths.adapters.compound_adapter.adapter import (
    CompoundAdapter,
    _parse_asset_info,
    _parse_reward_config,
    _parse_reward_owed,
    _parse_totals_basic,
    _parse_user_basic,
)
from wayfinder_paths.core.constants.base import MAX_UINT256
from wayfinder_paths.core.constants.compound_abi import COMET_ABI, COMET_REWARDS_ABI
from wayfinder_paths.core.constants.compound_contracts import COMPOUND_COMET_BY_CHAIN
from wayfinder_paths.core.utils.multicall import (
    Call,
    read_only_calls_multicall_or_gather,
)
from wayfinder_paths.core.utils.web3 import web3_from_chain_id


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
        assert len(seeds) == 17
        assert {seed.chain_id for seed in seeds} == {1, 137, 8453, 42161}
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


async def _web3_or_skip(chain_id: int) -> tuple[Any, Any]:
    ctx = web3_from_chain_id(chain_id)
    try:
        web3 = await ctx.__aenter__()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"RPC unreachable for chain {chain_id}: {exc}")
    try:
        await web3.eth.get_block_number()
    except Exception as exc:  # noqa: BLE001
        await ctx.__aexit__(None, None, None)
        pytest.skip(f"RPC unreachable for chain {chain_id}: {exc}")
    return ctx, web3


@pytest.mark.asyncio
@pytest.mark.local
@pytest.mark.skipif(
    os.environ.get("CI") == "true",
    reason="Live Compound shape test - local only",
)
async def test_live_compound_struct_reads_normalize_across_direct_and_multicall() -> None:
    chain_id = 8453
    market = COMPOUND_COMET_BY_CHAIN[chain_id]["markets"]["usdc"]
    comet_address = market["comet"]
    rewards_address = COMPOUND_COMET_BY_CHAIN[chain_id]["rewards"]
    account = "0x0000000000000000000000000000000000000001"

    ctx, web3 = await _web3_or_skip(chain_id)
    try:
        comet = web3.eth.contract(address=comet_address, abi=COMET_ABI)
        rewards = web3.eth.contract(address=rewards_address, abi=COMET_REWARDS_ABI)

        direct_asset_info = await comet.functions.getAssetInfo(0).call(
            block_identifier="latest"
        )
        asset_address = direct_asset_info[1]

        direct_rows = {
            "totals_basic": await comet.functions.totalsBasic().call(
                block_identifier="latest"
            ),
            "user_basic": await comet.functions.userBasic(account).call(
                block_identifier="latest"
            ),
            "asset_info": direct_asset_info,
            "asset_info_by_address": await comet.functions.getAssetInfoByAddress(
                asset_address
            ).call(block_identifier="latest"),
            "totals_collateral": await comet.functions.totalsCollateral(
                asset_address
            ).call(block_identifier="latest"),
            "reward_config": await rewards.functions.rewardConfig(comet_address).call(
                block_identifier="latest"
            ),
            "reward_owed": await rewards.functions.getRewardOwed(
                comet_address,
                account,
            ).call(block_identifier="latest"),
        }

        multicall_rows = await read_only_calls_multicall_or_gather(
            web3=web3,
            chain_id=chain_id,
            calls=[
                Call(comet, "totalsBasic"),
                Call(comet, "userBasic", args=(account,)),
                Call(comet, "getAssetInfo", args=(0,)),
                Call(comet, "getAssetInfoByAddress", args=(asset_address,)),
                Call(comet, "totalsCollateral", args=(asset_address,)),
                Call(rewards, "rewardConfig", args=(comet_address,)),
                Call(rewards, "getRewardOwed", args=(comet_address, account)),
            ],
            block_identifier="latest",
        )
    finally:
        await ctx.__aexit__(None, None, None)

    raw_pairs = [
        ("totals_basic", direct_rows["totals_basic"], multicall_rows[0]),
        ("user_basic", direct_rows["user_basic"], multicall_rows[1]),
        ("asset_info", direct_rows["asset_info"], multicall_rows[2]),
        (
            "asset_info_by_address",
            direct_rows["asset_info_by_address"],
            multicall_rows[3],
        ),
        ("totals_collateral", direct_rows["totals_collateral"], multicall_rows[4]),
        ("reward_config", direct_rows["reward_config"], multicall_rows[5]),
        ("reward_owed", direct_rows["reward_owed"], multicall_rows[6]),
    ]

    for _name, direct_value, multicall_value in raw_pairs:
        assert isinstance(direct_value, (list, tuple))
        assert isinstance(multicall_value, tuple)
        assert len(direct_value) == len(multicall_value)

    differences = [
        name
        for name, direct_value, multicall_value in raw_pairs
        if type(direct_value) is not type(multicall_value)
        or direct_value != multicall_value
    ]
    assert differences, "Expected at least one raw shape or value mismatch to justify normalization"

    assert _parse_totals_basic(direct_rows["totals_basic"]) == _parse_totals_basic(
        multicall_rows[0]
    )
    assert _parse_user_basic(direct_rows["user_basic"]) == _parse_user_basic(
        multicall_rows[1]
    )
    assert _parse_asset_info(direct_rows["asset_info"]) == _parse_asset_info(
        multicall_rows[2]
    )
    assert _parse_asset_info(
        direct_rows["asset_info_by_address"]
    ) == _parse_asset_info(multicall_rows[3])
    assert int(direct_rows["totals_collateral"][0]) == int(multicall_rows[4][0])
    assert _parse_reward_config(direct_rows["reward_config"]) == _parse_reward_config(
        multicall_rows[5]
    )
    assert _parse_reward_owed(direct_rows["reward_owed"]) == _parse_reward_owed(
        multicall_rows[6]
    )
