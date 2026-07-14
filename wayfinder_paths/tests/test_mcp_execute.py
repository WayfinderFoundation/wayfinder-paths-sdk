from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.utils.token_resolver import TokenResolver
from wayfinder_paths.mcp.tools.execute import onchain_swap

QUOTE_ID = "quote-1234567890123456"


@pytest.fixture(autouse=True)
def _clear_token_resolver_cache():
    TokenResolver._token_details_cache.clear()
    TokenResolver._gas_token_cache.clear()


def _quote_intent(
    wallet: dict,
    from_meta: dict,
    to_meta: dict,
    best_quote: dict,
    slippage: float = 0.005,
) -> dict:
    return {
        "quote_id": QUOTE_ID,
        "expires_at": 1_800_000_000,
        "request": {
            "from_wallet": wallet["address"],
            "to_wallet": wallet["address"],
            "from_token": from_meta["address"],
            "to_token": to_meta["address"],
            "from_chain": from_meta["chain_id"],
            "to_chain": to_meta["chain_id"],
            "from_amount": int(best_quote["input_amount"]),
            "to_amount": None,
            "slippage": slippage,
        },
        "best_quote": best_quote,
    }


@pytest.mark.asyncio
async def test_resolve_token_meta_native_gas_token_null_address():
    """Native gas tokens (e.g. ETH) may have address=null from the API.

    TokenResolver should normalize null -> ZERO_ADDRESS when the metadata looks
    like a native gas token.
    """
    meta = {
        "asset_id": "ethereum",
        "symbol": "ETH",
        "decimals": 18,
        "address": None,
        "chain_id": 1,
    }
    with patch(
        "wayfinder_paths.core.utils.token_resolver.TOKEN_CLIENT.get_gas_token",
        new=AsyncMock(return_value=meta),
    ):
        out = await TokenResolver.resolve_token_meta("ethereum-ethereum")
        assert out["address"] == ZERO_ADDRESS
        assert out["chain_id"] == 1


@pytest.mark.asyncio
async def test_resolve_token_meta_native_gas_token_missing_address():
    """Same as above but address key is missing entirely."""
    meta = {
        "asset_id": "ethereum",
        "symbol": "ETH",
        "decimals": 18,
        "chain_id": 8453,
    }
    with patch(
        "wayfinder_paths.core.utils.token_resolver.TOKEN_CLIENT.get_gas_token",
        new=AsyncMock(return_value=meta),
    ):
        out = await TokenResolver.resolve_token_meta("ethereum-base")
        assert out["address"] == ZERO_ADDRESS
        assert out["chain_id"] == 8453


@pytest.mark.asyncio
async def test_resolve_token_meta_erc20_null_address_raises():
    """Non-native tokens with null address should raise (real error)."""
    meta = {
        "asset_id": "usd-coin",
        "symbol": "USDC",
        "decimals": 6,
        "address": None,
        "chain_id": 1,
    }
    with patch(
        "wayfinder_paths.core.utils.token_resolver.TOKEN_CLIENT.get_token_details",
        new=AsyncMock(return_value=meta),
    ):
        with pytest.raises(ValueError, match="Cannot resolve token"):
            await TokenResolver.resolve_token_meta("usd-coin-ethereum")


@pytest.mark.asyncio
async def test_resolve_token_meta_normal_erc20_unchanged():
    """Normal ERC20 tokens should pass through unchanged."""
    meta = {
        "asset_id": "usd-coin",
        "symbol": "USDC",
        "decimals": 6,
        "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "chain_id": 1,
    }
    with patch(
        "wayfinder_paths.core.utils.token_resolver.TOKEN_CLIENT.get_token_details",
        new=AsyncMock(return_value=meta),
    ):
        out = await TokenResolver.resolve_token_meta("usd-coin-ethereum")
        assert out["address"] == "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


@pytest.mark.asyncio
async def test_swap_missing_wallet_label_is_structured():
    out = await onchain_swap(
        wallet_label=" ",
        from_token="from",
        to_token="to",
        amount="1.0",
        quote_id=QUOTE_ID,
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_swap_requires_bound_quote_id():
    out = await onchain_swap(
        wallet_label="main",
        from_token="from",
        to_token="to",
        amount="1.0",
        quote_id="",
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "quote_required"


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

    async def fake_resolve(query: str, *, chain_id: int | None = None):
        _ = chain_id
        if query == "from":
            return from_meta
        if query == "to":
            return to_meta
        raise AssertionError(f"unexpected token query: {query}")

    fake_brap = AsyncMock()
    best_quote = {
        "provider": "brap_best",
        "input_amount": "1000000",
        "calldata": {
            "to": "0x" + "33" * 20,
            "data": "0xdeadbeef",
            "value": "0",
        },
    }
    fake_brap.get_quote_intent = AsyncMock(
        return_value=_quote_intent(wallet, from_meta, to_meta, best_quote)
    )

    async def fake_ensure_allowance(**_kwargs):  # noqa: ANN003
        return True, "0xapprove"

    with (
        patch(
            "wayfinder_paths.core.utils.wallets.find_wallet_by_label",
            return_value=wallet,
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.TokenResolver.resolve_token_meta",
            new_callable=AsyncMock,
            side_effect=fake_resolve,
        ),
        patch("wayfinder_paths.mcp.tools.execute.BRAP_CLIENT", fake_brap),
        patch(
            "wayfinder_paths.mcp.tools.execute.ensure_allowance",
            new=AsyncMock(side_effect=fake_ensure_allowance),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.send_transaction",
            new_callable=AsyncMock,
            return_value="0xtx",
        ) as send_transaction_mock,
        patch(
            "wayfinder_paths.mcp.tools.execute.get_token_balance",
            new=AsyncMock(return_value=10**18),
        ),
    ):
        out1 = await onchain_swap(
            wallet_label="main",
            from_token="from",
            to_token="to",
            amount="1.0",
            quote_id=QUOTE_ID,
            slippage_bps=50,
        )
        assert out1["ok"] is True
        assert "approval" in out1["result"]["effects"]
        assert out1["result"]["status"] == "confirmed"
        assert out1["result"]["effects"]["swap"]["txn_hash"] == "0xtx"
        send_transaction_mock.assert_awaited_once()
        assert send_transaction_mock.await_args.kwargs["wait_for_receipt"] is True
        assert send_transaction_mock.await_args.kwargs["confirmations"] == 0

        send_transaction_mock.reset_mock()

        out2 = await onchain_swap(
            wallet_label="main",
            from_token="from",
            to_token="to",
            amount="1.0",
            quote_id=QUOTE_ID,
            slippage_bps=50,
            wait_for_receipt=False,
        )
        assert out2["ok"] is True
        assert out2["result"]["status"] == "submitted"
        send_transaction_mock.assert_awaited_once()
        assert send_transaction_mock.await_args.kwargs["wait_for_receipt"] is False
        assert send_transaction_mock.await_args.kwargs["confirmations"] == 0
        fake_brap.wait_for_bridge_execution.assert_not_awaited()

        send_transaction_mock.reset_mock()
        mismatch = await onchain_swap(
            wallet_label="main",
            from_token="from",
            to_token="to",
            amount="2.0",
            quote_id=QUOTE_ID,
            slippage_bps=50,
        )
        assert mismatch["ok"] is False
        assert mismatch["error"]["code"] == "quote_mismatch"
        send_transaction_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_cross_chain_swap_waits_for_bridge(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WAYFINDER_MCP_STATE_PATH", str(tmp_path / "mcp.sqlite3"))
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))

    wallet = {
        "address": "0x000000000000000000000000000000000000dEaD",
        "private_key_hex": "0x" + "11" * 32,
    }
    from_meta = {
        "symbol": "USDC",
        "decimals": 6,
        "chain_id": 1,
        "address": "0x1111111111111111111111111111111111111111",
    }
    to_meta = {
        "symbol": "USDC",
        "decimals": 6,
        "chain_id": 8453,
        "address": "0x2222222222222222222222222222222222222222",
    }
    bridge_tracking = {
        "provider": "lifi",
        "requires_source_tx_hash": True,
        "from_chain": 1,
        "to_chain": 8453,
        "bridge": "across",
    }

    async def fake_resolve(query: str, *, chain_id: int | None = None):
        _ = chain_id
        return from_meta if query == "from" else to_meta

    fake_brap = AsyncMock()
    best_quote = {
        "provider": "lifi",
        "input_amount": "1000000",
        "calldata": {
            "to": "0x" + "33" * 20,
            "data": "0xdeadbeef",
            "value": "0",
        },
        "bridge_tracking": bridge_tracking,
    }
    fake_brap.get_quote_intent = AsyncMock(
        return_value=_quote_intent(wallet, from_meta, to_meta, best_quote)
    )
    fake_brap.wait_for_bridge_execution = AsyncMock(
        return_value={"is_success": True, "state": "completed"}
    )

    async def fake_ensure_allowance(**_kwargs):  # noqa: ANN003
        return True, "0xapprove"

    with (
        patch(
            "wayfinder_paths.core.utils.wallets.find_wallet_by_label",
            return_value=wallet,
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.TokenResolver.resolve_token_meta",
            new_callable=AsyncMock,
            side_effect=fake_resolve,
        ),
        patch("wayfinder_paths.mcp.tools.execute.BRAP_CLIENT", fake_brap),
        patch(
            "wayfinder_paths.mcp.tools.execute.ensure_allowance",
            new=AsyncMock(side_effect=fake_ensure_allowance),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.send_transaction",
            new_callable=AsyncMock,
            return_value="0xsrctx",
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.get_token_balance",
            new=AsyncMock(return_value=10**18),
        ),
    ):
        out = await onchain_swap(
            wallet_label="main",
            from_token="from",
            to_token="to",
            amount="1.0",
            quote_id=QUOTE_ID,
        )

    assert out["ok"] is True
    assert out["result"]["status"] == "confirmed"
    assert out["result"]["effects"]["bridge"]["is_success"] is True
    fake_brap.wait_for_bridge_execution.assert_awaited_once()
    call_kwargs = fake_brap.wait_for_bridge_execution.await_args.kwargs
    assert call_kwargs["bridge_tracking"] == bridge_tracking
    assert call_kwargs["tx_hash"] == "0xsrctx"


@pytest.mark.asyncio
async def test_execute_cross_chain_swap_failed_bridge_marks_failed(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("WAYFINDER_MCP_STATE_PATH", str(tmp_path / "mcp.sqlite3"))
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))

    wallet = {
        "address": "0x000000000000000000000000000000000000dEaD",
        "private_key_hex": "0x" + "11" * 32,
    }
    from_meta = {
        "decimals": 6,
        "chain_id": 1,
        "address": "0x1111111111111111111111111111111111111111",
    }
    to_meta = {
        "decimals": 6,
        "chain_id": 8453,
        "address": "0x2222222222222222222222222222222222222222",
    }

    fake_brap = AsyncMock()
    best_quote = {
        "provider": "lifi",
        "input_amount": "1000000",
        "calldata": {
            "to": "0x" + "33" * 20,
            "data": "0xdeadbeef",
            "value": "0",
        },
        "bridge_tracking": {
            "provider": "lifi",
            "requires_source_tx_hash": True,
            "from_chain": 1,
            "to_chain": 8453,
        },
    }
    fake_brap.get_quote_intent = AsyncMock(
        return_value=_quote_intent(wallet, from_meta, to_meta, best_quote)
    )
    fake_brap.wait_for_bridge_execution = AsyncMock(
        return_value={"is_success": False, "state": "failed", "error": "reverted"}
    )

    async def fake_resolve(query: str, *, chain_id: int | None = None):
        _ = chain_id
        return from_meta if query == "from" else to_meta

    async def fake_ensure_allowance(**_kwargs):  # noqa: ANN003
        return True, "0xapprove"

    with (
        patch(
            "wayfinder_paths.core.utils.wallets.find_wallet_by_label",
            return_value=wallet,
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.TokenResolver.resolve_token_meta",
            new_callable=AsyncMock,
            side_effect=fake_resolve,
        ),
        patch("wayfinder_paths.mcp.tools.execute.BRAP_CLIENT", fake_brap),
        patch(
            "wayfinder_paths.mcp.tools.execute.ensure_allowance",
            new=AsyncMock(side_effect=fake_ensure_allowance),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.send_transaction",
            new_callable=AsyncMock,
            return_value="0xsrctx",
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.get_token_balance",
            new=AsyncMock(return_value=10**18),
        ),
    ):
        out = await onchain_swap(
            wallet_label="main",
            from_token="from",
            to_token="to",
            amount="1.0",
            quote_id=QUOTE_ID,
        )

    assert out["ok"] is True
    assert out["result"]["status"] == "failed"
    assert out["result"]["effects"]["bridge"]["is_success"] is False


@pytest.mark.asyncio
async def test_execute_swap_prefers_quote_approval_address(tmp_path: Path, monkeypatch):
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
        "chain_id": 8453,
        "address": "0x1111111111111111111111111111111111111111",
    }
    to_meta = {
        "token_id": "to",
        "symbol": "TO",
        "decimals": 6,
        "chain_id": 8453,
        "address": "0x2222222222222222222222222222222222222222",
    }

    async def fake_resolve(query: str, *, chain_id: int | None = None):
        _ = chain_id
        return from_meta if query == "from" else to_meta

    fake_brap = AsyncMock()
    best_quote = {
        "provider": "lifi",
        "approvalAddress": "0x" + "44" * 20,
        "input_amount": "1000000",
        "output_amount": "2000000",
        "calldata": {
            "to": "0x" + "33" * 20,
            "data": "0xdeadbeef",
            "value": "0",
        },
    }
    fake_brap.get_quote_intent = AsyncMock(
        return_value=_quote_intent(wallet, from_meta, to_meta, best_quote)
    )
    ensure_mock = AsyncMock(return_value=(True, "0xapprove"))

    with (
        patch(
            "wayfinder_paths.core.utils.wallets.find_wallet_by_label",
            return_value=wallet,
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.TokenResolver.resolve_token_meta",
            new_callable=AsyncMock,
            side_effect=fake_resolve,
        ),
        patch("wayfinder_paths.mcp.tools.execute.BRAP_CLIENT", fake_brap),
        patch("wayfinder_paths.mcp.tools.execute.ensure_allowance", ensure_mock),
        patch(
            "wayfinder_paths.mcp.tools.execute.send_transaction",
            new_callable=AsyncMock,
            return_value="0xtx",
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.get_token_balance",
            new=AsyncMock(return_value=10**18),
        ),
    ):
        out = await onchain_swap(
            wallet_label="main",
            from_token="from",
            to_token="to",
            amount="1.0",
            quote_id=QUOTE_ID,
        )

    assert out["ok"] is True
    assert out["result"]["status"] == "confirmed"
    assert ensure_mock.await_args.kwargs["spender"] == "0x" + "44" * 20


@pytest.mark.asyncio
async def test_execute_bound_uniswap_quote_runs_prerequisites_in_order(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("WAYFINDER_MCP_STATE_PATH", str(tmp_path / "mcp.sqlite3"))
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))
    wallet = {
        "address": "0x000000000000000000000000000000000000dEaD",
        "private_key_hex": "0x" + "11" * 32,
    }
    from_meta = {
        "symbol": "GRID",
        "decimals": 18,
        "chain_id": 4663,
        "address": "0x62537C09A874Cfe886E052D5afcd28356A98e549",
    }
    to_meta = {
        "symbol": "ETH",
        "decimals": 18,
        "chain_id": 4663,
        "address": ZERO_ADDRESS,
    }
    prerequisites = [
        {
            "to": "0x" + "44" * 20,
            "data": "0xapprove1",
            "value": "0x0",
            "description": "Approve Permit2",
        },
        {
            "to": "0x" + "55" * 20,
            "data": "0xapprove2",
            "value": "0x0",
            "description": "Authorize router",
        },
    ]
    best_quote = {
        "provider": "uniswap_v4",
        "quote": {"route": "uniswap_v4"},
        "input_amount": str(10**18),
        "calldata": {
            "to": "0x" + "33" * 20,
            "data": "0xswap",
            "value": 0,
        },
        "prerequisite_transactions": prerequisites,
    }
    fake_brap = AsyncMock()
    fake_brap.get_quote_intent = AsyncMock(
        return_value=_quote_intent(wallet, from_meta, to_meta, best_quote)
    )
    send_mock = AsyncMock(side_effect=["0xapproval1", "0xapproval2", "0xswap"])
    ensure_mock = AsyncMock(return_value=(True, None))

    async def fake_resolve(query: str, *, chain_id: int | None = None):
        _ = chain_id
        return from_meta if query == "from" else to_meta

    with (
        patch(
            "wayfinder_paths.core.utils.wallets.find_wallet_by_label",
            return_value=wallet,
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.TokenResolver.resolve_token_meta",
            new_callable=AsyncMock,
            side_effect=fake_resolve,
        ),
        patch("wayfinder_paths.mcp.tools.execute.BRAP_CLIENT", fake_brap),
        patch("wayfinder_paths.mcp.tools.execute.ensure_allowance", ensure_mock),
        patch(
            "wayfinder_paths.mcp.tools.execute.send_transaction",
            send_mock,
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.get_token_balance",
            new=AsyncMock(return_value=10**18),
        ),
    ):
        out = await onchain_swap(
            wallet_label="main",
            from_token="from",
            to_token="to",
            amount="1.0",
            quote_id=QUOTE_ID,
        )

    assert out["ok"] is True
    assert [item["txn_hash"] for item in out["result"]["effects"]["prerequisites"]] == [
        "0xapproval1",
        "0xapproval2",
    ]
    assert out["result"]["effects"]["swap"]["txn_hash"] == "0xswap"
    assert send_mock.await_count == 3
    ensure_mock.assert_not_awaited()
