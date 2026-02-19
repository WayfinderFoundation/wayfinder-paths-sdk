from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eth_utils import to_checksum_address

from wayfinder_paths.adapters.avantis_adapter.adapter import AvantisAdapter
from wayfinder_paths.core.constants.contracts import (
    AVANTIS_AVUSDC,
    AVANTIS_VAULT_MANAGER,
    BASE_USDC,
)

FAKE_WALLET = "0x1234567890123456789012345678901234567890"


@pytest.fixture
def adapter():
    return AvantisAdapter(
        config={"strategy_wallet": {"address": FAKE_WALLET}}
    )


@pytest.fixture
def adapter_with_signer():
    return AvantisAdapter(
        config={"strategy_wallet": {"address": FAKE_WALLET}},
        strategy_wallet_signing_callback=AsyncMock(return_value="0xdeadbeef"),
    )


@pytest.fixture
def adapter_no_wallet():
    return AvantisAdapter(config={})


def _mock_call(return_value):
    """Shorthand: MagicMock whose .call() is an AsyncMock returning *return_value*."""
    return MagicMock(call=AsyncMock(return_value=return_value))


def _make_erc4626_contract(
    *,
    asset=BASE_USDC,
    decimals=6,
    symbol="avUSDC",
    name="Avantis USDC Vault",
    total_assets=1_000_000_000,
    total_supply=950_000_000,
    balance_of=500_000,
    convert_to_assets=520_000,
    max_redeem=500_000,
    max_withdraw=520_000,
):
    contract = MagicMock()
    contract.functions.asset = MagicMock(return_value=_mock_call(asset))
    contract.functions.decimals = MagicMock(return_value=_mock_call(decimals))
    contract.functions.symbol = MagicMock(return_value=_mock_call(symbol))
    contract.functions.name = MagicMock(return_value=_mock_call(name))
    contract.functions.totalAssets = MagicMock(return_value=_mock_call(total_assets))
    contract.functions.totalSupply = MagicMock(return_value=_mock_call(total_supply))
    contract.functions.balanceOf = MagicMock(return_value=_mock_call(balance_of))
    contract.functions.convertToAssets = MagicMock(
        return_value=_mock_call(convert_to_assets)
    )
    contract.functions.maxRedeem = MagicMock(return_value=_mock_call(max_redeem))
    contract.functions.maxWithdraw = MagicMock(return_value=_mock_call(max_withdraw))
    return contract


# ---------------------------------------------------------------------------
# Config / init
# ---------------------------------------------------------------------------


def test_adapter_type(adapter):
    assert adapter.adapter_type == "AVANTIS"


def test_default_addresses(adapter):
    assert adapter.chain_id == 8453
    assert adapter.vault == AVANTIS_AVUSDC
    assert adapter.vault_manager == AVANTIS_VAULT_MANAGER
    assert adapter.underlying == BASE_USDC


def test_custom_config_overrides():
    custom_vault = "0x000000000000000000000000000000000000aaaa"
    custom_manager = "0x000000000000000000000000000000000000bbbb"
    custom_underlying = "0x000000000000000000000000000000000000cccc"
    adapter = AvantisAdapter(
        config={
            "avantis": {
                "vault": custom_vault,
                "vault_manager": custom_manager,
                "underlying": custom_underlying,
            },
        }
    )
    assert adapter.vault == to_checksum_address(custom_vault)
    assert adapter.vault_manager == to_checksum_address(custom_manager)
    assert adapter.underlying == to_checksum_address(custom_underlying)


def test_no_wallet_configured(adapter_no_wallet):
    assert adapter_no_wallet.strategy_wallet_address is None


# ---------------------------------------------------------------------------
# borrow / repay (unsupported)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_borrow_and_repay_unsupported(adapter):
    ok, msg = await adapter.borrow()
    assert ok is False
    assert "does not support" in msg.lower()

    ok, msg = await adapter.repay()
    assert ok is False
    assert "does not support" in msg.lower()


# ---------------------------------------------------------------------------
# lend guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lend_requires_signing_callback(adapter):
    ok, msg = await adapter.lend(amount=1)
    assert ok is False
    assert "signing callback" in str(msg).lower()


@pytest.mark.asyncio
async def test_lend_requires_wallet(adapter_no_wallet):
    ok, msg = await adapter_no_wallet.lend(amount=1)
    assert ok is False
    assert "wallet address" in str(msg).lower()


@pytest.mark.asyncio
async def test_lend_rejects_zero_amount(adapter_with_signer):
    ok, msg = await adapter_with_signer.lend(amount=0)
    assert ok is False
    assert "positive" in str(msg).lower()


@pytest.mark.asyncio
async def test_lend_rejects_negative_amount(adapter_with_signer):
    ok, msg = await adapter_with_signer.lend(amount=-5)
    assert ok is False
    assert "positive" in str(msg).lower()


# ---------------------------------------------------------------------------
# unlend guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unlend_requires_wallet(adapter_no_wallet):
    ok, msg = await adapter_no_wallet.unlend(amount=1)
    assert ok is False
    assert "wallet address" in str(msg).lower()


@pytest.mark.asyncio
async def test_unlend_requires_signing_callback(adapter):
    ok, msg = await adapter.unlend(amount=1)
    assert ok is False
    assert "signing callback" in str(msg).lower()


@pytest.mark.asyncio
async def test_unlend_rejects_zero_amount(adapter_with_signer):
    ok, msg = await adapter_with_signer.unlend(amount=0)
    assert ok is False
    assert "positive" in str(msg).lower()


# ---------------------------------------------------------------------------
# unlend redeem_full with zero shares
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unlend_redeem_full_zero_shares(adapter_with_signer):
    mock_contract = _make_erc4626_contract(max_redeem=0, balance_of=0)
    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(return_value=mock_contract)

    @asynccontextmanager
    async def mock_web3_ctx(_chain_id):
        yield mock_web3

    with patch(
        "wayfinder_paths.adapters.avantis_adapter.adapter.web3_from_chain_id",
        mock_web3_ctx,
    ):
        ok, msg = await adapter_with_signer.unlend(amount=0, redeem_full=True)

    assert ok is True
    assert msg == "no shares to redeem"


# ---------------------------------------------------------------------------
# get_pos
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_pos_requires_wallet(adapter_no_wallet):
    ok, msg = await adapter_no_wallet.get_pos()
    assert ok is False
    assert "wallet address" in str(msg).lower()


@pytest.mark.asyncio
async def test_get_pos_happy_path(adapter):
    mock_contract = _make_erc4626_contract()
    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(return_value=mock_contract)

    @asynccontextmanager
    async def mock_web3_ctx(_chain_id):
        yield mock_web3

    with patch(
        "wayfinder_paths.adapters.avantis_adapter.adapter.web3_from_chain_id",
        mock_web3_ctx,
    ):
        ok, data = await adapter.get_pos()

    assert ok is True
    assert isinstance(data, dict)
    assert data["mtoken_balance"] == 500_000
    assert data["underlying_balance"] == 520_000
    assert data["borrow_balance"] == 0
    assert data["decimals"] == 6
    assert data["max_redeem"] == 500_000
    assert data["max_withdraw"] == 520_000
    assert data["total_assets"] == 1_000_000_000
    assert data["total_supply"] == 950_000_000


# ---------------------------------------------------------------------------
# get_all_markets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_all_markets_happy_path(adapter):
    mock_contract = _make_erc4626_contract()
    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(return_value=mock_contract)

    @asynccontextmanager
    async def mock_web3_ctx(_chain_id):
        yield mock_web3

    with patch(
        "wayfinder_paths.adapters.avantis_adapter.adapter.web3_from_chain_id",
        mock_web3_ctx,
    ):
        ok, markets = await adapter.get_all_markets()

    assert ok is True
    assert isinstance(markets, list)
    assert len(markets) == 1
    market = markets[0]
    assert market["chain_id"] == 8453
    assert market["vault"] == AVANTIS_AVUSDC
    assert market["symbol"] == "avUSDC"
    assert market["name"] == "Avantis USDC Vault"
    assert market["decimals"] == 6
    assert market["total_assets"] == 1_000_000_000
    assert market["total_supply"] == 950_000_000
    assert market["tvl"] == 1_000_000_000


# ---------------------------------------------------------------------------
# get_full_user_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_full_user_state_with_position(adapter):
    pos_data = {
        "mtoken_balance": 500_000,
        "underlying_balance": 520_000,
        "share_price": 1_040_000,
        "max_redeem": 500_000,
        "max_withdraw": 520_000,
    }
    with patch.object(
        adapter, "get_pos", new_callable=AsyncMock, return_value=(True, pos_data)
    ):
        ok, state = await adapter.get_full_user_state(account=FAKE_WALLET)

    assert ok is True
    assert isinstance(state, dict)
    assert state["protocol"] == "avantis"
    assert state["chainId"] == 8453
    assert len(state["positions"]) == 1
    pos = state["positions"][0]
    assert pos["mTokenBalance"] == 500_000
    assert pos["suppliedUnderlying"] == 520_000
    assert pos["borrowedUnderlying"] == 0


@pytest.mark.asyncio
async def test_get_full_user_state_zero_positions_excluded(adapter):
    pos_data = {
        "mtoken_balance": 0,
        "underlying_balance": 0,
        "share_price": 0,
        "max_redeem": 0,
        "max_withdraw": 0,
    }
    with patch.object(
        adapter, "get_pos", new_callable=AsyncMock, return_value=(True, pos_data)
    ):
        ok, state = await adapter.get_full_user_state(
            account=FAKE_WALLET, include_zero_positions=False
        )

    assert ok is True
    assert state["positions"] == []


@pytest.mark.asyncio
async def test_get_full_user_state_zero_positions_included(adapter):
    pos_data = {
        "mtoken_balance": 0,
        "underlying_balance": 0,
        "share_price": 0,
        "max_redeem": 0,
        "max_withdraw": 0,
    }
    with patch.object(
        adapter, "get_pos", new_callable=AsyncMock, return_value=(True, pos_data)
    ):
        ok, state = await adapter.get_full_user_state(
            account=FAKE_WALLET, include_zero_positions=True
        )

    assert ok is True
    assert len(state["positions"]) == 1


@pytest.mark.asyncio
async def test_get_full_user_state_propagates_get_pos_failure(adapter):
    with patch.object(
        adapter,
        "get_pos",
        new_callable=AsyncMock,
        return_value=(False, "rpc error"),
    ):
        ok, msg = await adapter.get_full_user_state(account=FAKE_WALLET)

    assert ok is False
    assert "rpc error" in str(msg)
