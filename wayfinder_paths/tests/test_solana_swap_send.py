from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from solders.hash import Hash
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.constants.chains import CHAIN_ID_SOLANA
from wayfinder_paths.core.utils.token_resolver import TokenResolver
from wayfinder_paths.mcp.tools.execute import onchain_send, onchain_swap

SENDER = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
# Mixed-case base58 (case-sensitive) — lets us assert it is never checksummed/lowered.
RECIPIENT = "8uqKmV5bMcoGw2AxoEMkseHF78ZDrAWpACvfUhxwmqqT"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_SIG = "5" + "j" * 87  # base58-ish placeholder signature the SVM path returns


@pytest.fixture(autouse=True)
def _clear_token_resolver_cache():
    TokenResolver._token_details_cache.clear()
    TokenResolver._gas_token_cache.clear()


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WAYFINDER_MCP_STATE_PATH", str(tmp_path / "mcp.sqlite3"))
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))


@pytest.fixture(autouse=True)
def _solana_enabled():
    # Default the flag on; the flag-off tests re-patch it to False.
    with patch(
        "wayfinder_paths.mcp.tools.execute.is_solana_enabled",
        new_callable=AsyncMock,
        return_value=True,
    ):
        yield


def _svm_callback():
    cb = AsyncMock()
    cb.wallet_address = SENDER
    cb.chain_type = "solana"
    return cb


def _unsigned_v0_b64(payer: str, to: str, lamports: int = 1_000) -> str:
    """A real, decodable unsigned v0 transaction, base64-encoded."""
    payer_pk = Pubkey.from_string(payer)
    ix = transfer(
        TransferParams(
            from_pubkey=payer_pk,
            to_pubkey=Pubkey.from_string(to),
            lamports=lamports,
        )
    )
    msg = MessageV0.try_compile(
        payer=payer_pk,
        instructions=[ix],
        address_lookup_table_accounts=[],
        recent_blockhash=Hash.default(),
    )
    sigs = [Signature.default() for _ in range(msg.header.num_required_signatures)]
    return base64.b64encode(bytes(VersionedTransaction.populate(msg, sigs))).decode()


# ---------------------------------------------------------------------------
# onchain_swap — Solana route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_swap_solana_route_broadcasts_via_svm_and_skips_allowance():
    from_meta = {
        "symbol": "USDC",
        "decimals": 6,
        "chain_id": CHAIN_ID_SOLANA,
        "address": USDC_MINT,
    }
    to_meta = {
        "symbol": "SOL",
        "decimals": 9,
        "chain_id": CHAIN_ID_SOLANA,
        "address": ZERO_ADDRESS,
    }
    serialized = _unsigned_v0_b64(SENDER, RECIPIENT)

    async def fake_resolve(query: str, *, chain_id: int | None = None):
        return from_meta if query == "from" else to_meta

    fake_brap = AsyncMock()
    fake_brap.get_quote = AsyncMock(
        return_value={
            "quotes": [{"provider": "jupiter"}],
            "best_quote": {
                "provider": "jupiter",
                "input_amount": "1000000",
                "calldata": {
                    "chainType": "solana",
                    "chainId": CHAIN_ID_SOLANA,
                    "serializedTransaction": serialized,
                },
            },
        }
    )

    with (
        patch(
            "wayfinder_paths.mcp.tools.execute.get_wallet_signing_callback_for_chain",
            new=AsyncMock(return_value=(_svm_callback(), SENDER)),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.TokenResolver.resolve_token_meta",
            new_callable=AsyncMock,
            side_effect=fake_resolve,
        ),
        patch("wayfinder_paths.mcp.tools.execute.BRAP_CLIENT", fake_brap),
        patch(
            "wayfinder_paths.mcp.tools.execute.get_solana_token_balance",
            new=AsyncMock(return_value=10**9),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.send_svm_versioned_transaction",
            new_callable=AsyncMock,
            return_value=SOL_SIG,
        ) as svm_send,
        patch(
            "wayfinder_paths.mcp.tools.execute.send_transaction",
            new_callable=AsyncMock,
        ) as evm_send,
        patch(
            "wayfinder_paths.mcp.tools.execute.ensure_allowance",
            new_callable=AsyncMock,
        ) as ensure,
    ):
        out = await onchain_swap(
            wallet_label="main",
            from_token="from",
            to_token="to",
            amount="1.0",
        )

    assert out["ok"] is True
    assert out["result"]["status"] == "confirmed"
    assert out["result"]["effects"]["swap"]["txn_hash"] == SOL_SIG
    assert (
        out["result"]["effects"]["swap"]["explorer_url"]
        == f"https://solscan.io/tx/{SOL_SIG}"
    )
    # No approval on Solana, and the EVM broadcast must never fire.
    assert "approval" not in out["result"]["effects"]
    evm_send.assert_not_awaited()
    ensure.assert_not_awaited()

    # The SVM broadcaster received the decoded VersionedTransaction (not a str/dict).
    svm_send.assert_awaited_once()
    vt_arg = svm_send.await_args.args[0]
    assert isinstance(vt_arg, VersionedTransaction)
    assert svm_send.await_args.kwargs["chain_id"] == CHAIN_ID_SOLANA
    assert svm_send.await_args.kwargs["wait_for_confirmation"] is True


@pytest.mark.asyncio
async def test_swap_solana_route_detected_by_chain_id_without_chaintype():
    meta = {
        "symbol": "USDC",
        "decimals": 6,
        "chain_id": CHAIN_ID_SOLANA,
        "address": USDC_MINT,
    }
    serialized = _unsigned_v0_b64(SENDER, RECIPIENT)

    fake_brap = AsyncMock()
    fake_brap.get_quote = AsyncMock(
        return_value={
            "quotes": [{"provider": "jupiter"}],
            "best_quote": {
                "provider": "jupiter",
                "input_amount": "1000000",
                "calldata": {"serializedTransaction": serialized},
            },
        }
    )

    with (
        patch(
            "wayfinder_paths.mcp.tools.execute.get_wallet_signing_callback_for_chain",
            new=AsyncMock(return_value=(_svm_callback(), SENDER)),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.TokenResolver.resolve_token_meta",
            new_callable=AsyncMock,
            return_value=meta,
        ),
        patch("wayfinder_paths.mcp.tools.execute.BRAP_CLIENT", fake_brap),
        patch(
            "wayfinder_paths.mcp.tools.execute.get_solana_token_balance",
            new=AsyncMock(return_value=10**9),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.send_svm_versioned_transaction",
            new_callable=AsyncMock,
            return_value=SOL_SIG,
        ) as svm_send,
        patch(
            "wayfinder_paths.mcp.tools.execute.send_transaction",
            new_callable=AsyncMock,
        ) as evm_send,
    ):
        out = await onchain_swap(
            wallet_label="main", from_token="from", to_token="to", amount="1.0"
        )

    assert out["ok"] is True
    svm_send.assert_awaited_once()
    evm_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_swap_solana_route_missing_serialized_tx_errors():
    meta = {
        "symbol": "USDC",
        "decimals": 6,
        "chain_id": CHAIN_ID_SOLANA,
        "address": USDC_MINT,
    }
    fake_brap = AsyncMock()
    fake_brap.get_quote = AsyncMock(
        return_value={
            "quotes": [{"provider": "jupiter"}],
            "best_quote": {
                "provider": "jupiter",
                "input_amount": "1000000",
                "calldata": {"chainType": "solana"},
            },
        }
    )

    with (
        patch(
            "wayfinder_paths.mcp.tools.execute.get_wallet_signing_callback_for_chain",
            new=AsyncMock(return_value=(_svm_callback(), SENDER)),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.TokenResolver.resolve_token_meta",
            new_callable=AsyncMock,
            return_value=meta,
        ),
        patch("wayfinder_paths.mcp.tools.execute.BRAP_CLIENT", fake_brap),
        patch(
            "wayfinder_paths.mcp.tools.execute.get_solana_token_balance",
            new=AsyncMock(return_value=10**9),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.send_svm_versioned_transaction",
            new_callable=AsyncMock,
        ) as svm_send,
    ):
        out = await onchain_swap(
            wallet_label="main", from_token="from", to_token="to", amount="1.0"
        )

    assert out["ok"] is False
    assert out["error"]["code"] == "quote_error"
    svm_send.assert_not_awaited()


# ---------------------------------------------------------------------------
# onchain_send — chain 900
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_solana_spl_builds_envelope_and_broadcasts_via_svm():
    token_meta = {
        "symbol": "USDC",
        "decimals": 6,
        "chain_id": CHAIN_ID_SOLANA,
        "address": USDC_MINT,
    }
    serialized = _unsigned_v0_b64(SENDER, RECIPIENT)
    envelope = {
        "chainType": "solana",
        "chainId": CHAIN_ID_SOLANA,
        "serializedTransaction": serialized,
        "lastValidBlockHeight": 250_000_000,
    }

    with (
        patch(
            "wayfinder_paths.mcp.tools.execute.get_wallet_signing_callback_for_chain",
            new=AsyncMock(return_value=(_svm_callback(), SENDER)),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.TokenResolver.resolve_token_meta",
            new_callable=AsyncMock,
            return_value=token_meta,
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.get_solana_token_balance",
            new=AsyncMock(return_value=5_000_000),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.build_solana_send_transaction",
            new_callable=AsyncMock,
            return_value=envelope,
        ) as build_svm,
        patch(
            "wayfinder_paths.mcp.tools.execute.build_send_transaction",
            new_callable=AsyncMock,
        ) as build_evm,
        patch(
            "wayfinder_paths.mcp.tools.execute.send_svm_versioned_transaction",
            new_callable=AsyncMock,
            return_value=SOL_SIG,
        ) as svm_send,
        patch(
            "wayfinder_paths.mcp.tools.execute.send_transaction",
            new_callable=AsyncMock,
        ) as evm_send,
    ):
        out = await onchain_send(
            wallet_label="main",
            token="usdc",
            recipient=RECIPIENT,
            amount="1.0",
        )

    assert out["ok"] is True
    assert out["result"]["status"] == "confirmed"
    assert out["result"]["effects"]["send_erc20"]["txn_hash"] == SOL_SIG
    # Recipient passed through un-checksummed (base58 is case-sensitive).
    assert out["result"]["recipient"] == RECIPIENT

    # Solana build + SVM broadcast fired; the EVM equivalents did not.
    build_svm.assert_awaited_once()
    assert build_svm.await_args.kwargs["to_address"] == RECIPIENT
    assert build_svm.await_args.kwargs["token_address"] == USDC_MINT
    assert build_svm.await_args.kwargs["chain_id"] == CHAIN_ID_SOLANA
    build_evm.assert_not_awaited()
    evm_send.assert_not_awaited()

    svm_send.assert_awaited_once()
    vt_arg = svm_send.await_args.args[0]
    assert isinstance(vt_arg, VersionedTransaction)


@pytest.mark.asyncio
async def test_send_solana_native_sol_uses_native_label():
    token_meta = {
        "symbol": "SOL",
        "decimals": 9,
        "chain_id": CHAIN_ID_SOLANA,
        "address": ZERO_ADDRESS,
    }
    envelope = {
        "chainType": "solana",
        "chainId": CHAIN_ID_SOLANA,
        "serializedTransaction": _unsigned_v0_b64(SENDER, RECIPIENT),
        "lastValidBlockHeight": 250_000_000,
    }

    with (
        patch(
            "wayfinder_paths.mcp.tools.execute.get_wallet_signing_callback_for_chain",
            new=AsyncMock(return_value=(_svm_callback(), SENDER)),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.TokenResolver.resolve_token_meta",
            new_callable=AsyncMock,
            return_value=token_meta,
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.get_solana_token_balance",
            new=AsyncMock(return_value=10**9),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.build_solana_send_transaction",
            new_callable=AsyncMock,
            return_value=envelope,
        ) as build_svm,
        patch(
            "wayfinder_paths.mcp.tools.execute.send_svm_versioned_transaction",
            new_callable=AsyncMock,
            return_value=SOL_SIG,
        ) as svm_send,
    ):
        out = await onchain_send(
            wallet_label="main",
            token="native",
            recipient=RECIPIENT,
            amount="0.5",
            chain_id=CHAIN_ID_SOLANA,
        )

    assert out["ok"] is True
    assert "send_native" in out["result"]["effects"]
    assert out["result"]["effects"]["send_native"]["txn_hash"] == SOL_SIG
    build_svm.assert_awaited_once()
    assert build_svm.await_args.kwargs["token_address"] == ZERO_ADDRESS
    svm_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_solana_insufficient_balance_short_circuits_before_build():
    token_meta = {
        "symbol": "USDC",
        "decimals": 6,
        "chain_id": CHAIN_ID_SOLANA,
        "address": USDC_MINT,
    }
    with (
        patch(
            "wayfinder_paths.mcp.tools.execute.get_wallet_signing_callback_for_chain",
            new=AsyncMock(return_value=(_svm_callback(), SENDER)),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.TokenResolver.resolve_token_meta",
            new_callable=AsyncMock,
            return_value=token_meta,
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.get_solana_token_balance",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.build_solana_send_transaction",
            new_callable=AsyncMock,
        ) as build_svm,
        patch(
            "wayfinder_paths.mcp.tools.execute.send_svm_versioned_transaction",
            new_callable=AsyncMock,
        ) as svm_send,
    ):
        out = await onchain_send(
            wallet_label="main",
            token="usdc",
            recipient=RECIPIENT,
            amount="1.0",
        )

    assert out["ok"] is False
    assert out["error"]["code"] == "insufficient_balance"
    build_svm.assert_not_awaited()
    svm_send.assert_not_awaited()


# ---------------------------------------------------------------------------
# solana_enabled guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_swap_solana_blocked_when_flag_disabled():
    meta = {
        "symbol": "USDC",
        "decimals": 6,
        "chain_id": CHAIN_ID_SOLANA,
        "address": USDC_MINT,
    }
    with (
        patch(
            "wayfinder_paths.mcp.tools.execute.is_solana_enabled",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.get_wallet_signing_callback_for_chain",
            new=AsyncMock(return_value=(_svm_callback(), SENDER)),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.TokenResolver.resolve_token_meta",
            new_callable=AsyncMock,
            return_value=meta,
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.send_svm_versioned_transaction",
            new_callable=AsyncMock,
        ) as svm_send,
    ):
        out = await onchain_swap(
            wallet_label="main", from_token="from", to_token="to", amount="1.0"
        )

    assert out["ok"] is False
    assert out["error"]["code"] == "solana_disabled"
    svm_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_solana_blocked_when_flag_disabled():
    token_meta = {
        "symbol": "USDC",
        "decimals": 6,
        "chain_id": CHAIN_ID_SOLANA,
        "address": USDC_MINT,
    }
    with (
        patch(
            "wayfinder_paths.mcp.tools.execute.is_solana_enabled",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.get_wallet_signing_callback_for_chain",
            new=AsyncMock(return_value=(_svm_callback(), SENDER)),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.TokenResolver.resolve_token_meta",
            new_callable=AsyncMock,
            return_value=token_meta,
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.build_solana_send_transaction",
            new_callable=AsyncMock,
        ) as build_svm,
        patch(
            "wayfinder_paths.mcp.tools.execute.send_svm_versioned_transaction",
            new_callable=AsyncMock,
        ) as svm_send,
    ):
        out = await onchain_send(
            wallet_label="main", token="usdc", recipient=RECIPIENT, amount="1.0"
        )

    assert out["ok"] is False
    assert out["error"]["code"] == "solana_disabled"
    build_svm.assert_not_awaited()
    svm_send.assert_not_awaited()
