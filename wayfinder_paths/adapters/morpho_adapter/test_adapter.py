from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.adapters.morpho_adapter.adapter import MorphoAdapter
from wayfinder_paths.core.constants.base import MAX_UINT256
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.morpho_constants import MERKL_DISTRIBUTOR_ADDRESS


@pytest.fixture
def adapter():
    return MorphoAdapter(
        config={},
        sign_callback=AsyncMock(return_value=b"\x00" * 65),
        wallet_address="0x81830bC5f811aF86fF6f17Fb9a619088B09Dff43",
    )


def _mock_market(unique_key: str, *, loan_addr: str, collateral_addr: str) -> dict:
    return {
        "marketId": unique_key,
        "listed": True,
        "lltv": str(860000000000000000),
        "irmAddress": "0x46415998764C29aB2a25CbeA6254146D50D22687",
        "oracle": {"address": "0xD09048c8B568Dbf5f189302beA26c9edABFC4858"},
        "loanAsset": {
            "address": loan_addr,
            "symbol": "WETH",
            "name": "Wrapped Ether",
            "decimals": 18,
            "price": {"usd": 2000.0},
        },
        "collateralAsset": {
            "address": collateral_addr,
            "symbol": "USDC",
            "name": "USD Coin",
            "decimals": 6,
            "price": {"usd": 1.0},
        },
        "state": {
            "supplyApy": 0.01,
            "netSupplyApy": 0.01,
            "borrowApy": 0.02,
            "netBorrowApy": 0.02,
            "utilization": 0.5,
            "apyAtTarget": 0.015,
            "liquidityAssets": str(123),
            "liquidityAssetsUsd": 1.23,
            "supplyAssets": str(456),
            "supplyAssetsUsd": 4.56,
            "borrowAssets": str(333),
            "borrowAssetsUsd": 3.33,
        },
    }


def test_adapter_type(adapter):
    assert adapter.adapter_type == "MORPHO"


def test_strategy_address_optional():
    a = MorphoAdapter(config={})
    assert a.wallet_address is None


@pytest.mark.asyncio
async def test_get_all_markets_success(adapter):
    key = "0x" + "11" * 32
    market = _mock_market(
        key,
        loan_addr="0x4200000000000000000000000000000000000006",
        collateral_addr="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    )

    with patch(
        "wayfinder_paths.adapters.morpho_adapter.adapter.MORPHO_CLIENT.get_all_markets",
        new=AsyncMock(return_value=[market]),
    ):
        ok, markets = await adapter.get_all_markets(chain_id=CHAIN_ID_BASE)

    assert ok is True
    assert isinstance(markets, list)
    assert len(markets) == 1
    assert markets[0]["marketId"] == key
    assert markets[0]["uniqueKey"] == key
    assert markets[0]["chainId"] == CHAIN_ID_BASE
    assert markets[0]["loan"]["price_usd"] == 2000.0
    assert markets[0]["state"]["supply_apy"] == 0.01


@pytest.mark.asyncio
async def test_lend_encodes_supply(adapter):
    key = "0x" + "22" * 32
    market = _mock_market(
        key,
        loan_addr="0x4200000000000000000000000000000000000006",
        collateral_addr="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    )

    with (
        patch.object(adapter, "_get_market", new=AsyncMock(return_value=market)),
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.ensure_allowance",
            new=AsyncMock(return_value=(True, None)),
        ) as mock_allow,
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.encode_call",
            new=AsyncMock(return_value={"chainId": CHAIN_ID_BASE}),
        ) as mock_encode,
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.send_transaction",
            new=AsyncMock(return_value="0xabc"),
        ),
    ):
        ok, tx = await adapter.lend(
            chain_id=CHAIN_ID_BASE,
            market_unique_key=key,
            qty=123,
        )

    assert ok is True
    assert tx == "0xabc"
    mock_allow.assert_awaited_once()
    mock_encode.assert_awaited_once()


@pytest.mark.asyncio
async def test_supply_collateral_uses_collateral_token(adapter):
    key = "0x" + "33" * 32
    market = _mock_market(
        key,
        loan_addr="0x4200000000000000000000000000000000000006",
        collateral_addr="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    )

    with (
        patch.object(adapter, "_get_market", new=AsyncMock(return_value=market)),
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.ensure_allowance",
            new=AsyncMock(return_value=(True, None)),
        ) as mock_allow,
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.encode_call",
            new=AsyncMock(return_value={"chainId": CHAIN_ID_BASE}),
        ),
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.send_transaction",
            new=AsyncMock(return_value="0xabc"),
        ),
    ):
        ok, _tx = await adapter.supply_collateral(
            chain_id=CHAIN_ID_BASE,
            market_unique_key=key,
            qty=1_000_000,
        )

    assert ok is True
    _, kwargs = mock_allow.await_args
    assert (
        kwargs["token_address"].lower()
        == "0x833589fcD6eDb6E08f4c7C32D4f71b54bdA02913".lower()
    )


@pytest.mark.asyncio
async def test_repay_full_uses_shares(adapter):
    key = "0x" + "44" * 32
    market = _mock_market(
        key,
        loan_addr="0x4200000000000000000000000000000000000006",
        collateral_addr="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    )

    with (
        patch.object(adapter, "_get_market", new=AsyncMock(return_value=market)),
        patch.object(adapter, "_position", new=AsyncMock(return_value=(0, 999, 0))),
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.ensure_allowance",
            new=AsyncMock(return_value=(True, None)),
        ) as mock_allow,
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.encode_call",
            new=AsyncMock(return_value={"chainId": CHAIN_ID_BASE}),
        ) as mock_encode,
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.send_transaction",
            new=AsyncMock(return_value="0xabc"),
        ),
    ):
        ok, _tx = await adapter.repay(
            chain_id=CHAIN_ID_BASE,
            market_unique_key=key,
            qty=0,
            repay_full=True,
        )

    assert ok is True
    _, allow_kwargs = mock_allow.await_args
    assert allow_kwargs["amount"] == MAX_UINT256

    _, encode_kwargs = mock_encode.await_args
    assert encode_kwargs["fn_name"] == "repay"
    assert encode_kwargs["args"][1] == 0
    assert encode_kwargs["args"][2] == 999


@pytest.mark.asyncio
async def test_withdraw_full_uses_shares(adapter):
    key = "0x" + "55" * 32
    market = _mock_market(
        key,
        loan_addr="0x4200000000000000000000000000000000000006",
        collateral_addr="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    )

    with (
        patch.object(adapter, "_get_market", new=AsyncMock(return_value=market)),
        patch.object(adapter, "_position", new=AsyncMock(return_value=(888, 0, 0))),
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.encode_call",
            new=AsyncMock(return_value={"chainId": CHAIN_ID_BASE}),
        ) as mock_encode,
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.send_transaction",
            new=AsyncMock(return_value="0xabc"),
        ),
    ):
        ok, _tx = await adapter.unlend(
            chain_id=CHAIN_ID_BASE,
            market_unique_key=key,
            qty=0,
            withdraw_full=True,
        )

    assert ok is True
    _, encode_kwargs = mock_encode.await_args
    assert encode_kwargs["fn_name"] == "withdraw"
    assert encode_kwargs["args"][1] == 0
    assert encode_kwargs["args"][2] == 888


@pytest.mark.asyncio
async def test_get_health_computes_maxes(adapter):
    key = "0x" + "66" * 32
    market = _mock_market(
        key,
        loan_addr="0x4200000000000000000000000000000000000006",
        collateral_addr="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    )
    market["lltv"] = str(500000000000000000)
    market["state"]["price"] = str(2 * (10**36))
    market["state"]["liquidityAssets"] = str(50)

    pos = {
        "healthFactor": 1.2,
        "priceVariationToLiquidationPrice": 0.1,
        "state": {
            "collateral": str(100),
            "borrowAssets": str(40),
        },
    }

    with (
        patch.object(adapter, "_get_market", new=AsyncMock(return_value=market)),
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.MORPHO_CLIENT.get_market_position",
            new=AsyncMock(return_value=pos),
        ),
    ):
        ok, out = await adapter.get_health(
            chain_id=CHAIN_ID_BASE, market_unique_key=key, account=None
        )

    assert ok is True
    assert out["max_borrow_assets"] == 50
    assert out["max_withdraw_collateral_assets"] == 60


@pytest.mark.asyncio
async def test_get_full_user_state_per_chain_filters_zero_positions(adapter):
    positions = [
        {
            "market": {"marketId": "0x" + "11" * 32},
            "state": {"supplyShares": 0, "borrowShares": 0, "collateral": 0},
        },
        {
            "market": {"marketId": "0x" + "22" * 32},
            "state": {"supplyShares": 1, "borrowShares": 0, "collateral": 0},
        },
    ]

    with patch(
        "wayfinder_paths.adapters.morpho_adapter.adapter.MORPHO_CLIENT.get_all_market_positions",
        new=AsyncMock(return_value=positions),
    ):
        ok, state = await adapter.get_full_user_state_per_chain(
            account="0x81830bC5f811aF86fF6f17Fb9a619088B09Dff43",
            chain_id=CHAIN_ID_BASE,
            include_zero_positions=False,
        )

    assert ok is True
    assert len(state["positions"]) == 1
    assert state["positions"][0]["marketId"] == "0x" + "22" * 32
    assert state["positions"][0]["marketUniqueKey"] == "0x" + "22" * 32


@pytest.mark.asyncio
async def test_claim_merkl_rewards_noop_when_none(adapter):
    with (
        patch.object(
            adapter,
            "get_claimable_rewards",
            new=AsyncMock(
                return_value=(
                    True,
                    {"merkl": {"rewards": []}},
                )
            ),
        ),
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.encode_call",
            new=AsyncMock(return_value={"chainId": CHAIN_ID_BASE}),
        ) as mock_encode,
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.send_transaction",
            new=AsyncMock(return_value="0xabc"),
        ) as mock_send,
    ):
        ok, tx = await adapter.claim_merkl_rewards(chain_id=CHAIN_ID_BASE)

    assert ok is True
    assert tx is None
    mock_encode.assert_not_awaited()
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_merkl_rewards_encodes_claim(adapter):
    token_a = "0x4200000000000000000000000000000000000006"
    token_b = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    rewards = [
        {
            "token": {"address": token_a},
            "amount": "99",
            "proofs": ["0x" + "11" * 32],
        },
        {
            "token": {"address": token_b},
            "amount": "101",
            "proofs": ["0x" + "22" * 32, "0x" + "33" * 32],
        },
    ]
    with (
        patch.object(
            adapter,
            "get_claimable_rewards",
            new=AsyncMock(
                return_value=(
                    True,
                    {"merkl": {"rewards": rewards}},
                )
            ),
        ),
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.encode_call",
            new=AsyncMock(return_value={"chainId": CHAIN_ID_BASE}),
        ) as mock_encode,
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.send_transaction",
            new=AsyncMock(return_value="0xabc"),
        ),
    ):
        ok, tx = await adapter.claim_merkl_rewards(
            chain_id=CHAIN_ID_BASE,
            min_claim_amount=100,
        )

    assert ok is True
    assert tx == "0xabc"

    _args, kwargs = mock_encode.await_args
    assert kwargs["target"] == MERKL_DISTRIBUTOR_ADDRESS
    assert kwargs["fn_name"] == "claim"

    users, tokens, amounts, proofs = kwargs["args"]
    assert users == [adapter.wallet_address]
    assert tokens == [token_b]
    assert amounts == [101]
    assert proofs == [["0x" + "22" * 32, "0x" + "33" * 32]]


@pytest.mark.asyncio
async def test_claim_urd_rewards_sends_tx_data(adapter):
    dists = [
        {
            "claimable": "0",
            "txData": "0x1234",
            "distributor": {"address": "0x1111111111111111111111111111111111111111"},
        },
        {
            "claimable": "5",
            "txData": "0xabcd",
            "distributor": {"address": "0x2222222222222222222222222222222222222222"},
        },
    ]
    with (
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.MORPHO_REWARDS_CLIENT.get_user_distributions",
            new=AsyncMock(return_value=dists),
        ),
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.send_transaction",
            new=AsyncMock(return_value="0xaaa"),
        ) as mock_send,
    ):
        ok, txs = await adapter.claim_urd_rewards(
            chain_id=CHAIN_ID_BASE,
            min_claimable=1,
        )

    assert ok is True
    assert txs == ["0xaaa"]
    mock_send.assert_awaited_once()

    _args, kwargs = mock_send.await_args
    tx = _args[0]
    assert tx["to"].lower() == "0x2222222222222222222222222222222222222222"
    assert tx["data"] == "0xabcd"


@pytest.mark.asyncio
async def test_borrow_with_jit_liquidity_atomic_uses_bundler(adapter):
    adapter.bundler_address = "0x1111111111111111111111111111111111111111"
    market_key = "0x" + "aa" * 32
    withdraw_key = "0x" + "bb" * 32
    market = _mock_market(
        market_key,
        loan_addr="0x4200000000000000000000000000000000000006",
        collateral_addr="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    )
    market["state"]["liquidityAssets"] = "0"
    market["publicAllocatorSharedLiquidity"] = [
        {
            "assets": "1000",
            "publicAllocator": {
                "address": "0x9999999999999999999999999999999999999999"
            },
            "vault": {"address": "0x8888888888888888888888888888888888888888"},
            "withdrawMarket": {"marketId": withdraw_key},
            "supplyMarket": {"marketId": market_key},
        }
    ]
    withdraw_market = _mock_market(
        withdraw_key,
        loan_addr="0x4200000000000000000000000000000000000006",
        collateral_addr="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    )

    async def _get_market(*, chain_id: int, unique_key: str):
        return market if unique_key == market_key else withdraw_market

    with (
        patch.object(adapter, "_get_market", new=AsyncMock(side_effect=_get_market)),
        patch.object(
            adapter, "get_public_allocator_fee", new=AsyncMock(return_value=(True, 123))
        ),
        patch.object(
            adapter,
            "_encode_data",
            new=AsyncMock(side_effect=["0xcall1", "0xcall2"]),
        ),
        patch.object(
            adapter,
            "bundler_multicall",
            new=AsyncMock(return_value=(True, "0xmulticall")),
        ) as mock_multi,
    ):
        ok, tx = await adapter.borrow_with_jit_liquidity(
            chain_id=CHAIN_ID_BASE,
            market_unique_key=market_key,
            qty=10,
            atomic=True,
        )

    assert ok is True
    assert tx == "0xmulticall"

    _args, kwargs = mock_multi.await_args
    assert kwargs["calls"] == ["0xcall1", "0xcall2"]
    assert kwargs["value"] == 123


@pytest.mark.asyncio
async def test_get_all_vaults_formats_current_v1_and_v2_fields(adapter):
    asset = {
        "address": "0x2222222222222222222222222222222222222222",
        "symbol": "USDC",
        "name": "USD Coin",
        "decimals": 6,
        "price": {"usd": 1.0},
    }
    reward = {
        "supplyApr": 0.02,
        "asset": {
            "address": "0x3333333333333333333333333333333333333333",
            "symbol": "MORPHO",
            "name": "Morpho",
            "decimals": 18,
            "price": {"usd": 4.0},
        },
    }
    v1 = {
        "address": "0x1111111111111111111111111111111111111111",
        "symbol": "v1",
        "name": "Vault V1",
        "listed": True,
        "asset": asset,
        "state": {
            "apy": 0.03,
            "netApy": 0.04,
            "netApyExcludingRewards": 0.02,
            "avgNetApy": 0.035,
            "avgNetApyExcludingRewards": 0.025,
            "totalAssets": "100",
            "totalAssetsUsd": 100.0,
            "totalSupply": "99",
            "allRewards": [reward],
            "allocation": [{"market": {"marketId": "0x" + "11" * 32}}],
        },
    }
    v2 = {
        "address": "0x4444444444444444444444444444444444444444",
        "type": "MetaMorphoV2",
        "symbol": "v2",
        "name": "Vault V2",
        "listed": True,
        "asset": asset,
        "apy": 0.05,
        "netApy": 0.06,
        "avgNetApy": 0.055,
        "avgNetApyExcludingRewards": 0.045,
        "totalAssets": "200",
        "totalAssetsUsd": 200.0,
        "totalSupply": "198",
        "sharePrice": 1.01,
        "liquidity": "50",
        "liquidityUsd": 50.0,
        "idleAssets": "10",
        "idleAssetsUsd": 10.0,
        "rewards": [reward],
        "adapters": {"items": [{"address": "0x5555", "type": "MorphoMarketV1"}]},
    }

    with (
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.MORPHO_CLIENT.get_all_vaults",
            new=AsyncMock(return_value=[v1]),
        ),
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.MORPHO_CLIENT.get_all_vault_v2s",
            new=AsyncMock(return_value=[v2]),
        ),
    ):
        ok, vaults = await adapter.get_all_vaults(chain_id=CHAIN_ID_BASE)

    assert ok is True
    assert isinstance(vaults, list)
    assert vaults[0]["state"]["all_rewards"] == [reward]
    assert vaults[0]["state"]["incentives"][0]["token"]["price_usd"] == 4.0
    assert vaults[1]["vault_type"] == "MetaMorphoV2"
    assert vaults[1]["state"]["avg_apy"] == 0.045
    assert vaults[1]["state"]["avg_net_apy_excluding_rewards"] == 0.045
    assert vaults[1]["state"]["share_price"] == 1.01
    assert vaults[1]["state"]["idle_assets"] == 10


@pytest.mark.asyncio
async def test_claim_rewards_defaults_to_merkl_only(adapter):
    with (
        patch.object(
            adapter, "claim_merkl_rewards", new=AsyncMock(return_value=(True, "0xabc"))
        ) as mock_merkl,
        patch.object(
            adapter, "claim_urd_rewards", new=AsyncMock(return_value=(True, ["0xdef"]))
        ) as mock_urd,
    ):
        ok, out = await adapter.claim_rewards(chain_id=CHAIN_ID_BASE)

    assert ok is True
    assert out["merkl_tx"] == "0xabc"
    assert "urd_txs" not in out
    mock_merkl.assert_awaited_once()
    mock_urd.assert_not_awaited()


@pytest.mark.asyncio
async def test_vault_deposit_approves_asset_and_calls_deposit(adapter):
    vault = "0x1111111111111111111111111111111111111111"
    asset = "0x2222222222222222222222222222222222222222"

    with (
        patch.object(adapter, "_vault_asset", new=AsyncMock(return_value=asset)),
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.ensure_allowance",
            new=AsyncMock(return_value=(True, None)),
        ) as mock_allow,
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.encode_call",
            new=AsyncMock(return_value={"chainId": CHAIN_ID_BASE}),
        ) as mock_encode,
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.send_transaction",
            new=AsyncMock(return_value="0xabc"),
        ),
    ):
        ok, tx = await adapter.vault_deposit(
            chain_id=CHAIN_ID_BASE,
            vault_address=vault,
            assets=123,
        )

    assert ok is True
    assert tx == "0xabc"

    _args, allow_kwargs = mock_allow.await_args
    assert allow_kwargs["token_address"] == asset
    assert allow_kwargs["spender"].lower() == vault.lower()

    _args, encode_kwargs = mock_encode.await_args
    assert encode_kwargs["fn_name"] == "deposit"
    assert encode_kwargs["args"][0] == 123


@pytest.mark.asyncio
async def test_get_all_markets_drops_null_state(adapter):
    """Markets Morpho returned with no state (transient degradation) are dropped."""
    key = "0x" + "11" * 32
    good = _mock_market(
        key,
        loan_addr="0x4200000000000000000000000000000000000006",
        collateral_addr="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    )
    bad = _mock_market(
        "0x" + "22" * 32,
        loan_addr="0x4200000000000000000000000000000000000006",
        collateral_addr="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    )
    bad["state"] = None  # Morpho returned an item with no state snapshot

    with patch(
        "wayfinder_paths.adapters.morpho_adapter.adapter.MORPHO_CLIENT.get_all_markets",
        new=AsyncMock(return_value=[good, bad]),
    ):
        ok, kept = await adapter.get_all_markets(chain_id=CHAIN_ID_BASE)
        ok2, all_ = await adapter.get_all_markets(
            chain_id=CHAIN_ID_BASE, require_state=False
        )

    assert ok and ok2
    assert [m["marketId"] for m in kept] == [key]  # null-state market dropped
    assert len(all_) == 2  # opt-out keeps everything


def _vault_asset() -> dict:
    return {
        "address": "0x2222222222222222222222222222222222222222",
        "symbol": "USDC",
        "name": "USD Coin",
        "decimals": 6,
        "price": {"usd": 1.0},
    }


@pytest.mark.asyncio
async def test_get_all_vaults_drops_incomplete(adapter):
    """v1 vaults with null state and v2 vaults with no apy are dropped; no fake 0%."""
    asset = _vault_asset()
    v1_good = {
        "address": "0x1111111111111111111111111111111111111111",
        "symbol": "v1good",
        "listed": True,
        "asset": asset,
        "state": {"apy": 0.03, "netApy": 0.04, "totalAssets": "1", "totalAssetsUsd": 1.0},
    }
    v1_bad = {
        "address": "0x1111111111111111111111111111111111111112",
        "symbol": "v1bad",
        "listed": True,
        "asset": asset,
        "state": None,  # transient -> would otherwise rank at fabricated 0%
    }
    v2_good = {
        "address": "0x4444444444444444444444444444444444444444",
        "type": "MetaMorphoV2",
        "symbol": "v2good",
        "listed": True,
        "asset": asset,
        "apy": 0.05,
        "netApy": 0.06,
    }
    v2_bad = {
        "address": "0x4444444444444444444444444444444444444445",
        "type": "MetaMorphoV2",
        "symbol": "v2bad",
        "listed": True,
        "asset": asset,
        "apy": None,
        "netApy": None,
    }

    with (
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.MORPHO_CLIENT.get_all_vaults",
            new=AsyncMock(return_value=[v1_good, v1_bad]),
        ),
        patch(
            "wayfinder_paths.adapters.morpho_adapter.adapter.MORPHO_CLIENT.get_all_vault_v2s",
            new=AsyncMock(return_value=[v2_good, v2_bad]),
        ),
    ):
        ok, kept = await adapter.get_all_vaults(chain_id=CHAIN_ID_BASE)
        ok2, all_ = await adapter.get_all_vaults(
            chain_id=CHAIN_ID_BASE, require_state=False
        )

    assert ok and ok2
    kept_symbols = {v["symbol"] for v in kept}
    assert kept_symbols == {"v1good", "v2good"}  # incomplete vaults dropped
    # every kept vault has a real (non-None) APY -- never a fabricated 0%
    assert all(v["state"]["apy_with_rewards"] is not None for v in kept)
    assert len(all_) == 4  # opt-out keeps everything
