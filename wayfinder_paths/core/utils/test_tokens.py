from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wayfinder_paths.core.utils.tokens import wait_for_allowance_visible


class _FakeEth:
    def __init__(self, blocks: list[int]):
        self._blocks = blocks
        self._idx = 0

    @property
    def block_number(self):
        async def _value() -> int:
            value = self._blocks[min(self._idx, len(self._blocks) - 1)]
            self._idx += 1
            return value

        return _value()


def _fake_web3(blocks: list[int]):
    w3 = MagicMock()
    w3.eth = _FakeEth(blocks)
    return w3


def _web3s_context(web3s: list):
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=web3s)
    context.__aexit__ = AsyncMock(return_value=None)
    return context


@pytest.mark.asyncio
async def test_wait_for_allowance_visible_already_sufficient():
    with (
        patch(
            "wayfinder_paths.core.utils.tokens.web3s_from_chain_id",
            return_value=_web3s_context([_fake_web3([100])]),
        ),
        patch(
            "wayfinder_paths.core.utils.tokens.get_token_allowance",
            new=AsyncMock(return_value=200),
        ) as allowance_mock,
    ):
        out = await wait_for_allowance_visible(
            token_address="0x1111111111111111111111111111111111111111",
            chain_id=8453,
            owner="0x000000000000000000000000000000000000dEaD",
            spender="0x3333333333333333333333333333333333333333",
            amount=100,
            max_attempts=1,
        )

    assert out["status"] == "already_sufficient"
    assert out["observed_allowance_raw"] == 200
    allowance_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_wait_for_allowance_visible_after_approval_block_reaches_rpc():
    with (
        patch(
            "wayfinder_paths.core.utils.tokens.wait_for_transaction_receipt",
            new=AsyncMock(return_value={"blockNumber": 11}),
        ),
        patch(
            "wayfinder_paths.core.utils.tokens.web3s_from_chain_id",
            return_value=_web3s_context([_fake_web3([10, 11])]),
        ),
        patch(
            "wayfinder_paths.core.utils.tokens.get_token_allowance",
            new=AsyncMock(return_value=200),
        ),
        patch("wayfinder_paths.core.utils.tokens.asyncio.sleep", new=AsyncMock()),
    ):
        out = await wait_for_allowance_visible(
            token_address="0x1111111111111111111111111111111111111111",
            chain_id=8453,
            owner="0x000000000000000000000000000000000000dEaD",
            spender="0x3333333333333333333333333333333333333333",
            amount=100,
            approval_tx_hash="0xapprove",
            max_attempts=2,
        )

    assert out["status"] == "approval_confirmed_visible"
    assert out["approval_block"] == 11
    assert out["attempts"] == 2


@pytest.mark.asyncio
async def test_wait_for_allowance_visible_times_out_when_allowance_insufficient():
    with (
        patch(
            "wayfinder_paths.core.utils.tokens.web3s_from_chain_id",
            return_value=_web3s_context([_fake_web3([11, 11])]),
        ),
        patch(
            "wayfinder_paths.core.utils.tokens.get_token_allowance",
            new=AsyncMock(return_value=50),
        ),
        patch("wayfinder_paths.core.utils.tokens.asyncio.sleep", new=AsyncMock()),
    ):
        out = await wait_for_allowance_visible(
            token_address="0x1111111111111111111111111111111111111111",
            chain_id=8453,
            owner="0x000000000000000000000000000000000000dEaD",
            spender="0x3333333333333333333333333333333333333333",
            amount=100,
            approval_block=11,
            max_attempts=2,
        )

    assert out["status"] == "approval_not_visible_yet"
    assert out["observed_allowance_raw"] == 50


@pytest.mark.asyncio
async def test_wait_for_allowance_visible_returns_read_failure():
    with (
        patch(
            "wayfinder_paths.core.utils.tokens.web3s_from_chain_id",
            return_value=_web3s_context([_fake_web3([11])]),
        ),
        patch(
            "wayfinder_paths.core.utils.tokens.get_token_allowance",
            new=AsyncMock(side_effect=ValueError("bad allowance")),
        ),
    ):
        out = await wait_for_allowance_visible(
            token_address="0x1111111111111111111111111111111111111111",
            chain_id=8453,
            owner="0x000000000000000000000000000000000000dEaD",
            spender="0x3333333333333333333333333333333333333333",
            amount=100,
            max_attempts=1,
        )

    assert out["status"] == "allowance_read_failed"
    assert "bad allowance" in out["errors"][0]
