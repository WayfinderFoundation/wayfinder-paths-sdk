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
        "uniqueKey": unique_key,
        "listed": True,
        "lltv": str(860000000000000000),
        "irmAddress": "0x46415998764C29aB2a25CbeA6254146D50D22687",
        "oracle": {"address": "0xD09048c8B568Dbf5f189302beA26c9edABFC4858"},
        "loanAsset": {
            "address": loan_addr,
            "symbol": "WETH",
            "name": "Wrapped Ether",
            "decimals": 18,
            "priceUsd": 2000.0,
        },
        "collateralAsset": {
            "address": collateral_addr,
            "symbol": "USDC",
            "name": "USD Coin",
            "decimals": 6,
            "priceUsd": 1.0,
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
    assert markets[0]["uniqueKey"] == key
    assert markets[0]["chainId"] == CHAIN_ID_BASE
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
            "market": {"uniqueKey": "0x" + "11" * 32},
            "state": {"supplyShares": 0, "borrowShares": 0, "collateral": 0},
        },
        {
            "market": {"uniqueKey": "0x" + "22" * 32},
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
            "withdrawMarket": {"uniqueKey": withdraw_key},
            "supplyMarket": {"uniqueKey": market_key},
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
