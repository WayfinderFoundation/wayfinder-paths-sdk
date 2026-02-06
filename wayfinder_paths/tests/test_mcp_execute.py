from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.mcp.tools.execute import _select_token_chain, execute


def test_select_token_chain_native_gas_token_null_address():
    """Native gas tokens (e.g. ETH) may have address=null from the API.

    _select_token_chain should normalize null -> ZERO_ADDRESS when the
    metadata looks like a native gas token, mirroring the send path.
    """
    meta = {
        "asset_id": "ethereum",
        "symbol": "ETH",
        "decimals": 18,
        "address": None,
        "chain_id": 1,
    }
    chain_id, addr = _select_token_chain(meta, query="ethereum-ethereum")
    assert addr == ZERO_ADDRESS
    assert chain_id is not None


def test_select_token_chain_native_gas_token_missing_address():
    """Same as above but address key is missing entirely."""
    meta = {
        "asset_id": "ethereum",
        "symbol": "ETH",
        "decimals": 18,
        "chain_id": 8453,
    }
    chain_id, addr = _select_token_chain(meta, query="ethereum-base")
    assert addr == ZERO_ADDRESS
    assert chain_id == 8453


def test_select_token_chain_erc20_null_address_not_normalized():
    """Non-native tokens with null address should stay None (real error)."""
    meta = {
        "asset_id": "usd-coin",
        "symbol": "USDC",
        "decimals": 6,
        "address": None,
        "chain_id": 1,
    }
    _chain_id, addr = _select_token_chain(meta, query="usd-coin-ethereum")
    assert addr is None


def test_select_token_chain_normal_erc20_unchanged():
    """Normal ERC20 tokens should pass through unchanged."""
    meta = {
        "asset_id": "usd-coin",
        "symbol": "USDC",
        "decimals": 6,
        "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "chain_id": 1,
    }
    _chain_id, addr = _select_token_chain(meta, query="usd-coin-ethereum")
    assert addr == "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


@pytest.mark.asyncio
async def test_execute_validation_error_is_structured():
    # swap requires from_token and to_token
    out = await execute(kind="swap", wallet_label="main", amount="1")
    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_request"
    assert isinstance(out["error"]["details"], list)


@pytest.mark.asyncio
async def test_execute_swap(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WAYFINDER_MCP_STATE_PATH", str(tmp_path / "mcp.sqlite3"))
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))

    wallet = {
        "address": "0x000000000000000000000000000000000000dEaD",
        "private_key_hex": "0x" + "11" * 32,
    }

    from_meta = {
        "token_id": "from",
        "symbol": "FROM",
        "decimals": 6,
        "chain_id": 42161,
        "address": "0x1111111111111111111111111111111111111111",
    }
    to_meta = {
        "token_id": "to",
        "symbol": "TO",
        "decimals": 6,
        "chain_id": 42161,
        "address": "0x2222222222222222222222222222222222222222",
    }

    async def fake_resolve(query: str):
        if query == "from":
            return query, from_meta
        if query == "to":
            return query, to_meta
        raise AssertionError(f"unexpected token query: {query}")

    fake_brap = AsyncMock()
    fake_brap.get_quote = AsyncMock(
        return_value={
            "quotes": [
                {"provider": "brap_best"},
                {"provider": "brap_alt"},
            ],
            "best_quote": {
                "provider": "brap_best",
                "input_amount": "1000000",
                "calldata": {
                    "to": "0x" + "33" * 20,
                    "data": "0xdeadbeef",
                    "value": "0",
                },
            },
        }
    )

    async def fake_ensure_allowance(**_kwargs):  # noqa: ANN003
        return True, "0xapprove"

    with (
        patch(
            "wayfinder_paths.mcp.tools.execute.find_wallet_by_label",
            return_value=wallet,
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute._resolve_token_meta",
            side_effect=fake_resolve,
        ),
        patch("wayfinder_paths.mcp.tools.execute.BRAP_CLIENT", fake_brap),
        patch(
            "wayfinder_paths.mcp.tools.execute.ensure_allowance",
            new=AsyncMock(side_effect=fake_ensure_allowance),
        ),
    ):
        out1 = await execute(
            kind="swap",
            wallet_label="main",
            from_token="from",
            to_token="to",
            amount="1",
            slippage_bps=50,
        )
        assert out1["ok"] is True
        assert out1["result"]["kind"] == "swap"
        assert "approval" in out1["result"]["effects"]
