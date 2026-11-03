import copy
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from solders.pubkey import Pubkey
from spl.token.constants import TOKEN_PROGRAM_ID

from wayfinder_paths.adapters.definitive_adapter import solana as svm
from wayfinder_paths.adapters.definitive_adapter.adapter import (
    DefinitiveAdapter,
    build_flash_request,
    flash_cancel_message,
)

MODULE = "wayfinder_paths.adapters.definitive_adapter.adapter"
FUNDER = "DSZ1D4Hp17Y1CtAUt69npmvDKNk3LVz8dZKSe7QdsoX1"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
GROUP = "3jBWeQrnfEhw5LY53hwcoYKQJsrTUbtLbmGrNYCr1Fiq"  # stand-in group key
# svm block from a live Flash quote (FUNDER buys SOL with 5 USDC).
LIVE = json.loads(
    """{"ataSetupIxs": [{"programId": "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL", "accounts": [{"pubkey": "DSZ1D4Hp17Y1CtAUt69npmvDKNk3LVz8dZKSe7QdsoX1", "isSigner": true, "isWritable": true}, {"pubkey": "JEJfd84aqSVeQ2qEXb9ACP5eUKeE9emsq2kJSxdwMDTU", "isSigner": false, "isWritable": true}, {"pubkey": "DSZ1D4Hp17Y1CtAUt69npmvDKNk3LVz8dZKSe7QdsoX1", "isSigner": false, "isWritable": false}, {"pubkey": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "isSigner": false, "isWritable": false}, {"pubkey": "11111111111111111111111111111111", "isSigner": false, "isWritable": false}, {"pubkey": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "isSigner": false, "isWritable": false}], "data": "2"}, {"programId": "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL", "accounts": [{"pubkey": "DSZ1D4Hp17Y1CtAUt69npmvDKNk3LVz8dZKSe7QdsoX1", "isSigner": true, "isWritable": true}, {"pubkey": "CsqQCiPN7h6FtCwsnWsbYmQ6VU82jwkm9NNw9rWBEEPK", "isSigner": false, "isWritable": true}, {"pubkey": "DSZ1D4Hp17Y1CtAUt69npmvDKNk3LVz8dZKSe7QdsoX1", "isSigner": false, "isWritable": false}, {"pubkey": "So11111111111111111111111111111111111111112", "isSigner": false, "isWritable": false}, {"pubkey": "11111111111111111111111111111111", "isSigner": false, "isWritable": false}, {"pubkey": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "isSigner": false, "isWritable": false}], "data": "2"}], "delegateIx": {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "accounts": [{"pubkey": "JEJfd84aqSVeQ2qEXb9ACP5eUKeE9emsq2kJSxdwMDTU", "isSigner": false, "isWritable": true}, {"pubkey": "3jBWeQrnfEhw5LY53hwcoYKQJsrTUbtLbmGrNYCr1Fiq", "isSigner": false, "isWritable": false}, {"pubkey": "DSZ1D4Hp17Y1CtAUt69npmvDKNk3LVz8dZKSe7QdsoX1", "isSigner": true, "isWritable": false}], "data": "492pM2b6VgiB"}}"""
)
PROGRAMS = {USDC: TOKEN_PROGRAM_ID, svm.WSOL_MINT: TOKEN_PROGRAM_ID}


def setup(live=None, **overrides):
    args = {
        "funder": FUNDER,
        "programs": PROGRAMS,
        "spent_mint": USDC,
        "max_delegate": 5_000_000,
        "max_wrap_lamports": 0,
    } | overrides
    return svm.check_flash_svm_setup(live or LIVE, None, **args)


def test_delegate_is_flash_orders_pda():
    assert str(svm.flash_delegate_pda()) == GROUP  # same key as the live delegate


def test_live_setup_is_accepted_and_rebuilt_byte_for_byte():
    ixs = setup()
    assert [str(ix.program_id) for ix in ixs] == [
        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
        str(TOKEN_PROGRAM_ID),
    ]
    assert svm.b58encode(bytes(ixs[2].data)) == LIVE["delegateIx"]["data"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s["delegateIx"]["accounts"][1].update(pubkey=FUNDER),
        lambda s: s["delegateIx"].update(data="492pM2b6VgiC"),
        lambda s: s["ataSetupIxs"][0]["accounts"][2].update(pubkey=GROUP),
        lambda s: s["ataSetupIxs"][0].update(
            programId="11111111111111111111111111111111"
        ),
    ],
)
def test_setup_refusals(mutate):
    live = copy.deepcopy(LIVE)
    mutate(live)
    with pytest.raises(ValueError, match="Refusing to sign"):
        setup(live)


def test_sponsored_delegation_is_refused_for_now():
    with pytest.raises(ValueError, match="sponsored delegation"):
        setup({"delegateIx": None, "sponsoredDelegateTx": "AQ=="})


def test_order_hash_matches_backend_and_frontend_vector():
    owner = Pubkey.from_string(FUNDER)
    from wayfinder_paths.core.utils.svm_tokens import get_associated_token_address

    digest = svm.flash_svm_order_hash(
        output_account=get_associated_token_address(
            owner, Pubkey.from_string(svm.WSOL_MINT), TOKEN_PROGRAM_ID
        ),
        nonce=77176920176852997,
        deadline=281474976710655,
        input_account=get_associated_token_address(
            owner, Pubkey.from_string(USDC), TOKEN_PROGRAM_ID
        ),
        group=GROUP,
    )
    assert digest == "NzyMknRrWHVXschTKUrJeD"
    ok = f"DFS|m={USDC}|t=5000000|h={digest}"
    assert (
        svm.check_flash_svm_message(
            ok, spent_mint=USDC, max_total=5_000_000, expected_hash=digest
        )
        == ok
    )
    for bad in (
        f"DFS|m={svm.WSOL_MINT}|t=5000000|h={digest}",
        f"DFS|m={USDC}|t=5000001|h={digest}",
        f"DFS|m={USDC}|t=5000000|h=GoSSBXENh6jS5Yox7dK9uL",
        ok + "\n",
    ):
        with pytest.raises(ValueError, match="Refusing"):
            svm.check_flash_svm_message(
                bad, spent_mint=USDC, max_total=5_000_000, expected_hash=digest
            )


def test_native_sol_is_spent_as_wsol():
    request = build_flash_request(
        "stop_loss",
        chain_id=900,
        from_token=svm.NATIVE_SOL,
        to_token=USDC,
        amount="2",
        stop_loss_price=120,
    )
    assert request["targetChain"] == "solana"
    assert request["targetAsset"] == svm.WSOL_MINT
    assert request["svmUseNativeSOL"] is True


def _solana_adapter():
    return DefinitiveAdapter(
        sign_callback=AsyncMock(),
        svm_message_callback=AsyncMock(return_value="base58sig"),
        wallet_address=FUNDER,
    )


@pytest.mark.asyncio
async def test_place_solana_order_runs_setup_then_signs_the_checked_message():
    owner = Pubkey.from_string(FUNDER)
    from wayfinder_paths.core.utils.svm_tokens import get_associated_token_address

    digest = svm.flash_svm_order_hash(
        output_account=get_associated_token_address(
            owner, Pubkey.from_string(svm.WSOL_MINT), TOKEN_PROGRAM_ID
        ),
        nonce=77,
        deadline=281474976710655,
        input_account=get_associated_token_address(
            owner, Pubkey.from_string(USDC), TOKEN_PROGRAM_ID
        ),
        group=GROUP,
    )
    message = f"DFS|m={USDC}|t=5000000|h={digest}"
    ready = {
        "quoteId": "q2",
        "svm": {"orderMessage": message, "nonce": "77", "deadline": "281474976710655"},
    }
    client = MagicMock(
        config=AsyncMock(return_value={"solana_group": GROUP}),
        quote=AsyncMock(side_effect=[{"quoteId": "q1", "svm": LIVE}, ready]),
        submit=AsyncMock(return_value="oid"),
    )
    adapter = _solana_adapter()
    adapter._send_solana_setup = AsyncMock(return_value="setupsig")
    request = build_flash_request(
        "limit_buy",
        chain_id=900,
        from_token=USDC,
        to_token=svm.WSOL_MINT,
        amount="5",
        limit_price=50,
    )
    with (
        patch(f"{MODULE}.DEFINITIVE_CLIENT", client),
        patch(f"{MODULE}.solana_client_from_chain_id"),
        patch(
            f"{MODULE}._resolve_token_program_id",
            AsyncMock(return_value=TOKEN_PROGRAM_ID),
        ),
        patch(f"{MODULE}.get_spl_token_balance", AsyncMock(return_value=5_000_000)),
    ):
        ok, res = await adapter.place_order(900, request, 5_000_000)
    assert ok, res
    assert res == {"order_id": "oid", "setup_tx_signature": "setupsig"}
    adapter.svm_message_callback.assert_awaited_once_with(message)
    sent = client.submit.await_args.args[0]
    assert sent["svmOrderMessage"] == message and sent["svmNonce"] == "77"
    assert "evmOrderTypedData" not in sent


@pytest.mark.asyncio
async def test_place_solana_order_fails_closed_without_the_group():
    client = MagicMock(
        config=AsyncMock(return_value={"solana_group": ""}), quote=AsyncMock()
    )
    adapter = _solana_adapter()
    request = build_flash_request(
        "limit_buy",
        chain_id=900,
        from_token=USDC,
        to_token=svm.WSOL_MINT,
        amount="5",
        limit_price=50,
    )
    with patch(f"{MODULE}.DEFINITIVE_CLIENT", client):
        ok, res = await adapter.place_order(900, request, 5_000_000)
    assert not ok and "aren't available" in res["error"]
    client.quote.assert_not_awaited()
    adapter.svm_message_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_solana_cancel_signs_flash_cancel_message():
    order_id = "5b0e6f7a-3c1d-4e2f-9a8b-7c6d5e4f3a2b"
    client = MagicMock(
        get_order=AsyncMock(
            return_value={"order": {"targetAsset": {"chain": {"name": "solana"}}}}
        ),
        cancel=AsyncMock(),
    )
    adapter = _solana_adapter()
    with patch(f"{MODULE}.DEFINITIVE_CLIENT", client):
        ok, res = await adapter.cancel_order(order_id)
    assert ok and res["status"] == "cancelled"
    adapter.svm_message_callback.assert_awaited_once_with(
        flash_cancel_message(order_id)
    )
    client.cancel.assert_awaited_once_with(FUNDER, order_id, "base58sig")
