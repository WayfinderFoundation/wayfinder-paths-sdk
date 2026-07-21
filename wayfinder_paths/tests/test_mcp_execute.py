from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.utils.token_resolver import TokenResolver
from wayfinder_paths.mcp.tools.execute import onchain_swap


@pytest.fixture(autouse=True)
def _clear_token_resolver_cache():
    TokenResolver._token_details_cache.clear()
    TokenResolver._gas_token_cache.clear()


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
        wallet_label=" ", from_token="from", to_token="to", amount="1.0"
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_request"


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
            slippage_bps=50,
        )
        assert out1["ok"] is True
        assert "approval" in out1["result"]["effects"]
        assert out1["result"]["status"] == "confirmed"
        assert out1["result"]["effects"]["swap"]["txn_hash"] == "0xtx"
        send_transaction_mock.assert_awaited_once()
        assert send_transaction_mock.await_args.kwargs["wait_for_receipt"] is True
        assert send_transaction_mock.await_args.kwargs["confirmations"] == 0
        # The executed swap must land in backend history — per-token PnL basis
        # is derived from these records server-side.
        fake_brap.record_swap_transaction.assert_awaited_once()
        record = fake_brap.record_swap_transaction.await_args.args[0]
        assert record["tx_hash"] == "0xtx"
        assert record["status"] == "CONFIRMED"
        assert record["from_token_address"] == from_meta["address"]
        assert record["to_token_address"] == to_meta["address"]
        assert record["from_amount"] == "1.0"
        assert record["quote_provider"] == "brap_best"
        assert record["metadata"] == {"source": "agent_onchain_swap"}

        send_transaction_mock.reset_mock()
        fake_brap.record_swap_transaction.reset_mock()

        out2 = await onchain_swap(
            wallet_label="main",
            from_token="from",
            to_token="to",
            amount="1.0",
            slippage_bps=50,
            wait_for_receipt=False,
        )
        assert out2["ok"] is True
        assert out2["result"]["status"] == "submitted"
        send_transaction_mock.assert_awaited_once()
        assert send_transaction_mock.await_args.kwargs["wait_for_receipt"] is False
        assert send_transaction_mock.await_args.kwargs["confirmations"] == 0
        fake_brap.wait_for_bridge_execution.assert_not_awaited()
        # Fire-and-forget broadcast still records history, as PENDING.
        fake_brap.record_swap_transaction.assert_awaited_once()
        assert (
            fake_brap.record_swap_transaction.await_args.args[0]["status"] == "PENDING"
        )


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
    fake_brap.get_quote = AsyncMock(
        return_value={
            "quotes": [{"provider": "lifi"}],
            "best_quote": {
                "provider": "lifi",
                "input_amount": "1000000",
                "calldata": {
                    "to": "0x" + "33" * 20,
                    "data": "0xdeadbeef",
                    "value": "0",
                },
                "bridge_tracking": bridge_tracking,
            },
        }
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
    fake_brap.get_quote = AsyncMock(
        return_value={
            "quotes": [{"provider": "lifi"}],
            "best_quote": {
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
            },
        }
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
    fake_brap.get_quote = AsyncMock(
        return_value={
            "quotes": [{"provider": "lifi"}],
            "best_quote": {
                "provider": "lifi",
                "approvalAddress": "0x" + "44" * 20,
                "input_amount": "1000000",
                "output_amount": "2000000",
                "calldata": {
                    "to": "0x" + "33" * 20,
                    "data": "0xdeadbeef",
                    "value": "0",
                },
            },
        }
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
        )

    assert out["ok"] is True
    assert out["result"]["status"] == "confirmed"
    assert ensure_mock.await_args.kwargs["spender"] == "0x" + "44" * 20


def test_swap_history_payload_maps_quote_fields():
    """Payload construction pinned against a real BRAP/LiFi quote shape (the
    GME-on-robinhood purchase that exposed the missing-basis bug)."""
    from wayfinder_paths.mcp.tools.execute import _swap_history_payload

    best_quote = {
        "provider": "lifi",
        "input_amount": 3000000000000000,
        "output_amount": 1681709558498559701890,
        "input_amount_usd": 5.20944,
        "output_amount_usd": 4.730682622247619,
        "fee_estimate": {"fee_total_usd": 0.013},
        "bridge_tracking": None,
    }
    payload = _swap_history_payload(
        tx_hash="0xhash",
        status="confirmed",
        sender="0xsender",
        recipient="0xsender",
        from_chain_id=4663,
        to_chain_id=4663,
        from_meta={"address": "0x" + "0" * 40, "symbol": "ETH", "decimals": 18},
        to_meta={
            "address": "0x7e86381a763f0ecca2bdf27c54eac403ddd48123",
            "symbol": "GME",
            "decimals": 18,
        },
        amount="0.003",
        best_quote=best_quote,
    )
    assert payload["status"] == "CONFIRMED"
    assert payload["confirmations"] == 1
    assert payload["from_amount"] == "0.003"
    assert payload["from_amount_usd"] == 5.20944
    assert float(payload["to_amount"]) == pytest.approx(1681.7095584986, rel=1e-9)
    assert payload["to_amount_usd"] == 4.730682622247619
    assert payload["quote_provider"] == "lifi"
    assert payload["fee_total_usd"] == 0.013

    # Failed swaps record FAILED; unknown output tolerated as null.
    failed = _swap_history_payload(
        tx_hash="0xhash",
        status="failed",
        sender="0xsender",
        recipient="0xsender",
        from_chain_id=4663,
        to_chain_id=4663,
        from_meta={"address": "0x" + "0" * 40, "symbol": "ETH"},
        to_meta={"address": "0xabc", "symbol": "GME"},
        amount="0.003",
        best_quote={"provider": "lifi"},
    )
    assert failed["status"] == "FAILED"
    assert failed["to_amount"] is None
    assert failed["confirmations"] == 0


@pytest.mark.asyncio
async def test_record_swap_history_swallows_errors():
    from wayfinder_paths.mcp.tools.execute import _record_swap_history

    boom = AsyncMock()
    boom.record_swap_transaction = AsyncMock(side_effect=RuntimeError("backend down"))
    with patch("wayfinder_paths.mcp.tools.execute.BRAP_CLIENT", boom):
        await _record_swap_history({"tx_hash": "0x1"})  # must not raise
    boom.record_swap_transaction.assert_awaited_once()
