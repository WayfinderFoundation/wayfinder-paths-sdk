from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wayfinder_paths.adapters.etherfi_adapter.adapter import (
    EtherfiAdapter,
    _as_hex_bytes32,
    _normalize_permit_tuple,
)
from wayfinder_paths.core.constants.chains import (
    CHAIN_ID_ARBITRUM,
    CHAIN_ID_BASE,
    CHAIN_ID_ETHEREUM,
)
from wayfinder_paths.core.constants.etherfi_contracts import (
    ETHERFI_BY_CHAIN,
    weeth_token_by_chain_id,
)
from wayfinder_paths.testing import fake_signing

WALLET = "0x1234567890123456789012345678901234567890"
ENTRY = ETHERFI_BY_CHAIN[CHAIN_ID_ETHEREUM]

PATCH_PREFIX = "wayfinder_paths.adapters.etherfi_adapter.adapter"


@pytest.fixture
def adapter():
    return EtherfiAdapter(
        config={},
        signing=fake_signing(sign=AsyncMock(return_value=b"\x00" * 32)),
        wallet_address=WALLET,
    )


@pytest.fixture
def read_only_adapter():
    return EtherfiAdapter(config={})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_web3_ctx():
    mock_web3 = MagicMock()

    @asynccontextmanager
    async def ctx(_chain_id):
        yield mock_web3

    return ctx, mock_web3


# ---------------------------------------------------------------------------
# Construction & static helpers
# ---------------------------------------------------------------------------


def test_adapter_type():
    assert EtherfiAdapter.adapter_type == "ETHERFI"


def test_wallet_address_checksummed():
    adapter = EtherfiAdapter(
        config={}, wallet_address="0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
    )
    assert adapter.wallet_address == "0xABcdEFABcdEFabcdEfAbCdefabcdeFABcDEFabCD"


def test_no_wallet_is_none():
    adapter = EtherfiAdapter(config={})
    assert adapter.wallet_address is None


def test_weeth_token_by_chain_id_mainnet():
    assert (
        weeth_token_by_chain_id(CHAIN_ID_ETHEREUM)
        == "0xCd5fE23C85820F7B72D0926FC9b05b43E359b7ee"
    )


def test_weeth_token_by_chain_id_l2s():
    assert weeth_token_by_chain_id(CHAIN_ID_BASE) is not None
    assert weeth_token_by_chain_id(CHAIN_ID_ARBITRUM) is not None


def test_weeth_token_by_chain_id_unsupported_raises():
    with pytest.raises(ValueError, match="Unsupported"):
        weeth_token_by_chain_id(999999)


def test_entry_unsupported_chain():
    adapter = EtherfiAdapter(config={})
    with pytest.raises(ValueError, match="Unsupported"):
        adapter._entry(999999)


def test_entry_mainnet():
    adapter = EtherfiAdapter(config={})
    entry = adapter._entry(CHAIN_ID_ETHEREUM)
    assert "liquidity_pool" in entry
    assert "eeth" in entry
    assert "weeth" in entry
    assert "withdraw_request_nft" in entry


# ---------------------------------------------------------------------------
# _as_hex_bytes32
# ---------------------------------------------------------------------------


def test_as_hex_bytes32_int():
    result = _as_hex_bytes32(42)
    assert len(result) == 32
    assert int.from_bytes(result, "big") == 42


def test_as_hex_bytes32_hex_string():
    result = _as_hex_bytes32("0x" + "ab" * 32)
    assert len(result) == 32


def test_as_hex_bytes32_bytes():
    result = _as_hex_bytes32(b"\x01")
    assert len(result) == 32
    assert result[-1] == 1


def test_as_hex_bytes32_too_long():
    with pytest.raises(ValueError, match="too long"):
        _as_hex_bytes32(b"\x00" * 33)


def test_as_hex_bytes32_negative_int():
    with pytest.raises(ValueError, match="non-negative"):
        _as_hex_bytes32(-1)


# ---------------------------------------------------------------------------
# _normalize_permit_tuple
# ---------------------------------------------------------------------------


def test_normalize_permit_from_dict():
    permit = {
        "value": 100,
        "deadline": 9999,
        "v": 27,
        "r": "0x" + "aa" * 32,
        "s": "0x" + "bb" * 32,
    }
    value, deadline, v, r, s = _normalize_permit_tuple(permit)
    assert value == 100
    assert deadline == 9999
    assert v == 27
    assert len(r) == 32
    assert len(s) == 32


def test_normalize_permit_from_tuple():
    r_bytes = b"\xaa" * 32
    s_bytes = b"\xbb" * 32
    result = _normalize_permit_tuple((100, 9999, 27, r_bytes, s_bytes))
    assert result[0] == 100
    assert result[1] == 9999
    assert result[2] == 27


def test_normalize_permit_invalid_type():
    with pytest.raises(ValueError, match="permit must be"):
        _normalize_permit_tuple("not a permit")


def test_normalize_permit_wrong_length_tuple():
    with pytest.raises(ValueError, match="permit must be"):
        _normalize_permit_tuple((1, 2, 3))


# ---------------------------------------------------------------------------
# Input validation (all write methods should reject bad amounts)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stake_eth_rejects_zero(adapter):
    ok, msg = await adapter.stake_eth(amount_wei=0)
    assert ok is False
    assert "positive" in msg.lower()


@pytest.mark.asyncio
async def test_stake_eth_rejects_negative(adapter):
    ok, msg = await adapter.stake_eth(amount_wei=-1)
    assert ok is False
    assert "positive" in msg.lower()


@pytest.mark.asyncio
async def test_wrap_eeth_rejects_zero(adapter):
    ok, msg = await adapter.wrap_eeth(amount_eeth=0)
    assert ok is False
    assert "positive" in msg.lower()


@pytest.mark.asyncio
async def test_unwrap_weeth_rejects_zero(adapter):
    ok, msg = await adapter.unwrap_weeth(amount_weeth=0)
    assert ok is False
    assert "positive" in msg.lower()


@pytest.mark.asyncio
async def test_wrap_eeth_with_permit_rejects_zero(adapter):
    ok, msg = await adapter.wrap_eeth_with_permit(
        amount_eeth=0,
        permit={
            "value": 0,
            "deadline": 0,
            "v": 27,
            "r": b"\x00" * 32,
            "s": b"\x00" * 32,
        },
    )
    assert ok is False
    assert "positive" in msg.lower()


@pytest.mark.asyncio
async def test_request_withdraw_rejects_zero(adapter):
    ok, msg = await adapter.request_withdraw(amount_eeth=0)
    assert ok is False
    assert "positive" in msg.lower()


@pytest.mark.asyncio
async def test_request_withdraw_with_permit_rejects_zero(adapter):
    ok, msg = await adapter.request_withdraw_with_permit(
        amount_eeth=0,
        permit={
            "value": 0,
            "deadline": 0,
            "v": 27,
            "r": b"\x00" * 32,
            "s": b"\x00" * 32,
        },
    )
    assert ok is False
    assert "positive" in msg.lower()


@pytest.mark.asyncio
async def test_claim_withdraw_rejects_negative(adapter):
    ok, msg = await adapter.claim_withdraw(token_id=-1)
    assert ok is False
    assert "non-negative" in msg.lower()


@pytest.mark.asyncio
async def test_claim_withdraw_allows_zero_token_id(adapter):
    """Token ID 0 is valid (the first minted NFT)."""
    with (
        patch(f"{PATCH_PREFIX}.encode_call", new_callable=AsyncMock) as mock_encode,
        patch(f"{PATCH_PREFIX}.send_transaction", new_callable=AsyncMock) as mock_send,
    ):
        mock_encode.return_value = {"to": ENTRY["withdraw_request_nft"], "data": "0x"}
        mock_send.return_value = "0x" + "ab" * 32
        ok, tx = await adapter.claim_withdraw(token_id=0)
    assert ok is True


# ---------------------------------------------------------------------------
# require_wallet guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stake_requires_wallet(read_only_adapter):
    ok, msg = await read_only_adapter.stake_eth(amount_wei=10**18)
    assert ok is False
    assert "wallet" in msg.lower()


@pytest.mark.asyncio
async def test_wrap_requires_wallet(read_only_adapter):
    ok, msg = await read_only_adapter.wrap_eeth(amount_eeth=10**18)
    assert ok is False
    assert "wallet" in msg.lower()


@pytest.mark.asyncio
async def test_unwrap_requires_wallet(read_only_adapter):
    ok, msg = await read_only_adapter.unwrap_weeth(amount_weeth=10**18)
    assert ok is False
    assert "wallet" in msg.lower()


@pytest.mark.asyncio
async def test_request_withdraw_requires_wallet(read_only_adapter):
    ok, msg = await read_only_adapter.request_withdraw(amount_eeth=10**18)
    assert ok is False
    assert "wallet" in msg.lower()


@pytest.mark.asyncio
async def test_claim_requires_wallet(read_only_adapter):
    ok, msg = await read_only_adapter.claim_withdraw(token_id=1)
    assert ok is False
    assert "wallet" in msg.lower()


# ---------------------------------------------------------------------------
# get_pos: read-only, no wallet required
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_pos_requires_account_when_no_wallet():
    adapter = EtherfiAdapter(config={})
    ok, msg = await adapter.get_pos()
    assert ok is False
    assert "account" in msg.lower()


@pytest.mark.asyncio
async def test_get_pos_unsupported_chain():
    adapter = EtherfiAdapter(config={})
    ok, err = await adapter.get_pos(
        account="0x0000000000000000000000000000000000000000",
        chain_id=0,
    )
    assert ok is False
    assert isinstance(err, str) and "Unsupported" in err


@pytest.mark.asyncio
async def test_get_pos_returns_expected_structure(adapter):
    ctx_factory, mock_web3 = _mock_web3_ctx()

    eeth_balance = 10**18
    weeth_balance = 5 * 10**17
    weeth_rate = 1_050_000_000_000_000_000  # 1.05 ETH per weETH
    eeth_shares = 9 * 10**17
    total_pooled = 100_000 * 10**18
    weeth_eeth_equiv = 525_000_000_000_000_000  # 0.525 ETH

    mock_multicall = AsyncMock(
        return_value=[
            eeth_balance,
            weeth_balance,
            weeth_rate,
            eeth_shares,
            total_pooled,
        ]
    )

    mock_weeth_contract = MagicMock()
    mock_weeth_contract.functions.getEETHByWeETH = MagicMock(
        return_value=MagicMock(call=AsyncMock(return_value=weeth_eeth_equiv))
    )

    def route_contract(address, abi):
        if address == ENTRY["weeth"]:
            return mock_weeth_contract
        return MagicMock()

    mock_web3.eth.contract = MagicMock(side_effect=route_contract)

    with (
        patch(f"{PATCH_PREFIX}.web3_from_chain_id", ctx_factory),
        patch(f"{PATCH_PREFIX}.read_only_calls_multicall_or_gather", mock_multicall),
    ):
        ok, pos = await adapter.get_pos(chain_id=CHAIN_ID_ETHEREUM)

    assert ok is True
    assert pos["protocol"] == "etherfi"
    assert pos["chain_id"] == CHAIN_ID_ETHEREUM
    assert pos["account"] == adapter.wallet_address

    assert pos["eeth"]["balance_raw"] == eeth_balance
    assert pos["eeth"]["shares_raw"] == eeth_shares
    assert pos["weeth"]["balance_raw"] == weeth_balance
    assert pos["weeth"]["eeth_equivalent_raw"] == weeth_eeth_equiv
    assert pos["weeth"]["rate"] == weeth_rate
    assert pos["liquidity_pool"]["total_pooled_ether"] == total_pooled

    assert "liquidity_pool" in pos["contracts"]
    assert "eeth" in pos["contracts"]
    assert "weeth" in pos["contracts"]
    assert "withdraw_request_nft" in pos["contracts"]


@pytest.mark.asyncio
async def test_get_pos_without_shares(adapter):
    ctx_factory, mock_web3 = _mock_web3_ctx()

    # With include_shares=False, multicall returns 4 values (no shares).
    mock_multicall = AsyncMock(return_value=[10**18, 0, 10**18, 200_000 * 10**18])

    mock_web3.eth.contract = MagicMock(return_value=MagicMock())

    with (
        patch(f"{PATCH_PREFIX}.web3_from_chain_id", ctx_factory),
        patch(f"{PATCH_PREFIX}.read_only_calls_multicall_or_gather", mock_multicall),
    ):
        ok, pos = await adapter.get_pos(include_shares=False)

    assert ok is True
    assert "shares_raw" not in pos["eeth"]
    assert "balance_raw" in pos["eeth"]


@pytest.mark.asyncio
async def test_get_pos_zero_weeth_skips_equiv_call(adapter):
    """When weeth balance is 0, getEETHByWeETH should NOT be called."""
    ctx_factory, mock_web3 = _mock_web3_ctx()

    mock_multicall = AsyncMock(
        return_value=[10**18, 0, 10**18, 9 * 10**17, 100 * 10**18]
    )

    mock_weeth_contract = MagicMock()
    mock_get_eeth = MagicMock(return_value=MagicMock(call=AsyncMock()))
    mock_weeth_contract.functions.getEETHByWeETH = mock_get_eeth

    def route_contract(address, abi):
        if address == ENTRY["weeth"]:
            return mock_weeth_contract
        return MagicMock()

    mock_web3.eth.contract = MagicMock(side_effect=route_contract)

    with (
        patch(f"{PATCH_PREFIX}.web3_from_chain_id", ctx_factory),
        patch(f"{PATCH_PREFIX}.read_only_calls_multicall_or_gather", mock_multicall),
    ):
        ok, pos = await adapter.get_pos()

    assert ok is True
    assert pos["weeth"]["eeth_equivalent_raw"] == 0
    mock_get_eeth.assert_not_called()


# ---------------------------------------------------------------------------
# stake_eth: paused check, referral routing, encode/send args
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stake_returns_false_when_paused(adapter):
    with patch.object(adapter, "_is_paused", new_callable=AsyncMock, return_value=True):
        ok, msg = await adapter.stake_eth(amount_wei=10**18)
    assert ok is False
    assert "paused" in msg.lower()


@pytest.mark.asyncio
async def test_stake_skips_pause_check_when_disabled(adapter):
    with (
        patch.object(adapter, "_is_paused", new_callable=AsyncMock) as mock_paused,
        patch(f"{PATCH_PREFIX}.encode_call", new_callable=AsyncMock) as mock_encode,
        patch(f"{PATCH_PREFIX}.send_transaction", new_callable=AsyncMock) as mock_send,
    ):
        mock_encode.return_value = {"to": ENTRY["liquidity_pool"], "data": "0x"}
        mock_send.return_value = "0xhash"
        ok, _ = await adapter.stake_eth(amount_wei=10**18, check_paused=False)

    mock_paused.assert_not_called()
    assert ok is True


@pytest.mark.asyncio
async def test_stake_without_referral_uses_deposit_no_args(adapter):
    with (
        patch.object(adapter, "_is_paused", new_callable=AsyncMock, return_value=False),
        patch(f"{PATCH_PREFIX}.encode_call", new_callable=AsyncMock) as mock_encode,
        patch(f"{PATCH_PREFIX}.send_transaction", new_callable=AsyncMock) as mock_send,
    ):
        mock_encode.return_value = {"to": ENTRY["liquidity_pool"], "data": "0x"}
        mock_send.return_value = "0xhash"
        ok, tx = await adapter.stake_eth(amount_wei=10**18)

    assert ok is True
    call_kwargs = mock_encode.call_args.kwargs
    assert call_kwargs["fn_name"] == "deposit()"
    assert call_kwargs["args"] == []
    assert call_kwargs["value"] == 10**18


@pytest.mark.asyncio
async def test_stake_with_referral_uses_deposit_address(adapter):
    referral = "0xDeaDbeefdEAdbeefdEadbEEFdeadbeEFdEaDbeeF"
    with (
        patch.object(adapter, "_is_paused", new_callable=AsyncMock, return_value=False),
        patch(f"{PATCH_PREFIX}.encode_call", new_callable=AsyncMock) as mock_encode,
        patch(f"{PATCH_PREFIX}.send_transaction", new_callable=AsyncMock) as mock_send,
    ):
        mock_encode.return_value = {"to": ENTRY["liquidity_pool"], "data": "0x"}
        mock_send.return_value = "0xhash"
        ok, tx = await adapter.stake_eth(amount_wei=10**18, referral=referral)

    assert ok is True
    call_kwargs = mock_encode.call_args.kwargs
    assert call_kwargs["fn_name"] == "deposit(address)"
    assert len(call_kwargs["args"]) == 1


# ---------------------------------------------------------------------------
# wrap_eeth: allowance then wrap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrap_eeth_calls_ensure_allowance_then_wrap(adapter):
    with (
        patch(f"{PATCH_PREFIX}.ensure_allowance", new_callable=AsyncMock) as mock_allow,
        patch(f"{PATCH_PREFIX}.encode_call", new_callable=AsyncMock) as mock_encode,
        patch(f"{PATCH_PREFIX}.send_transaction", new_callable=AsyncMock) as mock_send,
    ):
        mock_allow.return_value = (True, "0xapproval")
        mock_encode.return_value = {"to": ENTRY["weeth"], "data": "0x"}
        mock_send.return_value = "0xwrap_hash"

        ok, tx = await adapter.wrap_eeth(amount_eeth=10**18)

    assert ok is True
    assert tx == "0xwrap_hash"

    # Allowance should be for eETH -> weETH spender.
    allow_kwargs = mock_allow.call_args.kwargs
    assert allow_kwargs["token_address"] == ENTRY["eeth"]
    assert allow_kwargs["spender"] == ENTRY["weeth"]
    assert allow_kwargs["amount"] == 10**18

    # Wrap call should target weETH contract.
    encode_kwargs = mock_encode.call_args.kwargs
    assert encode_kwargs["target"] == ENTRY["weeth"]
    assert encode_kwargs["fn_name"] == "wrap"
    assert encode_kwargs["args"] == [10**18]


@pytest.mark.asyncio
async def test_wrap_eeth_returns_false_when_allowance_fails(adapter):
    with patch(
        f"{PATCH_PREFIX}.ensure_allowance", new_callable=AsyncMock
    ) as mock_allow:
        mock_allow.return_value = (False, "approval rejected")
        ok, msg = await adapter.wrap_eeth(amount_eeth=10**18)

    assert ok is False
    assert "rejected" in msg.lower()


# ---------------------------------------------------------------------------
# unwrap_weeth: direct call, no allowance needed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unwrap_weeth_targets_weeth_contract(adapter):
    with (
        patch(f"{PATCH_PREFIX}.encode_call", new_callable=AsyncMock) as mock_encode,
        patch(f"{PATCH_PREFIX}.send_transaction", new_callable=AsyncMock) as mock_send,
    ):
        mock_encode.return_value = {"to": ENTRY["weeth"], "data": "0x"}
        mock_send.return_value = "0xunwrap_hash"
        ok, tx = await adapter.unwrap_weeth(amount_weeth=5 * 10**17)

    assert ok is True
    encode_kwargs = mock_encode.call_args.kwargs
    assert encode_kwargs["target"] == ENTRY["weeth"]
    assert encode_kwargs["fn_name"] == "unwrap"
    assert encode_kwargs["args"] == [5 * 10**17]


# ---------------------------------------------------------------------------
# request_withdraw: allowance, encode to liquidity_pool, receipt parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_withdraw_calls_liquidity_pool(adapter):
    with (
        patch(f"{PATCH_PREFIX}.ensure_allowance", new_callable=AsyncMock) as mock_allow,
        patch(f"{PATCH_PREFIX}.encode_call", new_callable=AsyncMock) as mock_encode,
        patch(f"{PATCH_PREFIX}.send_transaction", new_callable=AsyncMock) as mock_send,
        patch.object(
            adapter, "_parse_request_id_from_receipt", new_callable=AsyncMock
        ) as mock_parse,
    ):
        mock_allow.return_value = (True, "0xapproval")
        mock_encode.return_value = {"to": ENTRY["liquidity_pool"], "data": "0x"}
        mock_send.return_value = "0xwithdraw_hash"
        mock_parse.return_value = 42

        ok, res = await adapter.request_withdraw(amount_eeth=10**17)

    assert ok is True
    assert res["tx"] == "0xwithdraw_hash"
    assert res["request_id"] == 42
    assert res["amount_eeth"] == 10**17
    # Recipient defaults to wallet_address.
    assert res["recipient"] == adapter.wallet_address

    # Allowance should be eETH -> liquidity_pool.
    allow_kwargs = mock_allow.call_args.kwargs
    assert allow_kwargs["spender"] == ENTRY["liquidity_pool"]

    encode_kwargs = mock_encode.call_args.kwargs
    assert encode_kwargs["target"] == ENTRY["liquidity_pool"]
    assert encode_kwargs["fn_name"] == "requestWithdraw"


@pytest.mark.asyncio
async def test_request_withdraw_custom_recipient(adapter):
    # Lowercase input — should be checksummed in the response.
    custom_recipient = "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    with (
        patch(f"{PATCH_PREFIX}.ensure_allowance", new_callable=AsyncMock) as mock_allow,
        patch(f"{PATCH_PREFIX}.encode_call", new_callable=AsyncMock) as mock_encode,
        patch(f"{PATCH_PREFIX}.send_transaction", new_callable=AsyncMock) as mock_send,
        patch.object(adapter, "_parse_request_id_from_receipt", new_callable=AsyncMock),
    ):
        mock_allow.return_value = (True, "0x")
        mock_encode.return_value = {"to": ENTRY["liquidity_pool"], "data": "0x"}
        mock_send.return_value = "0xhash"

        ok, res = await adapter.request_withdraw(
            amount_eeth=10**17, recipient=custom_recipient
        )

    assert ok is True
    # Should be checksummed (not the raw lowercase input).
    assert res["recipient"] != custom_recipient
    assert res["recipient"].lower() == custom_recipient.lower()


@pytest.mark.asyncio
async def test_request_withdraw_skip_request_id(adapter):
    with (
        patch(f"{PATCH_PREFIX}.ensure_allowance", new_callable=AsyncMock) as mock_allow,
        patch(f"{PATCH_PREFIX}.encode_call", new_callable=AsyncMock) as mock_encode,
        patch(f"{PATCH_PREFIX}.send_transaction", new_callable=AsyncMock) as mock_send,
        patch.object(
            adapter, "_parse_request_id_from_receipt", new_callable=AsyncMock
        ) as mock_parse,
    ):
        mock_allow.return_value = (True, "0x")
        mock_encode.return_value = {"to": ENTRY["liquidity_pool"], "data": "0x"}
        mock_send.return_value = "0xhash"

        ok, res = await adapter.request_withdraw(
            amount_eeth=10**17, include_request_id=False
        )

    assert ok is True
    assert res["request_id"] is None
    mock_parse.assert_not_called()


# ---------------------------------------------------------------------------
# is_withdraw_finalized / get_claimable_withdraw
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_withdraw_finalized_returns_bool():
    ctx_factory, mock_web3 = _mock_web3_ctx()
    mock_nft = MagicMock()
    mock_nft.functions.isFinalized = MagicMock(
        return_value=MagicMock(call=AsyncMock(return_value=True))
    )
    mock_web3.eth.contract = MagicMock(return_value=mock_nft)

    adapter = EtherfiAdapter(config={})
    with patch(f"{PATCH_PREFIX}.web3_from_chain_id", ctx_factory):
        ok, finalized = await adapter.is_withdraw_finalized(token_id=5)

    assert ok is True
    assert finalized is True


@pytest.mark.asyncio
async def test_get_claimable_returns_zero_when_not_finalized():
    ctx_factory, mock_web3 = _mock_web3_ctx()
    mock_nft = MagicMock()
    mock_nft.functions.isFinalized = MagicMock(
        return_value=MagicMock(call=AsyncMock(return_value=False))
    )
    mock_nft.functions.getClaimableAmount = MagicMock(
        return_value=MagicMock(call=AsyncMock(return_value=999))
    )
    mock_web3.eth.contract = MagicMock(return_value=mock_nft)

    adapter = EtherfiAdapter(config={})
    with patch(f"{PATCH_PREFIX}.web3_from_chain_id", ctx_factory):
        ok, amt = await adapter.get_claimable_withdraw(token_id=5)

    assert ok is True
    assert amt == 0
    # getClaimableAmount should NOT be called when not finalized.
    mock_nft.functions.getClaimableAmount.assert_not_called()


@pytest.mark.asyncio
async def test_get_claimable_returns_amount_when_finalized():
    ctx_factory, mock_web3 = _mock_web3_ctx()
    mock_nft = MagicMock()
    mock_nft.functions.isFinalized = MagicMock(
        return_value=MagicMock(call=AsyncMock(return_value=True))
    )
    mock_nft.functions.getClaimableAmount = MagicMock(
        return_value=MagicMock(call=AsyncMock(return_value=10**18))
    )
    mock_web3.eth.contract = MagicMock(return_value=mock_nft)

    adapter = EtherfiAdapter(config={})
    with patch(f"{PATCH_PREFIX}.web3_from_chain_id", ctx_factory):
        ok, amt = await adapter.get_claimable_withdraw(token_id=5)

    assert ok is True
    assert amt == 10**18


# ---------------------------------------------------------------------------
# _is_paused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_paused_returns_contract_value(adapter):
    ctx_factory, mock_web3 = _mock_web3_ctx()
    mock_lp = MagicMock()
    mock_lp.functions.paused = MagicMock(
        return_value=MagicMock(call=AsyncMock(return_value=False))
    )
    mock_web3.eth.contract = MagicMock(return_value=mock_lp)

    with patch(f"{PATCH_PREFIX}.web3_from_chain_id", ctx_factory):
        result = await adapter._is_paused(chain_id=CHAIN_ID_ETHEREUM)

    assert result is False
